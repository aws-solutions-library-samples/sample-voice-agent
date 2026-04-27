"""Tests for app/services/post_call.py — Phase 7E PR 2.

Covers:
- Format helpers (transcript rendering, field instructions, prompt
  byte-parity with OG)
- Skip paths (no fields, empty transcript)
- Happy path: Bedrock Converse called with right shape, returns
  parsed JSON
- Markdown-fence stripping (model wrapping despite prompt rule)
- Selector validation (invalid choice → "invalid: <value>" prefix)
- Fallback model on retryable Bedrock error (ValidationException,
  AccessDeniedException, ResourceNotFoundException)
- Non-retryable error → fail without fallback
- Malformed JSON / non-dict / empty response → {"_error": ...}
- Never raises (caller's finally block must complete)
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Stub agent_config import so tests don't need pipecat at collection
try:
    from app.services.agent_config import PostCallConfig, PostCallField
except ImportError:
    pytest.skip(
        "pipecat / pydantic stubs missing (container-only)",
        allow_module_level=True,
    )

from app.services import post_call
from app.services.post_call import (
    _build_field_instructions,
    _build_prompt,
    _format_transcript,
    run_post_call_analyses,
)


# =============================================================================
# Format helpers — byte-parity with OG core/post_call.py
# =============================================================================


class TestFormatTranscript:
    def test_appends_in_order_with_labels(self):
        turns = [
            {"speaker": "user", "content": "Hello"},
            {"speaker": "assistant", "content": "Hi there"},
            {"speaker": "user", "content": "Goodbye"},
        ]
        out = _format_transcript(turns)
        assert "Caller: Hello" in out
        assert "Agent: Hi there" in out
        assert "Caller: Goodbye" in out
        # Order preserved
        assert out.index("Hello") < out.index("Hi there") < out.index("Goodbye")

    def test_supports_og_role_field_too(self):
        # Tolerate OG shape ({role, content}) so a future caller using
        # different transcript source still works.
        turns = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
        ]
        out = _format_transcript(turns)
        assert "Caller: x" in out
        assert "Agent: y" in out

    def test_handles_list_content(self):
        # OpenAI-style content blocks {text: "..."}.
        turns = [{"role": "user", "content": [{"text": "abc"}, {"text": "def"}]}]
        out = _format_transcript(turns)
        assert "Caller: abc def" in out


class TestBuildFieldInstructions:
    def test_text_field(self):
        f = PostCallField(name="summary", type="text", description="brief notes")
        out = _build_field_instructions([f])
        assert "1. summary (text): brief notes" in out

    def test_text_field_with_format_example(self):
        f = PostCallField(
            name="claim_id",
            type="text",
            description="claim number",
            format_examples=["12345-ABC"],
        )
        out = _build_field_instructions([f])
        assert 'Example format: "12345-ABC"' in out

    def test_selector_field_lists_choices(self):
        f = PostCallField(
            name="outcome",
            type="selector",
            description="result",
            choices=["resolved", "unresolved", "callback"],
        )
        out = _build_field_instructions([f])
        assert "select ONE of: resolved, unresolved, callback" in out


class TestBuildPrompt:
    def test_includes_transcript_fields_and_case_data(self):
        prompt = _build_prompt(
            transcript_text="Caller: hi\nAgent: hello",
            field_instructions="1. summary (text): brief notes",
            case_data={"claim_id": "ABC-123"},
        )
        assert "Caller: hi" in prompt
        assert "Agent: hello" in prompt
        assert "summary" in prompt
        assert "ABC-123" in prompt
        # OG byte-parity rules
        assert "Output JSON only" in prompt
        assert "no markdown" in prompt

    def test_no_case_data_says_no_case_data_provided(self):
        prompt = _build_prompt("transcript", "fields", {})
        assert "No case data provided." in prompt


# =============================================================================
# Skip paths
# =============================================================================


class TestSkipPaths:
    @pytest.mark.asyncio
    async def test_no_pca_config_returns_empty(self):
        result = await run_post_call_analyses(
            pca_config=None,
            transcript=[{"speaker": "user", "content": "hi"}],
            case_data={},
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_pca_with_zero_fields_returns_empty(self):
        cfg = PostCallConfig(model="claude-sonnet-4-6", fields=[])
        result = await run_post_call_analyses(
            pca_config=cfg,
            transcript=[{"speaker": "user", "content": "hi"}],
            case_data={},
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_transcript_returns_empty(self):
        cfg = PostCallConfig(
            model="claude-sonnet-4-6",
            fields=[PostCallField(name="x", type="text", description="y")],
        )
        result = await run_post_call_analyses(
            pca_config=cfg, transcript=[], case_data={}
        )
        assert result == {}


# =============================================================================
# Happy path
# =============================================================================


def _bedrock_response(text: str) -> dict:
    """Build the Bedrock Converse-API response shape."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        }
    }


