"""Tests for factory.create_stt_service — Phase 7A-followup STT keywords wiring.

Pre-fix: ``stt_language`` + ``stt_keywords`` were read off the agent's
PipelineConfig but never passed into Deepgram's constructor (factory
had a TODO comment + ``keywords_wired=False`` log flag). The fix
threads both into ``DeepgramSTTService.Settings(...)``. These tests
pin the wiring so the regression can't sneak back.

Skips locally without pipecat (matches the existing test pattern).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

try:
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.transcriptions.language import Language
except ImportError:
    pytest.skip(
        "pipecat not available (container-only dependency)",
        allow_module_level=True,
    )

# DeepgramSTTService.__init__ does network setup; patch it for unit tests
# so we can introspect what got passed without mocking the entire stack.
from app.services.factory import create_stt_service


def _config(stt_language: str = "", stt_keywords: list | None = None):
    """Build a minimal config-like object exposing just the STT fields
    create_stt_service consumes."""
    cfg = MagicMock()
    cfg.stt_provider = "deepgram"
    cfg.stt_language = stt_language
    cfg.stt_keywords = list(stt_keywords or [])
    return cfg


@pytest.fixture(autouse=True)
def _set_dg_key(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-dg-key")


# =============================================================================
# No language / keywords supplied — settings stays None (preserves pre-7A
# behavior for agents that don't configure these — e.g., Chris)
# =============================================================================


class TestNoSettingsWhenAgentEmpty:
    def test_empty_language_and_keywords_omits_settings(self):
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(_config(stt_language="", stt_keywords=[]))
            kwargs = mock_init.call_args.kwargs
        assert "settings" not in kwargs

    def test_default_en_language_only_still_sends_settings(self):
        # 'en' IS a valid Language enum value, so we DO build settings
        # for it. Pre-7A behavior would have used the deepgram default
        # (Language.EN) — same result, but now explicit.
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(_config(stt_language="en", stt_keywords=[]))
            kwargs = mock_init.call_args.kwargs
        assert "settings" in kwargs
        assert kwargs["settings"].language == Language.EN


# =============================================================================
# Keywords passed through
# =============================================================================


class TestKeywordsWired:
    def test_keywords_list_threaded_into_settings(self):
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(
                _config(
                    stt_language="en",
                    stt_keywords=["EOB", "denied claim", "prior auth"],
                )
            )
            kwargs = mock_init.call_args.kwargs
        assert "settings" in kwargs
        assert kwargs["settings"].keywords == ["EOB", "denied claim", "prior auth"]

    def test_keywords_only_no_language_still_builds_settings(self):
        # Aurora row could conceivably have keywords but no language.
        # Settings should still be built; language stays unset (defaults
        # to Deepgram's hardcoded fallback inside the service).
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(_config(stt_language="", stt_keywords=["abc"]))
            kwargs = mock_init.call_args.kwargs
        assert "settings" in kwargs
        assert kwargs["settings"].keywords == ["abc"]


# =============================================================================
# Language coercion / unknown values
# =============================================================================


class TestLanguageCoercion:
    @pytest.mark.parametrize("lang_str,expected", [
        ("en", Language.EN),
        ("en-US", Language.EN_US),
        ("es", Language.ES),
        ("es-MX", Language.ES_MX),
    ])
    def test_known_bcp47_codes_become_enum(self, lang_str, expected):
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(_config(stt_language=lang_str))
            kwargs = mock_init.call_args.kwargs
        assert kwargs["settings"].language == expected

    def test_unknown_language_falls_back_to_default(self):
        # Aurora typo / wrong-format value → don't crash, just skip
        # language and warn. The kwargs should NOT include a busted
        # language field.
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(_config(stt_language="klingon"))
            kwargs = mock_init.call_args.kwargs
        # No keywords, no valid language → no settings argument at all
        assert "settings" not in kwargs

    def test_language_strips_whitespace(self):
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(_config(stt_language="  en  "))
            kwargs = mock_init.call_args.kwargs
        assert kwargs["settings"].language == Language.EN


# =============================================================================
# api_key + sample_rate are still passed (didn't accidentally drop them)
# =============================================================================


class TestBaselineKwargsPreserved:
    def test_api_key_and_sample_rate_passed(self):
        with patch("pipecat.services.deepgram.stt.DeepgramSTTService.__init__") as mock_init:
            mock_init.return_value = None
            create_stt_service(_config())
            kwargs = mock_init.call_args.kwargs
        assert kwargs["api_key"] == "test-dg-key"
        assert kwargs["sample_rate"] == 8000
