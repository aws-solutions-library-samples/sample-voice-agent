"""Press-digit (DTMF) tool for the voice agent.

Allows the LLM to programmatically press keypad digits to navigate
IVR (interactive voice response) menus on the far end. Uses
pipecat's OutputDTMFUrgentFrame which routes through
DailyTransport._write_dtmf_native → Daily's SDK send_dtmf API.

Named ``press_digit`` to match Aurora's VALID_TOOL_TYPES (see
cosentus-voice-api-lambda/index.mjs).

Capability requirements:
    - TRANSPORT:   Needs an active transport to emit DTMF frames.
    - SIP_SESSION: DTMF is only meaningful on an actual PSTN call.
                   WebRTC / local dev calls skip registration (the
                   LLM won't see the tool, keeping it from being
                   invoked in tests where pressing tones is a no-op).

── DTMF pacing ─────────────────────────────────────────────────────

OG's press_digit (voiceagent/core/tool_handlers.py) queued DTMF
frames back-to-back with no inter-digit pause. Some IVRs (notably
Aetna and UHC's older carrier paths) miss digits when tones arrive
<60 ms apart: their detectors require ~50 ms of tone audio + a
50 ms gap. On real calls the leaky OG version lost the second or
third digit from rapid bursts like claim-number reads.

Fix: insert a configurable pacing delay between digits. Defaults to
120 ms (safe for every carrier we've tested). Override via the
PRESS_DIGIT_PACING_MS env var if a specific IVR is faster or slower.

── TTS-leak mitigation ─────────────────────────────────────────────

Claude often emits conversational text alongside tool_use blocks
("Sure, one moment" + press_digit). On a Daily-bridged SIP call the
DTMF is out-of-band so the far end hears only the text. On WebRTC
demos the DTMF can leak into the mic stream and sounds wrong.

We push an InterruptionTaskFrame BEFORE the DTMF to cut any
in-flight TTS. The pipeline converts it to an InterruptionFrame
downstream which cancels TTS, flushes buffers, and emits
BotStoppedSpeaking. Adding a short settle delay (60 ms) after the
interruption gives the cancel time to propagate through the pipeline
before the first tone is queued — empirically eliminates the ~300 ms
audible leak noted in OG's docstring.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

import structlog
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    InterruptionTaskFrame,
    OutputDTMFUrgentFrame,
)

from ..capabilities import PipelineCapability
from ..context import ToolContext
from ..result import ToolResult, ToolStatus, error_result
from ..schema import ToolCategory, ToolDefinition, ToolParameter

logger = structlog.get_logger(__name__)


# Valid DTMF characters. ``A/B/C/D`` (extended DTMF) are rarely used
# and not supported by Daily's send_dtmf endpoint, so they're omitted
# here. If a specific IVR ever needs them, add to KeypadEntry first.
_VALID_DTMF = frozenset("0123456789*#")


def _read_pacing_ms() -> float:
    """Read inter-digit pacing from env; fall back to 120 ms.

    Kept as a function (not a module constant) so operators can flip
    the value via ECS env-var without a redeploy.
    """
    raw = os.environ.get("PRESS_DIGIT_PACING_MS", "").strip()
    if not raw:
        return 120.0
    try:
        val = float(raw)
    except ValueError:
        logger.warning(
            "press_digit_pacing_ms_invalid", value=raw, using_default_ms=120.0
        )
        return 120.0
    if val < 0 or val > 2000:
        logger.warning(
            "press_digit_pacing_ms_out_of_range",
            value=val,
            using_default_ms=120.0,
        )
        return 120.0
    return val


# Settle delay after the pre-DTMF TTS interruption. 60 ms is enough
# for the InterruptionFrame to propagate through STT → LLM → TTS →
# Transport in our measured pipeline, before the first DTMF lands.
_INTERRUPTION_SETTLE_MS = 60.0


async def press_digit_executor(
    arguments: Dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    """Send DTMF tones through the active transport.

    Flow:
        1. Validate digit string (empty → error; invalid chars → error)
        2. Push InterruptionTaskFrame to cut any in-flight TTS
        3. Sleep briefly to let the interruption propagate
        4. Queue one OutputDTMFUrgentFrame per digit, with configurable
           inter-digit pacing
        5. Return success with run_llm=False (bot stays silent; the
           IVR's response becomes the next user turn)

    Args:
        arguments: {"digits": "0123456789*#"}
        context: Tool execution context (needs queue_frame + sip_session_id)

    Returns:
        ToolResult with status + count-of-digits
    """
    digits_str = (arguments.get("digits") or "").strip()

    if not digits_str:
        return error_result(
            error_code="PRESS_DIGIT_EMPTY",
            error_message="No digits provided. Pass the keypad digits to press, e.g. '1234'.",
        )

    invalid = sorted({c for c in digits_str if c not in _VALID_DTMF})
    if invalid:
        return error_result(
            error_code="PRESS_DIGIT_INVALID_CHARS",
            error_message=(
                f"Invalid digit(s): {''.join(invalid)}. "
                "Allowed: 0-9, *, #."
            ),
        )

    if context.queue_frame is None:
        logger.error(
            "press_digit_failed_no_queue_frame",
            call_id=context.call_id,
        )
        return error_result(
            error_code="PRESS_DIGIT_UNAVAILABLE",
            error_message=(
                "Unable to press digits at this time. "
                "The pipeline frame queue is not connected."
            ),
        )

    pacing_ms = _read_pacing_ms()
    logger.info(
        "press_digit_requested",
        call_id=context.call_id,
        session_id=context.session_id,
        digit_count=len(digits_str),
        pacing_ms=pacing_ms,
        sip_session_id=context.sip_session_id,
    )

    # Step 2: cut any TTS that's currently speaking so the caller
    # doesn't hear "sure" leaking alongside DTMF tones. Swallow errors
    # — even if the interruption fails to queue, the tones will still
    # go through.
    try:
        await context.queue_frame(InterruptionTaskFrame())
    except Exception as exc:
        logger.warning(
            "press_digit_interruption_queue_failed",
            call_id=context.call_id,
            error=str(exc),
        )

    # Step 3: let the interruption propagate (TTS cancel, buffer
    # flush, BotStoppedSpeaking emit) before we queue the DTMF. 60 ms
    # measured as sufficient in the DailyTransport pipeline.
    await asyncio.sleep(_INTERRUPTION_SETTLE_MS / 1000.0)

    # Step 4: queue the digits one at a time. Daily's send_dtmf API
    # accepts a single tone per call, so we iterate. ``transport_
    # destination`` targets the SIP session we want to dial tones
    # into; when it's None the transport drops the frame (and we've
    # already been gated out by the SIP_SESSION capability at
    # registration time).
    try:
        for i, char in enumerate(digits_str):
            await context.queue_frame(
                OutputDTMFUrgentFrame(
                    button=KeypadEntry(char),
                    transport_destination=context.sip_session_id,
                )
            )
            # Inter-digit pacing — skip the sleep after the last digit
            # so we don't add dead time before returning to the LLM.
            if i < len(digits_str) - 1 and pacing_ms > 0:
                await asyncio.sleep(pacing_ms / 1000.0)
    except Exception as exc:
        logger.error(
            "press_digit_queue_failed",
            call_id=context.call_id,
            digits_pressed_so_far=i,
            error=str(exc),
            exc_info=True,
        )
        return error_result(
            error_code="PRESS_DIGIT_FAILED",
            error_message=(
                "Unable to send DTMF tones. Please try again or ask "
                "the customer to press the digits directly."
            ),
        )

    logger.info(
        "press_digit_sent",
        call_id=context.call_id,
        digit_count=len(digits_str),
    )

    return ToolResult(
        status=ToolStatus.SUCCESS,
        content={
            "pressed": True,
            "digit_count": len(digits_str),
            "message": f"Sent {len(digits_str)} DTMF tone(s).",
        },
        # Stay silent after pressing — the IVR's next prompt is what
        # becomes the next user turn. Matches OG behavior.
        run_llm=False,
    )


press_digit_tool = ToolDefinition(
    name="press_digit",
    description=(
        # Default description; Aurora agents override this with their
        # own copy (see pipeline_ecs._register_tools tools_config
        # handling). Chris's production prompt, for example, adds
        # strict rules about emitting ZERO conversational text
        # alongside a press_digit tool call.
        "Press DTMF digits on the phone keypad to navigate IVR menus. "
        "Valid input: digits 0-9, *, and #. Multi-digit input is sent "
        "as a sequence (e.g. '1234' presses four digits in order). "
        "Use only when the IVR has prompted for keypad input. After "
        "pressing, wait silently for the IVR's response — do NOT "
        "speak between the press and the response."
    ),
    category=ToolCategory.SYSTEM,
    parameters=[
        ToolParameter(
            name="digits",
            type="string",
            description=(
                "The digit(s) to press. Allowed characters: 0-9, *, #. "
                "Examples: '1' (single digit), '1234567890' (account "
                "number), '*6' (keypad shortcut)."
            ),
            required=True,
            pattern=r"^[0-9*#]+$",
        ),
    ],
    executor=press_digit_executor,
    timeout_seconds=10.0,
    requires=frozenset({
        PipelineCapability.TRANSPORT,
        PipelineCapability.SIP_SESSION,
    }),
)