def _config_one_text_field() -> "PostCallConfig":
    return PostCallConfig(
        model="claude-sonnet-4-6",
        fields=[
            PostCallField(
                name="summary", type="text", description="brief notes"
            )
        ],
    )


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_bedrock_called_with_correct_shape(self):
        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(
                return_value=_bedrock_response('{"summary": "Resolved on call"}')
            )
            result = await run_post_call_analyses(
                pca_config=_config_one_text_field(),
                transcript=[
                    {"speaker": "user", "content": "I have a question"},
                    {"speaker": "assistant", "content": "Sure, what is it?"},
                ],
                case_data={"claim_id": "X"},
            )
        assert result == {"summary": "Resolved on call"}

        # Verify the Converse call was shaped correctly
        call_kwargs = mock_bedrock.converse.call_args.kwargs
        assert call_kwargs["modelId"] == "us.anthropic.claude-sonnet-4-6"
        assert call_kwargs["inferenceConfig"]["temperature"] == 0.0
        assert call_kwargs["messages"][0]["role"] == "user"
        prompt = call_kwargs["messages"][0]["content"][0]["text"]
        assert "Caller: I have a question" in prompt
        assert "summary" in prompt
        assert "claim_id" in prompt

    @pytest.mark.asyncio
    async def test_strips_markdown_fences_around_json(self):
        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(
                return_value=_bedrock_response(
                    '```json\n{"summary": "OK"}\n```'
                )
            )
            result = await run_post_call_analyses(
                pca_config=_config_one_text_field(),
                transcript=[{"speaker": "user", "content": "x"}],
                case_data={},
            )
        assert result == {"summary": "OK"}

    @pytest.mark.asyncio
    async def test_invalid_selector_choice_marked_invalid(self):
        cfg = PostCallConfig(
            model="claude-sonnet-4-6",
            fields=[
                PostCallField(
                    name="outcome",
                    type="selector",
                    description="result",
                    choices=["resolved", "unresolved"],
                )
            ],
        )
        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(
                return_value=_bedrock_response('{"outcome": "callback"}')
            )
            result = await run_post_call_analyses(
                pca_config=cfg,
                transcript=[{"speaker": "user", "content": "x"}],
                case_data={},
            )
        assert result == {"outcome": "invalid: callback"}


# =============================================================================
# Error paths — Bedrock failures
# =============================================================================


def _client_error(code: str) -> Exception:
    """Build a botocore.exceptions.ClientError-like exception."""
    e = Exception(f"{code} from Bedrock")
    e.response = {"Error": {"Code": code, "Message": "x"}}  # type: ignore[attr-defined]
    return e


class TestRetryableErrors:
    @pytest.mark.asyncio
    async def test_validation_error_falls_back_to_default_model(self):
        # Primary model raises ValidationException → fall back to
        # _FALLBACK_MODEL_SHORT (claude-sonnet-4-6). If primary IS
        # already the fallback, we still need a custom test.
        cfg = PostCallConfig(
            model="claude-haiku-4-5",  # different from fallback
            fields=[PostCallField(name="x", type="text", description="y")],
        )
        call_count = {"n": 0}

        def converse_side_effect(**_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _client_error("ValidationException")
            return _bedrock_response('{"x": "y"}')

        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(side_effect=converse_side_effect)
            result = await run_post_call_analyses(
                pca_config=cfg,
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        assert result == {"x": "y"}
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_access_denied_falls_back(self):
        cfg = PostCallConfig(
            model="claude-haiku-4-5",
            fields=[PostCallField(name="x", type="text", description="y")],
        )
        call_count = {"n": 0}

        def converse_side_effect(**_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _client_error("AccessDeniedException")
            return _bedrock_response('{"x": "y"}')

        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(side_effect=converse_side_effect)
            result = await run_post_call_analyses(
                pca_config=cfg,
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        assert result == {"x": "y"}

    @pytest.mark.asyncio
    async def test_non_retryable_error_does_not_fall_back(self):
        cfg = PostCallConfig(
            model="claude-haiku-4-5",
            fields=[PostCallField(name="x", type="text", description="y")],
        )

        def converse_side_effect(**_kwargs):
            raise _client_error("ThrottlingException")

        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(side_effect=converse_side_effect)
            result = await run_post_call_analyses(
                pca_config=cfg,
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        assert "_error" in result
        # Only 1 attempt (no fallback for non-retryable)
        assert mock_bedrock.converse.call_count == 1

    @pytest.mark.asyncio
    async def test_both_models_fail_returns_error(self):
        cfg = PostCallConfig(
            model="claude-haiku-4-5",
            fields=[PostCallField(name="x", type="text", description="y")],
        )

        def converse_side_effect(**_kwargs):
            raise _client_error("ValidationException")

        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(side_effect=converse_side_effect)
            result = await run_post_call_analyses(
                pca_config=cfg,
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        assert "_error" in result


class TestParseFailures:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_error(self):
        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(
                return_value=_bedrock_response("not json at all")
            )
            result = await run_post_call_analyses(
                pca_config=_config_one_text_field(),
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        assert result == {"_error": "Failed to parse analysis response"}

    @pytest.mark.asyncio
    async def test_non_dict_response_returns_error(self):
        # LLM returned a JSON array instead of object
        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(
                return_value=_bedrock_response('["a", "b"]')
            )
            result = await run_post_call_analyses(
                pca_config=_config_one_text_field(),
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        assert "_error" in result

    @pytest.mark.asyncio
    async def test_empty_text_response_continues_to_fallback(self):
        cfg = PostCallConfig(
            model="claude-haiku-4-5",
            fields=[PostCallField(name="x", type="text", description="y")],
        )
        call_count = {"n": 0}

        def converse_side_effect(**_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _bedrock_response("")  # empty text
            return _bedrock_response('{"x": "ok"}')

        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(side_effect=converse_side_effect)
            result = await run_post_call_analyses(
                pca_config=cfg,
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        assert result == {"x": "ok"}
        assert call_count["n"] == 2


# =============================================================================
# Never-raises contract
# =============================================================================


class TestNeverRaises:
    @pytest.mark.asyncio
    async def test_bedrock_raises_unexpected_returns_error_dict(self):
        with patch.object(post_call, "_BEDROCK") as mock_bedrock:
            mock_bedrock.converse = MagicMock(side_effect=Exception("network exploded"))
            result = await run_post_call_analyses(
                pca_config=_config_one_text_field(),
                transcript=[{"speaker": "user", "content": "hi"}],
                case_data={},
            )
        # Must return error dict, not propagate exception.
        assert isinstance(result, dict)
        assert "_error" in result
