"""
HMAC signature verification for Daily.co pinless dial-in webhooks.

Daily signs every webhook request with HMAC-SHA256. Headers:
    X-Pinless-Signature  - HMAC-SHA256 hex digest of "{timestamp}.{body}"
    X-Pinless-Timestamp  - Unix timestamp (seconds) when Daily signed the request

Secret handling:
    The HMAC secret is stored BASE-64 encoded in AWS Secrets Manager under the
    key DAILY_HMAC_SECRET. We must decode it once before computing HMACs.

Reference: docs/reference/daily-setup.md, "Security: Verify Webhook Signature".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300
SIGNATURE_HEADER = "X-Pinless-Signature"
TIMESTAMP_HEADER = "X-Pinless-Timestamp"

_secret_cache: Optional[dict] = None


class VerificationError(Exception):
    """Raised when signature verification fails for any reason."""


def verify_signature(
    *,
    body: bytes,
    signature: str,
    timestamp: str,
    hmac_secret_b64: str,
    max_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    now: Optional[float] = None,
) -> None:
    """
    Verify a Daily pinless-dialin webhook signature.

    Raises VerificationError with a short reason on any failure. Returns None
    on success. Callers should treat any exception as a 401.

    Args:
        body: Raw request body as bytes (NOT the parsed JSON). Must be
            byte-for-byte what Daily POSTed; re-serializing will produce
            a different signature.
        signature: Value of the X-Pinless-Signature header (hex digest).
        timestamp: Value of the X-Pinless-Timestamp header (unix seconds
            as a string, or milliseconds — we accept both defensively).
        hmac_secret_b64: BASE-64 encoded HMAC secret as returned by Daily's
            pinless_dialin configuration endpoint.
        max_skew_seconds: Reject requests whose timestamp is more than this
            many seconds old (default 300 = 5 min). Prevents replay of
            previously-captured signed requests.
        now: Unix timestamp (seconds) to compare against. Defaults to
            time.time(). Injectable for tests.
    """
    if not signature:
        raise VerificationError("missing signature header")
    if not timestamp:
        raise VerificationError("missing timestamp header")
    if not hmac_secret_b64 or hmac_secret_b64 == "null":
        raise VerificationError("hmac secret not configured")

    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise VerificationError("timestamp header is not an integer")

    # Accept both seconds (10 digits) and milliseconds (13 digits). Daily's
    # docs aren't explicit about the unit; real traffic should tell us.
    if ts_int > 10_000_000_000:
        ts_int = ts_int // 1000

    current = now if now is not None else time.time()
    skew = abs(current - ts_int)
    if skew > max_skew_seconds:
        raise VerificationError(
            f"timestamp out of window: skew={skew:.0f}s > {max_skew_seconds}s"
        )

    try:
        secret_bytes = base64.b64decode(hmac_secret_b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise VerificationError(f"hmac secret is not valid base64: {exc}")

    # Daily's canonical form: "{timestamp}.{body}" where body is the raw
    # JSON bytes. Reference: docs/reference/daily-setup.md.
    message = timestamp.encode("utf-8") + b"." + body
    expected = hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise VerificationError("signature mismatch")


def load_hmac_secret(
    *,
    secret_arn: Optional[str] = None,
    refresh: bool = False,
) -> Optional[str]:
    """
    Fetch DAILY_HMAC_SECRET from AWS Secrets Manager (cached across invocations).

    Returns None if the secret is not configured. Returns the literal string
    "null" when the secret value is the string "null" (legacy bug state — the
    verifier will reject this correctly). Raises nothing.

    Args:
        secret_arn: Secret ARN. Defaults to env DAILY_API_KEY_SECRET_ARN.
        refresh: If True, bypass the cache and re-fetch.
    """
    global _secret_cache

    arn = secret_arn or os.environ.get("DAILY_API_KEY_SECRET_ARN")
    if not arn:
        logger.warning("DAILY_API_KEY_SECRET_ARN not set; HMAC verification disabled")
        return None

    if _secret_cache is None or refresh:
        try:
            client = boto3.client("secretsmanager")
            response = client.get_secret_value(SecretId=arn)
            _secret_cache = json.loads(response.get("SecretString", "{}"))
        except Exception as exc:  # noqa: BLE001 — log and degrade, don't crash Lambda
            logger.error(
                "failed to load secret %s: %s; failing closed on HMAC verify",
                arn,
                exc,
            )
            return None

    return _secret_cache.get("DAILY_HMAC_SECRET") if _secret_cache else None


def _reset_cache_for_tests() -> None:
    """Test helper: clear the module-level cache between tests."""
    global _secret_cache
    _secret_cache = None
