"""Tests for press_digit_tool.

Covers:
- ToolDefinition metadata (name, capabilities, category, required param)
- Catalog registration
- Input validation (empty, invalid chars)
- Executor happy path (InterruptionTaskFrame queued, one DTMF per digit)
- Inter-digit pacing (asyncio.sleep called N-1 times)
- sip_session_id passed through as transport_destination
- Error paths (queue_frame is None, queue_frame raises)
- run_llm=False on success (bot stays silent after pressing)
- PRESS_DIGIT_PACING_MS env var override
"""

from __future__ import annotations

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import (
        InterruptionTaskFrame,
        OutputDTMFUrgentFrame,
    )
except ImportError:
    pytest.skip(
        "pipecat not available (container-only dependency)",
        allow_module_level=True,
    )

from app.tools.builtin.press_digit_tool import (
    press_digit_executor,
    press_digit_tool,
    _read_pacing_ms,
)
from app.tools.capabilities import PipelineCapability
from app.tools.context import ToolContext
from app.tools.result import ToolStatus
from app.tools.schema import ToolCategory


# =============================================================================
# ToolDefinition metadata
# =============================================================================


class TestPressDigitToolDefinition:
    def test_tool_name_matches_aurora(self):
        # Must match cosentus-voice-api-lambda VALID_TOOL_TYPES.
        assert press_digit_tool.name == "press_digit"

    def test_category_is_system(self):
        assert press_digit_tool.category == ToolCategory.SYSTEM

    def test_requires_transport_and_sip(self):
        # Pressing tones on a call with no SIP session is meaningless
        # — the LLM shouldn't even see the tool on WebRTC test calls.
        assert press_digit_tool.requires == frozenset({
            PipelineCapability.TRANSPORT,
            PipelineCapability.SIP_SESSION,
        })

    def test_digits_parameter_required(self):
        param_names = [p.name for p in press_digit_tool.parameters]
        assert "digits" in param_names
        digits = next(p for p in press_digit_tool.parameters if p.name == "digits")
        assert digits.required is True

    def test_digits_pattern_enforced(self):
        digits = next(p for p in press_digit_tool.parameters if p.name == "digits")
        assert digits.pattern == r"^[0-9*#]+$"

    def test_timeout(self):
        # 10s covers the worst-case (long account number + carrier pacing).
        assert press_digit_tool.timeout_seconds == 10.0

    def test_registered_in_catalog(self):
        from app.tools.builtin.catalog import ALL_LOCAL_TOOLS

        assert press_digit_tool in ALL_LOCAL_TOOLS

    def test_bedrock_tool_spec_format(self):
        spec = press_digit_tool.to_bedrock_tool_spec()
        assert spec["toolSpec"]["name"] == "press_digit"
        assert "digits" in spec["toolSpec"]["inputSchema"]["json"]["properties"]


# =============================================================================
# Input validation
# =============================================================================


def _make_ctx(with_queue: bool = True, sip_session_id: str | None = "s-123") -> ToolContext:
    queue_frame = AsyncMock() if with_queue else None
    return ToolContext(
        call_id="call-1",
        session_id="session-1",
        sip_session_id=sip_session_id,
        queue_frame=queue_frame,
    )


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_empty_digits_returns_error(self):
        ctx = _make_ctx()
        result = await press_digit_executor({"digits": ""}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "PRESS_DIGIT_EMPTY"
        ctx.queue_frame.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_error(self):
        ctx = _make_ctx()
        result = await press_digit_executor({"digits": "   "}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "PRESS_DIGIT_EMPTY"

    @pytest.mark.asyncio
    async def test_invalid_chars_returns_error(self):
        ctx = _make_ctx()
        result = await press_digit_executor({"digits": "1a2"}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "PRESS_DIGIT_INVALID_CHARS"
        assert "a" in result.error_message
        ctx.queue_frame.assert_not_called()

    @pytest.mark.asyncio
    async def test_letters_only_rejected(self):
        ctx = _make_ctx()
        result = await press_digit_executor({"digits": "abc"}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "PRESS_DIGIT_INVALID_CHARS"


# =============================================================================
# Executor happy path
# =============================================================================


class TestExecutorHappyPath:
    @pytest.mark.asyncio
    async def test_single_digit_queues_one_frame_plus_interrupt(self):
        ctx = _make_ctx()
        # Zero pacing → skip sleeps for fast assertion
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock):
                result = await press_digit_executor({"digits": "1"}, ctx)

        assert result.status == ToolStatus.SUCCESS
        assert result.content["digit_count"] == 1
        # Exactly 2 queue_frame calls: 1 InterruptionTaskFrame + 1 DTMF
        assert ctx.queue_frame.call_count == 2
        frames = [call.args[0] for call in ctx.queue_frame.call_args_list]
        assert isinstance(frames[0], InterruptionTaskFrame)
        assert isinstance(frames[1], OutputDTMFUrgentFrame)
        assert frames[1].button == KeypadEntry.ONE

    @pytest.mark.asyncio
    async def test_multi_digit_queues_one_frame_per_digit(self):
        ctx = _make_ctx()
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock):
                result = await press_digit_executor({"digits": "12345"}, ctx)

        assert result.status == ToolStatus.SUCCESS
        assert result.content["digit_count"] == 5
        # 1 interrupt + 5 DTMF
        assert ctx.queue_frame.call_count == 6

    @pytest.mark.asyncio
    async def test_star_and_pound_accepted(self):
        ctx = _make_ctx()
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock):
                result = await press_digit_executor({"digits": "*#"}, ctx)
        assert result.status == ToolStatus.SUCCESS
        frames = [call.args[0] for call in ctx.queue_frame.call_args_list]
        dtmf_frames = [f for f in frames if isinstance(f, OutputDTMFUrgentFrame)]
        assert dtmf_frames[0].button == KeypadEntry.STAR
        assert dtmf_frames[1].button == KeypadEntry.POUND

    @pytest.mark.asyncio
    async def test_sip_session_id_passed_as_transport_destination(self):
        ctx = _make_ctx(sip_session_id="sip-789")
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock):
                await press_digit_executor({"digits": "1"}, ctx)
        frames = [call.args[0] for call in ctx.queue_frame.call_args_list]
        dtmf = [f for f in frames if isinstance(f, OutputDTMFUrgentFrame)][0]
        assert dtmf.transport_destination == "sip-789"

    @pytest.mark.asyncio
    async def test_success_result_sets_run_llm_false(self):
        # Staying silent after pressing is critical — the IVR's next
        # prompt IS the next user turn. If we flipped run_llm=True the
        # bot would interject mid-IVR.
        ctx = _make_ctx()
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock):
                result = await press_digit_executor({"digits": "1"}, ctx)
        assert result.run_llm is False


