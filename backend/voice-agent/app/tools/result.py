"""Tool execution results for the tool calling framework.

This module provides ToolResult and ToolStatus for representing the outcome
of tool executions, including success, error, timeout, and cancellation states.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class ToolStatus(Enum):
    """Tool execution status."""

    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class ToolResult:
    """Result from tool execution.

    Maps to Bedrock Converse API toolResult format and provides
    user-friendly error messages for TTS fallback.

    Attributes:
        status: Execution status (SUCCESS, ERROR, TIMEOUT, CANCELLED)
        content: Result content dict (for SUCCESS)
        error_message: Human-readable error message (for ERROR/TIMEOUT)
        error_code: Machine-readable error code (for ERROR/TIMEOUT)
        execution_time_ms: Actual execution time in milliseconds
        run_llm: Whether the LLM should re-infer after this result.
            None (default) uses Pipecat's default behavior. Set to False
            for terminal tools like end_call that end the pipeline and
            don't need a follow-up LLM response.
        spoken_response: Text to speak directly via TTS when run_llm=False.
            For deterministic tools where the response is known in advance,
            this skips the second LLM roundtrip and speaks directly.

    Example:
        >>> # Successful result
        >>> result = ToolResult(
        ...     status=ToolStatus.SUCCESS,
        ...     content={"order_id": "12345", "status": "shipped"}
        ... )
        >>>
        >>> # Error result
        >>> result = ToolResult(
        ...     status=ToolStatus.ERROR,
        ...     error_code="NOT_FOUND",
        ...     error_message="Order not found"
        ... )
    """

    status: ToolStatus
    content: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    execution_time_ms: float = 0.0
    run_llm: Optional[bool] = None
    spoken_response: Optional[str] = None

    def is_success(self) -> bool:
        """Check if the tool execution was successful."""
        return self.status == ToolStatus.SUCCESS

    def to_bedrock_tool_result(self, tool_use_id: str) -> Dict[str, Any]:
        """Convert to Bedrock Converse API toolResult format.

        Args:
            tool_use_id: ID from the tool_use block in LLM response

        Returns:
            Dict in Bedrock toolResult format:
            {
              "toolResult": {
                "toolUseId": "...",
                "content": [{"json": {...}} | {"text": "..."}],
                "status": "success" | "error"
              }
            }
        """
        if self.status == ToolStatus.SUCCESS:
            return {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": [{"json": self.content or {}}],
                    "status": "success",
                }
            }
        else:
            # For errors, return text content with error details
            error_text = self._format_error_text()
            return {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": [{"text": error_text}],
                    "status": "error",
                }
            }

    def _format_error_text(self) -> str:
        """Format error information for LLM context."""
        if self.error_code and self.error_message:
            return f"{self.error_code}: {self.error_message}"
        elif self.error_message:
            return self.error_message
        elif self.error_code:
            return f"Error: {self.error_code}"
        else:
            return "Tool execution failed"

    def to_user_message(self) -> str:
        """Generate user-friendly message for TTS.

        Used when tool fails to provide graceful error messaging.
        Returns empty string for SUCCESS (LLM synthesizes response)
        and CANCELLED (user interrupted, don't speak).

        Returns:
            User-friendly error message for TTS, or empty string.
        """
        if self.status == ToolStatus.SUCCESS:
            return ""  # LLM will synthesize response from tool result

        if self.status == ToolStatus.CANCELLED:
            return ""  # User interrupted, don't speak

        if self.status == ToolStatus.TIMEOUT:
            return (
                "I'm sorry, that request is taking longer than expected. "
                "Let me try something else."
            )

        # Generic error message
        return (
            "I encountered an issue while looking that up. "
            "Let me know if you'd like me to try again."
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        d: Dict[str, Any] = {
            "status": self.status.value,
            "content": self.content,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "execution_time_ms": round(self.execution_time_ms, 1),
        }
        if self.run_llm is not None:
            d["run_llm"] = self.run_llm
        if self.spoken_response is not None:
            d["spoken_response"] = self.spoken_response
        return d


def success_result(content: Dict[str, Any]) -> ToolResult:
    """Create a successful tool result.

    Args:
        content: Result content dict

    Returns:
        ToolResult with SUCCESS status
    """
    return ToolResult(status=ToolStatus.SUCCESS, content=content)


def error_result(
    error_message: str,
    error_code: Optional[str] = None,
) -> ToolResult:
    """Create an error tool result.

    Args:
        error_message: Human-readable error message
        error_code: Optional machine-readable error code

    Returns:
        ToolResult with ERROR status
    """
    return ToolResult(
        status=ToolStatus.ERROR,
        error_code=error_code,
        error_message=error_message,
    )


def timeout_result(timeout_seconds: float) -> ToolResult:
    """Create a timeout tool result.

    Args:
        timeout_seconds: The timeout threshold that was exceeded

    Returns:
        ToolResult with TIMEOUT status
    """
    return ToolResult(
        status=ToolStatus.TIMEOUT,
        error_code="EXECUTION_TIMEOUT",
        error_message=f"Tool execution exceeded {timeout_seconds}s timeout",
    )


def cancelled_result() -> ToolResult:
    """Create a cancelled tool result.

    Returns:
        ToolResult with CANCELLED status
    """
    return ToolResult(status=ToolStatus.CANCELLED)


def direct_response_result(
    content: Dict[str, Any], spoken_response: str
) -> ToolResult:
    """Create a result that speaks directly without LLM re-inference.

    For deterministic tools where the response is known in advance,
    this skips the second Bedrock Converse API call and speaks the
    response directly via TTS.

    Args:
        content: Result content dict (still included for logging/context)
        spoken_response: Text to speak directly via TTS

    Returns:
        ToolResult with run_llm=False and spoken_response set
    """
    return ToolResult(
        status=ToolStatus.SUCCESS,
        content=content,
        run_llm=False,
        spoken_response=spoken_response,
    )
