"""Tests for the pipeline capability detection and tool filtering system.

These tests verify:
- PipelineCapability enum completeness
- detect_capabilities() probes the runtime environment correctly
- Tool catalog filtering based on capabilities
- SSM disabled-tools override behavior
- ToolDefinition.requires field defaults and behavior
- Integration with _register_tools() in pipeline_ecs
"""

import os
import pytest
from unittest.mock import MagicMock, patch

try:
    from app.tools.capabilities import PipelineCapability, detect_capabilities
    from app.tools.builtin.catalog import ALL_LOCAL_TOOLS
    from app.tools.builtin import time_tool, transfer_call_tool
    from app.observability import MetricsCollector
except ImportError:
    pytest.skip(
        "Container-only dependencies not available (structlog/pipecat)",
        allow_module_level=True,
    )

from app.tools.schema import ToolDefinition, ToolCategory, ToolParameter
from app.tools import ToolRegistry, ToolExecutor, success_result


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def metrics_collector():
    """Create a metrics collector for testing."""
    return MetricsCollector(
        call_id="cap-test-call",
        session_id="cap-test-session",
        environment="test",
    )


@pytest.fixture
def mock_transport():
    """Create a mock DailyTransport."""
    transport = MagicMock()
    # Standard transport -- no special methods
    # Remove any auto-created attributes so hasattr checks are accurate
    if hasattr(transport, "collect_dtmf"):
        del transport.collect_dtmf
    if hasattr(transport, "pause_recording"):
        del transport.pause_recording
    spec_attrs = [attr for attr in dir(transport) if not attr.startswith("_")]
    return transport


@pytest.fixture
def mock_transport_with_dtmf():
    """Transport with DTMF collection support."""
    transport = MagicMock()
    transport.collect_dtmf = MagicMock()
    if hasattr(transport, "pause_recording"):
        del transport.pause_recording
    return transport


@pytest.fixture
def mock_transport_with_recording():
    """Transport with recording control support."""
    transport = MagicMock()
    transport.pause_recording = MagicMock()
    if hasattr(transport, "collect_dtmf"):
        del transport.collect_dtmf
    return transport


@pytest.fixture
def mock_transport_full():
    """Transport with all optional capabilities."""
    transport = MagicMock()
    transport.collect_dtmf = MagicMock()
    transport.pause_recording = MagicMock()
    return transport


@pytest.fixture
def sip_session_tracker():
    """SIP session tracker dict."""
    return {"session_id": "test-sip-session-123"}


# =============================================================================
# PipelineCapability Enum Tests
# =============================================================================


class TestPipelineCapabilityEnum:
    """Test the PipelineCapability enum definition."""

    def test_has_all_expected_members(self):
        """Enum should have all declared capability members."""
        # TRANSFER_DESTINATION was removed in Phase 7C when transfer_call
        # moved from env-var config to Aurora per-agent settings.
        expected = {
            "BASIC",
            "TRANSPORT",
            "SIP_SESSION",
            "DTMF_COLLECTION",
            "RECORDING_CONTROL",
        }
        actual = {member.name for member in PipelineCapability}
        assert actual == expected

    def test_enum_values_are_strings(self):
        """All enum values should be lowercase string identifiers."""
        for member in PipelineCapability:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()

    def test_basic_is_always_the_minimal_capability(self):
        """BASIC should be the foundation capability."""
        assert PipelineCapability.BASIC.value == "basic"

    def test_capabilities_are_hashable_for_frozenset(self):
        """Capabilities must be usable in frozensets for tool requirements."""
        caps = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
            }
        )
        assert len(caps) == 2
        assert PipelineCapability.BASIC in caps

    def test_enum_count(self):
        """Guard against accidentally removing capabilities."""
        assert len(PipelineCapability) == 5


# =============================================================================
# detect_capabilities() Tests
# =============================================================================