# =============================================================================
# Inter-digit pacing
# =============================================================================


class TestPacing:
    @pytest.mark.asyncio
    async def test_sleep_called_between_digits_default(self):
        ctx = _make_ctx()
        with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            await press_digit_executor({"digits": "123"}, ctx)
        # Expected: 1 settle sleep + 2 inter-digit sleeps (between
        # digits, not after the last) = 3 sleeps for 3 digits.
        assert sleep_mock.call_count == 3
        # First sleep is the settle delay (60ms = 0.06s).
        assert sleep_mock.call_args_list[0].args == (0.06,)
        # Subsequent sleeps use default pacing (120ms = 0.12s).
        assert sleep_mock.call_args_list[1].args == (0.12,)
        assert sleep_mock.call_args_list[2].args == (0.12,)

    @pytest.mark.asyncio
    async def test_pacing_env_var_override(self):
        ctx = _make_ctx()
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "250"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
                await press_digit_executor({"digits": "12"}, ctx)
        # Settle 0.06s + one inter-digit 0.25s
        assert sleep_mock.call_args_list[1].args == (0.25,)

    @pytest.mark.asyncio
    async def test_zero_pacing_skips_inter_digit_sleep(self):
        ctx = _make_ctx()
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
                await press_digit_executor({"digits": "12"}, ctx)
        # Only the settle sleep; no inter-digit sleeps because pacing=0.
        assert sleep_mock.call_count == 1
        assert sleep_mock.call_args_list[0].args == (0.06,)


class TestPacingHelper:
    def test_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _read_pacing_ms() == 120.0

    def test_respects_valid_value(self):
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "200"}):
            assert _read_pacing_ms() == 200.0

    def test_rejects_negative(self):
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "-50"}):
            assert _read_pacing_ms() == 120.0

    def test_rejects_out_of_range(self):
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "5000"}):
            assert _read_pacing_ms() == 120.0

    def test_rejects_non_numeric(self):
        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "slow"}):
            assert _read_pacing_ms() == 120.0


# =============================================================================
# Error paths
# =============================================================================


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_no_queue_frame_returns_unavailable(self):
        ctx = _make_ctx(with_queue=False)
        result = await press_digit_executor({"digits": "1"}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "PRESS_DIGIT_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_interrupt_queue_failure_does_not_abort(self):
        # If the InterruptionTaskFrame push fails (transient), we
        # should still try to send the DTMF — the tone matters more
        # than the TTS-cancel nicety.
        ctx = _make_ctx()
        call_count = {"n": 0}

        async def queue_frame(frame):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("pipe blocked")

        ctx.queue_frame = queue_frame  # type: ignore[assignment]

        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock):
                result = await press_digit_executor({"digits": "1"}, ctx)

        assert result.status == ToolStatus.SUCCESS
        # 1 (failed) interrupt + 1 DTMF
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_dtmf_queue_failure_returns_error(self):
        ctx = _make_ctx()
        call_count = {"n": 0}

        async def queue_frame(frame):
            call_count["n"] += 1
            # Fail on the DTMF push (2nd call; 1st is Interrupt)
            if call_count["n"] == 2:
                raise RuntimeError("transport closed")

        ctx.queue_frame = queue_frame  # type: ignore[assignment]

        with patch.dict(os.environ, {"PRESS_DIGIT_PACING_MS": "0"}):
            with patch("app.tools.builtin.press_digit_tool.asyncio.sleep", new_callable=AsyncMock):
                result = await press_digit_executor({"digits": "12"}, ctx)

        assert result.status == ToolStatus.ERROR
        assert result.error_code == "PRESS_DIGIT_FAILED"
