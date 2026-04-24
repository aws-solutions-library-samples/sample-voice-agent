"""
Unit tests for app.services.agent_config — Phase 7A agent runtime-config
loader. Covers the HTTP contract with the voice-api Lambda, Pydantic
parsing, fallback behavior, and the Bedrock model-ID adapter.

Network is mocked via an injected aiohttp.ClientSession (the loader
accepts one via the `session=` kwarg for exactly this purpose).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from app.services.agent_config import (
    AgentConfig,
    AgentConfigMeta,
    LLMConfig,
    TTSConfig,
    TTSSettings,
    STTConfig,
    ToolConfig,
    RecordingConfig,
    PostCallConfig,
    PostCallField,
    load_agent_config,
    resolve_bedrock_model_id,
    _SHORT_TO_BEDROCK,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


def _runtime_config_shape(**overrides) -> dict:
    """Build a response body that matches the Lambda's buildRuntimeConfig."""
    body = {
        "name": "chris-claim-status",
        "display_name": "Chris — claim status",
        "description": "",
        "system_prompt": "You are Chris, a billing specialist.",
        "first_message": "Hi, this is Chris.",
        "ivr_goal": "",
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "max_tokens": 170,
            "temperature": 0.15,
            "enable_prompt_caching": False,
        },
        "tts": {
            "provider": "elevenlabs",
            "voice_id": "ZoiZ8fuDWInAcwPXaVeq",
            "model": "eleven_flash_v2_5",
            "settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.3,
                "use_speaker_boost": False,
                "speed": 1.0,
            },
        },
        "stt": {"provider": "deepgram", "language": "en", "keywords": []},
        "tools": [
            {"type": "end_call", "description": "hang up", "settings": {}},
            {"type": "press_digit", "description": "", "settings": {}},
        ],
        "recording": {"enabled": True, "channels": 2},
        "post_call_analyses": {
            "model": "claude-haiku-4-5-20251001",
            "fields": [
                {
                    "name": "disposition",
                    "type": "selector",
                    "description": "",
                    "format_examples": [],
                    "choices": ["paid", "promise", "refuse"],
                }
            ],
        },
        "_meta": {
            "agent_id": "576b22a4-42ad-4ac1-8a2b-7067fb5c5cd4",
            "version": 1776901839407,
        },
    }
    body.update(overrides)
    return body


class _FakeResponse:
    """Mimics aiohttp ClientResponse for `async with` + await patterns."""

    def __init__(self, *, status: int, body: dict | str):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body if isinstance(self._body, dict) else json.loads(self._body)

    async def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)


class _FakeSession:
    """Mimics aiohttp.ClientSession. Records calls for assertions."""

    def __init__(self, response: _FakeResponse | None = None, side_effect: Exception | None = None):
        self.response = response
        self.side_effect = side_effect
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.side_effect:
            raise self.side_effect
        return self.response

    async def close(self):
        self.closed = True


# ── AgentConfig parse tests ────────────────────────────────────────────────


class TestAgentConfigParse:
    def test_parses_full_chris_shape_from_lambda(self):
        cfg = AgentConfig.model_validate(_runtime_config_shape())
        assert cfg.name == "chris-claim-status"
        assert cfg.llm.model == "claude-sonnet-4-6"
        assert cfg.llm.temperature == 0.15
        assert cfg.tts.voice_id == "ZoiZ8fuDWInAcwPXaVeq"
        assert cfg.tts.settings.stability == 0.5
        assert cfg.tts.settings.use_speaker_boost is False
        assert len(cfg.tools) == 2
        assert cfg.tools[0].type == "end_call"
        assert cfg.recording.enabled is True
        assert cfg.post_call_analyses is not None
        assert cfg.meta.agent_id == "576b22a4-42ad-4ac1-8a2b-7067fb5c5cd4"

    def test_meta_populated_from_underscore_key(self):
        """Pydantic must read `_meta` (Lambda's key) via populate_by_name."""
        cfg = AgentConfig.model_validate(_runtime_config_shape())
        assert cfg.meta.version == 1776901839407

    def test_accepts_null_post_call_analyses(self):
        cfg = AgentConfig.model_validate(_runtime_config_shape(post_call_analyses=None))
        assert cfg.post_call_analyses is None

    def test_use_speaker_boost_none_preserved_distinct_from_false(self):
        cfg = AgentConfig.model_validate(
            _runtime_config_shape(
                tts={
                    "provider": "elevenlabs",
                    "voice_id": "x",
                    "model": "m",
                    "settings": {"use_speaker_boost": None},
                }
            )
        )
        assert cfg.tts.settings.use_speaker_boost is None

    def test_fallback_is_generic_but_valid(self):
        fb = AgentConfig.fallback(agent_name="chris-claim-status")
        assert fb.name == "chris-claim-status"
        # First message must exist so the caller hears SOMETHING
        assert len(fb.first_message) > 0
        # System prompt should apologize for the degraded state
        assert "cosentus" in fb.system_prompt.lower() or "assistant" in fb.system_prompt.lower()
        # No tools by default — unknown which ones would work
        assert fb.tools == []


