"""Tests for MetricsObserver's MetricsFrame → collector routing.

Added in 7B to cover the Pipecat-0.0.108 fix where the observer was
ignoring MetricsFrame entirely, leaving stt_latency_ms / llm_ttfb_ms /
tts_ttfb_ms / llm_input_tokens / llm_output_tokens as None in every
turn_completed log.

Each test feeds a hand-rolled MetricsFrame into the observer and
asserts the matching collector recorder fired with the expected
millisecond value. The routing rules (processor-name prefix matching)
are the public contract between the observer and the upstream pipecat
services — if a test fails because a service rename drifted the
prefix, update the rule here and document why.
"""

from __future__ import annotations

import pytest

try:
    from pipecat.frames.frames import MetricsFrame
    from pipecat.metrics.metrics import (
        LLMTokenUsage,
        LLMUsageMetricsData,
        ProcessingMetricsData,
        TTFBMetricsData,
        TTSUsageMetricsData,
    )
    from pipecat.observers.base_observer import FramePushed
    from pipecat.processors.frame_processor import FrameDirection
except ImportError:
    pytest.skip(
        "pipecat not available (container-only dependency)",
        allow_module_level=True,
    )

from unittest.mock import MagicMock

from app.observability import MetricsCollector, MetricsObserver


def _fp(frame) -> FramePushed:
    """Build a minimal FramePushed for observer.on_push_frame(data)."""
    return FramePushed(
        source=MagicMock(),
        destination=MagicMock(),
        frame=frame,
        direction=FrameDirection.DOWNSTREAM,
        timestamp=0,
    )


def _make_collector() -> MetricsCollector:
    c = MetricsCollector(call_id="test-call", session_id="test-session")
    c.start_turn()  # _current_turn needs to exist for recorders to stick
    return c


@pytest.mark.asyncio
async def test_bedrock_llm_ttfb_routed_to_llm_recorder():
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[
            TTFBMetricsData(
                processor="AWSBedrockLLMService#0",
                value=0.850,
                model="us.anthropic.claude-sonnet-4-6",
            )
        ]
    )
    await obs.on_push_frame(_fp(frame))
    assert collector._current_turn.llm_ttfb_ms == pytest.approx(850.0, abs=0.1)


@pytest.mark.asyncio
async def test_elevenlabs_tts_ttfb_routed_to_tts_recorder():
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[TTFBMetricsData(processor="ElevenLabsTTSService#0", value=0.320)]
    )
    await obs.on_push_frame(_fp(frame))
    assert collector._current_turn.tts_ttfb_ms == pytest.approx(320.0, abs=0.1)


@pytest.mark.asyncio
async def test_deepgram_stt_processing_routed_to_stt_recorder():
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[ProcessingMetricsData(processor="DeepgramSTTService#0", value=0.145)]
    )
    await obs.on_push_frame(_fp(frame))
    assert collector._current_turn.stt_latency_ms == pytest.approx(145.0, abs=0.1)


@pytest.mark.asyncio
async def test_llm_usage_populates_token_counts():
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[
            LLMUsageMetricsData(
                processor="AWSBedrockLLMService#0",
                value=LLMTokenUsage(
                    prompt_tokens=420,
                    completion_tokens=55,
                    total_tokens=475,
                ),
            )
        ]
    )
    await obs.on_push_frame(_fp(frame))
    assert collector._current_turn.llm_input_tokens == 420
    assert collector._current_turn.llm_output_tokens == 55


@pytest.mark.asyncio
async def test_batched_metrics_frame_handles_all_items():
    # A single MetricsFrame can carry multiple MetricsData entries (TTFB +
    # Processing + Usage for one pass). The observer must route each.
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[
            TTFBMetricsData(processor="AWSBedrockLLMService#0", value=0.700),
            ProcessingMetricsData(processor="AWSBedrockLLMService#0", value=1.200),
            LLMUsageMetricsData(
                processor="AWSBedrockLLMService#0",
                value=LLMTokenUsage(
                    prompt_tokens=100, completion_tokens=20, total_tokens=120
                ),
            ),
        ]
    )
    await obs.on_push_frame(_fp(frame))

    t = collector._current_turn
    assert t.llm_ttfb_ms == pytest.approx(700.0)
    assert t.llm_total_ms == pytest.approx(1200.0)
    assert t.llm_input_tokens == 100
    assert t.llm_output_tokens == 20


@pytest.mark.asyncio
async def test_unknown_processor_is_noop_not_crash():
    # If pipecat adds a new service class we don't recognize, the
    # observer should skip rather than crash the pipeline.
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[TTFBMetricsData(processor="FutureMysteryService#0", value=0.500)]
    )
    await obs.on_push_frame(_fp(frame))
    # Nothing should be recorded.
    assert collector._current_turn.llm_ttfb_ms is None
    assert collector._current_turn.tts_ttfb_ms is None
    assert collector._current_turn.stt_latency_ms is None


@pytest.mark.asyncio
async def test_upstream_direction_ignored():
    # Pipecat broadcasts frames in both directions; observers should only
    # see one. _is_new_frame filters UPSTREAM.
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[TTFBMetricsData(processor="AWSBedrockLLMService#0", value=0.900)]
    )
    upstream = FramePushed(
        source=MagicMock(),
        destination=MagicMock(),
        frame=frame,
        direction=FrameDirection.UPSTREAM,
        timestamp=0,
    )
    await obs.on_push_frame(upstream)
    assert collector._current_turn.llm_ttfb_ms is None


@pytest.mark.asyncio
async def test_tts_usage_logged_but_not_recorded_on_turn():
    # We log character counts for TTS cost visibility but don't attach
    # them to TurnMetrics (nothing consumes them yet). Verify the
    # observer doesn't crash and doesn't silently stuff the count into
    # some other field.
    collector = _make_collector()
    obs = MetricsObserver(collector)
    frame = MetricsFrame(
        data=[TTSUsageMetricsData(processor="ElevenLabsTTSService#0", value=128)]
    )
    await obs.on_push_frame(_fp(frame))
    t = collector._current_turn
    # None of the numeric latency fields should pick up the char count.
    assert t.stt_latency_ms is None
    assert t.llm_ttfb_ms is None
    assert t.tts_ttfb_ms is None
