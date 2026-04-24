"""Tool execution context for the tool calling framework.

This module provides the ToolContext class which is passed to tool executors
to provide session information, metrics collection, and cancellation support.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..observability import MetricsCollector
    from pipecat.frames.frames import Frame
    from pipecat.transports.base_transport import BaseTransport

#: Callback type for queuing frames into the pipeline.
#: Signature: async (Frame) -> None
QueueFrameFunc = Callable[["Frame"], Awaitable[None]]


@dataclass
class ToolContext:
    """Context passed to tool executors.

    Provides access to session state, user identity, and observability.
    Tool executors should check `cancelled` periodically for long-running
    operations to support barge-in cancellation.

    Attributes:
        call_id: Unique identifier for the phone call
        session_id: Unique identifier for the session
        turn_number: Current conversation turn number
        user_id: Optional user identifier (from authentication)
        user_phone: Optional user phone number (from caller ID)
        conversation_history: Recent conversation turns for context
        metrics_collector: Optional metrics collector for observability
        transport: Optional transport for SIP operations (e.g., transfers)
        queue_frame: Optional callback to queue a frame into the pipeline
            (e.g., EndFrame to end a call). Wired to PipelineTask.queue_frame
            at pipeline creation time.
        cancelled: Flag indicating the operation should be cancelled

    Example:
        >>> async def my_tool_executor(args: dict, context: ToolContext) -> ToolResult:
        ...     # Check cancellation for long operations
        ...     if context.cancelled:
        ...         return ToolResult(status=ToolStatus.CANCELLED)
        ...
        ...     # Use session info for audit logging
        ...     logger.info("tool_called", call_id=context.call_id)
        ...
        ...     # Execute tool logic...
        ...     return ToolResult(status=ToolStatus.SUCCESS, content={...})
    """

    # Session information
    call_id: str
    session_id: str
    turn_number: int = 0

    # User identity (from authentication or caller ID)
    user_id: Optional[str] = None
    user_phone: Optional[str] = None

    # Conversation history (last N turns for context)
    conversation_history: List[Dict[str, str]] = field(default_factory=list)

    # Observability
    metrics_collector: Optional["MetricsCollector"] = None

    # Transport for SIP operations (e.g., call transfers)
    transport: Optional["BaseTransport"] = None

    # SIP/Dial-in session ID for transfer operations
    # This is the participant session ID needed for SIP REFER
    sip_session_id: Optional[str] = None

    # Pipeline frame queue -- allows tools to push frames (e.g., EndFrame)
    # into the pipeline. Wired to PipelineTask.queue_frame() at runtime.
    # None when no pipeline task is available (e.g., in unit tests).
    queue_frame: Optional[QueueFrameFunc] = None

    # Per-tool settings from the agent's Aurora config.  Mirrors the
    # ``settings`` object on voice_agents.tools[].settings so tool
    # executors can read, e.g., the transfer targets dict, without
    # having to close over the whole AgentConfig.  Empty dict when the
    # agent didn't supply tool-specific config.
    tool_settings: Dict[str, Any] = field(default_factory=dict)

    # Cancellation signal
    cancelled: bool = False

    def cancel(self) -> None:
        """Mark context as cancelled (for barge-in).

        Tool executors should check the `cancelled` attribute periodically
        and abort execution when set to True.
        """
        self.cancelled = True

    def is_cancelled(self) -> bool:
        """Check if execution has been cancelled.

        Returns:
            True if the tool should abort execution.
        """
        return self.cancelled

    def get_last_user_message(self) -> Optional[str]:
        """Get the most recent user message from conversation history.

        Returns:
            The last user message content, or None if no history.
        """
        for msg in reversed(self.conversation_history):
            if msg.get("role") == "user":
                return msg.get("content")
        return None
