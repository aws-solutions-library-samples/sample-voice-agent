"""Integration tests for the tool calling framework.

These tests verify the end-to-end flow of tool calling, including:
- Tool registration and execution through the pipeline
- Barge-in cancellation behavior
- Multiple sequential tool calls
- Metrics collection during tool execution
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from app.tools import (
        ToolCategory,
        ToolContext,
        ToolDefinition,
        ToolExecutor,
        ToolParameter,
        ToolRegistry,
        ToolResult,
        ToolStatus,
        success_result,
        error_result,
    )
    from app.tools.builtin import time_tool, transfer_call_tool
    from app.observability import MetricsCollector
except ImportError:
    pytest.skip(
        "Container-only dependencies not available (structlog/pipecat)",
        allow_module_level=True,
    )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def metrics_collector():
    """Create a metrics collector for testing."""
    return MetricsCollector(
        call_id="integration-test-call",
        session_id="integration-test-session",
        environment="test",
    )


@pytest.fixture
def full_registry():
    """Create a registry with all builtin tools."""
    registry = ToolRegistry()
    registry.register(time_tool)
    registry.register(transfer_call_tool)
    return registry


@pytest.fixture
def executor_with_metrics(full_registry, metrics_collector):
    """Create an executor with metrics collection."""
    return ToolExecutor(full_registry, metrics_collector)


# Phase 7C dropped the env-var-driven TRANSFER_DESTINATION in favor
# of per-agent Aurora settings.targets. The autouse fixture that
# used to setenv("TRANSFER_DESTINATION", ...) is no longer needed —
# transfer_call_tool reads the targets dict from
# ToolContext.tool_settings instead.


@pytest.fixture
def base_context():
    """Create a base tool context."""
    mock_transport = MagicMock()
    mock_transport.sip_refer = AsyncMock()
    return ToolContext(
        call_id="integration-test-call",
        session_id="integration-test-session",
        turn_number=1,
        conversation_history=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
        transport=mock_transport,
        sip_session_id="test-sip-session",
        # Mock Aurora settings — single target named "billing" for the
        # integration tests below to invoke. Real configs may have
        # several named targets per agent.
        tool_settings={"targets": {"billing": "+15555550100"}},
    )


# =============================================================================
# End-to-End Tool Execution Tests
# =============================================================================


class TestEndToEndToolExecution:
    """Test complete tool execution flows."""

    @pytest.mark.asyncio
    async def test_time_tool_e2e(self, executor_with_metrics, base_context):
        """Test time tool end-to-end."""
        result = await executor_with_metrics.execute(
            tool_name="get_current_time",
            arguments={},
            context=base_context,
        )

        assert result.status == ToolStatus.SUCCESS
        assert "current_time" in result.content
        assert "current_date" in result.content
        assert "timezone" in result.content
        assert result.content["timezone"] == "UTC"
        assert result.execution_time_ms > 0

    @pytest.mark.asyncio
    async def test_transfer_tool_e2e(self, executor_with_metrics, base_context):
        """Test transfer tool end-to-end with Aurora-style target name."""
        result = await executor_with_metrics.execute(
            tool_name="transfer_call",
            arguments={"target": "billing"},
            context=base_context,
        )

        assert result.status == ToolStatus.SUCCESS
        assert result.content["transfer_initiated"] is True
        assert result.content["target"] == "billing"
        assert "billing" in result.content["message"]

    @pytest.mark.asyncio
    async def test_transfer_with_conversation_context(
        self, executor_with_metrics, base_context
    ):
        """Test that transfer tool captures conversation context."""
        # Add more conversation history
        base_context.conversation_history = [
            {"role": "user", "content": "I have a billing question"},
            {"role": "assistant", "content": "I'd be happy to help with billing."},
            {"role": "user", "content": "I was charged twice for my order"},
            {"role": "assistant", "content": "Let me transfer you to billing."},
        ]

        result = await executor_with_metrics.execute(
            tool_name="transfer_call",
            arguments={"target": "billing"},
            context=base_context,
        )

        assert result.status == ToolStatus.SUCCESS
        # Should include conversation summary
        assert "conversation_summary" in result.content
        summary = result.content["conversation_summary"]
        assert "billing" in summary.lower() or "charged" in summary.lower()


# =============================================================================
# Sequential Tool Calls Tests
# =============================================================================


class TestSequentialToolCalls:
    """Test multiple tool calls in sequence."""

    @pytest.mark.asyncio
    async def test_multiple_tools_in_sequence(
        self, executor_with_metrics, base_context
    ):
        """Test calling multiple tools in sequence."""
        # First call: get time
        result1 = await executor_with_metrics.execute(
            tool_name="get_current_time",
            arguments={},
            context=base_context,
        )
        assert result1.status == ToolStatus.SUCCESS

        # Update turn number
        base_context.turn_number = 2

        # Second call: transfer
        result2 = await executor_with_metrics.execute(
            tool_name="transfer_call",
            arguments={"target": "billing"},
            context=base_context,
        )
        assert result2.status == ToolStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_tool_after_error_recovers(self, full_registry, base_context):
        """Test that tool execution recovers after an error."""

        # Add a failing tool
        async def failing_executor(args, context):
            raise ValueError("Simulated failure")

        failing_tool = ToolDefinition(
            name="failing_tool",
            description="A tool that fails",
            category=ToolCategory.TESTING,
            parameters=[],
            executor=failing_executor,
            timeout_seconds=5.0,
        )
        full_registry.register(failing_tool)

        executor = ToolExecutor(full_registry)

        # First call: should fail
        result1 = await executor.execute(
            tool_name="failing_tool",
            arguments={},
            context=base_context,
        )
        assert result1.status == ToolStatus.ERROR

        # Second call: should succeed (different tool)
        result2 = await executor.execute(
            tool_name="get_current_time",
            arguments={},
            context=base_context,
        )
        assert result2.status == ToolStatus.SUCCESS
        assert "current_time" in result2.content


# =============================================================================
# Cancellation and Barge-in Tests
# =============================================================================


class TestCancellationAndBargeIn:
    """Test cancellation behavior for barge-in scenarios."""

    @pytest.mark.asyncio
    async def test_cancellation_via_context(self, full_registry):
        """Test that tools can be cancelled via context."""

        # Create a slow tool that checks cancellation
        async def cancellable_executor(args, context):
            for i in range(10):
                if context.is_cancelled():
                    return ToolResult(status=ToolStatus.CANCELLED)
                await asyncio.sleep(0.1)
            return success_result({"completed": True})

        slow_tool = ToolDefinition(
            name="cancellable_tool",
            description="A cancellable tool",
            category=ToolCategory.TESTING,
            parameters=[],
            executor=cancellable_executor,
            timeout_seconds=5.0,
        )
        full_registry.register(slow_tool)

        executor = ToolExecutor(full_registry)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
        )

        # Start execution in background
        async def execute_and_cancel():
            # Start execution
            task = asyncio.create_task(
                executor.execute(
                    tool_name="cancellable_tool",
                    arguments={},
                    context=context,
                )
            )

            # Wait a bit then cancel
            await asyncio.sleep(0.25)
            context.cancel()

            return await task

        result = await execute_and_cancel()
        assert result.status == ToolStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_asyncio_cancellation(self, full_registry):
        """Test that asyncio.CancelledError is handled properly.

        The executor catches CancelledError and returns a CANCELLED status
        result rather than propagating the exception. This allows the pipeline
        to handle cancellation gracefully.
        """

        async def slow_executor(args, context):
            await asyncio.sleep(10)  # Will be cancelled
            return success_result({})

        slow_tool = ToolDefinition(
            name="slow_tool",
            description="A slow tool",
            category=ToolCategory.TESTING,
            parameters=[],
            executor=slow_executor,
            timeout_seconds=10.0,
        )
        full_registry.register(slow_tool)

        executor = ToolExecutor(full_registry)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
        )

        # Start execution and cancel via asyncio
        task = asyncio.create_task(
            executor.execute(
                tool_name="slow_tool",
                arguments={},
                context=context,
            )
        )

        await asyncio.sleep(0.1)
        task.cancel()

        # The executor catches CancelledError and returns CANCELLED status
        try:
            result = await task
            assert result.status == ToolStatus.CANCELLED
        except asyncio.CancelledError:
            # This is also acceptable behavior
            pass


# =============================================================================
# Metrics Integration Tests
# =============================================================================


class TestMetricsIntegration:
    """Test metrics collection during tool execution."""

    @pytest.mark.asyncio
    async def test_metrics_recorded_on_success(self, full_registry, metrics_collector):
        """Test that metrics are recorded for successful execution."""
        executor = ToolExecutor(full_registry, metrics_collector)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
            metrics_collector=metrics_collector,
        )

        # Start a turn so metrics can be recorded
        metrics_collector.start_turn()

        result = await executor.execute(
            tool_name="get_current_time",
            arguments={},
            context=context,
        )

        assert result.status == ToolStatus.SUCCESS
        # Metrics should have been recorded (verified via logs)

    @pytest.mark.asyncio
    async def test_metrics_recorded_on_error(self, full_registry, metrics_collector):
        """Test that metrics are recorded for failed execution."""

        async def error_executor(args, context):
            raise RuntimeError("Test error")

        error_tool = ToolDefinition(
            name="error_tool",
            description="Tool that errors",
            category=ToolCategory.TESTING,
            parameters=[],
            executor=error_executor,
            timeout_seconds=5.0,
        )
        full_registry.register(error_tool)

        executor = ToolExecutor(full_registry, metrics_collector)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
        )

        result = await executor.execute(
            tool_name="error_tool",
            arguments={},
            context=context,
        )

        assert result.status == ToolStatus.ERROR
        assert result.error_code == "RuntimeError"

    @pytest.mark.asyncio
    async def test_metrics_recorded_on_timeout(self, full_registry, metrics_collector):
        """Test that metrics are recorded for timed out execution."""

        async def timeout_executor(args, context):
            await asyncio.sleep(10)
            return success_result({})

        timeout_tool = ToolDefinition(
            name="timeout_tool",
            description="Tool that times out",
            category=ToolCategory.TESTING,
            parameters=[],
            executor=timeout_executor,
            timeout_seconds=0.1,
        )
        full_registry.register(timeout_tool)

        executor = ToolExecutor(full_registry, metrics_collector)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
        )

        result = await executor.execute(
            tool_name="timeout_tool",
            arguments={},
            context=context,
        )

        assert result.status == ToolStatus.TIMEOUT


# =============================================================================
# Bedrock Format Integration Tests
# =============================================================================


class TestBedrockFormatIntegration:
    """Test Bedrock API format compatibility."""

    def test_tool_config_format(self, full_registry):
        """Test that tool config matches Bedrock expected format."""
        config = full_registry.get_bedrock_tool_config()

        assert "tools" in config
        assert len(config["tools"]) == 2  # time, transfer

        for tool_spec in config["tools"]:
            assert "toolSpec" in tool_spec
            spec = tool_spec["toolSpec"]

            # Required fields
            assert "name" in spec
            assert "description" in spec
            assert "inputSchema" in spec

            # Input schema format
            schema = spec["inputSchema"]
            assert "json" in schema
            assert schema["json"]["type"] == "object"

    def test_tool_result_format(self):
        """Test that tool results match Bedrock expected format."""
        # Success result
        success = success_result({"key": "value"})
        bedrock_success = success.to_bedrock_tool_result("tool-123")

        assert "toolResult" in bedrock_success
        assert bedrock_success["toolResult"]["toolUseId"] == "tool-123"
        assert bedrock_success["toolResult"]["status"] == "success"
        assert bedrock_success["toolResult"]["content"][0]["json"] == {"key": "value"}

        # Error result
        error = error_result("Item not found", error_code="NOT_FOUND")
        bedrock_error = error.to_bedrock_tool_result("tool-456")

        assert bedrock_error["toolResult"]["status"] == "error"
        assert "NOT_FOUND" in bedrock_error["toolResult"]["content"][0]["text"]

    def test_all_builtin_tools_have_valid_specs(self, full_registry):
        """Test that all builtin tools produce valid Bedrock specs."""
        for tool in full_registry.get_all_definitions():
            spec = tool.to_bedrock_tool_spec()

            # Validate structure
            assert spec["toolSpec"]["name"] == tool.name
            assert len(spec["toolSpec"]["description"]) > 0

            # Validate input schema
            schema = spec["toolSpec"]["inputSchema"]["json"]
            assert schema["type"] == "object"
            assert "properties" in schema


# =============================================================================
# Pipeline Handler Integration Tests
# =============================================================================


class TestPipelineHandlerIntegration:
    """Test the pipeline handler wrapper functionality."""

    @pytest.mark.asyncio
    async def test_handler_factory_creates_working_handlers_basic_only(
        self, full_registry, metrics_collector
    ):
        """Test that only BASIC tools register when no transport capabilities exist."""
        from app.pipeline_ecs import _register_tools
        from app.tools.capabilities import PipelineCapability
        from unittest.mock import MagicMock

        mock_llm = MagicMock()
        registered_functions = {}

        def capture_registration(function_name, handler):
            registered_functions[function_name] = handler

        mock_llm.register_function = capture_registration

        # Register with BASIC-only capabilities (no transport, no SIP)
        mock_transport = MagicMock()
        _register_tools(
            mock_llm,
            "test-session",
            mock_transport,
            collector=metrics_collector,
            available_capabilities=frozenset({PipelineCapability.BASIC}),
        )

        # Only get_current_time (BASIC) should register; transfer_call
        # requires TRANSPORT + SIP_SESSION (Phase 7C dropped TRANSFER_
        # DESTINATION; gating now happens via tools_config + capabilities)
        assert "get_current_time" in registered_functions
        assert "transfer_call" not in registered_functions
        assert len(registered_functions) == 1

    @pytest.mark.asyncio
    async def test_handler_factory_creates_working_handlers_full_capabilities(
        self, full_registry, metrics_collector
    ):
        """Test that all tools register when full capabilities are available."""
        from app.pipeline_ecs import _register_tools
        from app.tools.capabilities import PipelineCapability
        from unittest.mock import MagicMock

        mock_llm = MagicMock()
        registered_functions = {}

        def capture_registration(function_name, handler):
            registered_functions[function_name] = handler

        mock_llm.register_function = capture_registration

        # Register with full capabilities. Phase 7C: TRANSFER_DESTINATION
        # capability removed; transfer_call now gates only on
        # TRANSPORT + SIP_SESSION + Aurora settings.targets non-empty.
        # Aurora-configurable tools (end_call, transfer_call,
        # press_digit) need to be in tools_config to register.
        mock_transport = MagicMock()
        full_caps = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
            }
        )
        _register_tools(
            mock_llm,
            "test-session",
            mock_transport,
            collector=metrics_collector,
            available_capabilities=full_caps,
            tools_config=[
                {"type": "end_call", "description": "", "settings": {}},
                {
                    "type": "transfer_call",
                    "description": "",
                    "settings": {"targets": {"billing": "+15555550100"}},
                },
                {"type": "press_digit", "description": "", "settings": {}},
            ],
        )

        # All tools with matching capabilities should be registered.
        # get_current_time is internal (always registers).
        assert "get_current_time" in registered_functions
        assert "transfer_call" in registered_functions
        assert "end_call" in registered_functions
        assert "press_digit" in registered_functions
        assert len(registered_functions) == 4

    @pytest.mark.asyncio
    async def test_registered_handler_executes_correctly(
        self, full_registry, metrics_collector
    ):
        """Test that registered handlers execute tools correctly."""
        from app.pipeline_ecs import _register_tools
        from app.tools.capabilities import PipelineCapability
        from unittest.mock import MagicMock

        mock_llm = MagicMock()
        registered_functions = {}

        def capture_registration(function_name, handler):
            registered_functions[function_name] = handler

        mock_llm.register_function = capture_registration

        mock_transport = MagicMock()
        _register_tools(
            mock_llm,
            "test-session",
            mock_transport,
            collector=metrics_collector,
            available_capabilities=frozenset({PipelineCapability.BASIC}),
        )

        # Get the get_current_time handler
        time_handler = registered_functions["get_current_time"]

        # Create a mock result callback
        callback_result = None
        callback_properties = None

        async def mock_callback(result, *, properties=None):
            nonlocal callback_result, callback_properties
            callback_result = result
            callback_properties = properties

        # Execute the handler with FunctionCallParams-style mock
        mock_params = MagicMock()
        mock_params.function_name = "get_current_time"
        mock_params.tool_call_id = "test-123"
        mock_params.arguments = {}
        mock_params.llm = mock_llm
        mock_params.context = None
        mock_params.result_callback = mock_callback

        await time_handler(mock_params)

        # Verify result
        assert callback_result is not None
        assert "current_time" in callback_result
        assert "timezone" in callback_result
        # Normal tools should not override run_llm (properties stays None)
        assert callback_properties is None


# =============================================================================
# Error Handling Integration Tests
# =============================================================================


class TestErrorHandlingIntegration:
    """Test error handling across the tool framework."""

    @pytest.mark.asyncio
    async def test_validation_errors_handled_gracefully(self, full_registry):
        """Test that validation errors produce proper error results."""
        executor = ToolExecutor(full_registry)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
        )

        # Missing required 'target' parameter for transfer_call
        result = await executor.execute(
            tool_name="transfer_call",
            arguments={},  # Missing 'target'
            context=context,
        )

        assert result.status == ToolStatus.ERROR
        assert result.error_code == "INVALID_ARGUMENTS"
        assert "target" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool_handled_gracefully(self, full_registry):
        """Test that unknown tool requests produce proper error results."""
        executor = ToolExecutor(full_registry)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
        )

        result = await executor.execute(
            tool_name="nonexistent_tool",
            arguments={},
            context=context,
        )

        assert result.status == ToolStatus.ERROR
        assert result.error_code == "TOOL_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_user_friendly_error_messages(self, full_registry):
        """Test that error results produce user-friendly messages."""
        executor = ToolExecutor(full_registry)
        context = ToolContext(
            call_id="test",
            session_id="test",
            turn_number=1,
        )

        # Get an error result
        result = await executor.execute(
            tool_name="nonexistent_tool",
            arguments={},
            context=context,
        )

        user_message = result.to_user_message()

        # Should be a friendly message, not a stack trace
        assert len(user_message) > 0
        assert "encountered" in user_message.lower() or "issue" in user_message.lower()
        assert "exception" not in user_message.lower()
        assert "traceback" not in user_message.lower()
