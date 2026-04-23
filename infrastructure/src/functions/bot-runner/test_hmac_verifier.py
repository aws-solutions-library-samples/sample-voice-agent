"""
Unit tests for hmac_verifier.

Run with: pytest test_hmac_verifier.py -v
"""

import base64
import hashlib
import hmac as _hmac
import time
import unittest
from unittest.mock import MagicMock, patch

import hmac_verifier
from hmac_verifier import (
    DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    VerificationError,
    _reset_cache_for_tests,
    load_hmac_secret,
    verify_signature,
)


def _make_signature(body: bytes, timestamp: str, secret_bytes: bytes) -> str:
    """Produce the exact signature Daily would generate."""
    message = timestamp.encode("utf-8") + b"." + body
    return _hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()


class TestVerifySignature(unittest.TestCase):
    """Algorithm conformance tests."""

    def setUp(self):
        self.secret_bytes = b"shared-secret-256-bit-value-xxxxx"
        self.secret_b64 = base64.b64encode(self.secret_bytes).decode()
        self.body = b'{"From":"+15551234567","To":"+15559876543","callId":"abc","callDomain":"daily.co"}'
        self.now = 1776966000.0  # Fixed "now" for deterministic timestamp checks
        self.timestamp = str(int(self.now))
        self.signature = _make_signature(self.body, self.timestamp, self.secret_bytes)

    def test_valid_signature_passes(self):
        verify_signature(
            body=self.body,
            signature=self.signature,
            timestamp=self.timestamp,
            hmac_secret_b64=self.secret_b64,
            now=self.now,
        )

    def test_body_tampering_rejected(self):
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body + b"X",  # appended byte
                signature=self.signature,
                timestamp=self.timestamp,
                hmac_secret_b64=self.secret_b64,
                now=self.now,
            )
        self.assertIn("signature mismatch", str(ctx.exception))

    def test_signature_tampering_rejected(self):
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature=self.signature[:-1] + ("0" if self.signature[-1] != "0" else "1"),
                timestamp=self.timestamp,
                hmac_secret_b64=self.secret_b64,
                now=self.now,
            )
        self.assertIn("signature mismatch", str(ctx.exception))

    def test_wrong_secret_rejected(self):
        wrong = base64.b64encode(b"different-secret-xxxxxxxxxxxxxxxxxx").decode()
        with self.assertRaises(VerificationError):
            verify_signature(
                body=self.body,
                signature=self.signature,
                timestamp=self.timestamp,
                hmac_secret_b64=wrong,
                now=self.now,
            )

    def test_missing_signature_rejected(self):
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature="",
                timestamp=self.timestamp,
                hmac_secret_b64=self.secret_b64,
                now=self.now,
            )
        self.assertIn("missing signature", str(ctx.exception))

    def test_missing_timestamp_rejected(self):
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature=self.signature,
                timestamp="",
                hmac_secret_b64=self.secret_b64,
                now=self.now,
            )
        self.assertIn("missing timestamp", str(ctx.exception))

    def test_null_secret_rejected(self):
        """Regression for init-secrets.sh bug that saved literal string 'null'."""
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature=self.signature,
                timestamp=self.timestamp,
                hmac_secret_b64="null",
                now=self.now,
            )
        self.assertIn("not configured", str(ctx.exception))

    def test_empty_secret_rejected(self):
        with self.assertRaises(VerificationError):
            verify_signature(
                body=self.body,
                signature=self.signature,
                timestamp=self.timestamp,
                hmac_secret_b64="",
                now=self.now,
            )

    def test_invalid_base64_secret_rejected(self):
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature=self.signature,
                timestamp=self.timestamp,
                hmac_secret_b64="!!!not-base64!!!",
                now=self.now,
            )
        self.assertIn("not valid base64", str(ctx.exception))

    def test_non_integer_timestamp_rejected(self):
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature=self.signature,
                timestamp="notanumber",
                hmac_secret_b64=self.secret_b64,
                now=self.now,
            )
        self.assertIn("not an integer", str(ctx.exception))

    def test_stale_timestamp_rejected(self):
        stale_ts = str(int(self.now) - DEFAULT_MAX_CLOCK_SKEW_SECONDS - 1)
        sig = _make_signature(self.body, stale_ts, self.secret_bytes)
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature=sig,
                timestamp=stale_ts,
                hmac_secret_b64=self.secret_b64,
                now=self.now,
            )
        self.assertIn("out of window", str(ctx.exception))

    def test_future_timestamp_rejected(self):
        """Reject timestamps too far in the future too (clock skew is bidirectional)."""
        future_ts = str(int(self.now) + DEFAULT_MAX_CLOCK_SKEW_SECONDS + 1)
        sig = _make_signature(self.body, future_ts, self.secret_bytes)
        with self.assertRaises(VerificationError) as ctx:
            verify_signature(
                body=self.body,
                signature=sig,
                timestamp=future_ts,
                hmac_secret_b64=self.secret_b64,
                now=self.now,
            )
        self.assertIn("out of window", str(ctx.exception))

    def test_millisecond_timestamp_accepted(self):
        """Daily's docs don't specify unit; accept ms-scale timestamps too."""
        ms_ts = str(int(self.now * 1000))
        sig = _make_signature(self.body, ms_ts, self.secret_bytes)
        verify_signature(
            body=self.body,
            signature=sig,
            timestamp=ms_ts,
            hmac_secret_b64=self.secret_b64,
            now=self.now,
        )

    def test_empty_body_valid_with_correct_signature(self):
        """Edge case: Daily's test ping has trivial body {'test':'test'}."""
        tiny_body = b'{"test":"test"}'
        sig = _make_signature(tiny_body, self.timestamp, self.secret_bytes)
        verify_signature(
            body=tiny_body,
            signature=sig,
            timestamp=self.timestamp,
            hmac_secret_b64=self.secret_b64,
            now=self.now,
        )