class TestDetectCapabilities:
    """Test runtime capability detection."""

    def test_no_transport_returns_basic_only(self):
        """With no transport, only BASIC should be detected."""
        caps = detect_capabilities(transport=None)
        assert caps == frozenset({PipelineCapability.BASIC})

    def test_transport_present_adds_transport_capability(self, mock_transport):
        """A transport object enables the TRANSPORT capability."""
        caps = detect_capabilities(transport=mock_transport)
        assert PipelineCapability.TRANSPORT in caps
        assert PipelineCapability.BASIC in caps

    def test_transport_without_sip_tracker_no_sip_session(self, mock_transport):
        """Without a SIP tracker, SIP_SESSION is not detected."""
        caps = detect_capabilities(
            transport=mock_transport,
            sip_session_tracker=None,
        )
        assert PipelineCapability.SIP_SESSION not in caps

    def test_transport_with_sip_tracker_adds_sip_session(
        self, mock_transport, sip_session_tracker
    ):
        """SIP tracker presence enables SIP_SESSION capability."""
        caps = detect_capabilities(
            transport=mock_transport,
            sip_session_tracker=sip_session_tracker,
        )
        assert PipelineCapability.SIP_SESSION in caps

    def test_sip_tracker_without_transport_no_sip_session(self, sip_session_tracker):
        """SIP tracker alone (no transport) should NOT enable SIP_SESSION."""
        caps = detect_capabilities(
            transport=None,
            sip_session_tracker=sip_session_tracker,
        )
        assert PipelineCapability.SIP_SESSION not in caps
        assert caps == frozenset({PipelineCapability.BASIC})

    def test_transport_with_dtmf_support(self, mock_transport_with_dtmf):
        """Transport with collect_dtmf method enables DTMF_COLLECTION."""
        caps = detect_capabilities(transport=mock_transport_with_dtmf)
        assert PipelineCapability.DTMF_COLLECTION in caps

    def test_transport_without_dtmf_no_dtmf_capability(self, mock_transport):
        """Standard transport without collect_dtmf does not enable DTMF."""
        # Ensure the mock does NOT have collect_dtmf
        mock_transport = MagicMock(spec=[])
        caps = detect_capabilities(transport=mock_transport)
        assert PipelineCapability.DTMF_COLLECTION not in caps

    def test_transport_with_recording_control(self, mock_transport_with_recording):
        """Transport with pause_recording enables RECORDING_CONTROL."""
        caps = detect_capabilities(transport=mock_transport_with_recording)
        assert PipelineCapability.RECORDING_CONTROL in caps

    def test_transport_without_recording_no_capability(self):
        """Standard transport without pause_recording does not enable RECORDING_CONTROL."""
        transport = MagicMock(spec=[])
        caps = detect_capabilities(transport=transport)
        assert PipelineCapability.RECORDING_CONTROL not in caps

    def test_full_capability_detection(
        self, mock_transport_full, sip_session_tracker
    ):
        """All capabilities detected with full environment.

        Note: pre-Phase-7C this also included TRANSFER_DESTINATION
        gated on an env var. That capability was removed when
        transfer_call moved to Aurora per-agent settings.
        """
        caps = detect_capabilities(
            transport=mock_transport_full,
            sip_session_tracker=sip_session_tracker,
        )
        expected = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
                PipelineCapability.DTMF_COLLECTION,
                PipelineCapability.RECORDING_CONTROL,
            }
        )
        assert caps == expected

    def test_returns_frozenset(self):
        """Result should be immutable (frozenset)."""
        caps = detect_capabilities(transport=None)
        assert isinstance(caps, frozenset)

    def test_config_parameter_accepted(self):
        """Config parameter should be accepted without error (reserved for future)."""
        caps = detect_capabilities(transport=None, config={"some": "config"})
        assert PipelineCapability.BASIC in caps


# =============================================================================
# ToolDefinition.requires Field Tests
# =============================================================================


class TestToolDefinitionRequires:
    """Test the requires field on ToolDefinition."""

    def test_default_requires_is_empty_frozenset(self):
        """ToolDefinition.requires should default to empty frozenset."""
        tool = ToolDefinition(
            name="test_tool",
            description="A test tool",
            category=ToolCategory.SYSTEM,
            parameters=[],
            executor=lambda args, ctx: success_result({}),
        )
        assert tool.requires == frozenset()

    def test_requires_can_be_set_to_single_capability(self):
        """Tool can require a single capability."""
        tool = ToolDefinition(
            name="test_tool",
            description="A test tool",
            category=ToolCategory.SYSTEM,
            parameters=[],
            executor=lambda args, ctx: success_result({}),
            requires=frozenset({PipelineCapability.BASIC}),
        )
        assert tool.requires == frozenset({PipelineCapability.BASIC})

    def test_requires_can_be_set_to_multiple_capabilities(self):
        """Tool can require multiple capabilities."""
        reqs = frozenset(
            {
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
            }
        )
        tool = ToolDefinition(
            name="test_tool",
            description="A test tool",
            category=ToolCategory.SYSTEM,
            parameters=[],
            executor=lambda args, ctx: success_result({}),
            requires=reqs,
        )
        assert tool.requires == reqs

    def test_time_tool_requires_basic(self):
        """time_tool should require only BASIC."""
        assert time_tool.requires == frozenset({PipelineCapability.BASIC})

    def test_transfer_tool_requires_transport_sip_destination(self):
        """transfer_call_tool should require TRANSPORT, SIP_SESSION (TRANSFER_DESTINATION removed in 7C)."""
        expected = frozenset(
            {
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
            }
        )
        assert transfer_call_tool.requires == expected


