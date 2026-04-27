"""Regression test for VAD tuning. Phase 7E follow-up.

Pre-fix the fork only set ``stop_secs=0.3`` on SileroVADAnalyzer and
inherited every other VADParams default from pipecat (min_volume=0.6
in particular). On phone calls, residual acoustic echo from the bot's
own TTS frequently passed the 0.6 threshold and triggered false
barge-ins — observed 4 in a single 100s test call on 2026-04-27.

This test pins the four module-level VAD_* constants to the OG
voiceagent values so accidental drift back to pipecat defaults can't
sneak in. Each constant has an env-var override so production tuning
happens via ECS task-def env vars, not code edits.

Two test classes:

  * TestSourceReferences: works without pipecat. Reads the source as
    text to verify the constants exist and are passed to VADParams.
  * TestVADBehavior: needs pipecat's audio/vad submodules. Reloads
    pipeline_ecs with patched env to verify defaults + overrides.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# =============================================================================
# Source-grep test — works without pipecat
# =============================================================================


class TestSourceReferences:
    """Belt-and-suspenders: grep the source for each VAD_* constant
    to ensure none was accidentally dropped from the SileroVADAnalyzer
    construction. Doesn't import pipeline_ecs (which would pull in
    pipecat); reads the file as text. Always runs locally."""

    def test_create_voice_pipeline_references_each_constant(self):
        src = Path(__file__).parent.parent / "app" / "pipeline_ecs.py"
        text = src.read_text()
        for kwarg, const in (
            ("min_volume", "VAD_MIN_VOLUME"),
            ("confidence", "VAD_CONFIDENCE"),
            ("start_secs", "VAD_START_SECS"),
            ("stop_secs", "VAD_STOP_SECS"),
        ):
            assert f"{kwarg}={const}" in text, (
                f"VADParams call should pass {kwarg}={const}"
            )

    def test_module_defines_all_four_constants(self):
        src = Path(__file__).parent.parent / "app" / "pipeline_ecs.py"
        text = src.read_text()
        for const in (
            "VAD_MIN_VOLUME",
            "VAD_CONFIDENCE",
            "VAD_START_SECS",
            "VAD_STOP_SECS",
        ):
            assert f"{const} = float(os.getenv(" in text, (
                f"{const} should be a module-level env-var-overridable float"
            )


# =============================================================================
# Constant-reload tests — need pipecat's audio/vad submodules
# =============================================================================


try:
    # The conftest stub gives us a bare ``pipecat`` module but not the
    # submodules pipeline_ecs imports — try to load one of them; if
    # that fails we skip the rest of this file.
    import pipecat.audio.vad.silero  # noqa: F401

    _HAS_PIPECAT_DEEP = True
except ImportError:
    _HAS_PIPECAT_DEEP = False


_skip_no_pipecat = pytest.mark.skipif(
    not _HAS_PIPECAT_DEEP,
    reason="pipecat audio/vad submodules not available (container-only)",
)


def _reload():
    """Reload pipeline_ecs so the module-level VAD_* constants pick up
    whatever env state we just patched in."""
    from app import pipeline_ecs

    return importlib.reload(pipeline_ecs)


@_skip_no_pipecat
class TestVADBehavior:
    """Defaults match OG voiceagent values; env vars override."""

    # ── Defaults ──────────────────────────────────────────────────────

    def test_min_volume_default(self):
        os.environ.pop("VAD_MIN_VOLUME", None)
        mod = _reload()
        assert mod.VAD_MIN_VOLUME == 0.75

    def test_confidence_default(self):
        os.environ.pop("VAD_CONFIDENCE", None)
        mod = _reload()
        assert mod.VAD_CONFIDENCE == 0.7

    def test_start_secs_default(self):
        os.environ.pop("VAD_START_SECS", None)
        mod = _reload()
        assert mod.VAD_START_SECS == 0.2

    def test_stop_secs_default(self):
        # Note: pre-fix the fork used 0.3. OG uses 0.2. We match OG so
        # VAD endpointing behavior is consistent with the proven prod
        # baseline.
        os.environ.pop("VAD_STOP_SECS", None)
        mod = _reload()
        assert mod.VAD_STOP_SECS == 0.2

    # ── Env var overrides ─────────────────────────────────────────────

    def test_min_volume_override(self):
        with patch.dict(os.environ, {"VAD_MIN_VOLUME": "0.8"}):
            mod = _reload()
            assert mod.VAD_MIN_VOLUME == 0.8

    def test_confidence_override(self):
        with patch.dict(os.environ, {"VAD_CONFIDENCE": "0.85"}):
            mod = _reload()
            assert mod.VAD_CONFIDENCE == 0.85

    def test_start_secs_override(self):
        with patch.dict(os.environ, {"VAD_START_SECS": "0.1"}):
            mod = _reload()
            assert mod.VAD_START_SECS == 0.1

    def test_stop_secs_override(self):
        with patch.dict(os.environ, {"VAD_STOP_SECS": "0.5"}):
            mod = _reload()
            assert mod.VAD_STOP_SECS == 0.5

    # ── Type / shape sanity ──────────────────────────────────────────

    def test_constants_are_floats(self):
        # Reset env so we test the defaults
        for name in (
            "VAD_MIN_VOLUME",
            "VAD_CONFIDENCE",
            "VAD_START_SECS",
            "VAD_STOP_SECS",
        ):
            os.environ.pop(name, None)
        mod = _reload()
        for name in (
            "VAD_MIN_VOLUME",
            "VAD_CONFIDENCE",
            "VAD_START_SECS",
            "VAD_STOP_SECS",
        ):
            assert isinstance(getattr(mod, name), float), f"{name} must be float"
