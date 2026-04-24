"""Tests for end_call_tool.

Covers:
- ToolDefinition metadata (name, capabilities, category)
- Catalog registration
- Executor success path (queue_frame called with EndFrame)
- Executor error path (queue_frame is None)
- Executor error path (queue_frame raises exception)
- Reason logging
"""

import pytest
from unittest.mock import AsyncMock

try:
    from pipecat.frames.frames import EndFrame
except ImportError:
    pytest.skip(
        "pipecat not available (container-only dependency)", allow_module_level=True
    )

from app.tools.builtin.end_call_tool import end_call_tool, end_call_executor
from app.tools.capabilities import PipelineCapability
from app.tools.context import ToolContext
from app.tools.result import ToolStatus
from app.tools.schema import ToolCategory


# =============================================================================
# ToolDefinition Tests
# =============================================================================


class TestEndCallToolDefinition:
    """Test tool definition and capabilities."""

    def test_tool_name_matches_aurora(self):
        # Must match Lambda's VALID_TOOL_TYPES (see
        # cosentus-voice-api-lambda/index.mjs). Renaming here without a
        # coordinated Lambda schema change would desync agent configs.
        assert end_call_tool.name == "end_call"

    def test_category(self):
        assert end_call_tool.category == ToolCategory.SYSTEM

    def test_requires_transport(self):
        assert end_call_tool.requires == frozenset({PipelineCapability.TRANSPORT})

    def test_has_reason_parameter(self):
        param_names = [p.name for p in end_call_tool.parameters]
        assert "reason" in param_names

    def test_reason_parameter_is_optional(self):
        # Aurora stores end_call with empty properties. OG's end_call
        # takes no parameters. We keep ``reason`` around for audit
        # logging but it's OPTIONAL — forcing it required would break
        # LLM compatibility with minimal end_call invocations.
        reason_param = next(p for p in end_call_tool.parameters if p.name == "reason")
        assert reason_param.required is False

    def test_timeout(self):
        assert end_call_tool.timeout_seconds == 5.0

    def test_registered_in_catalog(self):
        from app.tools.builtin.catalog import ALL_LOCAL_TOOLS

        assert end_call_tool in ALL_LOCAL_TOOLS

    def test_description_mentions_end_call(self):
        assert "end" in end_call_tool.description.lower()

    def test_bedrock_tool_spec_format(self):
        spec = end_call_tool.to_bedrock_tool_spec()
        assert "toolSpec" in spec
        assert spec["toolSpec"]["name"] == "end_call"


# =============================================================================
# Executor Tests
# =============================================================================


class TestHangupExecutor:
    """Test tool execution logic."""

    @pytest.fixture
    def mock_queue_frame(self):
        """Mock queue_frame callback."""
        return AsyncMock()

    @pytest.fixture
    def context(self, mock_queue_frame):
        """Create a ToolContext with a mock queue_frame."""
        return ToolContext(
            call_id="test-call-123",
            session_id="test-session-456",
            turn_number=5,
            queue_frame=mock_queue_frame,
        )

    @pytest.fixture
    def context_no_queue_frame(self):
        """Create a ToolContext without queue_frame (simulates missing wiring)."""
        return ToolContext(
            call_id="test-call-123",
            session_id="test-session-456",
        )

    @pytest.mark.asyncio
    async def test_success_queues_endframe(self, context, mock_queue_frame):
        """Executor should queue an EndFrame when queue_frame is available."""
        result = await end_call_executor({"reason": "Issue resolved"}, context)

        assert result.status == ToolStatus.SUCCESS
        assert result.is_success()
        mock_queue_frame.assert_called_once()

        # Verify it was called with an EndFrame
        queued_frame = mock_queue_frame.call_args[0][0]
        assert isinstance(queued_frame, EndFrame)

    @pytest.mark.asyncio
    async def test_success_result_content(self, context):
        """Result should contain hangup confirmation data."""
        result = await end_call_executor({"reason": "Customer satisfied"}, context)

        assert result.status == ToolStatus.SUCCESS
        assert result.content["hangup_initiated"] is True
        assert result.content["reason"] == "Customer satisfied"
        assert result.content["call_id"] == "test-call-123"
        assert "message" in result.content

    @pytest.mark.asyncio
    async def test_success_result_suppresses_llm_reinference(self, context):
        """Hangup result should set run_llm=False to prevent redundant LLM call."""
        result = await end_call_executor({"reason": "Call complete"}, context)

        assert result.status == ToolStatus.SUCCESS
        assert result.run_llm is False

    @pytest.mark.asyncio
    async def test_error_result_does_not_suppress_llm(self, context_no_queue_frame):
        """Error results should not suppress LLM (default None lets Pipecat decide)."""
        result = await end_call_executor({"reason": "Done"}, context_no_queue_frame)

        assert result.status == ToolStatus.ERROR
        assert result.run_llm is None

    @pytest.mark.asyncio
    async def test_default_reason(self, context):
        """Executor should use a default reason when none provided."""
        result = await end_call_executor({}, context)

        assert result.status == ToolStatus.SUCCESS
        assert result.content["reason"] == "Conversation concluded"

    @pytest.mark.asyncio
    async def test_error_when_no_queue_frame(self, context_no_queue_frame):
        """Executor should return error when queue_frame is None."""
        result = await end_call_executor({"reason": "Done"}, context_no_queue_frame)

        assert result.status == ToolStatus.ERROR
        assert not result.is_success()
        assert result.error_code == "END_CALL_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_error_when_queue_frame_raises(self, context, mock_queue_frame):
        """Executor should handle exceptions from queue_frame gracefully."""
        mock_queue_frame.side_effect = RuntimeError("Pipeline crashed")

        result = await end_call_executor({"reason": "Done"}, context)

        assert result.status == ToolStatus.ERROR
        assert result.error_code == "END_CALL_FAILED"

    @pytest.mark.asyncio
    async def test_queue_frame_not_called_when_none(self, context_no_queue_frame):
        """Verify no attempt to call None queue_frame."""
        result = await end_call_executor({"reason": "Done"}, context_no_queue_frame)

        # Should return error, not raise AttributeError
        assert result.status == ToolStatus.ERROR


# =============================================================================
# Capability Gating Tests
# =============================================================================


class TestHangupCapabilityGating:
    """Test that the tool is correctly gated by capabilities."""

    def test_registers_with_transport_capability(self):
        """Tool should be included when TRANSPORT capability is available."""
        available = frozenset({PipelineCapability.BASIC, PipelineCapability.TRANSPORT})
        assert end_call_tool.requires <= available

    def test_excluded_without_transport_capability(self):
        """Tool should be excluded when only BASIC capability is available."""
        available = frozenset({PipelineCapability.BASIC})
        assert not (end_call_tool.requires <= available)

    def test_does_not_require_sip_session(self):
        """end_call should work for both SIP and WebRTC — no SIP required."""
        assert PipelineCapability.SIP_SESSION not in end_call_tool.requires
