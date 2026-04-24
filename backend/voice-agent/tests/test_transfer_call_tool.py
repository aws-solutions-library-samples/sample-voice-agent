"""Tests for transfer_call_tool.

Covers:
- ToolDefinition metadata (name matches Aurora, capability gating,
  schema shape — single ``target`` parameter)
- Catalog registration
- Executor happy path (sip_refer called with right sessionId/toEndPoint)
- Aurora targets dict resolution (target name → phone)
- Missing/unknown target error paths
- Missing tool_settings → TRANSFER_NOT_CONFIGURED
- SIP session ID fallback to transport participants
- conversation_summary truncation behavior
- Daily sip_refer exception → TRANSFER_FAILED
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

try:
    from pipecat.frames.frames import EndFrame  # noqa: F401  (proxy for pipecat install)
except ImportError:
    pytest.skip(
        "pipecat not available (container-only dependency)",
        allow_module_level=True,
    )

from app.tools.builtin.transfer_call_tool import (
    transfer_call_executor,
    transfer_call_tool,
    _build_conversation_summary,
)
from app.tools.capabilities import PipelineCapability
from app.tools.context import ToolContext
from app.tools.result import ToolStatus
from app.tools.schema import ToolCategory


# =============================================================================
# Fixtures
# =============================================================================


def _ctx_with_targets(targets=None, sip_session_id="sip-1"):
    transport = MagicMock()
    transport.sip_refer = AsyncMock()
    return ToolContext(
        call_id="call-1",
        session_id="session-1",
        sip_session_id=sip_session_id,
        transport=transport,
        tool_settings={"targets": targets} if targets is not None else {},
        conversation_history=[
            {"role": "user", "content": "I need help"},
            {"role": "assistant", "content": "Sure, what about?"},
        ],
    )


# =============================================================================
# ToolDefinition metadata
# =============================================================================


class TestTransferCallToolDefinition:
    def test_tool_name_matches_aurora(self):
        # Must match cosentus-voice-api-lambda VALID_TOOL_TYPES.
        assert transfer_call_tool.name == "transfer_call"

    def test_category_is_system(self):
        assert transfer_call_tool.category == ToolCategory.SYSTEM

    def test_requires_transport_and_sip_only(self):
        # Phase 7C dropped TRANSFER_DESTINATION capability — Aurora
        # settings.targets check happens at executor time, not via
        # capability gating.
        assert transfer_call_tool.requires == frozenset({
            PipelineCapability.TRANSPORT,
            PipelineCapability.SIP_SESSION,
        })

    def test_single_target_parameter(self):
        assert len(transfer_call_tool.parameters) == 1
        target = transfer_call_tool.parameters[0]
        assert target.name == "target"
        assert target.required is True

    def test_no_legacy_department_or_priority_params(self):
        # Pre-7C the tool had department/priority/reason. Aurora's
        # schema is just `target` + the per-agent settings.targets.
        names = {p.name for p in transfer_call_tool.parameters}
        assert "department" not in names
        assert "priority" not in names
        assert "reason" not in names

    def test_timeout_30s(self):
        # SIP REFER negotiation can take 3-10s with slow carriers.
        assert transfer_call_tool.timeout_seconds == 30.0

    def test_registered_in_catalog(self):
        from app.tools.builtin.catalog import ALL_LOCAL_TOOLS

        assert transfer_call_tool in ALL_LOCAL_TOOLS

    def test_bedrock_tool_spec(self):
        spec = transfer_call_tool.to_bedrock_tool_spec()
        assert spec["toolSpec"]["name"] == "transfer_call"
        props = spec["toolSpec"]["inputSchema"]["json"]["properties"]
        assert "target" in props
        assert "department" not in props


# =============================================================================
# Executor happy path
# =============================================================================


class TestExecutorHappyPath:
    @pytest.mark.asyncio
    async def test_resolves_target_to_phone_and_calls_sip_refer(self):
        ctx = _ctx_with_targets(
            targets={
                "billing": "+15551234567",
                "supervisor": "+15559998888",
            },
            sip_session_id="sip-abc",
        )

        result = await transfer_call_executor(
            {"target": "billing"}, ctx
        )

        assert result.status == ToolStatus.SUCCESS
        assert result.content["transfer_initiated"] is True
        assert result.content["target"] == "billing"
        # sip_refer called with the right sessionId + capital-P toEndPoint
        ctx.transport.sip_refer.assert_awaited_once()
        args = ctx.transport.sip_refer.await_args.args[0]
        assert args == {
            "sessionId": "sip-abc",
            "toEndPoint": "+15551234567",
        }

    @pytest.mark.asyncio
    async def test_includes_conversation_summary(self):
        ctx = _ctx_with_targets(targets={"billing": "+15551234567"})
        result = await transfer_call_executor({"target": "billing"}, ctx)
        assert result.status == ToolStatus.SUCCESS
        assert "conversation_summary" in result.content

    @pytest.mark.asyncio
    async def test_sip_session_id_fallback_via_transport_participants(self):
        # If sip_session_id isn't on the context (race on early
        # invocation), fall back to scanning transport._participants.
        ctx = _ctx_with_targets(
            targets={"billing": "+15551234567"}, sip_session_id=None
        )
        ctx.transport._participants = {
            "remote-1": {"sipFrom": "+15551234567"},
        }
        result = await transfer_call_executor({"target": "billing"}, ctx)
        assert result.status == ToolStatus.SUCCESS
        args = ctx.transport.sip_refer.await_args.args[0]
        assert args["sessionId"] == "remote-1"


# =============================================================================
# Error paths
# =============================================================================


class TestExecutorErrors:
    @pytest.mark.asyncio
    async def test_missing_target_arg(self):
        ctx = _ctx_with_targets(targets={"billing": "+15551234567"})
        result = await transfer_call_executor({}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TRANSFER_MISSING_TARGET"

    @pytest.mark.asyncio
    async def test_empty_target_arg(self):
        ctx = _ctx_with_targets(targets={"billing": "+15551234567"})
        result = await transfer_call_executor({"target": "  "}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TRANSFER_MISSING_TARGET"

    @pytest.mark.asyncio
    async def test_unknown_target_lists_valid_options(self):
        ctx = _ctx_with_targets(
            targets={"billing": "+15551234567", "supervisor": "+15559998888"}
        )
        result = await transfer_call_executor(
            {"target": "claims_supervisor"}, ctx
        )
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TRANSFER_UNKNOWN_TARGET"
        # Error message should enumerate valid targets so the LLM can
        # retry with one.
        assert "billing" in result.error_message
        assert "supervisor" in result.error_message

    @pytest.mark.asyncio
    async def test_no_targets_in_settings_returns_not_configured(self):
        # Aurora row missing/empty settings.targets — agent designer
        # error. Tool fails safe so the call continues without a busted
        # transfer attempt.
        ctx = _ctx_with_targets(targets={})
        result = await transfer_call_executor({"target": "billing"}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TRANSFER_NOT_CONFIGURED"

    @pytest.mark.asyncio
    async def test_targets_not_a_dict_returns_not_configured(self):
        # Defensive: Aurora schema says targets must be an object, but
        # belt-and-suspenders if someone manually edits the JSON wrong.
        ctx = _ctx_with_targets(targets=["+15551234567"])  # type: ignore[arg-type]
        result = await transfer_call_executor({"target": "billing"}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TRANSFER_NOT_CONFIGURED"

    @pytest.mark.asyncio
    async def test_no_sip_session_returns_failed(self):
        ctx = _ctx_with_targets(
            targets={"billing": "+15551234567"}, sip_session_id=None
        )
        # Empty transport participants too
        ctx.transport._participants = {}
        result = await transfer_call_executor({"target": "billing"}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TRANSFER_FAILED"

    @pytest.mark.asyncio
    async def test_sip_refer_exception_returns_failed(self):
        ctx = _ctx_with_targets(targets={"billing": "+15551234567"})
        ctx.transport.sip_refer.side_effect = RuntimeError("daily SDK timed out")
        result = await transfer_call_executor({"target": "billing"}, ctx)
        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TRANSFER_FAILED"


# =============================================================================
# Conversation summary helper
# =============================================================================


class TestConversationSummary:
    def test_empty_history(self):
        ctx = ToolContext(call_id="c", session_id="s")
        ctx.conversation_history = []
        assert _build_conversation_summary(ctx) == "No conversation history available."

    def test_truncates_long_messages(self):
        ctx = ToolContext(call_id="c", session_id="s")
        ctx.conversation_history = [
            {"role": "user", "content": "x" * 200},
        ]
        out = _build_conversation_summary(ctx)
        assert "..." in out
        assert out.startswith("User: ")

    def test_keeps_last_six_only(self):
        ctx = ToolContext(call_id="c", session_id="s")
        ctx.conversation_history = [
            {"role": "user", "content": f"msg-{i}"} for i in range(20)
        ]
        out = _build_conversation_summary(ctx)
        # Should only mention msg-14 through msg-19 (last 6)
        assert "msg-14" in out
        assert "msg-19" in out
        assert "msg-13" not in out
