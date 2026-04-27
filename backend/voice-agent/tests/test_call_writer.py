"""Tests for app/services/call_writer.py — Phase 7E.

Covers:
- CallRecord.to_lambda_body field mapping (every voice_calls column)
- Datetime → ISO 8601 with UTC tz handling (naive datetimes coerced)
- Optional / None / empty defaults serialize sanely
- write_call_record success path (Lambda 201 response)
- write_call_record failure modes:
    * VOICE_API_LAMBDA_NAME unset → False, structured warn log
    * boto3 invoke raises → False, structured error log
    * Lambda returns non-2xx → False, error log with body excerpt
    * Lambda returns no Payload → False
    * Lambda returns malformed JSON envelope → False
- Never raises (caller's after-call flow can't be killed by a write
  failure)

All tests are pure (no AWS calls) — boto3 is mocked.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# call_writer instantiates a module-level boto3 client at import. Set
# AWS region so boto3 doesn't error trying to discover one.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from app.services import call_writer
from app.services.call_writer import CallRecord, write_call_record


# =============================================================================
# CallRecord.to_lambda_body
# =============================================================================


class TestCallRecordSerialization:
    def test_full_record_serializes_every_field(self):
        rec = CallRecord(
            id="call-123",
            agent_name="chris-claim-status",
            agent_display_name="Chris — claim status",
            from_number="+19494360836",
            target_number="+12098075018",
            direction="inbound",
            status="completed",
            started_at=datetime(2026, 4, 27, 16, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 4, 27, 16, 5, 30, tzinfo=timezone.utc),
            duration_secs=330,
            case_data={"Service_Date": "2026-04-01"},
            transcript=[{"turn_number": 1, "speaker": "user", "content": "Hi"}],
            recording_path="recordings/call-123.wav",
            post_call_analyses={"summary": "ok"},
            error=None,
            batch_id="batch-99",
            batch_row_index=7,
        )
        body = rec.to_lambda_body()

        assert body["id"] == "call-123"
        assert body["agent_name"] == "chris-claim-status"
        assert body["agent_display_name"] == "Chris — claim status"
        assert body["from_number"] == "+19494360836"
        assert body["target_number"] == "+12098075018"
        assert body["direction"] == "inbound"
        assert body["status"] == "completed"
        assert body["started_at"] == "2026-04-27T16:00:00+00:00"
        assert body["ended_at"] == "2026-04-27T16:05:30+00:00"
        assert body["duration_secs"] == 330
        assert body["case_data"] == {"Service_Date": "2026-04-01"}
        assert body["transcript"] == [
            {"turn_number": 1, "speaker": "user", "content": "Hi"}
        ]
        assert body["recording_path"] == "recordings/call-123.wav"
        assert body["post_call_analyses"] == {"summary": "ok"}
        assert body["error"] is None
        assert body["batch_id"] == "batch-99"
        assert body["batch_row_index"] == 7
        assert "updated_at" in body  # auto-stamped

    def test_minimal_record_uses_safe_defaults(self):
        # Only the 5 required fields supplied
        rec = CallRecord(
            id="call-min",
            agent_name="x",
            target_number="",
            direction="inbound",
            status="completed",
        )
        body = rec.to_lambda_body()

        assert body["target_number"] == ""
        assert body["case_data"] == {}
        assert body["transcript"] == []
        assert body["post_call_analyses"] == {}
        assert body["recording_path"] is None
        assert body["error"] is None
        assert body["batch_id"] is None
        assert body["batch_row_index"] is None
        assert body["duration_secs"] == 0  # falsy duration → 0, not None
        assert body["started_at"] is None
        assert body["ended_at"] is None

    def test_naive_datetime_coerced_to_utc(self):
        # If a caller passes a naive datetime (no tzinfo), to_lambda_body
        # treats it as UTC rather than rejecting / producing an
        # ambiguous value.
        rec = CallRecord(
            id="x",
            agent_name="x",
            target_number="",
            direction="inbound",
            status="completed",
            started_at=datetime(2026, 4, 27, 12, 0, 0),  # naive
        )
        body = rec.to_lambda_body()
        assert body["started_at"] == "2026-04-27T12:00:00+00:00"

    def test_zero_duration_serializes_as_zero(self):
        rec = CallRecord(
            id="x",
            agent_name="x",
            target_number="",
            direction="inbound",
            status="completed",
            duration_secs=0,
        )
        body = rec.to_lambda_body()
        assert body["duration_secs"] == 0


# =============================================================================
# write_call_record — success
# =============================================================================


def _envelope(status: int, body: dict) -> dict:
    """Build the Lambda invoke response envelope."""
    raw = json.dumps({"statusCode": status, "body": json.dumps(body)}).encode("utf-8")
    payload = MagicMock()
    payload.read = MagicMock(return_value=raw)
    return {"Payload": payload}


def _record() -> CallRecord:
    return CallRecord(
        id="call-1",
        agent_name="chris-claim-status",
        target_number="+19494360836",
        direction="inbound",
        status="completed",
        started_at=datetime(2026, 4, 27, 16, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 4, 27, 16, 5, tzinfo=timezone.utc),
        duration_secs=300,
        transcript=[{"turn_number": 1, "speaker": "user", "content": "Hello"}],
    )


class TestWriteCallRecordSuccess:
    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "medcloud-voice-api:live"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(201, {"id": "call-1"})
                ok = await write_call_record(_record())
        assert ok is True

    @pytest.mark.asyncio
    async def test_success_invokes_correct_lambda_alias(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "medcloud-voice-api:live"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(201, {"id": "call-1"})
                await write_call_record(_record())

                call_kwargs = mock_lambda.invoke.call_args.kwargs
                assert call_kwargs["FunctionName"] == "medcloud-voice-api:live"
                assert call_kwargs["InvocationType"] == "RequestResponse"

                # Payload is API-Gateway-shaped event with /api/calls
                payload = json.loads(call_kwargs["Payload"].decode("utf-8"))
                assert payload["httpMethod"] == "POST"
                assert payload["path"] == "/api/calls"
                # Body is JSON-encoded CallRecord serialization
                inner = json.loads(payload["body"])
                assert inner["id"] == "call-1"
                assert inner["status"] == "completed"
                assert inner["transcript"][0]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_2xx_range_all_count_as_success(self):
        for status in (200, 201, 204, 299):
            with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
                with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                    mock_lambda.invoke.return_value = _envelope(status, {})
                    assert await write_call_record(_record()) is True


# =============================================================================
# write_call_record — failure modes (each must return False, never raise)
# =============================================================================


class TestWriteCallRecordFailures:
    @pytest.mark.asyncio
    async def test_no_lambda_env_returns_false(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": ""}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                ok = await write_call_record(_record())
                assert ok is False
                mock_lambda.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_invoke_raises_returns_false(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.side_effect = RuntimeError("network down")
                assert await write_call_record(_record()) is False

    @pytest.mark.asyncio
    async def test_no_payload_returns_false(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = {}  # no Payload
                assert await write_call_record(_record()) is False

    @pytest.mark.asyncio
    async def test_malformed_envelope_returns_false(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                payload = MagicMock()
                payload.read = MagicMock(return_value=b"not-json")
                mock_lambda.invoke.return_value = {"Payload": payload}
                assert await write_call_record(_record()) is False

    @pytest.mark.asyncio
    async def test_4xx_returns_false(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(
                    400, {"error": "Validation failed"}
                )
                assert await write_call_record(_record()) is False

    @pytest.mark.asyncio
    async def test_5xx_returns_false(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(
                    500, {"error": "DB timeout"}
                )
                assert await write_call_record(_record()) is False

    @pytest.mark.asyncio
    async def test_failure_never_propagates_exception(self):
        # Belt-and-suspenders: even if every step fails, the caller's
        # `await write_call_record(...)` must complete.
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.side_effect = Exception(
                    "boto3 went catastrophically wrong"
                )
                # Should NOT raise.
                result = await write_call_record(_record())
                assert result is False
