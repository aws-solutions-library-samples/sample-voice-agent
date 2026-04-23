"""
Unit tests for Bot Runner Lambda handler.

Run with: pytest test_handler.py -v
"""

import base64
import hashlib
import hmac as _hmac
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

# Set environment variables before importing handler.
# DAILY_HMAC_VERIFY=false keeps most existing tests unaffected; HMAC-specific
# tests below override this in-place.
os.environ["DAILY_API_KEY_SECRET_ARN"] = (
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret"
)
os.environ["DAILY_HMAC_VERIFY"] = "false"

import handler  # noqa: E402
import hmac_verifier  # noqa: E402


class TestStartSession(unittest.TestCase):
    """Tests for start_session handler."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_context = MagicMock()
        self.mock_context.aws_request_id = "test-request-id"

    def _make_event(self, body: dict) -> dict:
        """Create API Gateway event with body."""
        return {
            "body": json.dumps(body),
            "headers": {"Content-Type": "application/json"},
            "httpMethod": "POST",
            "path": "/start",
        }

    def test_missing_call_id_returns_400(self):
        """Should return 400 when callId is missing."""
        event = self._make_event(
            {
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("callId", body["error"])

    def test_missing_call_domain_returns_400(self):
        """Should return 400 when callDomain is missing."""
        event = self._make_event(
            {
                "callId": "test-call-123",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("callDomain", body["error"])

    def test_invalid_json_returns_400(self):
        """Should return 400 for invalid JSON body."""
        event = {
            "body": "not valid json {{{",
            "headers": {"Content-Type": "application/json"},
        }

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("Invalid JSON", body["error"])

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_successful_session_start(self, mock_daily_cls, mock_service_cls):
        """Should return 200 and session details on success."""
        # Set up mocks
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-test-123",
            "name": "voice-test-123",
            "id": "room-id-abc",
        }
        mock_daily.create_meeting_token.return_value = "test-token-xyz"
        mock_daily.get_sip_uri.return_value = "sip:room-id-abc@sip.daily.co"
        mock_daily_cls.return_value = mock_daily

        mock_service = MagicMock()
        mock_service.start_call.return_value = {"status": "started"}
        mock_service_cls.return_value = mock_service

        event = self._make_event(
            {
                "callId": "test-call-123",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["status"], "started")
        self.assertIn("sessionId", body)
        self.assertIn("roomUrl", body)
        self.assertIn("sipUri", body)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_daily_api_error_returns_500(self, mock_daily_cls, mock_service_cls):
        """Should return 500 when Daily API fails."""
        mock_daily = MagicMock()
        mock_daily.create_room.side_effect = ValueError("Daily API error: 401")
        mock_daily_cls.return_value = mock_daily

        event = self._make_event(
            {
                "callId": "test-call-123",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 500)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_ecs_service_error_returns_500(self, mock_daily_cls, mock_service_cls):
        """Should return 500 when ECS service fails."""
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-test-123",
            "name": "voice-test-123",
            "id": "room-id-abc",
        }
        mock_daily.create_meeting_token.return_value = "test-token-xyz"
        mock_daily.get_sip_uri.return_value = "sip:room-id-abc@sip.daily.co"
        mock_daily_cls.return_value = mock_daily

        mock_service = MagicMock()
        mock_service.start_call.side_effect = Exception("ECS service error")
        mock_service_cls.return_value = mock_service

        event = self._make_event(
            {
                "callId": "test-call-123",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 500)

    def test_empty_body_returns_400(self):
        """Should return 400 when body is empty."""
        event = {"body": "{}"}

        response = handler.start_session(event, self.mock_context)

        self.assertEqual(response["statusCode"], 400)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_session_id_format(self, mock_daily_cls, mock_service_cls):
        """Session ID should include call ID prefix."""
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-test-123",
            "name": "voice-test-123",
            "id": "room-id-abc",
        }
        mock_daily.create_meeting_token.return_value = "test-token-xyz"
        mock_daily.get_sip_uri.return_value = "sip:room-id-abc@sip.daily.co"
        mock_daily_cls.return_value = mock_daily

        mock_service = MagicMock()
        mock_service.start_call.return_value = {"status": "started"}
        mock_service_cls.return_value = mock_service

        event = self._make_event(
            {
                "callId": "my-call-id",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        response = handler.start_session(event, self.mock_context)
        body = json.loads(response["body"])

        self.assertTrue(body["sessionId"].startswith("voice-my-call-id-"))


class TestParseBody(unittest.TestCase):
    """Tests for _parse_body helper."""

    def test_parses_json_string(self):
        """Should parse JSON string body."""
        event = {"body": '{"key": "value"}'}
        result = handler._parse_body(event)
        self.assertEqual(result, {"key": "value"})

    def test_handles_dict_body(self):
        """Should handle dict body (already parsed)."""
        event = {"body": {"key": "value"}}
        result = handler._parse_body(event)
        self.assertEqual(result, {"key": "value"})

    def test_handles_empty_body(self):
        """Should return empty dict for missing body."""
        event = {}
        result = handler._parse_body(event)
        self.assertEqual(result, {})

    def test_raises_on_invalid_json(self):
        """Should raise ValueError for invalid JSON."""
        event = {"body": "not json"}
        with self.assertRaises(ValueError):
            handler._parse_body(event)


class TestResponseHelpers(unittest.TestCase):
    """Tests for response helper functions."""

    def test_success_response_format(self):
        """Success response should have correct format."""
        response = handler._success_response(200, {"key": "value"})

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        self.assertIn("Access-Control-Allow-Origin", response["headers"])
        body = json.loads(response["body"])
        self.assertEqual(body["key"], "value")

    def test_error_response_format(self):
        """Error response should have correct format."""
        response = handler._error_response(400, "Test error")

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        body = json.loads(response["body"])
        self.assertEqual(body["error"], "Test error")
        self.assertEqual(body["status"], "error")


class TestGetSystemPrompt(unittest.TestCase):
    """Tests for _get_system_prompt helper."""

    def test_returns_non_empty_prompt(self):
        """Should return a non-empty system prompt."""
        prompt = handler._get_system_prompt("+15551234567")
        self.assertTrue(len(prompt) > 0)

    def test_prompt_contains_assistant_context(self):
        """Prompt should contain assistant context."""
        prompt = handler._get_system_prompt("+15551234567")
        self.assertIn("assistant", prompt.lower())

    def test_prompt_contains_tool_usage_guidance(self):
        """Prompt should contain tool usage guidance for clean context aggregation."""
        prompt = handler._get_system_prompt("+15551234567")
        # Tool usage guidance prevents LLM from outputting text before tool calls,
        # which would cause context aggregation issues with concurrent text/tool frames
        self.assertIn("tool", prompt.lower())
        self.assertIn("directly", prompt.lower())


class TestHmacVerification(unittest.TestCase):
    """End-to-end tests for HMAC-verified handler path."""

    def setUp(self):
        hmac_verifier._reset_cache_for_tests()
        self.secret_bytes = b"test-hmac-secret-32-bytes-long!!"
        self.secret_b64 = base64.b64encode(self.secret_bytes).decode()

    def tearDown(self):
        hmac_verifier._reset_cache_for_tests()

    def _signed_event(self, body_dict: dict, *, timestamp: str = None, signature: str = None) -> dict:
        """Build an API Gateway event signed with our test secret."""
        # Match API Gateway's raw body delivery: JSON string, no whitespace changes.
        body_str = json.dumps(body_dict, separators=(",", ":"))
        ts = timestamp or str(int(time.time()))
        sig = signature or _hmac.new(
            self.secret_bytes,
            ts.encode() + b"." + body_str.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "body": body_str,
            "headers": {
                "Content-Type": "application/json",
                "X-Pinless-Signature": sig,
                "X-Pinless-Timestamp": ts,
            },
            "httpMethod": "POST",
            "path": "/start",
        }

    def _patch_env_and_secret(self):
        """Enable HMAC verify + stub Secrets Manager response."""
        env_patch = patch.dict("os.environ", {"DAILY_HMAC_VERIFY": "true"})
        handler_patch = patch.object(handler, "_HMAC_VERIFY_ENABLED", True)
        boto_patch = patch("hmac_verifier.boto3.client")
        env_patch.start()
        handler_patch.start()
        mock_boto = boto_patch.start()
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"DAILY_HMAC_SECRET": self.secret_b64}),
        }
        mock_boto.return_value = mock_sm
        self.addCleanup(env_patch.stop)
        self.addCleanup(handler_patch.stop)
        self.addCleanup(boto_patch.stop)

    def test_bad_signature_returns_401(self):
        """Tampered signature must return 401 Unauthorized."""
        self._patch_env_and_secret()
        event = self._signed_event(
            {
                "callId": "test-call",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            },
            signature="0" * 64,  # wrong
        )

        mock_context = MagicMock(aws_request_id="test-req")
        response = handler.start_session(event, mock_context)

        self.assertEqual(response["statusCode"], 401)
        body = json.loads(response["body"])
        self.assertEqual(body["error"], "Unauthorized")

    def test_missing_signature_header_returns_401(self):
        """Requests without X-Pinless-Signature must return 401."""
        self._patch_env_and_secret()
        event = self._signed_event(
            {
                "callId": "test-call",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            },
        )
        event["headers"].pop("X-Pinless-Signature")

        mock_context = MagicMock(aws_request_id="test-req")
        response = handler.start_session(event, mock_context)

        self.assertEqual(response["statusCode"], 401)

    def test_stale_timestamp_returns_401(self):
        """Replay-attack prevention: timestamps >5 min old must be rejected."""
        self._patch_env_and_secret()
        stale_ts = str(int(time.time()) - 600)  # 10 min old
        event = self._signed_event(
            {
                "callId": "test-call",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            },
            timestamp=stale_ts,  # signature is computed with this stale ts, so it matches HMAC-wise but will fail skew check
        )

        mock_context = MagicMock(aws_request_id="test-req")
        response = handler.start_session(event, mock_context)

        self.assertEqual(response["statusCode"], 401)

    @patch("handler.EcsServiceClient")
    @patch("handler.DailyClient")
    def test_valid_signature_passes_through(self, mock_daily_cls, mock_service_cls):
        """Well-formed signed request reaches the PSTN handler."""
        self._patch_env_and_secret()

        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-x",
            "name": "voice-x",
            "id": "room-x",
        }
        mock_daily.create_meeting_token.return_value = "token"
        mock_daily.get_sip_uri.return_value = "sip:room-x@sip.daily.co"
        mock_daily_cls.return_value = mock_daily

        mock_service = MagicMock()
        mock_service.start_call.return_value = {"status": "started"}
        mock_service_cls.return_value = mock_service

        event = self._signed_event(
            {
                "callId": "test-call",
                "callDomain": "test.daily.co",
                "from": "+15551234567",
            }
        )

        mock_context = MagicMock(aws_request_id="test-req")
        response = handler.start_session(event, mock_context)

        self.assertEqual(response["statusCode"], 200)

    def test_verify_disabled_bypasses_check(self):
        """With DAILY_HMAC_VERIFY=false, unsigned requests skip auth."""
        # DAILY_HMAC_VERIFY is set to "false" at module level (setUpModule).
        # Existing passing tests already prove this — they run without any
        # signature headers and still reach the body-validation 400.
        event = {
            "body": json.dumps({"callDomain": "test.daily.co", "from": "+15551234567"}),
            "headers": {"Content-Type": "application/json"},
        }
        mock_context = MagicMock(aws_request_id="test-req")
        response = handler.start_session(event, mock_context)

        # Would be 401 if verify was enforced; instead we hit the missing-callId 400.
        self.assertEqual(response["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
