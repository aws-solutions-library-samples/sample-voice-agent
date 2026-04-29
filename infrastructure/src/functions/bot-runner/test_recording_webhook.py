"""Tests for the Phase 7E PR 3 recording_webhook + route dispatch.

Covers:
- Router dispatch (path-based + body-based)
- Webhook payload parsing (envelope shape + flat shape)
- Event-type branching (ready-to-download / error / unknown / missing)
- voice-api Lambda invoke wiring (FunctionName, payload shape)
- Idempotent / never-raises contract
- Missing VOICE_API_LAMBDA_NAME → skip patch, ack 200 (no exception)
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

# Match the conftest pattern used by other handler tests.
os.environ.setdefault(
    "DAILY_API_KEY_SECRET_ARN",
    "arn:aws:secretsmanager:us-east-1:000000000000:secret:test",
)
os.environ.setdefault("DAILY_HMAC_VERIFY", "false")
os.environ.setdefault("VOICE_API_LAMBDA_NAME", "medcloud-voice-api:live")

import handler  # noqa: E402


class TestRouterRecordingPath(unittest.TestCase):
    """The route() dispatcher must hand /recording-webhook to the
    new handler, not to the existing dial-out / session paths."""

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.aws_request_id = "test-req-id"

    def _event(self, path=None, raw_path=None, body=None):
        ev = {"body": json.dumps(body) if body else "{}"}
        if path is not None:
            ev["path"] = path
        if raw_path is not None:
            ev["rawPath"] = raw_path
        return ev

    @patch.object(handler, "recording_webhook")
    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_routes_recording_webhook_by_path(
        self, mock_session, mock_dial_out, mock_rec
    ):
        mock_rec.return_value = {"statusCode": 200}
        handler.route(self._event(path="/recording-webhook"), self.ctx)
        mock_rec.assert_called_once()
        mock_session.assert_not_called()
        mock_dial_out.assert_not_called()

    @patch.object(handler, "recording_webhook")
    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_routes_recording_webhook_by_raw_path(
        self, mock_session, mock_dial_out, mock_rec
    ):
        mock_rec.return_value = {"statusCode": 200}
        handler.route(self._event(raw_path="/poc/recording-webhook"), self.ctx)
        mock_rec.assert_called_once()
        mock_session.assert_not_called()
        mock_dial_out.assert_not_called()

    @patch.object(handler, "recording_webhook")
    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_routes_invocation_type_recording_webhook(
        self, mock_session, mock_dial_out, mock_rec
    ):
        mock_rec.return_value = {"statusCode": 200}
        ev = self._event(body={"invocation_type": "recording_webhook"})
        handler.route(ev, self.ctx)
        mock_rec.assert_called_once()


class TestRecordingWebhookHappyPath(unittest.TestCase):
    """``recording.ready-to-download`` is the canonical happy path —
    Daily's webhook with the file already in S3."""

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.aws_request_id = "test-req-id"

    def _payload(self, room_name="voice-out-abc123", s3_key=None):
        return {
            "type": "recording.ready-to-download",
            "id": "evt-1",
            "version": "1.0",
            "event_ts": 1746123456,
            "payload": {
                "type": "cloud-audio-only",
                "recording_id": "rec-uuid-1",
                "room_name": room_name,
                "start_ts": 1746123100,
                "status": "finished",
                "max_participants": 2,
                "duration": 92,
                "s3_key": s3_key
                or f"voice-recordings/{room_name}/1746123100_rec-uuid-1.m4a",
            },
        }

    @patch.object(handler, "_patch_recording_path")
    def test_dispatches_patch_to_voice_api(self, mock_patch):
        ev = {"body": json.dumps(self._payload())}
        result = handler.recording_webhook(ev, self.ctx)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "ok")
        mock_patch.assert_called_once()
        room, s3_key, _req = mock_patch.call_args[0]
        self.assertEqual(room, "voice-out-abc123")
        self.assertTrue(s3_key.endswith(".m4a"))

    @patch.object(handler, "_patch_recording_path")
    def test_accepts_flat_payload_shape(self, mock_patch):
        # Some integration tests / curl-from-cli invocations may send
        # the payload at the top level rather than nested under
        # `.payload`. The handler accepts both for robustness.
        ev = {
            "body": json.dumps(
                {
                    "type": "recording.ready-to-download",
                    "room_name": "voice-flat-1",
                    "s3_key": "voice-recordings/voice-flat-1/123_rec.m4a",
                    "duration": 30,
                }
            )
        }
        result = handler.recording_webhook(ev, self.ctx)
        self.assertEqual(result["statusCode"], 200)
        mock_patch.assert_called_once()
        room, s3_key, _req = mock_patch.call_args[0]
        self.assertEqual(room, "voice-flat-1")
        self.assertEqual(s3_key, "voice-recordings/voice-flat-1/123_rec.m4a")


