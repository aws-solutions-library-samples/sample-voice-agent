"""
Observability module for voice pipeline timing metrics.

Provides MetricsCollector for accumulating per-turn and per-call metrics,
with CloudWatch EMF (Embedded Metric Format) output for automatic metric
extraction from logs.

Usage:
    collector = MetricsCollector(call_id, session_id)

    # Automatic timing with context manager
    async with collector.time_stt():
        result = await stt_service.run(audio)

    # Manual timing for external measurements
    collector.record_llm_ttfb(312.5)

    # Turn boundaries
    collector.end_turn(user_text="hello", assistant_text="Hi there!")

    # Call completion
    summary = collector.finalize(status="completed")
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

import struct
import math
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    MetricsFrame,
    TextFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    ProcessingMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService

logger = structlog.get_logger(__name__)


# =============================================================================
# Frame Deduplication Helper
# =============================================================================


def _is_new_frame(seen: dict[type, set[int]], data: FramePushed) -> bool:
    """Return True if this frame should be processed by the observer.

    Pipecat's observer ``on_push_frame`` fires once **per processor hop** in
    the pipeline, so a single ``BotStartedSpeakingFrame`` triggers N calls
    where N is the number of processors.  Additionally, pipecat's
    ``broadcast_frame`` creates two distinct frame instances (downstream +
    upstream) for the same logical event.

    This helper skips upstream frames entirely and tracks seen ``frame.id``
    values per frame *class* so each observer processes exactly **one**
    notification per logical pipeline event.
    """
    # Upstream copies are redundant for observation -- skip them.
    if data.direction == FrameDirection.UPSTREAM:
        return False

    frame = data.frame
    frame_type = type(frame)
    frame_id = frame.id
    ids = seen.get(frame_type)
    if ids is None:
        seen[frame_type] = {frame_id}
        return True
    if frame_id in ids:
        return False
    ids.add(frame_id)
    return True


# =============================================================================
# Frame Observer (Non-blocking Pipeline Monitoring)
# =============================================================================


class MetricsObserver(BaseObserver):
    """
    Observer that watches frames for metrics without blocking pipeline.

    Uses pipecat's observer pattern - observers run in separate async tasks
    and cannot block the pipeline. This is the correct way to monitor frames
    for metrics collection.

    Usage:
        observer = MetricsObserver(collector)
        task = PipelineTask(pipeline, observers=[observer])
    """

    def __init__(self, collector: "MetricsCollector"):
        super().__init__()
        self._collector = collector
        self._turn_active = False
        self._seen: dict[type, set[int]] = {}

    async def on_push_frame(self, data: FramePushed):
        """Called when frames are pushed - non-blocking observation."""
        frame = data.frame

        # Track user speech start (beginning of turn)
        if isinstance(frame, UserStartedSpeakingFrame):
            if not _is_new_frame(self._seen, data):
                return
            if not self._turn_active:
                self._collector.start_turn()
                self._turn_active = True
                logger.debug("metrics_observer_turn_started")

        # Track user speech stop (mark VAD stop time for E2E latency)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if not _is_new_frame(self._seen, data):
                return
            self._collector.mark_vad_stop()
            logger.debug("metrics_observer_vad_stop")

        # Track first TTS audio — marks the end of E2E latency measurement
        # (user-stop → first-audio-out). We DO NOT end the turn here
        # because several per-stage metrics (TTS TTFB, LLM token usage)
        # are emitted AFTER this frame and need _current_turn to still
        # be alive on the collector to land. See BotStoppedSpeakingFrame
        # below for the actual turn-end trigger.
        elif isinstance(frame, TTSStartedFrame):
            if not _is_new_frame(self._seen, data):
                return
            self._collector.mark_first_audio()

        # End the turn once the bot is completely done speaking. By this
        # point every downstream processor has had a chance to emit its
        # metrics (TTS TTFB on first audio chunk, LLM usage at
        # messageStop, etc.) so nothing arrives too late. Previous
        # behavior ended the turn on TTSStartedFrame which caused
        # tts_ttfb_ms / llm_input_tokens / llm_output_tokens to come
        # back null on any turn where those frames happened to arrive
        # after TTS started.
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if not _is_new_frame(self._seen, data):
                return
            if self._turn_active:
                self._collector.end_turn()
                self._turn_active = False
                logger.debug("metrics_observer_turn_ended")

        # Pipecat 0.0.108 services emit MetricsFrame via
        # FrameProcessorMetrics (pipecat/processors/metrics/
        # frame_processor_metrics.py). Each frame carries one or more
        # MetricsData items with a `processor` tag identifying the
        # source (e.g. "AWSBedrockLLMService#0"). Route TTFB /
        # Processing / Usage data into the matching collector
        # recorders so turn_completed reports non-null values.
        #
        # Without this branch, agent_response_latency_ms worked but
        # stt_latency_ms / llm_ttfb_ms / tts_ttfb_ms / llm_input_tokens
        # / llm_output_tokens all came back None (observed 2026-04-24).
        elif isinstance(frame, MetricsFrame):
            if not _is_new_frame(self._seen, data):
                return
            self._handle_metrics_frame(frame)

    def _handle_metrics_frame(self, frame: MetricsFrame) -> None:
        """Route MetricsData items to the right collector recorder.

        Processor names in pipecat are formatted like
        ``"AWSBedrockLLMService#0"`` — class name + deterministic
        instance number. Match on the class-name prefix so future
        renumbering (multiple service instances) doesn't break
        routing.
        """
        for item in frame.data:
            proc = (item.processor or "").lower()
            value_ms: Optional[float] = None

            if isinstance(item, TTFBMetricsData):
                # value is in seconds, convert to ms
                value_ms = item.value * 1000.0

                # LLM TTFB — Bedrock / Anthropic / OpenAI / Azure …
                if "llm" in proc or "bedrock" in proc or "claude" in proc:
                    self._collector.record_llm_ttfb(value_ms)
                    logger.debug(
                        "metrics_observer_llm_ttfb",
                        processor=item.processor,
                        value_ms=round(value_ms, 1),
                    )
                # TTS TTFB — ElevenLabs / Cartesia / OpenAI TTS
                elif "tts" in proc or "elevenlabs" in proc or "cartesia" in proc:
                    self._collector.record_tts_ttfb(value_ms)
                    logger.debug(
                        "metrics_observer_tts_ttfb",
                        processor=item.processor,
                        value_ms=round(value_ms, 1),
                    )

            elif isinstance(item, ProcessingMetricsData):
                value_ms = item.value * 1000.0
                # STT processing time maps to our stt_latency metric
                if "stt" in proc or "deepgram" in proc or "whisper" in proc:
                    self._collector.record_stt_latency(value_ms)
                    logger.debug(
                        "metrics_observer_stt_latency",
                        processor=item.processor,
                        value_ms=round(value_ms, 1),
                    )
                # LLM total processing time
                elif "llm" in proc or "bedrock" in proc or "claude" in proc:
                    self._collector.record_llm_total(value_ms)

            elif isinstance(item, LLMUsageMetricsData):
                # LLMUsageMetricsData.value is a LLMTokenUsage object.
                usage = item.value
                self._collector.record_llm_usage(
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
                logger.debug(
                    "metrics_observer_llm_usage",
                    processor=item.processor,
                    input_tokens=getattr(usage, "prompt_tokens", 0),
                    output_tokens=getattr(usage, "completion_tokens", 0),
                )

            elif isinstance(item, TTSUsageMetricsData):
                # Character count; we don't currently track but log
                # for visibility into TTS cost.
                logger.debug(
                    "metrics_observer_tts_usage",
                    processor=item.processor,
                    characters=item.value,
                )


class ConversationObserver(BaseObserver):
    """
    Observer that logs conversation content without blocking the pipeline.

    Captures user speech (TranscriptionFrame), bot responses (TextFrame),
    and barge-in events for debugging and quality analysis.

    Usage:
        observer = ConversationObserver(collector, enabled=True)
        task = PipelineTask(pipeline, observers=[observer])

    Environment:
        ENABLE_CONVERSATION_LOGGING: Set to "true" to enable (default: false)
    """

    def __init__(
        self,
        collector: "MetricsCollector",
        enabled: bool = True,
    ):
        super().__init__()
        self._collector = collector
        self._enabled = enabled
        self._current_bot_text: List[str] = []
        self._bot_speaking = False
        self._barge_in_detected = False  # Prevent duplicate counts per speaking session
        self._llm_responding = False  # Track LLM response boundaries
        self._current_user_text: Optional[str] = None
        self._seen: dict[type, set[int]] = {}

        if enabled:
            logger.info(
                "conversation_observer_created",
                call_id=collector.call_id,
                session_id=collector.session_id,
            )

    async def on_push_frame(self, data: FramePushed):
        """Called when frames are pushed - non-blocking observation."""
        if not self._enabled:
            return

        frame = data.frame
        source = data.source

        # --- Source-filtered frame types: dedup must happen AFTER the source
        #     check, because different hops have different sources and we only
        #     want the one from the correct source (LLMService).

        # Capture user speech from final transcription
        if isinstance(frame, TranscriptionFrame):
            if not _is_new_frame(self._seen, data):
                return
            self._handle_transcription(frame)

        # Track LLM response start - begin capturing text
        # IMPORTANT: Only process frames from the LLM service to avoid duplicates
        elif isinstance(frame, LLMFullResponseStartFrame):
            if isinstance(source, LLMService):
                if not _is_new_frame(self._seen, data):
                    return
                self._llm_responding = True
                self._current_bot_text = []  # Clear any stale text

        # Accumulate bot response text (only from LLM, only during LLM response)
        elif isinstance(frame, TextFrame):
            if self._llm_responding and isinstance(source, LLMService):
                if not _is_new_frame(self._seen, data):
                    return
                self._handle_text_frame(frame)

        # Track LLM response end - flush accumulated response
        elif isinstance(frame, LLMFullResponseEndFrame):
            if isinstance(source, LLMService):
                if not _is_new_frame(self._seen, data):
                    return
                self._flush_bot_response()
                self._llm_responding = False

        # Track TTS generation state (for metrics, not barge-in)
        elif isinstance(frame, TTSStartedFrame):
            pass  # TTS generation started (not used for barge-in)

        elif isinstance(frame, TTSStoppedFrame):
            pass  # TTS generation stopped (not used for barge-in)

        # --- Speaking-state frame types: no source filter needed, dedup early.

        # Track bot audio playback state for barge-in detection
        # BotStartedSpeakingFrame fires when audio actually starts playing
        # BotStoppedSpeakingFrame fires when audio playback completes
        elif isinstance(frame, BotStartedSpeakingFrame):
            if not _is_new_frame(self._seen, data):
                return
            self._bot_speaking = True
            self._barge_in_detected = False  # Reset for new speaking session
            logger.debug(
                "conversation_observer_bot_started_speaking",
                call_id=self._collector.call_id,
                bot_speaking=self._bot_speaking,
            )

        elif isinstance(frame, BotStoppedSpeakingFrame):
            if not _is_new_frame(self._seen, data):
                return
            self._bot_speaking = False
            logger.debug(
                "conversation_observer_bot_stopped_speaking",
                call_id=self._collector.call_id,
                bot_speaking=self._bot_speaking,
            )

        # Detect barge-in (user interrupts while bot audio is playing)
        # Only count once per bot speaking session to avoid duplicate counts
        elif isinstance(frame, UserStartedSpeakingFrame):
            if not _is_new_frame(self._seen, data):
                return
            logger.debug(
                "conversation_observer_user_started_speaking",
                call_id=self._collector.call_id,
                bot_speaking=self._bot_speaking,
            )
            if self._bot_speaking and not self._barge_in_detected:
                self._barge_in_detected = True
                self._log_barge_in()

    def _handle_transcription(self, frame: TranscriptionFrame) -> None:
        """Handle user transcription frame."""
        text = frame.text.strip() if frame.text else ""
        if not text:
            return

        self._current_user_text = text
        self._log_conversation_turn(speaker="user", content=text)

    def _handle_text_frame(self, frame: TextFrame) -> None:
        """Accumulate text frames for complete bot response."""
        text = frame.text if frame.text else ""
        if text:
            self._current_bot_text.append(text)

    def _flush_bot_response(self) -> None:
        """Log accumulated bot response when TTS completes."""
        if not self._current_bot_text:
            return

        # Join chunks with spaces (TextFrame tokens often lack whitespace)
        full_response = " ".join(self._current_bot_text)
        # Remove spaces before punctuation: "Hello ." -> "Hello."
        full_response = re.sub(r"\s+([.,!?;:'])", r"\1", full_response)
        # Normalize multiple spaces to single space
        full_response = " ".join(full_response.split())

        if full_response:
            self._log_conversation_turn(speaker="assistant", content=full_response)

        self._current_bot_text = []

    def _log_conversation_turn(self, speaker: str, content: str) -> None:
        """Log a conversation turn with correlation fields."""
        turn_number = self._collector.turn_count or 1

        logger.info(
            "conversation_turn",
            call_id=self._collector.call_id,
            session_id=self._collector.session_id,
            turn_number=turn_number,
            speaker=speaker,
            content=content,
        )

    def _log_barge_in(self) -> None:
        """Log a barge-in event when user interrupts the bot."""
        turn_number = self._collector.turn_count or 1

        # Increment the interruption counter
        self._collector.record_interruption()

        logger.info(
            "barge_in",
            call_id=self._collector.call_id,
            session_id=self._collector.session_id,
            turn_number=turn_number,
        )

        # Also flush any partial bot response that was interrupted
        if self._current_bot_text:
            partial_response = "".join(self._current_bot_text).strip()
            if partial_response:
                logger.debug(
                    "conversation_turn_interrupted",
                    call_id=self._collector.call_id,
                    session_id=self._collector.session_id,
                    turn_number=turn_number,
                    speaker="assistant",
                    content=partial_response,
                    interrupted=True,
                )
            self._current_bot_text = []


class AudioQualityObserver(BaseObserver):
    """
    Observer that tracks audio quality metrics without blocking pipeline.

    Monitors InputAudioRawFrame to calculate RMS levels, peak amplitude,
    and silence duration for quality assessment. Integrates with
    MetricsCollector for EMF emission at turn boundaries.

    Audio Quality Metrics:
    - RMS (Root Mean Square): Average audio power level in dBFS
    - Peak: Maximum amplitude in dBFS (detects clipping)
    - Silence Duration: Time between speech segments

    Usage:
        observer = AudioQualityObserver(collector)
        task = PipelineTask(pipeline, observers=[observer])

    Environment:
        ENABLE_AUDIO_QUALITY_MONITORING: Set to "true" to enable (default: true)
    """

    # Silence threshold in dBFS (below this is considered silence)
    SILENCE_THRESHOLD_DB = -40.0
    # Poor audio threshold in dBFS (typical phone audio is -40 to -50 dBFS)
    # Anything below -55 dBFS is considered too quiet/poor quality
    POOR_AUDIO_THRESHOLD_DB = -55.0
    # Max value for 16-bit signed audio
    MAX_AMPLITUDE = 32767

    def __init__(
        self,
        collector: "MetricsCollector",
        enabled: bool = True,
    ):
        """
        Initialize audio quality observer.

        Args:
            collector: MetricsCollector for aggregating metrics
            enabled: Enable/disable monitoring
        """
        super().__init__()
        self._collector = collector
        self._enabled = enabled

        # Accumulators for turn-level metrics
        self._rms_samples: List[float] = []
        self._peak_samples: List[float] = []
        self._silence_start_time: Optional[float] = None
        self._last_speech_end_time: Optional[float] = None
        self._user_speaking = False
        # Track which turn number we last recorded poor audio for
        # (prevents counting poor audio multiple times per conversation turn)
        self._poor_audio_last_turn: int = 0
        self._seen: dict[type, set[int]] = {}

        if enabled:
            logger.info(
                "audio_quality_observer_created",
                call_id=collector.call_id,
                session_id=collector.session_id,
            )

    async def on_push_frame(self, data: FramePushed) -> None:
        """
        Called when frames are pushed - non-blocking observation.

        Frame Types Observed:
        - InputAudioRawFrame: Calculate RMS, peak, detect silence
        - UserStartedSpeakingFrame: Reset silence timer, record silence duration
        - UserStoppedSpeakingFrame: Mark speech end time
        """
        if not self._enabled:
            return

        frame = data.frame

        if not _is_new_frame(self._seen, data):
            return

        try:
            # Process audio frames for quality metrics
            if isinstance(frame, InputAudioRawFrame):
                self._process_audio_frame(frame)

            # Track user speech start
            elif isinstance(frame, UserStartedSpeakingFrame):
                self._handle_speech_start()

            # Track user speech stop
            elif isinstance(frame, UserStoppedSpeakingFrame):
                self._handle_speech_stop()

        except Exception as e:
            logger.warning(
                "audio_quality_observer_error",
                error=str(e),
                frame_type=type(frame).__name__,
                call_id=self._collector.call_id,
            )
            # DO NOT re-raise - observers must never crash the pipeline

    def _process_audio_frame(self, frame: InputAudioRawFrame) -> None:
        """
        Process audio frame for quality metrics.

        Args:
            frame: InputAudioRawFrame with raw audio data
        """
        if not hasattr(frame, "audio") or frame.audio is None:
            return

        audio_bytes = frame.audio
        if len(audio_bytes) < 2:
            return

        try:
            # Convert bytes to 16-bit signed integers
            num_samples = len(audio_bytes) // 2
            samples = struct.unpack(f"<{num_samples}h", audio_bytes)

            if not samples:
                return

            # Calculate RMS
            sum_squares = sum(s * s for s in samples)
            rms = (sum_squares / len(samples)) ** 0.5

            # Convert to dBFS (decibels relative to full scale)
            if rms > 0:
                rms_db = 20 * math.log10(rms / self.MAX_AMPLITUDE)
            else:
                rms_db = -96.0  # Essentially silence

            # Calculate peak
            peak = max(abs(s) for s in samples)
            if peak > 0:
                peak_db = 20 * math.log10(peak / self.MAX_AMPLITUDE)
            else:
                peak_db = -96.0

            # Accumulate samples for turn-level averaging
            self._rms_samples.append(rms_db)
            self._peak_samples.append(peak_db)

        except (struct.error, ValueError) as e:
            logger.debug(
                "audio_processing_error",
                error=str(e),
                call_id=self._collector.call_id,
            )

    def _handle_speech_start(self) -> None:
        """Handle user started speaking - record silence duration if applicable."""
        current_time = time.perf_counter()

        # Calculate silence duration if we had a previous speech end
        if self._last_speech_end_time is not None:
            silence_duration_ms = (current_time - self._last_speech_end_time) * 1000
            self._collector.record_silence_duration(silence_duration_ms)
            logger.debug(
                "silence_duration_recorded",
                silence_duration_ms=round(silence_duration_ms, 1),
                call_id=self._collector.call_id,
            )

        self._user_speaking = True
        # Reset accumulators for new speech segment
        self._rms_samples = []
        self._peak_samples = []

    def _handle_speech_stop(self) -> None:
        """Handle user stopped speaking - record audio quality metrics."""
        self._last_speech_end_time = time.perf_counter()
        self._user_speaking = False

        # Calculate and record turn-level audio quality
        if self._rms_samples:
            avg_rms_db = sum(self._rms_samples) / len(self._rms_samples)
            self._collector.record_audio_rms(avg_rms_db)

            # Check for poor audio quality - only count once per conversation turn
            # (VAD may fire multiple times per turn, but we only want to count poor
            # audio once per actual conversational exchange)
            current_turn = self._collector.turn_count
            if avg_rms_db < self.POOR_AUDIO_THRESHOLD_DB:
                # Only record if we haven't already recorded for this turn
                if current_turn > self._poor_audio_last_turn:
                    self._collector.record_poor_audio_turn()
                    self._poor_audio_last_turn = current_turn
                    logger.debug(
                        "poor_audio_detected",
                        avg_rms_db=round(avg_rms_db, 1),
                        turn_number=current_turn,
                        call_id=self._collector.call_id,
                    )

            logger.debug(
                "audio_quality_recorded",
                avg_rms_db=round(avg_rms_db, 1),
                sample_count=len(self._rms_samples),
                call_id=self._collector.call_id,
            )

        if self._peak_samples:
            avg_peak_db = sum(self._peak_samples) / len(self._peak_samples)
            max_peak_db = max(self._peak_samples)
            self._collector.record_audio_peak(avg_peak_db)

            # Check for clipping (peak near 0 dBFS)
            if max_peak_db > -1.0:
                logger.info(
                    "audio_clipping_detected",
                    max_peak_db=round(max_peak_db, 1),
                    call_id=self._collector.call_id,
                )


class STTQualityObserver(BaseObserver):
    """
    Observer that tracks STT quality metrics from Deepgram transcriptions.

    Monitors both TranscriptionFrame (final) and InterimTranscriptionFrame
    (interim) to extract confidence scores and track transcription counts.

    Supports both cloud Deepgram (LiveResultResponse object) and SageMaker
    Deepgram (raw dict) result formats.
    """

    def __init__(
        self,
        collector: "MetricsCollector",
        enabled: bool = True,
    ):
        super().__init__()
        self._collector = collector
        self._enabled = enabled
        self._confidence_scores: List[float] = []
        self._interim_count = 0
        self._final_count = 0
        self._seen: dict[type, set[int]] = {}

        if enabled:
            logger.info(
                "stt_quality_observer_created",
                call_id=collector.call_id,
            )

    async def on_push_frame(self, data: FramePushed) -> None:
        if not self._enabled:
            return

        frame = data.frame

        if not _is_new_frame(self._seen, data):
            return

        # TranscriptionFrame is a parent of InterimTranscriptionFrame,
        # so check InterimTranscriptionFrame first for correct classification.
        if isinstance(frame, InterimTranscriptionFrame):
            await self._handle_transcription(frame, is_final=False)
        elif isinstance(frame, TranscriptionFrame):
            await self._handle_transcription(frame, is_final=True)

    async def _handle_transcription(
        self, frame: TranscriptionFrame, is_final: bool
    ) -> None:
        """Process transcription frame for quality metrics.

        Uses frame type (not result.is_final) to determine final vs interim,
        since pipecat already makes this determination when emitting frames.
        Supports both dict results (SageMaker STT) and object results (cloud STT).
        """
        confidence = self._extract_confidence(frame)

        # Track confidence scores
        if confidence is not None:
            self._confidence_scores.append(confidence)

        # Track interim vs final counts based on frame type
        if is_final:
            self._final_count += 1
            # Record metrics when we get a final transcription
            self._record_stt_metrics(frame)
        else:
            self._interim_count += 1

    def _extract_confidence(self, frame: TranscriptionFrame) -> Optional[float]:
        """Extract confidence score from transcription frame result.

        Handles both dict results (SageMaker DeepgramSTT) and object results
        (cloud DeepgramSTT with LiveResultResponse).
        """
        if not hasattr(frame, "result") or frame.result is None:
            return None

        result = frame.result

        # Dict result (SageMaker STT: result=parsed JSON dict)
        if isinstance(result, dict):
            try:
                alternatives = result.get("channel", {}).get("alternatives", [])
                if alternatives:
                    return alternatives[0].get("confidence")
            except (IndexError, AttributeError, TypeError):
                return None

        # Object result (cloud Deepgram STT: result=LiveResultResponse)
        if hasattr(result, "channel") and result.channel:
            channel = result.channel
            if hasattr(channel, "alternatives") and channel.alternatives:
                best_alt = channel.alternatives[0]
                if hasattr(best_alt, "confidence"):
                    return best_alt.confidence

        return None

    def _record_stt_metrics(self, frame: TranscriptionFrame) -> None:
        """Record STT quality metrics for the turn.

        Records final_count, interim_count, and word_count even when
        no confidence scores are available.
        """
        word_count = len(frame.text.split()) if frame.text else 0

        avg_confidence = None
        min_confidence = None
        if self._confidence_scores:
            avg_confidence = sum(self._confidence_scores) / len(self._confidence_scores)
            min_confidence = min(self._confidence_scores)

        self._collector.record_stt_quality(
            confidence_avg=avg_confidence,
            confidence_min=min_confidence,
            interim_count=self._interim_count,
            final_count=self._final_count,
            word_count=word_count,
        )

        logger.debug(
            "stt_quality_recorded",
            confidence_avg=(
                round(avg_confidence, 3) if avg_confidence is not None else None
            ),
            confidence_min=(
                round(min_confidence, 3) if min_confidence is not None else None
            ),
            interim_count=self._interim_count,
            final_count=self._final_count,
            word_count=word_count,
        )

        # Reset accumulators for next turn
        self._confidence_scores = []
        self._interim_count = 0
        self._final_count = 0


class LLMQualityObserver(BaseObserver):
    """
    Observer that tracks LLM quality metrics.

    Monitors LLM response frames to track token counts and generation speed.
    """

    def __init__(
        self,
        collector: "MetricsCollector",
        enabled: bool = True,
    ):
        super().__init__()
        self._collector = collector
        self._enabled = enabled
        self._response_start_time: Optional[float] = None
        self._token_count: float = 0
        self._in_llm_response = False
        self._seen: dict[type, set[int]] = {}

        if enabled:
            logger.info(
                "llm_quality_observer_created",
                call_id=collector.call_id,
            )

    async def on_push_frame(self, data: FramePushed) -> None:
        if not self._enabled:
            return

        frame = data.frame

        if not _is_new_frame(self._seen, data):
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._response_start_time = time.perf_counter()
            self._token_count = 0
            self._in_llm_response = True

        elif isinstance(frame, TextFrame) and self._in_llm_response:
            # Estimate tokens (rough approximation: ~4 chars per token)
            if frame.text:
                estimated_tokens = len(frame.text) / 4
                self._token_count += estimated_tokens

        elif isinstance(frame, LLMFullResponseEndFrame) and self._in_llm_response:
            if self._response_start_time and self._token_count > 0:
                duration_sec = time.perf_counter() - self._response_start_time
                tokens_per_sec = (
                    self._token_count / duration_sec if duration_sec > 0 else 0
                )

                self._collector.record_llm_quality(
                    output_tokens=int(self._token_count),
                    tokens_per_second=tokens_per_sec,
                )

                logger.debug(
                    "llm_quality_recorded",
                    output_tokens=int(self._token_count),
                    tokens_per_second=round(tokens_per_sec, 1),
                    duration_sec=round(duration_sec, 2),
                )

            # Reset state
            self._response_start_time = None
            self._token_count = 0
            self._in_llm_response = False


class ConversationFlowObserver(BaseObserver):
    """
    Observer that analyzes conversation flow patterns.

    Tracks turn-taking timing, speaking durations, and response delays.
    """

    def __init__(
        self,
        collector: "MetricsCollector",
        enabled: bool = True,
    ):
        super().__init__()
        self._collector = collector
        self._enabled = enabled

        # State tracking
        self._last_user_stop_time: Optional[float] = None
        self._last_bot_stop_time: Optional[float] = None
        self._user_speaking_start: Optional[float] = None
        self._bot_speaking_start: Optional[float] = None

        # Timing accumulators
        self._total_user_speaking_time = 0.0
        self._total_bot_speaking_time = 0.0
        self._turn_gaps: List[float] = []
        self._seen: dict[type, set[int]] = {}

        if enabled:
            logger.info(
                "conversation_flow_observer_created",
                call_id=collector.call_id,
            )

    async def on_push_frame(self, data: FramePushed) -> None:
        if not self._enabled:
            return

        frame = data.frame

        if not _is_new_frame(self._seen, data):
            return

        current_time = time.perf_counter()

        if isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking_start = current_time

            # Calculate gap since last bot stopped speaking
            if self._last_bot_stop_time:
                gap_ms = (current_time - self._last_bot_stop_time) * 1000
                self._turn_gaps.append(gap_ms)
                self._collector.record_turn_gap(gap_ms)

        elif isinstance(frame, UserStoppedSpeakingFrame):
            if self._user_speaking_start:
                speaking_duration = (current_time - self._user_speaking_start) * 1000
                self._total_user_speaking_time += (
                    speaking_duration / 1000
                )  # Convert to seconds
                self._collector.record_user_speaking_duration(speaking_duration)

            self._last_user_stop_time = current_time
            self._user_speaking_start = None

        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking_start = current_time

            # Calculate response delay since user stopped
            if self._last_user_stop_time:
                delay_ms = (current_time - self._last_user_stop_time) * 1000
                self._collector.record_response_delay(delay_ms)

        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._bot_speaking_start:
                speaking_duration = (current_time - self._bot_speaking_start) * 1000
                self._total_bot_speaking_time += (
                    speaking_duration / 1000
                )  # Convert to seconds
                self._collector.record_bot_speaking_duration(speaking_duration)

            self._last_bot_stop_time = current_time
            self._bot_speaking_start = None

    def finalize_call_metrics(self, call_duration_sec: float) -> None:
        """Calculate final conversation flow metrics for the call."""
        total_speaking_time = (
            self._total_user_speaking_time + self._total_bot_speaking_time
        )

        if total_speaking_time > 0:
            user_ratio = self._total_user_speaking_time / total_speaking_time
            self._collector.record_speaking_ratio(user_ratio)

        if self._turn_gaps:
            avg_gap = sum(self._turn_gaps) / len(self._turn_gaps)
            self._collector.record_avg_turn_gap(avg_gap)


class QualityScoreCalculator:
    """
    Calculates composite quality scores for turns and calls.

    Uses weighted combination of latency, audio quality, STT confidence,
    conversation flow, and network quality metrics.
    """

    # Weights for different quality factors (must sum to 1.0)
    WEIGHTS = {
        "latency": 0.3,  # Agent response latency
        "audio": 0.2,  # Audio RMS quality
        "stt_confidence": 0.2,  # STT confidence
        "flow": 0.15,  # Conversation flow smoothness
        "network": 0.15,  # WebRTC quality
    }

    # Thresholds for quality scoring
    THRESHOLDS = {
        "excellent_latency_ms": 800,
        "poor_latency_ms": 2000,
        "excellent_audio_db": -30,
        "poor_audio_db": -55,
        "excellent_confidence": 0.9,
        "poor_confidence": 0.6,
        "excellent_gap_ms": 500,
        "poor_gap_ms": 2000,
        "excellent_rtt_ms": 50,
        "poor_rtt_ms": 200,
    }

    @classmethod
    def calculate_turn_quality(cls, turn: TurnMetrics) -> float:
        """Calculate quality score for a single turn (0.0 to 1.0)."""
        scores = {}

        # Latency score (0.0 = poor, 1.0 = excellent)
        if turn.agent_response_latency_ms is not None:
            scores["latency"] = cls._score_latency(turn.agent_response_latency_ms)

        # Audio quality score
        if turn.audio_rms_db is not None:
            scores["audio"] = cls._score_audio_quality(turn.audio_rms_db)

        # STT confidence score
        if turn.stt_confidence_avg is not None:
            scores["stt_confidence"] = cls._score_stt_confidence(
                turn.stt_confidence_avg
            )

        # Flow score (turn gap)
        if turn.turn_gap_ms is not None:
            scores["flow"] = cls._score_turn_gap(turn.turn_gap_ms)

        # Network score
        if turn.webrtc_rtt_ms is not None:
            scores["network"] = cls._score_network_quality(turn.webrtc_rtt_ms)

        # Calculate weighted average of available scores
        if not scores:
            return 0.5  # Neutral score if no data

        total_weight = sum(cls.WEIGHTS[key] for key in scores.keys())
        weighted_sum = sum(scores[key] * cls.WEIGHTS[key] for key in scores.keys())

        return weighted_sum / total_weight if total_weight > 0 else 0.5

    @classmethod
    def _score_latency(cls, latency_ms: float) -> float:
        """Score latency from 0.0 (poor) to 1.0 (excellent)."""
        if latency_ms <= cls.THRESHOLDS["excellent_latency_ms"]:
            return 1.0
        elif latency_ms >= cls.THRESHOLDS["poor_latency_ms"]:
            return 0.0
        else:
            range_ms = (
                cls.THRESHOLDS["poor_latency_ms"]
                - cls.THRESHOLDS["excellent_latency_ms"]
            )
            offset_ms = latency_ms - cls.THRESHOLDS["excellent_latency_ms"]
            return 1.0 - (offset_ms / range_ms)

    @classmethod
    def _score_audio_quality(cls, rms_db: float) -> float:
        """Score audio quality from 0.0 (poor) to 1.0 (excellent)."""
        if rms_db >= cls.THRESHOLDS["excellent_audio_db"]:
            return 1.0
        elif rms_db <= cls.THRESHOLDS["poor_audio_db"]:
            return 0.0
        else:
            range_db = (
                cls.THRESHOLDS["excellent_audio_db"] - cls.THRESHOLDS["poor_audio_db"]
            )
            offset_db = rms_db - cls.THRESHOLDS["poor_audio_db"]
            return offset_db / range_db

    @classmethod
    def _score_stt_confidence(cls, confidence: float) -> float:
        """Score STT confidence from 0.0 (poor) to 1.0 (excellent)."""
        if confidence >= cls.THRESHOLDS["excellent_confidence"]:
            return 1.0
        elif confidence <= cls.THRESHOLDS["poor_confidence"]:
            return 0.0
        else:
            range_conf = (
                cls.THRESHOLDS["excellent_confidence"]
                - cls.THRESHOLDS["poor_confidence"]
            )
            offset_conf = confidence - cls.THRESHOLDS["poor_confidence"]
            return offset_conf / range_conf

    @classmethod
    def _score_turn_gap(cls, gap_ms: float) -> float:
        """Score turn gap from 0.0 (poor) to 1.0 (excellent)."""
        if gap_ms <= cls.THRESHOLDS["excellent_gap_ms"]:
            return 1.0
        elif gap_ms >= cls.THRESHOLDS["poor_gap_ms"]:
            return 0.0
        else:
            range_ms = (
                cls.THRESHOLDS["poor_gap_ms"] - cls.THRESHOLDS["excellent_gap_ms"]
            )
            offset_ms = gap_ms - cls.THRESHOLDS["excellent_gap_ms"]
            return 1.0 - (offset_ms / range_ms)

    @classmethod
    def _score_network_quality(cls, rtt_ms: float) -> float:
        """Score network quality from 0.0 (poor) to 1.0 (excellent)."""
        if rtt_ms <= cls.THRESHOLDS["excellent_rtt_ms"]:
            return 1.0
        elif rtt_ms >= cls.THRESHOLDS["poor_rtt_ms"]:
            return 0.0
        else:
            range_ms = (
                cls.THRESHOLDS["poor_rtt_ms"] - cls.THRESHOLDS["excellent_rtt_ms"]
            )
            offset_ms = rtt_ms - cls.THRESHOLDS["excellent_rtt_ms"]
            return 1.0 - (offset_ms / range_ms)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class TurnMetrics:
    """Metrics for a single conversation turn."""

    turn_number: int
    started_at: float = field(default_factory=time.perf_counter)

    # Latency metrics (milliseconds)
    stt_latency_ms: Optional[float] = None
    llm_ttfb_ms: Optional[float] = None
    llm_total_ms: Optional[float] = None
    tts_ttfb_ms: Optional[float] = None
    agent_response_latency_ms: Optional[float] = None  # Renamed from e2e_latency_ms

    # E2E tracking
    vad_stop_time: Optional[float] = None
    first_audio_time: Optional[float] = None

    # Audio quality metrics
    audio_rms_db: Optional[float] = None
    audio_peak_db: Optional[float] = None
    silence_duration_ms: Optional[float] = None

    # STT Quality metrics
    stt_confidence_avg: Optional[float] = None
    stt_confidence_min: Optional[float] = None
    stt_interim_count: int = 0
    stt_final_count: int = 0
    stt_word_count: Optional[int] = None

    # LLM Quality metrics
    llm_input_tokens: Optional[int] = None
    llm_output_tokens: Optional[int] = None
    llm_tokens_per_second: Optional[float] = None

    # WebRTC/Network Quality metrics
    webrtc_rtt_ms: Optional[float] = None
    webrtc_jitter_ms: Optional[float] = None
    webrtc_packet_loss_percent: Optional[float] = None
    webrtc_bitrate_kbps: Optional[float] = None

    # Conversation Flow metrics
    turn_gap_ms: Optional[float] = None
    user_speaking_duration_ms: Optional[float] = None
    bot_speaking_duration_ms: Optional[float] = None
    response_delay_ms: Optional[float] = None
    was_abandoned: bool = False

    # Composite Quality Score
    quality_score: Optional[float] = None  # 0.0 to 1.0

    # Content (optional, for conversation logging)
    user_text: Optional[str] = None
    assistant_text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "turn_number": self.turn_number,
            "stt_latency_ms": self.stt_latency_ms,
            "llm_ttfb_ms": self.llm_ttfb_ms,
            "llm_total_ms": self.llm_total_ms,
            "tts_ttfb_ms": self.tts_ttfb_ms,
            "agent_response_latency_ms": self.agent_response_latency_ms,
            "audio_rms_db": self.audio_rms_db,
            "audio_peak_db": self.audio_peak_db,
            "silence_duration_ms": self.silence_duration_ms,
            "stt_confidence_avg": self.stt_confidence_avg,
            "stt_confidence_min": self.stt_confidence_min,
            "stt_interim_count": self.stt_interim_count,
            "stt_final_count": self.stt_final_count,
            "stt_word_count": self.stt_word_count,
            "llm_input_tokens": self.llm_input_tokens,
            "llm_output_tokens": self.llm_output_tokens,
            "llm_tokens_per_second": self.llm_tokens_per_second,
            "webrtc_rtt_ms": self.webrtc_rtt_ms,
            "webrtc_jitter_ms": self.webrtc_jitter_ms,
            "webrtc_packet_loss_percent": self.webrtc_packet_loss_percent,
            "webrtc_bitrate_kbps": self.webrtc_bitrate_kbps,
            "turn_gap_ms": self.turn_gap_ms,
            "user_speaking_duration_ms": self.user_speaking_duration_ms,
            "bot_speaking_duration_ms": self.bot_speaking_duration_ms,
            "response_delay_ms": self.response_delay_ms,
            "was_abandoned": self.was_abandoned,
            "quality_score": self.quality_score,
        }


@dataclass
class CallMetrics:
    """Aggregated metrics for a complete call."""

    call_id: str
    session_id: str
    environment: str
    started_at: float = field(default_factory=time.monotonic)

    # Aggregates
    turn_count: int = 0
    interruption_count: int = 0
    total_stt_ms: float = 0.0
    total_llm_ms: float = 0.0
    total_tts_ms: float = 0.0

    # Audio quality aggregates
    poor_audio_turns: int = 0

    # Status
    completion_status: str = "in_progress"
    error_category: Optional[str] = None

    # Turn history (for avg calculations)
    _turn_metrics: List[TurnMetrics] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        """Total call duration in seconds."""
        return time.monotonic() - self.started_at

    @property
    def avg_stt_ms(self) -> float:
        """Average STT latency across turns."""
        values = [t.stt_latency_ms for t in self._turn_metrics if t.stt_latency_ms]
        return sum(values) / len(values) if values else 0.0

    @property
    def avg_llm_ms(self) -> float:
        """Average LLM total time across turns."""
        values = [t.llm_total_ms for t in self._turn_metrics if t.llm_total_ms]
        return sum(values) / len(values) if values else 0.0

    @property
    def avg_tts_ms(self) -> float:
        """Average TTS TTFB across turns."""
        values = [t.tts_ttfb_ms for t in self._turn_metrics if t.tts_ttfb_ms]
        return sum(values) / len(values) if values else 0.0

    @property
    def avg_agent_response_ms(self) -> float:
        """Average agent response latency across turns."""
        values = [
            t.agent_response_latency_ms
            for t in self._turn_metrics
            if t.agent_response_latency_ms
        ]
        return sum(values) / len(values) if values else 0.0

    @property
    def avg_rms_db(self) -> float:
        """Average RMS level across turns."""
        values = [
            t.audio_rms_db for t in self._turn_metrics if t.audio_rms_db is not None
        ]
        return sum(values) / len(values) if values else 0.0

    @property
    def avg_peak_db(self) -> float:
        """Average peak level across turns."""
        values = [
            t.audio_peak_db for t in self._turn_metrics if t.audio_peak_db is not None
        ]
        return sum(values) / len(values) if values else 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "call_id": self.call_id,
            "session_id": self.session_id,
            "environment": self.environment,
            "duration_seconds": round(self.duration_seconds, 2),
            "turn_count": self.turn_count,
            "interruption_count": self.interruption_count,
            "avg_stt_ms": round(self.avg_stt_ms, 1),
            "avg_llm_ms": round(self.avg_llm_ms, 1),
            "avg_tts_ms": round(self.avg_tts_ms, 1),
            "avg_agent_response_ms": round(self.avg_agent_response_ms, 1),
            "avg_rms_db": round(self.avg_rms_db, 1),
            "avg_peak_db": round(self.avg_peak_db, 1),
            "poor_audio_turns": self.poor_audio_turns,
            "completion_status": self.completion_status,
            "error_category": self.error_category,
        }


# =============================================================================
# Timing Context Manager
# =============================================================================


class TimingContext:
    """
    Async context manager for timing operations.

    Supports both total time and time-to-first-byte measurements.

    Usage:
        async with TimingContext(collector, "llm", record_ttfb=True) as timer:
            async for chunk in stream():
                timer.mark_first_byte()  # Call on first chunk
                yield chunk
        # Total time automatically recorded on exit
    """

    def __init__(
        self,
        collector: MetricsCollector,
        metric_name: str,
        record_total: bool = True,
        record_ttfb: bool = False,
    ):
        self.collector = collector
        self.metric_name = metric_name
        self.record_total = record_total
        self.record_ttfb = record_ttfb

        self.start_time: Optional[float] = None
        self.first_byte_time: Optional[float] = None

    async def __aenter__(self) -> TimingContext:
        self.start_time = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None and self.record_total and self.start_time is not None:
            elapsed_ms = (time.perf_counter() - self.start_time) * 1000
            self.collector._record_metric(f"{self.metric_name}_total", elapsed_ms)
        return False

    def mark_first_byte(self) -> None:
        """Mark the arrival of the first response byte."""
        if (
            self.first_byte_time is None
            and self.record_ttfb
            and self.start_time is not None
        ):
            self.first_byte_time = time.perf_counter()
            ttfb_ms = (self.first_byte_time - self.start_time) * 1000
            self.collector._record_metric(f"{self.metric_name}_ttfb", ttfb_ms)

    @property
    def elapsed_ms(self) -> float:
        """Current elapsed time in milliseconds."""
        if self.start_time is None:
            return 0.0
        return (time.perf_counter() - self.start_time) * 1000


# =============================================================================
# EMF Logger
# =============================================================================


class EMFLogger:
    """
    Emits CloudWatch Embedded Metric Format (EMF) logs.

    EMF logs are JSON with a special _aws metadata block that CloudWatch
    automatically parses to extract metrics. No CloudWatch agent required.

    Reference:
    https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
    """

    def __init__(
        self,
        namespace: str = "VoiceAgent/Pipeline",
        environment: str = "production",
    ):
        self.namespace = namespace
        self.environment = environment

    def emit_turn_metrics(
        self,
        call_id: str,
        turn: TurnMetrics,
    ) -> None:
        """Emit EMF log for turn metrics."""
        metrics_list = []
        metric_values = {}

        # Only include metrics that have values
        if turn.stt_latency_ms is not None:
            metrics_list.append({"Name": "STTLatency", "Unit": "Milliseconds"})
            metric_values["STTLatency"] = round(turn.stt_latency_ms, 1)

        if turn.llm_ttfb_ms is not None:
            metrics_list.append({"Name": "LLMTimeToFirstByte", "Unit": "Milliseconds"})
            metric_values["LLMTimeToFirstByte"] = round(turn.llm_ttfb_ms, 1)

        if turn.llm_total_ms is not None:
            metrics_list.append(
                {"Name": "LLMTotalResponseTime", "Unit": "Milliseconds"}
            )
            metric_values["LLMTotalResponseTime"] = round(turn.llm_total_ms, 1)

        if turn.tts_ttfb_ms is not None:
            metrics_list.append({"Name": "TTSTimeToFirstByte", "Unit": "Milliseconds"})
            metric_values["TTSTimeToFirstByte"] = round(turn.tts_ttfb_ms, 1)

        if turn.agent_response_latency_ms is not None:
            metrics_list.append(
                {"Name": "AgentResponseLatency", "Unit": "Milliseconds"}
            )
            metric_values["AgentResponseLatency"] = round(
                turn.agent_response_latency_ms, 1
            )

        # Audio quality metrics
        if turn.audio_rms_db is not None:
            metrics_list.append({"Name": "AudioRMS", "Unit": "None"})
            metric_values["AudioRMS"] = round(turn.audio_rms_db, 1)

        if turn.audio_peak_db is not None:
            metrics_list.append({"Name": "AudioPeak", "Unit": "None"})
            metric_values["AudioPeak"] = round(turn.audio_peak_db, 1)

        if turn.silence_duration_ms is not None:
            metrics_list.append({"Name": "SilenceDuration", "Unit": "Milliseconds"})
            metric_values["SilenceDuration"] = round(turn.silence_duration_ms, 1)

        # STT Quality metrics
        if turn.stt_confidence_avg is not None:
            metrics_list.append({"Name": "STTConfidenceAvg", "Unit": "None"})
            metric_values["STTConfidenceAvg"] = round(turn.stt_confidence_avg, 3)

        if turn.stt_confidence_min is not None:
            metrics_list.append({"Name": "STTConfidenceMin", "Unit": "None"})
            metric_values["STTConfidenceMin"] = round(turn.stt_confidence_min, 3)

        if turn.stt_word_count is not None:
            metrics_list.append({"Name": "STTWordCount", "Unit": "Count"})
            metric_values["STTWordCount"] = turn.stt_word_count

        # LLM Quality metrics
        if turn.llm_output_tokens is not None:
            metrics_list.append({"Name": "LLMOutputTokens", "Unit": "Count"})
            metric_values["LLMOutputTokens"] = turn.llm_output_tokens

        if turn.llm_tokens_per_second is not None:
            metrics_list.append({"Name": "LLMTokensPerSecond", "Unit": "Count/Second"})
            metric_values["LLMTokensPerSecond"] = round(turn.llm_tokens_per_second, 1)

        # WebRTC Quality metrics
        if turn.webrtc_rtt_ms is not None:
            metrics_list.append({"Name": "WebRTCRTT", "Unit": "Milliseconds"})
            metric_values["WebRTCRTT"] = round(turn.webrtc_rtt_ms, 1)

        if turn.webrtc_jitter_ms is not None:
            metrics_list.append({"Name": "WebRTCJitter", "Unit": "Milliseconds"})
            metric_values["WebRTCJitter"] = round(turn.webrtc_jitter_ms, 1)

        if turn.webrtc_packet_loss_percent is not None:
            metrics_list.append({"Name": "WebRTCPacketLoss", "Unit": "Percent"})
            metric_values["WebRTCPacketLoss"] = round(
                turn.webrtc_packet_loss_percent, 2
            )

        # Conversation Flow metrics
        if turn.turn_gap_ms is not None:
            metrics_list.append({"Name": "TurnGap", "Unit": "Milliseconds"})
            metric_values["TurnGap"] = round(turn.turn_gap_ms, 1)

        if turn.response_delay_ms is not None:
            metrics_list.append({"Name": "ResponseDelay", "Unit": "Milliseconds"})
            metric_values["ResponseDelay"] = round(turn.response_delay_ms, 1)

        # Composite Quality Score
        if turn.quality_score is not None:
            metrics_list.append({"Name": "QualityScore", "Unit": "None"})
            metric_values["QualityScore"] = round(turn.quality_score, 3)

        if not metrics_list:
            return  # No metrics to emit

        emf_log = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [
                            ["Environment"],
                            ["Environment", "CallId"],
                        ],
                        "Metrics": metrics_list,
                    }
                ],
            },
            "Environment": self.environment,
            "CallId": call_id,
            "TurnNumber": turn.turn_number,
            "event": "turn_metrics",
            **metric_values,
        }

        # EMF logs must be printed as single-line JSON to stdout
        print(json.dumps(emf_log), flush=True)

    def emit_call_summary(self, metrics: CallMetrics) -> None:
        """Emit EMF log for call summary."""
        emf_log = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [
                            ["Environment"],
                            ["Environment", "CompletionStatus"],
                        ],
                        "Metrics": [
                            {"Name": "CallDuration", "Unit": "Seconds"},
                            {"Name": "TurnCount", "Unit": "Count"},
                            {"Name": "InterruptionCount", "Unit": "Count"},
                            {"Name": "AvgSTTLatency", "Unit": "Milliseconds"},
                            {"Name": "AvgLLMLatency", "Unit": "Milliseconds"},
                            {"Name": "AvgTTSLatency", "Unit": "Milliseconds"},
                            {"Name": "AvgAgentResponseLatency", "Unit": "Milliseconds"},
                            {"Name": "AvgAudioRMS", "Unit": "None"},
                            {"Name": "AvgAudioPeak", "Unit": "None"},
                            {"Name": "PoorAudioTurns", "Unit": "Count"},
                        ],
                    }
                ],
            },
            "Environment": metrics.environment,
            "CallId": metrics.call_id,
            "SessionId": metrics.session_id,
            "CallDuration": round(metrics.duration_seconds, 2),
            "TurnCount": metrics.turn_count,
            "InterruptionCount": metrics.interruption_count,
            "AvgSTTLatency": round(metrics.avg_stt_ms, 1),
            "AvgLLMLatency": round(metrics.avg_llm_ms, 1),
            "AvgTTSLatency": round(metrics.avg_tts_ms, 1),
            "AvgAgentResponseLatency": round(metrics.avg_agent_response_ms, 1),
            "AvgAudioRMS": round(metrics.avg_rms_db, 1),
            "AvgAudioPeak": round(metrics.avg_peak_db, 1),
            "PoorAudioTurns": metrics.poor_audio_turns,
            "CompletionStatus": metrics.completion_status,
            "ErrorCategory": metrics.error_category,
            "event": "call_summary",
        }

        print(json.dumps(emf_log), flush=True)

    def emit_tool_metrics(
        self,
        call_id: str,
        tool_name: str,
        category: str,
        status: str,
        execution_time_ms: float,
    ) -> None:
        """
        Emit EMF log for tool execution metrics.

        Args:
            call_id: Unique call identifier
            tool_name: Tool that was executed
            category: Tool category (customer_info, system, etc.)
            status: Execution status (success, error, timeout, cancelled)
            execution_time_ms: Execution duration in milliseconds
        """
        emf_log = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [
                            ["Environment"],
                            ["Environment", "ToolName"],
                            ["Environment", "ToolCategory"],
                            ["Environment", "ToolStatus"],
                        ],
                        "Metrics": [
                            {"Name": "ToolExecutionTime", "Unit": "Milliseconds"},
                            {"Name": "ToolInvocationCount", "Unit": "Count"},
                        ],
                    }
                ],
            },
            "Environment": self.environment,
            "CallId": call_id,
            "ToolName": tool_name,
            "ToolCategory": category,
            "ToolStatus": status,
            "ToolExecutionTime": round(execution_time_ms, 1),
            "ToolInvocationCount": 1,
            "event": "tool_execution",
        }

        print(json.dumps(emf_log), flush=True)

    def emit_session_health(
        self,
        active_sessions: int,
        error_count: int = 0,
        error_category: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """
        Emit EMF log for session health metrics.

        Called on session state changes (start/end) to track active session count
        and error frequency for operational visibility.

        Args:
            active_sessions: Current number of active sessions
            error_count: Number of errors in this emission window
            error_category: Category of error (if error_count > 0)
            task_id: ECS task ID for per-task visibility
        """
        metrics_list = [
            {"Name": "ActiveSessions", "Unit": "Count"},
        ]
        metric_values = {
            "ActiveSessions": active_sessions,
        }

        # Include error count if present
        if error_count > 0:
            metrics_list.append({"Name": "ErrorCount", "Unit": "Count"})
            metric_values["ErrorCount"] = error_count

        # Build dimensions - include ErrorCategory and TaskId if present
        dimensions = [["Environment"]]
        if task_id:
            dimensions.append(["Environment", "TaskId"])
        if error_category:
            dimensions.append(["Environment", "ErrorCategory"])

        emf_log = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": dimensions,
                        "Metrics": metrics_list,
                    }
                ],
            },
            "Environment": self.environment,
            "ActiveSessions": active_sessions,
            "event": "session_health",
            **metric_values,
        }

        # Add task_id to log if present
        if task_id:
            emf_log["TaskId"] = task_id

        # Add error category to log if present
        if error_category:
            emf_log["ErrorCategory"] = error_category

        print(json.dumps(emf_log), flush=True)


# =============================================================================
# Metrics Collector
# =============================================================================


class MetricsCollector:
    """
    Collects timing metrics for voice pipeline operations.

    Thread-safe accumulator that emits EMF logs at turn and call boundaries.

    Lifecycle:
        1. Create collector at call start
        2. Use timing context managers or record_* methods during processing
        3. Call end_turn() at each turn boundary
        4. Call finalize() when call ends

    Example:
        collector = MetricsCollector(call_id, session_id)

        # During a turn:
        async with collector.time_stt():
            transcript = await stt.run(audio)

        collector.record_llm_ttfb(312.5)
        collector.record_llm_total(1240.8)
        collector.record_tts_ttfb(89.3)

        collector.end_turn(user_text=transcript, assistant_text=response)

        # At call end:
        summary = collector.finalize(status="completed")
    """

    def __init__(
        self,
        call_id: str,
        session_id: str,
        environment: Optional[str] = None,
    ):
        self.call_id = call_id
        self.session_id = session_id
        self.environment = environment or os.environ.get("ENVIRONMENT", "production")

        # EMF logger
        self._emf = EMFLogger(environment=self.environment)

        # Call-level metrics
        self._call_metrics = CallMetrics(
            call_id=call_id,
            session_id=session_id,
            environment=self.environment,
        )

        # Current turn (None until start_turn called)
        self._current_turn: Optional[TurnMetrics] = None

        # E2E timing state
        self._vad_stop_time: Optional[float] = None

        logger.debug(
            "metrics_collector_created",
            call_id=call_id,
            session_id=session_id,
            environment=self.environment,
        )

    # -------------------------------------------------------------------------
    # Timing Context Managers
    # -------------------------------------------------------------------------

    @asynccontextmanager
    async def time_stt(self):
        """Time STT operation (total time only)."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.record_stt_latency(elapsed_ms)

    @asynccontextmanager
    async def time_llm(self):
        """
        Time LLM operation with TTFB support.

        Usage:
            async with collector.time_llm() as timer:
                async for chunk in llm.stream():
                    timer.mark_first_byte()
                    yield chunk
        """
        timer = TimingContext(self, "llm", record_total=True, record_ttfb=True)
        async with timer:
            yield timer

    @asynccontextmanager
    async def time_tts(self):
        """
        Time TTS operation with TTFB support.

        Usage:
            async with collector.time_tts() as timer:
                async for chunk in tts.stream():
                    timer.mark_first_byte()
                    yield chunk
        """
        timer = TimingContext(self, "tts", record_total=True, record_ttfb=True)
        async with timer:
            yield timer

    @asynccontextmanager
    async def time_agent_response(self):
        """
        Time agent response latency (VAD stop to first audio).

        Usage:
            async with collector.time_agent_response():
                # Process turn
                pass
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.record_agent_response_latency(elapsed_ms)

    # -------------------------------------------------------------------------
    # Manual Metric Recording
    # -------------------------------------------------------------------------

    def record_stt_latency(self, latency_ms: float) -> None:
        """Record STT latency for current turn."""
        if self._current_turn:
            self._current_turn.stt_latency_ms = latency_ms
            self._call_metrics.total_stt_ms += latency_ms

    def record_llm_ttfb(self, latency_ms: float) -> None:
        """Record LLM time-to-first-byte for current turn."""
        if self._current_turn:
            self._current_turn.llm_ttfb_ms = latency_ms

    def record_llm_total(self, latency_ms: float) -> None:
        """Record LLM total response time for current turn."""
        if self._current_turn:
            self._current_turn.llm_total_ms = latency_ms
            self._call_metrics.total_llm_ms += latency_ms

    def record_tts_ttfb(self, latency_ms: float) -> None:
        """Record TTS time-to-first-byte for current turn."""
        if self._current_turn:
            self._current_turn.tts_ttfb_ms = latency_ms
            self._call_metrics.total_tts_ms += latency_ms

    def record_agent_response_latency(self, latency_ms: float) -> None:
        """Record agent response latency for current turn."""
        if self._current_turn:
            self._current_turn.agent_response_latency_ms = latency_ms

    def record_interruption(self) -> None:
        """Record a barge-in (user interrupted the bot)."""
        self._call_metrics.interruption_count += 1
        logger.debug(
            "interruption_recorded",
            interruption_count=self._call_metrics.interruption_count,
        )

    def record_audio_rms(self, rms_db: float) -> None:
        """Record audio RMS level for current turn."""
        if self._current_turn:
            self._current_turn.audio_rms_db = rms_db

    def record_audio_peak(self, peak_db: float) -> None:
        """Record audio peak amplitude for current turn."""
        if self._current_turn:
            self._current_turn.audio_peak_db = peak_db

    def record_silence_duration(self, duration_ms: float) -> None:
        """Record silence duration before user speech."""
        if self._current_turn:
            self._current_turn.silence_duration_ms = duration_ms

    def record_poor_audio_turn(self) -> None:
        """Record that the current turn had poor audio quality."""
        self._call_metrics.poor_audio_turns += 1

    def record_stt_quality(
        self,
        confidence_avg: float,
        confidence_min: float,
        interim_count: int,
        final_count: int,
        word_count: int,
    ) -> None:
        """Record STT quality metrics for current turn."""
        if self._current_turn:
            self._current_turn.stt_confidence_avg = confidence_avg
            self._current_turn.stt_confidence_min = confidence_min
            self._current_turn.stt_interim_count = interim_count
            self._current_turn.stt_final_count = final_count
            self._current_turn.stt_word_count = word_count

    def record_llm_quality(
        self,
        output_tokens: int,
        tokens_per_second: float,
    ) -> None:
        """Record LLM quality metrics for current turn."""
        if self._current_turn:
            self._current_turn.llm_output_tokens = output_tokens
            self._current_turn.llm_tokens_per_second = tokens_per_second

    def record_llm_usage(
        self,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record LLM token usage for current turn.

        Called by MetricsObserver when a LLMUsageMetricsData frame is
        seen. Separate from record_llm_quality because usage info
        arrives AFTER generation completes (tokens_per_second isn't
        computable until then), while record_llm_quality is invoked
        by code paths that already know TPS.
        """
        if self._current_turn:
            if input_tokens:
                self._current_turn.llm_input_tokens = input_tokens
            if output_tokens:
                self._current_turn.llm_output_tokens = output_tokens

    def record_webrtc_quality(
        self,
        rtt_ms: Optional[float] = None,
        jitter_ms: Optional[float] = None,
        packet_loss_percent: Optional[float] = None,
        bitrate_kbps: Optional[float] = None,
    ) -> None:
        """Record WebRTC quality metrics for current turn."""
        if self._current_turn:
            if rtt_ms is not None:
                self._current_turn.webrtc_rtt_ms = rtt_ms
            if jitter_ms is not None:
                self._current_turn.webrtc_jitter_ms = jitter_ms
            if packet_loss_percent is not None:
                self._current_turn.webrtc_packet_loss_percent = packet_loss_percent
            if bitrate_kbps is not None:
                self._current_turn.webrtc_bitrate_kbps = bitrate_kbps

    def record_turn_gap(self, gap_ms: float) -> None:
        """Record turn gap (time between bot stop and user start)."""
        if self._current_turn:
            self._current_turn.turn_gap_ms = gap_ms

    def record_user_speaking_duration(self, duration_ms: float) -> None:
        """Record user speaking duration for current turn."""
        if self._current_turn:
            self._current_turn.user_speaking_duration_ms = duration_ms

    def record_bot_speaking_duration(self, duration_ms: float) -> None:
        """Record bot speaking duration for current turn."""
        if self._current_turn:
            self._current_turn.bot_speaking_duration_ms = duration_ms

    def record_response_delay(self, delay_ms: float) -> None:
        """Record response delay (time from user stop to bot start)."""
        if self._current_turn:
            self._current_turn.response_delay_ms = delay_ms

    def record_speaking_ratio(self, user_ratio: float) -> None:
        """Record speaking ratio for the call (user speaking time / total speaking time)."""
        # This is a call-level metric, not turn-level
        logger.debug(
            "speaking_ratio_recorded",
            user_ratio=round(user_ratio, 3),
            call_id=self.call_id,
        )

    def record_avg_turn_gap(self, avg_gap_ms: float) -> None:
        """Record average turn gap for the call."""
        # This is a call-level metric, not turn-level
        logger.debug(
            "avg_turn_gap_recorded",
            avg_gap_ms=round(avg_gap_ms, 1),
            call_id=self.call_id,
        )

    def record_quality_score(self, score: float) -> None:
        """Record composite quality score for current turn."""
        if self._current_turn:
            self._current_turn.quality_score = score

    def record_tool_execution(
        self,
        tool_name: str,
        category: str,
        status: str,
        execution_time_ms: float,
    ) -> None:
        """
        Record tool execution metrics.

        Args:
            tool_name: Tool that was executed
            category: Tool category (customer_info, system, etc.)
            status: Execution status (success, error, timeout, cancelled)
            execution_time_ms: Execution duration in milliseconds
        """
        # Emit EMF metrics immediately
        self._emf.emit_tool_metrics(
            call_id=self.call_id,
            tool_name=tool_name,
            category=category,
            status=status,
            execution_time_ms=execution_time_ms,
        )

        # Log for structured logging
        logger.info(
            "tool_execution",
            call_id=self.call_id,
            session_id=self.session_id,
            turn_number=self.turn_count,
            tool_name=tool_name,
            category=category,
            status=status,
            execution_time_ms=round(execution_time_ms, 1),
        )

    def mark_vad_stop(self) -> None:
        """Mark VAD stop time for E2E latency calculation."""
        self._vad_stop_time = time.perf_counter()
        if self._current_turn:
            self._current_turn.vad_stop_time = self._vad_stop_time

    def mark_first_audio(self) -> None:
        """Mark first audio output time and calculate agent response latency."""
        if self._vad_stop_time is not None:
            first_audio_time = time.perf_counter()
            agent_response_ms = (first_audio_time - self._vad_stop_time) * 1000
            self.record_agent_response_latency(agent_response_ms)
            if self._current_turn:
                self._current_turn.first_audio_time = first_audio_time
            # Reset for next turn
            self._vad_stop_time = None

    def _record_metric(self, metric_name: str, value: float) -> None:
        """Internal method for TimingContext to record metrics."""
        if metric_name == "stt_total":
            self.record_stt_latency(value)
        elif metric_name == "llm_ttfb":
            self.record_llm_ttfb(value)
        elif metric_name == "llm_total":
            self.record_llm_total(value)
        elif metric_name == "tts_ttfb":
            self.record_tts_ttfb(value)
        elif metric_name == "tts_total":
            # TTS total not tracked separately (TTFB is primary metric)
            pass
        elif metric_name == "agent_response_total":
            self.record_agent_response_latency(value)

    # -------------------------------------------------------------------------
    # Turn Lifecycle
    # -------------------------------------------------------------------------

    def start_turn(self) -> None:
        """Start a new conversation turn."""
        self._call_metrics.turn_count += 1
        self._current_turn = TurnMetrics(
            turn_number=self._call_metrics.turn_count,
        )
        logger.debug(
            "turn_started",
            turn_number=self._current_turn.turn_number,
        )

    def end_turn(
        self,
        user_text: str = "",
        assistant_text: str = "",
    ) -> None:
        """
        End the current turn and emit metrics.

        Args:
            user_text: User's transcribed speech (optional)
            assistant_text: Assistant's response (optional)
        """
        if self._current_turn is None:
            logger.warning("end_turn_called_without_start")
            return

        # Store conversation content
        self._current_turn.user_text = user_text
        self._current_turn.assistant_text = assistant_text

        # Add to history
        self._call_metrics._turn_metrics.append(self._current_turn)

        # Emit turn metrics via EMF
        self._emf.emit_turn_metrics(self.call_id, self._current_turn)

        # Log turn summary
        logger.info(
            "turn_completed",
            **self._current_turn.to_dict(),
        )

        # Reset current turn
        self._current_turn = None

    # -------------------------------------------------------------------------
    # Call Lifecycle
    # -------------------------------------------------------------------------

    def finalize(
        self,
        status: str = "completed",
        error_category: Optional[str] = None,
    ) -> CallMetrics:
        """
        Finalize call metrics and emit summary.

        Args:
            status: Completion status (completed, cancelled, error)
            error_category: Error category if status is error

        Returns:
            Final CallMetrics object
        """
        # Handle any incomplete turn
        if self._current_turn is not None:
            self.end_turn()

        # Update status
        self._call_metrics.completion_status = status
        self._call_metrics.error_category = error_category

        # Emit call summary via EMF
        self._emf.emit_call_summary(self._call_metrics)

        # Log call summary (also captured by structlog)
        logger.info(
            "call_metrics_summary",
            **self._call_metrics.to_dict(),
        )

        return self._call_metrics

    # -------------------------------------------------------------------------
    # Accessors
    # -------------------------------------------------------------------------

    @property
    def turn_count(self) -> int:
        """Current turn count."""
        return self._call_metrics.turn_count

    @property
    def current_turn(self) -> Optional[TurnMetrics]:
        """Current turn metrics (None if between turns)."""
        return self._current_turn

    @property
    def call_metrics(self) -> CallMetrics:
        """Call-level metrics."""
        return self._call_metrics


# =============================================================================
# Convenience Functions
# =============================================================================


def create_metrics_collector(
    call_id: str,
    session_id: str,
    environment: Optional[str] = None,
) -> MetricsCollector:
    """
    Factory function to create a MetricsCollector.

    Args:
        call_id: Unique call identifier
        session_id: Session identifier
        environment: Environment name (defaults to ENVIRONMENT env var)

    Returns:
        Configured MetricsCollector instance
    """
    return MetricsCollector(
        call_id=call_id,
        session_id=session_id,
        environment=environment,
    )