class TestLoadHmacSecret(unittest.TestCase):
    """Secret loader — verifies caching and degradation behavior."""

    def setUp(self):
        _reset_cache_for_tests()

    def tearDown(self):
        _reset_cache_for_tests()

    @patch.dict(
        "os.environ",
        {"DAILY_API_KEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
    )
    @patch("hmac_verifier.boto3.client")
    def test_loads_and_caches(self, mock_client):
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": '{"DAILY_HMAC_SECRET": "aGVsbG8="}',
        }
        mock_client.return_value = mock_sm

        v1 = load_hmac_secret()
        v2 = load_hmac_secret()

        self.assertEqual(v1, "aGVsbG8=")
        self.assertEqual(v2, "aGVsbG8=")
        # Exactly one Secrets Manager call despite two load_hmac_secret() calls.
        self.assertEqual(mock_sm.get_secret_value.call_count, 1)

    @patch.dict(
        "os.environ",
        {"DAILY_API_KEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
    )
    @patch("hmac_verifier.boto3.client")
    def test_refresh_bypasses_cache(self, mock_client):
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": '{"DAILY_HMAC_SECRET": "aGVsbG8="}',
        }
        mock_client.return_value = mock_sm

        load_hmac_secret()
        load_hmac_secret(refresh=True)

        self.assertEqual(mock_sm.get_secret_value.call_count, 2)

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_arn_returns_none(self):
        self.assertIsNone(load_hmac_secret())

    @patch.dict(
        "os.environ",
        {"DAILY_API_KEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
    )
    @patch("hmac_verifier.boto3.client")
    def test_fetch_error_returns_none_fail_closed(self, mock_client):
        """If Secrets Manager is unreachable, return None (verifier will reject)."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = Exception("SM unreachable")
        mock_client.return_value = mock_sm

        self.assertIsNone(load_hmac_secret())

    @patch.dict(
        "os.environ",
        {"DAILY_API_KEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
    )
    @patch("hmac_verifier.boto3.client")
    def test_missing_hmac_key_in_secret_returns_none(self, mock_client):
        """Secret loaded OK but has no DAILY_HMAC_SECRET field."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": '{"DEEPGRAM_API_KEY": "dg123"}',
        }
        mock_client.return_value = mock_sm

        self.assertIsNone(load_hmac_secret())


if __name__ == "__main__":
    unittest.main()
