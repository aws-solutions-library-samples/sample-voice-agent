"""Phase 7E: persist post-call data to ``voice_calls`` via the Lambda.

After a call wraps, the pipeline collects:

  - identifier (call_id, session_id, agent_id, agent_name)
  - timing (started_at, ended_at, duration_secs, status)
  - direction + numbers (inbound/outbound, target_number, from_number)
  - case_data (the hydrator dict — already on PipelineConfig)
  - transcript (list of turns, accumulated by ConversationObserver)
  - error context (last error message + categorization, if any)
  - batch metadata (batch_id, batch_row_index, batch_row_id) for
    outbound batch dialing — empty on inbound

This module bundles those into the JSON the Lambda's
``POST /api/calls`` upsert endpoint expects, then invokes the Lambda
via boto3 (same pattern as agent_config.py, IAM-native, no API key
handoff).

We use UPSERT semantics — calling this multiple times for the same
call_id is safe (the Lambda's INSERT…ON CONFLICT DO UPDATE handles
late-arriving fields like recording_path that get patched in by
PR #2 of 7E). Each call to ``write_call_record`` writes the FULL
current snapshot.

Failure modes
-------------

The Lambda invoke can fail (cold-start timeout, Lambda not
deployed, IAM lapse, Aurora hiccup). After-call persistence is
best-effort — a failure here LOGS but does not raise. The call
itself has already completed; the caller experience is unaffected.
The structured ``call_record_write_failed`` log is the operator's
signal that history persistence dropped.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import structlog

logger = structlog.get_logger(__name__)

VOICE_API_LAMBDA_NAME_ENV = "VOICE_API_LAMBDA_NAME"

# Synchronous boto3 client — same pattern as agent_config.py. Use
# asyncio.to_thread() to call invoke() from async code without
# pulling in aiobotocore (which has had connection-handling bugs in
# our deployment, see Phase 7A).
_LAMBDA = boto3.client("lambda")


@dataclass
class CallRecord:
    """Bundled per-call data for persistence to voice_calls.

    Fields here mirror voice_calls columns one-to-one (see Lambda's
    voice_calls DDL). Optional fields default to None so a partial
    record (e.g. mid-call status update) is well-formed.

    Field ordering is intentional — required identifiers first, then
    timing, then content, then batch correlation. Matches what an
    operator wants to see when triaging a problem call.
    """

    # Required
    id: str  # the call_id
    agent_name: str
    target_number: str  # E.164; for inbound, the From number; for outbound, the To
    direction: str  # 'inbound' | 'outbound'
    status: str  # 'pending' | 'in_progress' | 'completed' | 'failed' | …

    # Identification (optional but strongly recommended)
    agent_display_name: str = ""
    from_number: str = ""

    # Timing
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_secs: Optional[int] = None

    # Content
    case_data: Dict[str, Any] = field(default_factory=dict)
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    recording_path: Optional[str] = None
    post_call_analyses: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    # Batch correlation (Phase 7D outbound only)
    batch_id: Optional[str] = None
    batch_row_index: Optional[int] = None

    def to_lambda_body(self) -> Dict[str, Any]:
        """Serialize to the JSON shape Lambda's ``POST /api/calls``
        upsert handler expects."""

        def _iso(dt: Optional[datetime]) -> Optional[str]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()

        body: Dict[str, Any] = {
            "id": self.id,
            "agent_name": self.agent_name,
            "agent_display_name": self.agent_display_name,
            "from_number": self.from_number,
            "target_number": self.target_number,
            "direction": self.direction,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration_secs": int(self.duration_secs) if self.duration_secs else 0,
            "case_data": self.case_data or {},
            "transcript": self.transcript or [],
            "recording_path": self.recording_path,
            "post_call_analyses": self.post_call_analyses or {},
            "error": self.error,
            "batch_id": self.batch_id,
            "batch_row_index": self.batch_row_index,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return body


async def write_call_record(record: CallRecord) -> bool:
    """Best-effort upsert of a CallRecord into voice_calls via the Lambda.

    Returns:
        True on confirmed success (Lambda returned 2xx).
        False on any failure — error logged structured, exception
        is NOT propagated.
    """
    lambda_name = os.environ.get(VOICE_API_LAMBDA_NAME_ENV, "").strip()
    if not lambda_name:
        logger.warning(
            "call_record_write_skipped_no_lambda_env",
            call_id=record.id,
            env_var=VOICE_API_LAMBDA_NAME_ENV,
        )
        return False

    body = record.to_lambda_body()
    payload = {
        "httpMethod": "POST",
        "path": "/api/calls",
        "queryStringParameters": None,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

    try:
        resp = await asyncio.to_thread(
            _LAMBDA.invoke,
            FunctionName=lambda_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.error(
            "call_record_write_invoke_failed",
            call_id=record.id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False

    raw = resp.get("Payload")
    if raw is None:
        logger.error(
            "call_record_write_no_payload",
            call_id=record.id,
        )
        return False

    try:
        envelope = json.loads(raw.read())
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error(
            "call_record_write_bad_envelope",
            call_id=record.id,
            error=str(exc),
        )
        return False

    status = envelope.get("statusCode", 0)
    if 200 <= status < 300:
        logger.info(
            "call_record_written",
            call_id=record.id,
            agent_name=record.agent_name,
            direction=record.direction,
            status=record.status,
            transcript_turns=len(record.transcript or []),
            duration_secs=record.duration_secs or 0,
        )
        return True

    body_text = envelope.get("body", "")[:300] if isinstance(envelope.get("body"), str) else ""
    logger.error(
        "call_record_write_failed",
        call_id=record.id,
        status_code=status,
        body=body_text,
    )
    return False