# ── load_agent_config tests ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLoadAgentConfig:
    async def test_happy_path_returns_parsed_config(self):
        session = _FakeSession(
            response=_FakeResponse(status=200, body=_runtime_config_shape())
        )
        cfg = await load_agent_config(
            "chris-claim-status",
            base_url="http://example.test",
            api_key="test-key",
            session=session,
        )
        assert cfg.name == "chris-claim-status"
        assert cfg.llm.model == "claude-sonnet-4-6"
        assert len(session.calls) == 1
        url, kwargs = session.calls[0]
        assert url == "http://example.test/api/agents/chris-claim-status/runtime-config"
        assert kwargs["headers"]["X-API-Key"] == "test-key"

    async def test_uuid_is_passed_through_verbatim_in_path(self):
        session = _FakeSession(
            response=_FakeResponse(status=200, body=_runtime_config_shape())
        )
        uuid = "576b22a4-42ad-4ac1-8a2b-7067fb5c5cd4"
        await load_agent_config(
            uuid, base_url="http://x", api_key="k", session=session
        )
        assert session.calls[0][0].endswith(f"/api/agents/{uuid}/runtime-config")

    async def test_404_returns_fallback(self):
        session = _FakeSession(response=_FakeResponse(status=404, body={"detail": "Agent not found"}))
        cfg = await load_agent_config(
            "ghost-agent", base_url="http://x", api_key="k", session=session
        )
        # Fallback sentinel: meta.agent_id empty string
        assert cfg.meta.agent_id == ""
        assert cfg.name == "ghost-agent"
        assert "cosentus" in cfg.system_prompt.lower() or "assistant" in cfg.system_prompt.lower()

    async def test_500_returns_fallback(self):
        session = _FakeSession(response=_FakeResponse(status=500, body="server error"))
        cfg = await load_agent_config(
            "chris-claim-status", base_url="http://x", api_key="k", session=session
        )
        assert cfg.meta.agent_id == ""
        # Falls back using the requested name so logs correlate
        assert cfg.name == "chris-claim-status"

    async def test_malformed_json_returns_fallback(self):
        """Missing required fields → Pydantic ValidationError → fallback."""
        session = _FakeSession(
            response=_FakeResponse(
                status=200,
                body={"llm": "this should be an object, not a string"},
            )
        )
        cfg = await load_agent_config(
            "chris-claim-status", base_url="http://x", api_key="k", session=session
        )
        assert cfg.meta.agent_id == ""

    async def test_client_error_returns_fallback(self):
        err = aiohttp.ClientConnectionError("refused")
        session = _FakeSession(side_effect=err)
        cfg = await load_agent_config(
            "chris-claim-status", base_url="http://x", api_key="k", session=session
        )
        assert cfg.meta.agent_id == ""

    async def test_omits_api_key_header_when_not_provided(self):
        session = _FakeSession(
            response=_FakeResponse(status=200, body=_runtime_config_shape())
        )
        await load_agent_config(
            "chris-claim-status",
            base_url="http://x",
            api_key="",
            session=session,
        )
        # Empty api_key → X-API-Key header should NOT be set (Lambda behind
        # API Gateway may allow unauthenticated dev invocation)
        _, kwargs = session.calls[0]
        assert "X-API-Key" not in kwargs["headers"]

    async def test_base_url_trailing_slash_stripped(self):
        session = _FakeSession(
            response=_FakeResponse(status=200, body=_runtime_config_shape())
        )
        await load_agent_config(
            "chris-claim-status",
            base_url="http://example.test/",  # trailing slash
            api_key="k",
            session=session,
        )
        # No double-slash in the path
        assert "//api/agents" not in session.calls[0][0]
        assert session.calls[0][0] == "http://example.test/api/agents/chris-claim-status/runtime-config"


# ── resolve_bedrock_model_id tests ─────────────────────────────────────────


class TestResolveBedrockModelId:
    def test_short_form_maps_to_inference_profile(self):
        assert resolve_bedrock_model_id("claude-sonnet-4-6") == _SHORT_TO_BEDROCK["claude-sonnet-4-6"]
        assert resolve_bedrock_model_id("claude-haiku-4-5") == _SHORT_TO_BEDROCK["claude-haiku-4-5"]

    def test_bedrock_form_passes_through(self):
        # Already looks like a Bedrock ID (contains dots) — don't touch
        bedrock_id = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"
        assert resolve_bedrock_model_id(bedrock_id) == bedrock_id

    def test_empty_falls_back_to_env_default(self, monkeypatch):
        monkeypatch.delenv("LLM_MODEL_ID", raising=False)
        # Default fallback is Haiku-4.5
        assert resolve_bedrock_model_id("") == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert resolve_bedrock_model_id(None) == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_empty_respects_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_ID", "custom.profile.id:v1")
        assert resolve_bedrock_model_id("") == "custom.profile.id:v1"

    def test_unknown_short_form_passes_through_with_warning(self):
        # Passes through verbatim so Bedrock can return the canonical error
        assert resolve_bedrock_model_id("claude-next-gen-mystery") == "claude-next-gen-mystery"
