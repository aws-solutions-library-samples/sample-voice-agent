"""
Agent runtime-config loader for Phase 7A.

Fetches the per-call agent config from the Cosentus voice-api Lambda
(`GET /api/agents/:id/runtime-config`) at call start and parses it into
the Pydantic `AgentConfig` model the Fargate pipeline consumes. Shape
matches OG `voiceagent/core/config_loader.py::AgentConfig` exactly so
the Lambda's response goes through unchanged.

Caching
-------

No module-level cache. Each call site is expected to fetch once at call
start, hold the result in `PipelineConfig` for the session's lifetime,
and never refetch mid-call. This is intentional per the Phase 7A brief
("cache agent config per session") — a module-level cache would make it
harder to reason about config drift during a single call, and the load
itself is cheap (single Lambda invoke, ~200ms cold / ~50ms warm).

Fallback
--------

If the Lambda is unreachable (timeout, 5xx, invalid shape), we log
`agent_config_load_failed` at error level and return
`AgentConfig.fallback()` — a generic assistant with sane defaults —
rather than dropping the call. This is mandated by the brief: the
caller must finish, even badly, not hang up.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import aiohttp
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# ── Pydantic models ─────────────────────────────────────────────────────────
#
# These must stay byte-identical to OG's AgentConfig shape — the Lambda's
# buildRuntimeConfig() in index.mjs produces exactly this layout. If the
# Lambda's shape changes, update these and run the vitest suite on the
# Lambda side (`test/runtime-config.test.mjs`) to keep them synced.


class LLMConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 200
    temperature: float = 0.7
    enable_prompt_caching: bool = True


class TTSSettings(BaseModel):
    stability: Optional[float] = None
    similarity_boost: Optional[float] = None
    style: Optional[float] = None
    use_speaker_boost: Optional[bool] = None
    speed: Optional[float] = None


class TTSConfig(BaseModel):
    provider: str = "elevenlabs"
    voice_id: str = ""
    model: str = "eleven_turbo_v2_5"
    settings: TTSSettings = Field(default_factory=TTSSettings)


class STTConfig(BaseModel):
    provider: str = "deepgram"
    language: str = "en"
    keywords: list[str] = Field(default_factory=list)


class ToolConfig(BaseModel):
    type: str
    description: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class RecordingConfig(BaseModel):
    enabled: bool = True
    channels: int = 2


class PostCallField(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    format_examples: list[str] = Field(default_factory=list)
    choices: list[str] = Field(default_factory=list)


class PostCallConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    fields: list[PostCallField] = Field(default_factory=list)


class AgentConfigMeta(BaseModel):
    """Server-provided metadata for observability. Not consumed by pipeline logic."""
    agent_id: str = ""
    version: int = 0  # updated_at as unix millis


class AgentConfig(BaseModel):
    name: str = ""
    display_name: str = ""
    description: str = ""
    system_prompt: str = ""
    first_message: str = ""
    ivr_goal: str = ""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    tools: list[ToolConfig] = Field(default_factory=list)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    post_call_analyses: Optional[PostCallConfig] = None
    meta: AgentConfigMeta = Field(default_factory=AgentConfigMeta, alias="_meta")

    model_config = {"populate_by_name": True}

    @classmethod
    def fallback(cls, agent_name: str = "unknown") -> "AgentConfig":
        """
        Minimal safe config returned when the Lambda is unreachable.

        The system_prompt is intentionally generic — we prefer the caller
        hears a polite "I'm here to help" over a dropped call. The tools
        list is empty, meaning no transfer / hangup / DTMF; the caller can
        still converse and eventually hang up from their phone.
        """
        return cls(
            name=agent_name,
            display_name=agent_name,
            system_prompt=(
                "You are a helpful AI voice assistant for Cosentus. "
                "Be concise and conversational. Apologize if you can't help with "
                "something specific — we're still getting set up."
            ),
            first_message="Hi, this is Cosentus. How can I help you today?",
        )


# ── HTTP loader ─────────────────────────────────────────────────────────────


class AgentConfigLoadError(Exception):
    """Raised by load_agent_config when the caller wants the error instead of fallback."""


def _default_base_url() -> str:
    """
    Where to fetch runtime configs from. In prod ECS this is the API Gateway
    URL in front of the Lambda. For local dev / tests callers override via
    the base_url kwarg. Env var takes precedence over the hardcoded prod URL
    so a misconfigured dev environment can't accidentally hit prod.
    """
    return os.environ.get(
        "VOICE_API_BASE_URL",
        "https://api.cosentusaibackend.com",
    )


def _default_api_key() -> str:
    """
    X-API-Key for the Lambda. The Lambda's API Gateway is wired to require
    this header. Stored in Secrets Manager and loaded into the ECS task's
    env at startup (see service_main.py secrets_loader).
    """
    return os.environ.get("COSENTUS_API_KEY", "")


async def load_agent_config(
    agent_id_or_name: str,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_sec: float = 5.0,
    session: Optional[aiohttp.ClientSession] = None,
) -> AgentConfig:
    """
    Fetch one agent's runtime config from the voice-api Lambda.

    Returns AgentConfig.fallback() if the load fails — no exceptions propagate
    to the caller. The brief requires we never drop a call due to a config
    fetch failure.

    Structured log events emitted:
        agent_config_loaded       — success (always)
        agent_config_load_failed  — fallback path used (always on failure)

    Args:
        agent_id_or_name: agent UUID or name, passed verbatim as `:id`.
        base_url: optional override for the voice-api URL. Defaults to
            VOICE_API_BASE_URL env var or prod.
        api_key: optional X-API-Key override. Defaults to COSENTUS_API_KEY env var.
        timeout_sec: HTTP timeout. 5s generously accommodates cold Lambda.
        session: optional aiohttp.ClientSession for testing. If None, a
            fresh session is created per call — fine for once-per-call use.
    """
    resolved_url = (base_url or _default_base_url()).rstrip("/")
    resolved_key = api_key if api_key is not None else _default_api_key()
    url = f"{resolved_url}/api/agents/{agent_id_or_name}/runtime-config"
    headers = {"X-API-Key": resolved_key} if resolved_key else {}

    started = time.perf_counter()
    owned_session = session is None
    http = session or aiohttp.ClientSession()
    try:
        async with http.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status == 404:
                logger.error(
                    "agent_config_load_failed",
                    reason="not_found",
                    agent_id_or_name=agent_id_or_name,
                    status=404,
                    load_time_ms=(time.perf_counter() - started) * 1000,
                )
                return AgentConfig.fallback(agent_name=agent_id_or_name)

            if resp.status >= 400:
                body_preview = (await resp.text())[:200]
                logger.error(
                    "agent_config_load_failed",
                    reason="http_error",
                    agent_id_or_name=agent_id_or_name,
                    status=resp.status,
                    body_preview=body_preview,
                    load_time_ms=(time.perf_counter() - started) * 1000,
                )
                return AgentConfig.fallback(agent_name=agent_id_or_name)

            raw = await resp.json()

            try:
                config = AgentConfig.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 — any parse error → fallback
                logger.error(
                    "agent_config_load_failed",
                    reason="parse_error",
                    agent_id_or_name=agent_id_or_name,
                    error=str(exc),
                    load_time_ms=(time.perf_counter() - started) * 1000,
                )
                return AgentConfig.fallback(agent_name=agent_id_or_name)

            load_time_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "agent_config_loaded",
                agent_id=config.meta.agent_id or config.name,
                agent_name=config.name,
                version=config.meta.version,
                tools_count=len(config.tools),
                llm_model=config.llm.model,
                tts_voice_id=config.tts.voice_id,
                load_time_ms=load_time_ms,
            )
            return config

    except aiohttp.ClientError as exc:
        logger.error(
            "agent_config_load_failed",
            reason="client_error",
            agent_id_or_name=agent_id_or_name,
            error=str(exc),
            error_type=type(exc).__name__,
            load_time_ms=(time.perf_counter() - started) * 1000,
        )
        return AgentConfig.fallback(agent_name=agent_id_or_name)
    except TimeoutError:
        logger.error(
            "agent_config_load_failed",
            reason="timeout",
            agent_id_or_name=agent_id_or_name,
            timeout_sec=timeout_sec,
            load_time_ms=(time.perf_counter() - started) * 1000,
        )
        return AgentConfig.fallback(agent_name=agent_id_or_name)
    finally:
        if owned_session:
            await http.close()


# ── Bedrock model-ID resolver ──────────────────────────────────────────────
#
# The Lambda's runtime-config returns `llm.provider='anthropic'` and
# `llm.model='claude-sonnet-4-6'` (short form) because that's what the
# Aurora row stores (and what the OG voiceagent EC2 pipeline expects).
# The fork runs Claude through AWS Bedrock, which needs an inference
# profile ID like `us.anthropic.claude-sonnet-4-6-20251001-v1:0`.
#
# This adapter translates. Short forms that match the known-good prod
# mapping get the prod inference profile ID; anything already in full
# Bedrock form (contains `.`) passes through untouched; unknowns log a
# warning and pass through (Bedrock will reject with a clearer error).


# Map from Aurora's short model name → Bedrock inference profile ID.
# Add new entries here when the voice team adopts a new Claude version;
# keep the short names in sync with the Lambda's VALID_LLM_MODELS.
_SHORT_TO_BEDROCK: dict[str, str] = {
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
    "claude-sonnet-4-5": "us.anthropic.claude-sonnet-4-5-20250514-v1:0",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-sonnet-4-5-20250514": "us.anthropic.claude-sonnet-4-5-20250514-v1:0",
}


def resolve_bedrock_model_id(config_model: str) -> str:
    """
    Translate an agent's `llm.model` value into a Bedrock inference profile ID.

    Rules:
      1. Empty / None → fallback to the env LLM_MODEL_ID or Haiku-4.5.
      2. Looks like a Bedrock ID already (contains a dot) → passed through.
      3. In _SHORT_TO_BEDROCK lookup → substituted.
      4. Unknown string → logged warning, passed through as-is (lets Bedrock
         return a canonical error rather than us guessing).
    """
    if not config_model:
        return os.environ.get(
            "LLM_MODEL_ID",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        )
    if "." in config_model:
        return config_model
    mapped = _SHORT_TO_BEDROCK.get(config_model)
    if mapped:
        return mapped
    logger.warning(
        "bedrock_model_id_unknown_short_form",
        config_model=config_model,
        hint=(
            "Add an entry to _SHORT_TO_BEDROCK in agent_config.py if this is "
            "a valid Aurora llm_model value. Passing through as-is; Bedrock "
            "may reject."
        ),
    )
    return config_model
