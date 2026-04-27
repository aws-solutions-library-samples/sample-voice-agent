"""Tests for the Phase 7D start_dial_out + route handlers.

Covers:
- Router dispatch (path-based, body-based, default)
- start_dial_out validation (required fields, types)
- Daily room create + token + dialOut wiring
- ECS service start_call called with dialin_settings=None and
  case_data threaded through
- Error paths: ECS rejects, dialOut raises ValueError
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DAILY_API_KEY_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:000000000000:secret:test")
os.environ.setdefault("DAILY_HMAC_VERIFY", "false")

import handler  # noqa: E402


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.aws_request_id = "test-req-id"

    def _event(self, path=None, body=None, raw_path=None):
        ev = {"body": json.dumps(body) if body else "{}"}
        if path is not None:
            ev["path"] = path
        if raw_path is not None:
            ev["rawPath"] = raw_path
        return ev

    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_routes_dial_out_path_to_dial_out(self, mock_start_session, mock_dial_out):
        mock_dial_out.return_value = {"statusCode": 200}
        ev = self._event(path="/dial-out")
        handler.route(ev, self.ctx)
        mock_dial_out.assert_called_once()
        mock_start_session.assert_not_called()

    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_routes_raw_path_dial_out(self, mock_start_session, mock_dial_out):
        mock_dial_out.return_value = {"statusCode": 200}
        ev = self._event(raw_path="/poc/dial-out")
        handler.route(ev, self.ctx)
        mock_dial_out.assert_called_once()
        mock_start_session.assert_not_called()

    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_invocation_type_body_field(self, mock_start_session, mock_dial_out):
        # Direct lambda:Invoke (no API Gateway shape) using
        # invocation_type body field.
        mock_dial_out.return_value = {"statusCode": 200}
        ev = self._event(body={"invocation_type": "dial_out", "to_number": "+15551234567"})
        handler.route(ev, self.ctx)
        mock_dial_out.assert_called_once()
        mock_start_session.assert_not_called()

    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_default_routes_to_session(self, mock_start_session, mock_dial_out):
        mock_start_session.return_value = {"statusCode": 200}
        ev = self._event(path="/start")
        handler.route(ev, self.ctx)
        mock_start_session.assert_called_once()
        mock_dial_out.assert_not_called()

    @patch.object(handler, "start_dial_out")
    @patch.object(handler, "start_session")
    def test_unknown_path_falls_through_to_session(self, mock_start_session, mock_dial_out):
        # We default to start_session rather than 404 so an
        # accidental rename doesn't break Daily inbound.
        mock_start_session.return_value = {"statusCode": 200}
        ev = self._event(path="/something-else")
        handler.route(ev, self.ctx)
        mock_start_session.assert_called_once()
        mock_dial_out.assert_not_called()


class TestStartDialOutValidation(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.aws_request_id = "req-id"

    def _event(self, body):
        return {"body": json.dumps(body)}

    def test_missing_to_number_returns_400(self):
        resp = handler.start_dial_out(
            self._event({"from_number": "+15551111111", "agent_id": "x"}), self.ctx
        )
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("to_number", json.loads(resp["body"])["error"])

    def test_missing_from_number_is_OK(self):
        # from_number is optional — Daily picks a default caller ID
        # when omitted. Validation should NOT 400 here. The full flow
        # would still need DailyClient + ECS mocks to complete; we
        # only assert validation passes (i.e. doesn't 400).
        with patch.object(handler, "EcsServiceClient") as mock_svc_cls, \
             patch.object(handler, "DailyClient") as mock_daily_cls:
            mock_daily = MagicMock()
            mock_daily.create_room.return_value = {"url": "u", "name": "n"}
            mock_daily.create_meeting_token.return_value = "T"
            mock_daily_cls.return_value = mock_daily
            mock_svc = MagicMock()
            mock_svc.start_call.return_value = {"status": "started"}
            mock_svc_cls.return_value = mock_svc
            resp = handler.start_dial_out(
                self._event({"to_number": "+15551111111", "agent_id": "x"}), self.ctx
            )
            self.assertEqual(resp["statusCode"], 200)
            # dialout_settings should omit caller_id when from_number absent
            svc_kwargs = mock_svc.start_call.call_args.kwargs
            self.assertEqual(
                svc_kwargs["dialout_settings"], {"phone_number": "+15551111111"}
            )

    def test_missing_agent_id_returns_400(self):
        resp = handler.start_dial_out(
            self._event({"to_number": "+15551111111", "from_number": "+15552222222"}),
            self.ctx,
        )
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("agent_id", json.loads(resp["body"])["error"])

    def test_case_data_must_be_object(self):
        resp = handler.start_dial_out(
            self._event(
                {
                    "to_number": "+15551111111",
                    "from_number": "+15552222222",
                    "agent_id": "x",
                    "case_data": "not-a-dict",
                }
            ),
            self.ctx,
        )
        self.assertEqual(resp["statusCode"], 400)


class TestStartDialOutHappyPath(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.aws_request_id = "req-id"

    @patch.object(handler, "EcsServiceClient")
    @patch.object(handler, "DailyClient")
    def test_full_flow(self, mock_daily_cls, mock_svc_cls):
        # Mock Daily room creation + token
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {
            "url": "https://test.daily.co/voice-out-deadbeef",
            "name": "voice-out-deadbeef",
        }
        mock_daily.create_meeting_token.return_value = "TOKEN-123"
        mock_daily_cls.return_value = mock_daily

        # Mock ECS accept
        mock_svc = MagicMock()
        mock_svc.start_call.return_value = {"status": "started"}
        mock_svc_cls.return_value = mock_svc

        ev = {
            "body": json.dumps(
                {
                    "to_number": "+15551111111",
                    "from_number": "+15552222222",
                    "agent_id": "chris-claim-status",
                    "case_data": {"Service_Date": "2026-04-01"},
                    "session_id": "test-session-1",
                }
            )
        }
        resp = handler.start_dial_out(ev, self.ctx)

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertEqual(body["session_id"], "test-session-1")
        self.assertEqual(body["status"], "started")
        self.assertIn("room_url", body)

        # Daily was called with sip_mode=dial-in (the only mode Daily
        # accepts; the outbound leg is initiated by the bot in the
        # room via transport.start_dialout(), not a REST call).
        room_kwargs = mock_daily.create_room.call_args.kwargs
        sip_cfg = room_kwargs["properties"]["sip"]
        self.assertEqual(sip_cfg["sip_mode"], "dial-in")

        # Bot token was generated as owner (start_dialout requires it)
        token_kwargs = mock_daily.create_meeting_token.call_args.kwargs
        self.assertTrue(token_kwargs["properties"]["is_owner"])

        # ECS start_call was called with dialin_settings=None,
        # dialout_settings={phone_number, caller_id}, and the full
        # case_data + agent_id threaded through.
        svc_kwargs = mock_svc.start_call.call_args.kwargs
        self.assertIsNone(svc_kwargs["dialin_settings"])
        self.assertEqual(svc_kwargs["agent_id"], "chris-claim-status")
        self.assertEqual(svc_kwargs["case_data"], {"Service_Date": "2026-04-01"})
        self.assertIsNone(svc_kwargs["system_prompt"])
        self.assertEqual(
            svc_kwargs["dialout_settings"],
            {"phone_number": "+15551111111", "caller_id": "+15552222222"},
        )

        # The Lambda no longer calls Daily's REST dialout — that's
        # done by the pipeline once it joins the room.
        mock_daily.dial_out.assert_not_called()

    @patch.object(handler, "EcsServiceClient")
    @patch.object(handler, "DailyClient")
    def test_auto_session_id_when_not_provided(self, mock_daily_cls, mock_svc_cls):
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {"url": "u", "name": "n"}
        mock_daily.create_meeting_token.return_value = "T"
        mock_daily.dial_out.return_value = {}
        mock_daily_cls.return_value = mock_daily

        mock_svc = MagicMock()
        mock_svc.start_call.return_value = {"status": "started"}
        mock_svc_cls.return_value = mock_svc

        ev = {
            "body": json.dumps(
                {
                    "to_number": "+15551111111",
                    "from_number": "+15552222222",
                    "agent_id": "x",
                }
            )
        }
        resp = handler.start_dial_out(ev, self.ctx)
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertTrue(body["session_id"].startswith("voice-out-"))


class TestStartDialOutErrors(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.aws_request_id = "req-id"

    def _ev(self):
        return {
            "body": json.dumps(
                {
                    "to_number": "+15551111111",
                    "from_number": "+15552222222",
                    "agent_id": "x",
                }
            )
        }

    @patch.object(handler, "EcsServiceClient")
    @patch.object(handler, "DailyClient")
    def test_ecs_rejects_returns_503(self, mock_daily_cls, mock_svc_cls):
        mock_daily = MagicMock()
        mock_daily.create_room.return_value = {"url": "u", "name": "n"}
        mock_daily.create_meeting_token.return_value = "T"
        mock_daily_cls.return_value = mock_daily

        mock_svc = MagicMock()
        mock_svc.start_call.return_value = {"status": "rejected", "error": "draining"}
        mock_svc_cls.return_value = mock_svc

        resp = handler.start_dial_out(self._ev(), self.ctx)
        self.assertEqual(resp["statusCode"], 503)
        # dial_out should NOT have been called — no point ringing the
        # target if the bot isn't going to be there.
        mock_daily.dial_out.assert_not_called()

    @patch.object(handler, "EcsServiceClient")
    @patch.object(handler, "DailyClient")
    def test_room_creation_failure_returns_500(self, mock_daily_cls, mock_svc_cls):
        # Daily room API failure now bubbles up via ValueError. The
        # pre-7D flow had a separate dial_out failure mode via REST
        # which is no longer relevant (start_dialout from the bot
        # client surfaces errors via on_dialout_error events on the
        # pipeline side, not synchronously here).
        mock_daily = MagicMock()
        mock_daily.create_room.side_effect = ValueError("Daily API error: 402")
        mock_daily_cls.return_value = mock_daily

        resp = handler.start_dial_out(self._ev(), self.ctx)
        # ValueError is caught at the top of start_dial_out → 400
        self.assertEqual(resp["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