# =============================================================================
# Catalog Filtering Tests
# =============================================================================


class TestCatalogFiltering:
    """Test filtering the tool catalog by capabilities."""

    def test_catalog_has_expected_tools(self):
        """ALL_LOCAL_TOOLS should contain time and transfer tools."""
        tool_names = {tool.name for tool in ALL_LOCAL_TOOLS}
        assert "get_current_time" in tool_names
        assert "transfer_call" in tool_names

    def test_catalog_has_at_least_two_tools(self):
        """Catalog must have at least the two core tools."""
        assert len(ALL_LOCAL_TOOLS) >= 2

    def test_basic_only_filters_to_time_tool(self):
        """With BASIC-only capabilities, only time tool should pass."""
        basic_caps = frozenset({PipelineCapability.BASIC})
        passed = [
            tool
            for tool in ALL_LOCAL_TOOLS
            if (tool.requires or frozenset({PipelineCapability.BASIC})) <= basic_caps
        ]
        names = {t.name for t in passed}
        assert "get_current_time" in names
        assert "transfer_call" not in names

    def test_full_capabilities_include_all_tools(self, monkeypatch):
        """With all capabilities, all catalog tools should pass."""
        full_caps = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
                PipelineCapability.DTMF_COLLECTION,
                PipelineCapability.RECORDING_CONTROL,
            }
        )
        passed = [
            tool
            for tool in ALL_LOCAL_TOOLS
            if (tool.requires or frozenset({PipelineCapability.BASIC})) <= full_caps
        ]
        assert len(passed) == len(ALL_LOCAL_TOOLS)

    def test_partial_capabilities_exclude_transfer(self):
        """Transport without SIP_SESSION should exclude transfer tool."""
        partial_caps = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
                # Missing SIP_SESSION and TRANSFER_DESTINATION
            }
        )
        passed = [
            tool
            for tool in ALL_LOCAL_TOOLS
            if (tool.requires or frozenset({PipelineCapability.BASIC})) <= partial_caps
        ]
        names = {t.name for t in passed}
        assert "get_current_time" in names
        assert "transfer_call" not in names

    def test_disabled_tools_override_filters_even_when_capable(self):
        """disabled_tools config should skip tools even when capabilities are met."""
        full_caps = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
            }
        )
        disabled = {"transfer_call"}

        passed = [
            tool
            for tool in ALL_LOCAL_TOOLS
            if tool.name not in disabled
            and (tool.requires or frozenset({PipelineCapability.BASIC})) <= full_caps
        ]
        names = {t.name for t in passed}
        assert "get_current_time" in names
        assert "transfer_call" not in names

    def test_disabled_tools_can_disable_basic_tool(self):
        """Even BASIC tools can be disabled via config."""
        basic_caps = frozenset({PipelineCapability.BASIC})
        disabled = {"get_current_time"}

        passed = [
            tool
            for tool in ALL_LOCAL_TOOLS
            if tool.name not in disabled
            and (tool.requires or frozenset({PipelineCapability.BASIC})) <= basic_caps
        ]
        names = {t.name for t in passed}
        assert "get_current_time" not in names

    def test_empty_disabled_tools_disables_nothing(self):
        """Empty disabled set should not filter any tools."""
        full_caps = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
            }
        )
        disabled: set = set()

        passed = [
            tool
            for tool in ALL_LOCAL_TOOLS
            if tool.name not in disabled
            and (tool.requires or frozenset({PipelineCapability.BASIC})) <= full_caps
        ]
        assert len(passed) == len(ALL_LOCAL_TOOLS)


# =============================================================================
# _register_tools Integration Tests
# =============================================================================


