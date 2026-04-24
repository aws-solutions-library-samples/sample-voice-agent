"""Transfer-call tool for the voice agent.

Executes a SIP REFER via Daily's transport to hand the caller to a
different phone number. Target names + their destination numbers come
from the agent's Aurora config (voice_agents.tools[].settings.targets),
NOT from an environment variable. This means:

  * Multiple agents can share a transport but have disjoint target
    lists.
  * Changing a target number is a DB update — no redeploy needed.
  * The LLM-facing tool schema's ``target`` parameter carries the
    per-agent target names as an ``enum`` so Claude can only pick a
    valid target (populated dynamically in _register_tools).

Named ``transfer_call`` to match Aurora's VALID_TOOL_TYPES and OG's
core/tool_handlers.py handler name (voiceagent/core/tool_handlers.py:
transfer_call).

Capability requirements:
    - TRANSPORT:   Needs a Daily transport with sip_refer().
    - SIP_SESSION: Caller must be on a real SIP leg for REFER to
                   have any effect.

Aurora settings shape:

    {
      "type": "transfer_call",
      "description": "...",
      "settings": {
        "targets": {
          "billing_supervisor": "+13105551234",
          "hang_up_agent":      "+13105559876"
        }
      }
    }

If ``targets`` is missing or empty, the tool registers with a plain
string ``target`` parameter (no enum) and the executor fails safe —
telling Claude the tool isn't properly configured so the call
continues without a broken transfer attempt.
"""

from __future__ import annotations

import structlog
from typing import Any, Dict

from ..capabilities import PipelineCapability
from ..context import ToolContext
from ..result import ToolResult, error_result, success_result
from ..schema import ToolCategory, ToolDefinition, ToolParameter

logger = structlog.get_logger(__name__)


def _build_conversation_summary(context: ToolContext) -> str:
    """Build a brief conversation summary for the human agent receiving
    the transfer. Kept compact (last 6 turns, content truncated at 100
    chars) so it fits in call-history / CRM notes without bloating
    them. Same behavior as OG's transfer_call handler.
    """
    if not context.conversation_history:
        return "No conversation history available."
    recent = context.conversation_history[-6:]
    parts = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if len(content) > 100:
            content = content[:100] + "..."
        parts.append(f"{role.title()}: {content}")
    return " | ".join(parts)


async def transfer_call_executor(
    arguments: Dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    """Execute SIP REFER to the target phone number.

    Args:
        arguments: {"target": "<target_name>"}
        context: Tool execution context (sip_session_id + tool_settings.targets)

    Returns:
        ToolResult
    """
    target_name = (arguments.get("target") or "").strip()
    if not target_name:
        return error_result(
            error_code="TRANSFER_MISSING_TARGET",
            error_message="No transfer target specified. Pick one of the configured target names.",
        )

    # Settings come from Aurora (voice_agents.tools[].settings) via
    # ToolContext.tool_settings. Wired by _register_tools at
    # pipeline-creation time.
    targets = context.tool_settings.get("targets") or {}
    if not isinstance(targets, dict) or not targets:
        logger.error(
            "transfer_call_not_configured",
            call_id=context.call_id,
            target_name=target_name,
        )
        return error_result(
            error_code="TRANSFER_NOT_CONFIGURED",
            error_message=(
                "Transfer targets aren't configured for this agent. "
                "Tell the customer you can't transfer right now and "
                "either help them yourself or offer a callback."
            ),
        )

    phone_number = targets.get(target_name)
    if not phone_number:
        valid = sorted(targets.keys())
        logger.warning(
            "transfer_call_unknown_target",
            call_id=context.call_id,
            target_name=target_name,
            valid_targets=valid,
        )
        return error_result(
            error_code="TRANSFER_UNKNOWN_TARGET",
            error_message=(
                f"'{target_name}' is not a configured transfer target. "
                f"Valid targets: {', '.join(valid) or '(none)'}."
            ),
        )

    # Resolve the SIP session id. Primary source is the pipeline's
    # sip_session_tracker (populated on dialin_connected); fall back
    # to scanning transport participants for a SIP leg in case the
    # tracker hasn't been populated yet (race on early tool calls).
    sip_session_id = context.sip_session_id
    if not sip_session_id and context.transport is not None:
        participants = getattr(context.transport, "_participants", {}) or {}
        for participant_id, participant in participants.items():
            if participant.get("sipFrom"):
                sip_session_id = participant_id
                logger.info(
                    "sip_participant_fallback_used",
                    call_id=context.call_id,
                    sip_session_id=sip_session_id,
                )
                break

    if not sip_session_id:
        logger.error(
            "transfer_call_no_sip_session",
            call_id=context.call_id,
            target_name=target_name,
        )
        return error_result(
            error_code="TRANSFER_FAILED",
            error_message="Unable to identify the call to transfer. Please try again.",
        )

    conversation_summary = _build_conversation_summary(context)

    logger.info(
        "transfer_call_executing",
        call_id=context.call_id,
        target_name=target_name,
        sip_session_id=sip_session_id,
        # Never log the full number — mask all but last 4.
        destination_mask=(
            "****" + phone_number[-4:]
            if isinstance(phone_number, str) and len(phone_number) >= 4
            else "***"
        ),
    )

    try:
        # Daily's sip_refer API: { sessionId, toEndPoint }. Note the
        # capital P in toEndPoint — it's required by Daily's SDK.
        await context.transport.sip_refer(
            {
                "sessionId": sip_session_id,
                "toEndPoint": phone_number,
            }
        )
    except Exception as exc:
        logger.error(
            "transfer_call_refer_failed",
            call_id=context.call_id,
            target_name=target_name,
            error=str(exc),
            exc_info=True,
        )
        return error_result(
            error_code="TRANSFER_FAILED",
            error_message=(
                "I'm unable to complete the transfer right now. "
                "Let me try to help you myself, or you can call back "
                "in a few minutes."
            ),
        )

    logger.info(
        "transfer_call_refer_success",
        call_id=context.call_id,
        target_name=target_name,
        sip_session_id=sip_session_id,
    )

    return success_result(
        {
            "transfer_initiated": True,
            "target": target_name,
            "call_id": context.call_id,
            "conversation_summary": conversation_summary,
            "message": (
                f"I'm transferring you to {target_name} now. "
                "Please hold for just a moment."
            ),
        }
    )


transfer_call_tool = ToolDefinition(
    name="transfer_call",
    description=(
        # Default description; Aurora agents override via tools_config.
        "Transfer the caller to a specific pre-configured target. "
        "Only use when the caller explicitly requests a transfer, when "
        "you cannot help with their request, or when the issue "
        "requires a human. Do not transfer without first confirming "
        "with the caller."
    ),
    category=ToolCategory.SYSTEM,
    parameters=[
        # ``target`` is declared as a plain string here; _register_tools
        # rewrites this parameter at registration time to add
        # ``enum=[...target_names...]`` drawn from Aurora settings, so
        # the LLM can only emit valid target names.
        ToolParameter(
            name="target",
            type="string",
            description=(
                "Name of the transfer target. Must exactly match one of "
                "the configured targets for this agent."
            ),
            required=True,
        ),
    ],
    executor=transfer_call_executor,
    # SIP REFER negotiation can take 3-10 seconds with slow carriers;
    # 30s covers worst-case + a modest retry margin.
    timeout_seconds=30.0,
    requires=frozenset(
        {
            PipelineCapability.TRANSPORT,
            PipelineCapability.SIP_SESSION,
            # No TRANSFER_DESTINATION — that was the env-var-based
            # pre-7C path. Aurora-per-agent targets replace it.
        }
    ),
)
