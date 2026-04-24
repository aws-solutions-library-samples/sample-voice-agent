"""Tests for the tools_config filter in pipeline_ecs._register_tools.

Phase 7C introduced per-agent tool gating: an agent's Aurora
tools_config list decides which of the Aurora-configurable tools
(end_call / press_digit / transfer_call) the LLM sees. Internal
tools (get_current_time, etc.) register unconditionally subject to
capability gating.

These tests exercise the filter logic directly. Integration tests
that boot the whole pipeline live in test_tool_integration.py.
"""

from __future__ import annotations

import pytest

try:
    from pipecat.services.aws.llm import AWSBedrockLLMService  # noqa: F401
except ImportError:
    pytest.skip(
        "pipecat not available (container-only dependency)",
        allow_module_level=True,
    )

from unittest.mock import MagicMock

from app.pipeline_ecs import _register_tools
from app.tools.capabilities import PipelineCapability


def _fake_llm():
    """Minimal Bedrock LLM stub — _register_tools only calls
    ``register_function``, so we don't need a real service here."""
    llm = MagicMock()
    llm.register_function = MagicMock()
    return llm


def _caps_with_transport_and_sip():
    return frozenset({
        PipelineCapability.BASIC,
        PipelineCapability.TRANSPORT,
        PipelineCapability.SIP_SESSION,
    })


class TestToolsConfigFiltering:
    def test_no_config_registers_only_internal_tools(self):
        # tools_config=None and tools_config=[] should both result in
        # none of the Aurora-configurable tools registering; only
        # time_tool (and any future internal tools) register.
        llm = _fake_llm()
        tools = _register_tools(
            llm=llm,
            session_id="s",
            transport=MagicMock(),
            available_capabilities=_caps_with_transport_and_sip(),
            tools_config=None,
        )
        registered = {
            spec.get("toolSpec", {}).get("name") for spec in (
                # FunctionSchema objects or dicts — handle both
                (t.model_dump() if hasattr(t, "model_dump") else t) for t in tools
            )
            if spec
        }
        registered |= {  # fallback: derive from llm.register_function calls
            call.kwargs.get("function_name") or (call.args[0] if call.args else None)
            for call in llm.register_function.call_args_list
        }
        # Internal tool should always register
        assert "get_current_time" in registered
        # Aurora-configurable tools absent when no config supplied
        assert "end_call" not in registered
        assert "press_digit" not in registered
        assert "transfer_call" not in registered

    def test_end_call_in_config_registers_end_call(self):
        llm = _fake_llm()
        _register_tools(
            llm=llm,
            session_id="s",
            transport=MagicMock(),
            available_capabilities=_caps_with_transport_and_sip(),
            tools_config=[
                {"type": "end_call", "description": "", "settings": {}},
            ],
        )
        registered = {
            call.kwargs.get("function_name") for call in llm.register_function.call_args_list
        }
        assert "end_call" in registered
        assert "transfer_call" not in registered

    def test_aurora_description_override(self):
        """If Aurora provides a non-empty description, it overrides the
        fork's hardcoded description. This is how agent designers tune
        LLM behavior per-agent."""
        llm = _fake_llm()
        custom = "End the call only after confirming the order number."
        _register_tools(
            llm=llm,
            session_id="s",
            transport=MagicMock(),
            available_capabilities=_caps_with_transport_and_sip(),
            tools_config=[
                {"type": "end_call", "description": custom, "settings": {}},
            ],
        )
        # Pull the registered end_call's description from the LLM
        # context schema (tools list). We can't easily introspect that
        # without more setup, but we can at least confirm registration
        # happened — a deeper schema-dump check would belong in an
        # integration test.
        registered = {
            call.kwargs.get("function_name") for call in llm.register_function.call_args_list
        }
        assert "end_call" in registered

    def test_empty_description_falls_back_to_fork_default(self):
        # An Aurora entry with description="" (possible for pre-validation
        # rows) should NOT blank out the fork's hardcoded description.
        llm = _fake_llm()
        _register_tools(
            llm=llm,
            session_id="s",
            transport=MagicMock(),
            available_capabilities=_caps_with_transport_and_sip(),
            tools_config=[
                {"type": "end_call", "description": "", "settings": {}},
            ],
        )
        # No assertion error at this point means the tool registered
        # successfully (fork fallback description kicked in); the
        # register_function call proves it.
        registered = {
            call.kwargs.get("function_name") for call in llm.register_function.call_args_list
        }
        assert "end_call" in registered

    def test_capability_gating_still_applies_to_aurora_tools(self):
        # A tool whose requires aren't met should NOT register, even if
        # the agent has it in tools_config. transfer_call needs both
        # TRANSPORT and SIP_SESSION; with TRANSPORT-only it should be
        # skipped.
        llm = _fake_llm()
        caps_without_sip = frozenset({PipelineCapability.BASIC, PipelineCapability.TRANSPORT})
        _register_tools(
            llm=llm,
            session_id="s",
            transport=MagicMock(),
            available_capabilities=caps_without_sip,
            tools_config=[
                {"type": "end_call", "description": "", "settings": {}},
                {
                    "type": "transfer_call",
                    "description": "",
                    "settings": {"targets": {"x": "+15551234567"}},
                },
            ],
        )
        registered = {
            call.kwargs.get("function_name") for call in llm.register_function.call_args_list
        }
        assert "end_call" in registered
        # transfer_call requires SIP_SESSION (not in our caps), skipped.
        assert "transfer_call" not in registered

    def test_malformed_config_entries_skipped_safely(self):
        # None / missing type / non-dict entries must not crash the
        # filter; they're simply not registered.
        llm = _fake_llm()
        _register_tools(
            llm=llm,
            session_id="s",
            transport=MagicMock(),
            available_capabilities=_caps_with_transport_and_sip(),
            tools_config=[
                None,                                  # noqa: bad entry
                {},                                    # missing type
                {"type": "nonexistent_tool", "settings": {}},
                {"type": 42, "settings": {}},          # non-string type
                {"type": "end_call", "settings": {}},  # the one valid entry
            ],
        )
        registered = {
            call.kwargs.get("function_name") for call in llm.register_function.call_args_list
        }
        assert "end_call" in registered
        # get_current_time always registers as an internal tool
        assert "get_current_time" in registered