class TestRecordingWebhookErrorAndAckCases(unittest.TestCase):
    """Daily can deliver malformed / error / unknown events. The
    handler must always return 200 (no Lambda retries) and never
    raise out of the handler."""

    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.aws_request_id = "test-req-id"

    @patch.object(handler, "_patch_recording_path")
    def test_recording_error_event_acks_without_patch(self, mock_patch):
        ev = {
            "body": json.dumps(
                {
                    "type": "recording.error",
                    "payload": {
                        "room_name": "voice-err-1",
                        "message": "Daily upload failed",
                    },
                }
            )
        }
        result = handler.recording_webhook(ev, self.ctx)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "logged")
        mock_patch.assert_not_called()

    @patch.object(handler, "_patch_recording_path")
    def test_unknown_event_type_acks_without_patch(self, mock_patch):
        ev = {
            "body": json.dumps(
                {
                    "type": "recording.started",
                    "payload": {"room_name": "voice-x"},
                }
            )
        }
        result = handler.recording_webhook(ev, self.ctx)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "ignored")
        mock_patch.assert_not_called()

    @patch.object(handler, "_patch_recording_path")
    def test_missing_room_name_returns_200(self, mock_patch):
        ev = {
            "body": json.dumps(
                {
                    "type": "recording.ready-to-download",
                    "payload": {
                        "s3_key": "voice-recordings/x/1_y.m4a",
                        # no room_name
                    },
                }
            )
        }
        result = handler.recording_webhook(ev, self.ctx)
        self.assertEqual(result["statusCode"], 200)
        mock_patch.assert_not_called()

    @patch.object(handler, "_patch_recording_path")
    def test_missing_s3_key_returns_200(self, mock_patch):
        ev = {
            "body": json.dumps(
                {
                    "type": "recording.ready-to-download",
                    "payload": {"room_name": "voice-x"},
                }
            )
        }
        result = handler.recording_webhook(ev, self.ctx)
        self.assertEqual(result["statusCode"], 200)
        mock_patch.assert_not_called()

    @patch.object(handler, "_patch_recording_path")
    def test_malformed_body_returns_200(self, mock_patch):
        ev = {"body": "{not json"}
        result = handler.recording_webhook(ev, self.ctx)
        # _parse_body raises ValueError; handler catches.
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "ignored")
        mock_patch.assert_not_called()

    @patch.object(handler, "_patch_recording_path")
    def test_patch_exception_does_not_propagate(self, mock_patch):
        # _patch_recording_path catching its own errors is a defense-
        # in-depth contract; even if it ever raises, the webhook ack
        # must be 200 to prevent Daily from retrying forever.
        mock_patch.side_effect = RuntimeError("DB down")
        ev = {
            "body": json.dumps(
                {
                    "type": "recording.ready-to-download",
                    "payload": {
                        "room_name": "voice-x",
                        "s3_key": "voice-recordings/x/1_y.m4a",
                    },
                }
            )
        }
        result = handler.recording_webhook(ev, self.ctx)
        self.assertEqual(result["statusCode"], 200)


class TestPatchRecordingPathInvoke(unittest.TestCase):
    """The boto3 wiring: correct FunctionName, correct payload
    shape, no exception on Lambda errors."""

    @patch("boto3.client")
    def test_invokes_voice_api_with_recording_update_payload(self, mock_boto):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "statusCode": 200,
                "body": json.dumps({"id": "abc"}),
            }
        ).encode()
        mock_client.invoke.return_value = {"Payload": mock_response}
        mock_boto.return_value = mock_client

        handler._patch_recording_path(
            room_name="voice-x",
            s3_key="voice-recordings/voice-x/1_y.m4a",
            request_id="req-1",
        )

        mock_client.invoke.assert_called_once()
        kwargs = mock_client.invoke.call_args.kwargs
        self.assertEqual(kwargs["FunctionName"], "medcloud-voice-api:live")
        self.assertEqual(kwargs["InvocationType"], "RequestResponse")
        sent = json.loads(kwargs["Payload"].decode("utf-8"))
        self.assertEqual(sent["path"], "/api/calls/recording-update")
        self.assertEqual(sent["httpMethod"], "POST")
        body = json.loads(sent["body"])
        self.assertEqual(body["session_id"], "voice-x")
        self.assertEqual(body["recording_path"], "voice-recordings/voice-x/1_y.m4a")

    @patch("boto3.client")
    def test_no_voice_api_env_skips_patch_no_raise(self, mock_boto):
        with patch.dict(os.environ, {"VOICE_API_LAMBDA_NAME": ""}, clear=False):
            # Should log and return without ever instantiating the
            # boto client.
            handler._patch_recording_path(
                room_name="voice-x",
                s3_key="voice-recordings/voice-x/1_y.m4a",
                request_id="req-1",
            )
            mock_boto.assert_not_called()

    @patch("boto3.client")
    def test_404_response_logs_warning_no_raise(self, mock_boto):
        # Lambda returns 404 if no voice_calls row matches the
        # session_id (e.g. recording webhook fires before pipeline
        # writes the row, or session_id mismatch). We log and move
        # on.
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "statusCode": 404,
                "body": json.dumps({"detail": "No voice_calls row"}),
            }
        ).encode()
        mock_client.invoke.return_value = {"Payload": mock_response}
        mock_boto.return_value = mock_client

        # Must not raise.
        handler._patch_recording_path(
            room_name="voice-x",
            s3_key="voice-recordings/voice-x/1_y.m4a",
            request_id="req-1",
        )


class TestRoomCreationRecordingFlags(unittest.TestCase):
    """Source-grep: confirm both the room create properties and the
    bot meeting token carry the Phase 7E PR 3 recording flags. This
    is the cheap regression guard against an accidental revert to
    the OG "no recording" config."""

    def test_inbound_room_has_cloud_audio_only(self):
        from pathlib import Path
        src = Path(__file__).parent / "handler.py"
        text = src.read_text()
        # Three room-create sites + three token-create sites; each
        # must carry the recording flags.
        self.assertEqual(
            text.count('"enable_recording": "cloud-audio-only"'),
            3,
            "Each room create site (PSTN inbound, dial-out, SIP) must "
            "set enable_recording=cloud-audio-only",
        )
        self.assertEqual(
            text.count('"start_cloud_recording": True'),
            3,
            "Each bot meeting token must request auto-start of the "
            "recorder so we don't have to drive it from daily-js",
        )


if __name__ == "__main__":
    unittest.main()
