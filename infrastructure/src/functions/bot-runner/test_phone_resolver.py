"""Unit tests for Phase 7B phone_resolver module.

Covers the full failure-mode matrix:

  * happy path (Aurora row exists, inbound_agent assigned)
  * row exists but no inbound_agent → fallback
  * 404 not provisioned → fallback
  * 400 from Lambda → fallback
  * 500 from Lambda → fallback
  * Lambda invoke raises → fallback
  * missing VOICE_API_LAMBDA_NAME env var → fallback
  * missing to_number → fallback
  * DEFAULT_INBOUND_AGENT unset → returns None

The resolver must *never* raise — any exception kills the inbound
call. If a test starts passing by mistake because an exception is
being suppressed, that's fine; if a test fails because the resolver
raised, investigate before shipping.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch


# phone_resolver instantiates a module-level boto3 Lambda client at import
# time. Ensure that bindings are patchable before import.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import phone_resolver  # noqa: E402


def _envelope(status: int, body: dict) -> dict:
    """Build the Lambda invoke response envelope that API Gateway-style
    handlers emit."""
    raw = json.dumps({"statusCode": status, "body": json.dumps(body)}).encode("utf-8")
    payload = MagicMock()
    payload.read = MagicMock(return_value=raw)
    return {"Payload": payload}


class TestResolveInboundAgentId(unittest.TestCase):
    def setUp(self):
        # Reset env vars each test so they don't leak.
        self._env_patch = patch.dict(
            os.environ,
            {
                "VOICE_API_LAMBDA_NAME": "medcloud-voice-api:live",
                "DEFAULT_INBOUND_AGENT": "chris-claim-status",
            },
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    # ── Happy path ──────────────────────────────────────────────────────

    @patch.object(phone_resolver, "_LAMBDA")
    def test_returns_uuid_when_inbound_agent_set(self, mock_lambda):
        mock_lambda.invoke.return_value = _envelope(
            200,
            {
                "id": "phone-row-id",
                "number": "+12098075018",
                "inbound_agent": {
                    "id": "576b22a4-42ad-4ac1-8a2b-7067fb5c5cd4",
                    "name": "chris-claim-status",
                    "display_name": "Chris",
                },
                "outbound_agent": None,
            },
        )
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req-1")
        self.assertEqual(result, "576b22a4-42ad-4ac1-8a2b-7067fb5c5cd4")

        # Invoke should be to the configured alias with the expected
        # payload shape.
        call_args = mock_lambda.invoke.call_args
        self.assertEqual(call_args.kwargs["FunctionName"], "medcloud-voice-api:live")
        self.assertEqual(call_args.kwargs["InvocationType"], "RequestResponse")
        payload = json.loads(call_args.kwargs["Payload"].decode("utf-8"))
        self.assertEqual(payload["path"], "/api/phone-numbers/lookup")
        self.assertEqual(payload["queryStringParameters"], {"number": "+12098075018"})

    @patch.object(phone_resolver, "_LAMBDA")
    def test_prefers_uuid_over_name(self, mock_lambda):
        mock_lambda.invoke.return_value = _envelope(
            200,
            {
                "inbound_agent": {"id": "uuid-x", "name": "chris-claim-status"},
            },
        )
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        self.assertEqual(result, "uuid-x")

    @patch.object(phone_resolver, "_LAMBDA")
    def test_falls_back_to_name_when_id_missing(self, mock_lambda):
        # This isn't the canonical API shape but we should be defensive.
        mock_lambda.invoke.return_value = _envelope(
            200,
            {"inbound_agent": {"id": "", "name": "chloe"}},
        )
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        self.assertEqual(result, "chloe")

    # ── Fallback scenarios ──────────────────────────────────────────────

    @patch.object(phone_resolver, "_LAMBDA")
    def test_row_found_but_no_inbound_agent_uses_default(self, mock_lambda):
        mock_lambda.invoke.return_value = _envelope(
            200,
            {"number": "+12098075018", "inbound_agent": None, "outbound_agent": None},
        )
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        self.assertEqual(result, "chris-claim-status")

    @patch.object(phone_resolver, "_LAMBDA")
    def test_404_uses_default(self, mock_lambda):
        mock_lambda.invoke.return_value = _envelope(
            404, {"detail": "Phone number not provisioned"}
        )
        result = phone_resolver.resolve_inbound_agent_id("+19999999999", "req")
        self.assertEqual(result, "chris-claim-status")

    @patch.object(phone_resolver, "_LAMBDA")
    def test_400_uses_default(self, mock_lambda):
        mock_lambda.invoke.return_value = _envelope(400, {"error": "Invalid"})
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        self.assertEqual(result, "chris-claim-status")

    @patch.object(phone_resolver, "_LAMBDA")
    def test_500_uses_default(self, mock_lambda):
        mock_lambda.invoke.return_value = _envelope(500, {"error": "DB down"})
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        self.assertEqual(result, "chris-claim-status")

    @patch.object(phone_resolver, "_LAMBDA")
    def test_invoke_raises_uses_default(self, mock_lambda):
        mock_lambda.invoke.side_effect = RuntimeError("connection reset")
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        self.assertEqual(result, "chris-claim-status")

    @patch.object(phone_resolver, "_LAMBDA")
    def test_invoke_returns_no_payload(self, mock_lambda):
        mock_lambda.invoke.return_value = {}  # missing Payload
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        self.assertEqual(result, "chris-claim-status")

    @patch.object(phone_resolver, "_LAMBDA")
    def test_invoke_returns_non_json_body(self, mock_lambda):
        raw = json.dumps({"statusCode": 200, "body": "not-json"}).encode("utf-8")
        payload = MagicMock()
        payload.read = MagicMock(return_value=raw)
        mock_lambda.invoke.return_value = {"Payload": payload}
        result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
        # status=200 but body is invalid → _extract returns None →
        # treated as "no inbound_agent" → fallback.
        self.assertEqual(result, "chris-claim-status")

    # ── Edge: config missing ────────────────────────────────────────────

    def test_missing_to_number_uses_default(self):
        with patch.object(phone_resolver, "_LAMBDA") as mock_lambda:
            result = phone_resolver.resolve_inbound_agent_id("", "req")
            self.assertEqual(result, "chris-claim-status")
            # Should short-circuit before invoking Lambda.
            mock_lambda.invoke.assert_not_called()

    def test_missing_lambda_name_env_uses_default(self):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": ""}, clear=False):
            with patch.object(phone_resolver, "_LAMBDA") as mock_lambda:
                result = phone_resolver.resolve_inbound_agent_id("+12098075018", "req")
                self.assertEqual(result, "chris-claim-status")
                mock_lambda.invoke.assert_not_called()

    def test_no_default_agent_returns_none(self):
        with patch.dict(
            os.environ, {"DEFAULT_INBOUND_AGENT": ""}, clear=False
        ):
            with patch.object(phone_resolver, "_LAMBDA") as mock_lambda:
                mock_lambda.invoke.return_value = _envelope(
                    404, {"detail": "not found"}
                )
                result = phone_resolver.resolve_inbound_agent_id(
                    "+12098075018", "req"
                )
                self.assertIsNone(result)

    # ── _extract_inbound_agent helper ──────────────────────────────────

    def test_extract_handles_missing_key(self):
        self.assertIsNone(phone_resolver._extract_inbound_agent({}))

    def test_extract_handles_null_inbound(self):
        self.assertIsNone(
            phone_resolver._extract_inbound_agent({"inbound_agent": None})
        )

    def test_extract_handles_empty_strings(self):
        self.assertIsNone(
            phone_resolver._extract_inbound_agent(
                {"inbound_agent": {"id": "", "name": ""}}
            )
        )

    def test_extract_prefers_id_over_name(self):
        self.assertEqual(
            phone_resolver._extract_inbound_agent(
                {"inbound_agent": {"id": "u", "name": "n"}}
            ),
            "u",
        )


if __name__ == "__main__":
    unittest.main()