class TestRegisterToolsIntegration:
    """Integration tests for _register_tools with capability filtering."""

    @pytest.fixture(autouse=True)
    def set_transfer_destination(self, monkeypatch):
        """Phase 7C: TRANSFER_DESTINATION removed; this fixture is now a no-op kept for fixture-name backcompat."""
        # (intentionally empty — env var no longer drives transfer_call gating)

    @pytest.mark.asyncio
    async def test_basic_capabilities_registers_only_time(self, metrics_collector):
        """With BASIC-only, only get_current_time registers."""
        from app.pipeline_ecs import _register_tools

        mock_llm = MagicMock()
        registered = {}

        def capture(function_name, handler):
            registered[function_name] = handler

        mock_llm.register_function = capture

        mock_transport = MagicMock()
        bedrock_tools = _register_tools(
            mock_llm,
            "test-session",
            mock_transport,
            collector=metrics_collector,
            available_capabilities=frozenset({PipelineCapability.BASIC}),
        )

        assert "get_current_time" in registered
        assert "transfer_call" not in registered
        assert len(registered) == 1
        # Bedrock tools list should also have 1 entry
        assert len(bedrock_tools) == 1

    @pytest.mark.asyncio
    async def test_full_capabilities_registers_all(self, metrics_collector):
        """With full capabilities, both time and transfer tools register."""
        from app.pipeline_ecs import _register_tools

        mock_llm = MagicMock()
        registered = {}

        def capture(function_name, handler):
            registered[function_name] = handler

        mock_llm.register_function = capture

        mock_transport = MagicMock()
        full_caps = frozenset(
            {
                PipelineCapability.BASIC,
                PipelineCapability.TRANSPORT,
                PipelineCapability.SIP_SESSION,
            }
        )
        bedrock_tools = _register_tools(
            mock_llm,
            "test-session",
            mock_transport,
            collector=metrics_collector,
            available_capabilities=full_caps,
        )

        assert "get_current_time" in registered
        assert "transfer_call" in registered
        assert "hangup_call" in registered
        assert len(registered) == 3
        assert len(bedrock_tools) == 3

    @pytest.mark.asyncio
    async def test_none_capabilities_defaults_to_basic(self, metrics_collector):
        """When available_capabilities is None, defaults to {BASIC}."""
        from app.pipeline_ecs import _register_tools

        mock_llm = MagicMock()
        registered = {}

        def capture(function_name, handler):
            registered[function_name] = handler

        mock_llm.register_function = capture

        mock_transport = MagicMock()
        _register_tools(
            mock_llm,
            "test-session",
            mock_transport,
            collector=metrics_collector,
            available_capabilities=None,
        )

        # Should default to BASIC, registering only time tool
        assert "get_current_time" in registered
        assert "transfer_call" not in registered

    @pytest.mark.asyncio
    async def test_returns_function_schema_tool_specs(self, metrics_collector):
        """Returned tools should be FunctionSchema objects for ToolsSchema."""
        from app.pipeline_ecs import _register_tools
        from pipecat.adapters.schemas.function_schema import FunctionSchema

        mock_llm = MagicMock()
        mock_llm.register_function = MagicMock()

        mock_transport = MagicMock()
        function_schemas = _register_tools(
            mock_llm,
            "test-session",
            mock_transport,
            collector=metrics_collector,
            available_capabilities=frozenset({PipelineCapability.BASIC}),
        )

        assert len(function_schemas) >= 1
        for schema in function_schemas:
            assert isinstance(schema, FunctionSchema)
            assert schema.name
            assert schema.description

    @pytest.mark.asyncio
    async def test_disabled_tools_ssm_override(self, metrics_collector, monkeypatch):
        """Tools in disabled_tools config should be skipped."""
        from app.pipeline_ecs import _register_tools

        mock_llm = MagicMock()
        registered = {}

        def capture(function_name, handler):
            registered[function_name] = handler

        mock_llm.register_function = capture

        # Mock _get_config to return disabled_tools
        mock_config = MagicMock()
        mock_config.features.disabled_tools = "get_current_time"

        mock_transport = MagicMock()
        with patch("app.pipeline_ecs._get_config", return_value=mock_config):
            _register_tools(
                mock_llm,
                "test-session",
                mock_transport,
                collector=metrics_collector,
                available_capabilities=frozenset({PipelineCapability.BASIC}),
            )

        # get_current_time is disabled, and transfer needs more caps
        assert "get_current_time" not in registered
        assert len(registered) == 0
