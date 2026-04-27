"""Tests for call_writer.trigger_auto_actions — Phase 7E PR 2.

Covers the same failure-mode matrix as write_call_record:
- Lambda env not set → None, warn log
- 2xx → returns parsed body dict (with actions_taken/cost/quality_score)
- non-2xx → None, error log
- invoke raises → None, error log
- no Payload / malformed envelope → None
- Never raises (caller's finally must complete)
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from app.services import call_writer
from app.services.call_writer import trigger_auto_actions


def _envelope(status: int, body: dict) -> dict:
    raw = json.dumps({"statusCode": status, "body": json.dumps(body)}).encode("utf-8")
    payload = MagicMock()
    payload.read = MagicMock(return_value=raw)
    return {"Payload": payload}


class TestTriggerAutoActionsHappyPath:
    @pytest.mark.asyncio
    async def test_2xx_returns_body_dict(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "medcloud-voice-api:live"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(
                    200,
                    {
                        "call_id": "call-1",
                        "actions_taken": 3,
                        "quality_score": 80,
                        "cost": "0.0123",
                    },
                )
                result = await trigger_auto_actions("call-1")
        assert result is not None
        assert result["actions_taken"] == 3
        assert result["quality_score"] == 80
        assert result["cost"] == "0.0123"

    @pytest.mark.asyncio
    async def test_invokes_correct_alias_and_path(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "medcloud-voice-api:live"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(200, {})
                await trigger_auto_actions("abc-123")
                call_kwargs = mock_lambda.invoke.call_args.kwargs
        assert call_kwargs["FunctionName"] == "medcloud-voice-api:live"
        payload = json.loads(call_kwargs["Payload"].decode("utf-8"))
        assert payload["path"] == "/api/auto-actions"
        assert payload["httpMethod"] == "POST"
        body = json.loads(payload["body"])
        assert body == {"call_id": "abc-123"}


class TestTriggerAutoActionsFailures:
    @pytest.mark.asyncio
    async def test_no_lambda_env_returns_none(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": ""}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                result = await trigger_auto_actions("call-1")
                assert result is None
                mock_lambda.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_invoke_raises_returns_none(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.side_effect = RuntimeError("network down")
                assert await trigger_auto_actions("call-1") is None

    @pytest.mark.asyncio
    async def test_no_payload_returns_none(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = {}
                assert await trigger_auto_actions("call-1") is None

    @pytest.mark.asyncio
    async def test_malformed_envelope_returns_none(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                payload = MagicMock()
                payload.read = MagicMock(return_value=b"not-json")
                mock_lambda.invoke.return_value = {"Payload": payload}
                assert await trigger_auto_actions("call-1") is None

    @pytest.mark.asyncio
    async def test_404_returns_none(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(
                    404, {"detail": "Call not found"}
                )
                assert await trigger_auto_actions("call-1") is None

    @pytest.mark.asyncio
    async def test_5xx_returns_none(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(
                    500, {"error": "DB timeout"}
                )
                assert await trigger_auto_actions("call-1") is None

    @pytest.mark.asyncio
    async def test_never_propagates_exception(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": "x"}):
            with patch.object(call_writer, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.side_effect = Exception("boto exploded")
                # Must NOT raise.
                result = await trigger_auto_actions("call-1")
                assert result is None
