"""Regression tests for GitHub issue #29: "Agent re-speaks the tail of its
previous turn on every turn".

Root cause: DeepgramSageMakerTTSService.run_tts() used to yield its own
TTSStartedFrame/TTSStoppedFrame pair on EVERY call (i.e. once per sentence),
instead of once per LLM turn. Pipecat's TTSService/BaseOutputTransport/
RTVIObserver stack treats every TTSStoppedFrame as "bot stopped speaking".
When a turn has 2+ sentences, the mid-turn TTSStoppedFrame (emitted after the
first sentence) makes RTVIObserver queue the *next* sentence's output instead
of reporting it immediately -- RTVIObserver only flushes its queue on the
*next* "bot started speaking" event. For the last sentence of a turn, that
next "started speaking" doesn't happen until the following turn's first
audio chunk, so that sentence's report (and audio) surfaces at the start of
the next turn, sounding like the agent repeating itself.

The fix makes the service participate in Pipecat's per-turn audio-context
lifecycle (push_start_frame/push_stop_frames + create_audio_context/
append_to_audio_context/remove_audio_context) instead of framing each
sentence itself: run_tts() only sends "Speak"; flush_audio() (called once
per turn by the base class) sends "Flush"; and the response-processor task
appends audio directly to the turn's shared audio context, closing it only
when Deepgram's "Flushed" acknowledgement arrives.

These tests exercise the turn-boundary drain/flush logic directly against a
fake SageMaker BiDi client (no live network), proving:

1. A turn with multiple sentences shares ONE audio context/Started+Stopped
   pair (this is what the bug got wrong).
2. Audio from a turn cannot leak into the next turn's frames.
3. "Flush" is sent once per turn (via flush_audio()), not once per sentence.
4. Interruption (on_audio_context_interrupted) still sends "Clear" and does
   not leave audio_context bookkeeping stuck.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResult:
    """Mimics the `.value.bytes_` shape returned by SageMakerBidiClient."""

    def __init__(self, value):
        self.value = value


class _FakePayload:
    def __init__(self, bytes_):
        self.bytes_ = bytes_


class FakeBidiClient:
    """Minimal fake of SageMakerBidiClient's async BiDi protocol.

    Each "Speak" message schedules delivery of one audio chunk (the text,
    encoded) after a short delay, to simulate real network/model latency.
    "Flush" messages are recorded but do not themselves emit a "Flushed"
    acknowledgement -- callers can trigger that via `deliver_flushed()`,
    which lets tests control exactly when a turn's context closes.
    """

    def __init__(self, auto_flush: bool = True, flush_delay: float = 0.02):
        self.is_active = True
        self.sent = []
        self.auto_flush = auto_flush
        self.flush_delay = flush_delay
        self._response_queue: asyncio.Queue = asyncio.Queue()
        self._flush_count = 0

    async def start_session(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)
        if data.get("type") == "Speak":
            text = data["text"]

            async def deliver_audio(text=text):
                await asyncio.sleep(0.01)
                await self._response_queue.put(_FakeResult(_FakePayload(text.encode())))

            asyncio.create_task(deliver_audio())
        elif data.get("type") == "Flush":
            self._flush_count += 1
            if self.auto_flush:
                asyncio.create_task(self.deliver_flushed())

    async def deliver_flushed(self):
        await asyncio.sleep(self.flush_delay)
        await self._response_queue.put(
            _FakeResult(_FakePayload(json.dumps({"type": "Flushed"}).encode()))
        )

    async def receive_response(self):
        return await self._response_queue.get()

    async def close_session(self):
        self.is_active = False

    @property
    def flush_count(self) -> int:
        return self._flush_count


def make_tts(**kwargs):
    """Create a DeepgramSageMakerTTSService with the real SageMakerBidiClient
    import patched out (no network dependency)."""
    with patch("app.services.deepgram_sagemaker_tts.SageMakerBidiClient"):
        from app.services.deepgram_sagemaker_tts import DeepgramSageMakerTTSService

        return DeepgramSageMakerTTSService(
            endpoint_name="test-endpoint",
            region="us-east-1",
            sample_rate=8000,
            **kwargs,
        )


async def attach_fake_client(tts, fake_client: FakeBidiClient):
    """Wire a fake client directly, bypassing _connect()'s real SageMaker
    session negotiation, and start the response-processor task.

    Assumes the base class's audio-context serialization machinery has
    already been initialized (e.g. via `bootstrap_tts_in_pipeline()`, which
    runs a real StartFrame through the service the way Pipecat normally
    would) so create_audio_context()/append_to_audio_context() and
    self.create_task()/self.cancel_task() work correctly.
    """
    tts._client = fake_client
    tts._response_task = tts.create_task(tts._process_responses())


async def bootstrap_tts_in_pipeline(tts):
    """Run a minimal real Pipecat pipeline containing only `tts` long enough
    for StartFrame to be processed, then leave the pipeline task running in
    the background so self.create_task()/create_audio_context() etc. keep
    working for direct API calls in the test body.

    Overrides `tts._connect`/`tts._disconnect` to no-ops first: the base
    class's `start()` calls `_connect()` on StartFrame, which would
    otherwise try to negotiate a real SageMaker BiDi session. Tests attach
    their own FakeBidiClient afterwards via `attach_fake_client()`.

    Returns the background asyncio.Task running the pipeline; the caller is
    responsible for queuing an EndFrame and awaiting it (or cancelling it)
    at the end of the test.
    """
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineTask, PipelineParams
    from pipecat.pipeline.runner import PipelineRunner

    tts._connect = AsyncMock()
    tts._disconnect = AsyncMock()

    pipeline = Pipeline([tts])
    task = PipelineTask(pipeline, params=PipelineParams(), cancel_on_idle_timeout=False)
    runner = PipelineRunner()
    run_task = asyncio.create_task(runner.run(task))
    # Give StartFrame a moment to propagate through the (trivial) pipeline.
    await asyncio.sleep(0.05)
    return task, run_task


async def drain(tts, seconds: float = 0.15):
    """Let pending async tasks (audio delivery, response processing) run."""
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Base class wiring: TTSService should own Started/Stopped framing.
# ---------------------------------------------------------------------------


class TestBaseClassOwnsFraming:
    """Guards against regressing back to per-sentence Started/Stopped
    frames yielded directly from run_tts()."""

    def test_push_start_and_stop_frames_enabled(self):
        """The service must delegate Started/Stopped framing to the base
        TTSService via push_start_frame/push_stop_frames, rather than
        yielding TTSStartedFrame/TTSStoppedFrame itself per sentence."""
        tts = make_tts()
        assert tts._push_start_frame is True
        assert tts._push_stop_frames is True

    @pytest.mark.asyncio
    async def test_run_tts_does_not_yield_started_or_stopped_frames(self):
        """run_tts() must not yield TTSStartedFrame/TTSStoppedFrame itself;
        that framing belongs to the base class, applied once per turn."""
        from pipecat.frames.frames import EndFrame, TTSStartedFrame, TTSStoppedFrame

        tts = make_tts()
        pipeline_task, run_task = await bootstrap_tts_in_pipeline(tts)
        fake_client = FakeBidiClient()
        await attach_fake_client(tts, fake_client)

        frames = []
        async for frame in tts.run_tts("Hello there.", "ctx-1"):
            frames.append(frame)

        assert not any(
            isinstance(f, (TTSStartedFrame, TTSStoppedFrame)) for f in frames
        )

        await pipeline_task.queue_frame(EndFrame())
        await run_task


# ---------------------------------------------------------------------------
# Turn-boundary flush/drain logic (direct, no full pipeline).
# ---------------------------------------------------------------------------


class TestTurnBoundaryFlushDrain:
    """Exercises run_tts() / flush_audio() / _process_responses() directly,
    proving audio and control messages are scoped to the whole turn rather
    than to individual sentences."""

    @pytest.mark.asyncio
    async def test_flush_sent_once_per_turn_not_per_sentence(self):
        """flush_audio() should be the only thing that sends 'Flush'.
        Multiple sentences in one turn must not each trigger a Flush."""
        from pipecat.frames.frames import EndFrame

        tts = make_tts()
        pipeline_task, run_task = await bootstrap_tts_in_pipeline(tts)
        fake_client = FakeBidiClient(auto_flush=False)
        await attach_fake_client(tts, fake_client)

        context_id = "turn-1-ctx"

        # Simulate the base class creating one context for the turn and
        # routing two sentences into run_tts() under that same context_id.
        await tts.create_audio_context(context_id)
        tts._playing_context_id = context_id  # get_active_audio_context_id()

        async for _ in tts.run_tts("Hello there.", context_id):
            pass
        async for _ in tts.run_tts("How can I help you today?", context_id):
            pass

        flush_messages = [m for m in fake_client.sent if m.get("type") == "Flush"]
        assert (
            len(flush_messages) == 0
        ), "run_tts() must not send Flush per sentence; only flush_audio() should"

        # Now simulate the turn ending (base class calls flush_audio() once).
        await tts.flush_audio(context_id)
        await drain(tts)

        flush_messages = [m for m in fake_client.sent if m.get("type") == "Flush"]
        assert (
            len(flush_messages) == 1
        ), f"Expected exactly one Flush for the whole turn, got {len(flush_messages)}"

        await pipeline_task.queue_frame(EndFrame())
        await run_task

    @pytest.mark.asyncio
    async def test_speak_sent_once_per_sentence(self):
        """Unlike Flush, each sentence's text must still be sent to Deepgram
        individually via 'Speak' -- only the framing/flush is per-turn."""
        from pipecat.frames.frames import EndFrame

        tts = make_tts()
        pipeline_task, run_task = await bootstrap_tts_in_pipeline(tts)
        fake_client = FakeBidiClient(auto_flush=False)
        await attach_fake_client(tts, fake_client)

        context_id = "turn-1-ctx"
        await tts.create_audio_context(context_id)
        tts._playing_context_id = context_id

        async for _ in tts.run_tts("Hello there.", context_id):
            pass
        async for _ in tts.run_tts("How can I help you today?", context_id):
            pass

        speak_messages = [m for m in fake_client.sent if m.get("type") == "Speak"]
        assert [m["text"] for m in speak_messages] == [
            "Hello there.",
            "How can I help you today?",
        ]

        await pipeline_task.queue_frame(EndFrame())
        await run_task

    @pytest.mark.asyncio
    async def test_flushed_ack_closes_only_the_active_context(self):
        """The 'Flushed' acknowledgement must close exactly the turn's audio
        context (via get_active_audio_context_id()), and a turn's context
        must not still be open once Flushed has been received -- proving
        the context lifecycle is scoped to a whole turn, not a sentence.
        """
        from pipecat.frames.frames import EndFrame

        tts = make_tts()
        pipeline_task, run_task = await bootstrap_tts_in_pipeline(tts)
        try:
            fake_client = FakeBidiClient(auto_flush=False)
            await attach_fake_client(tts, fake_client)

            turn1_ctx = "turn-1-ctx"
            await tts.create_audio_context(turn1_ctx)

            async for _ in tts.run_tts("Hello there.", turn1_ctx):
                pass
            async for _ in tts.run_tts("How can I help you today?", turn1_ctx):
                pass
            await drain(tts)  # let both audio chunks be appended

            assert tts.audio_context_available(turn1_ctx), (
                "Context must still be open -- no Flushed received yet, even "
                "though the first sentence's audio has already arrived"
            )

            # Turn boundary: base class calls flush_audio(), Deepgram eventually
            # acks with Flushed -- this is the ONLY thing that should close
            # the turn's context, not the first sentence's audio finishing.
            await tts.flush_audio(turn1_ctx)
            await fake_client.deliver_flushed()
            await drain(tts)

            assert not tts.audio_context_available(
                turn1_ctx
            ), "Flushed ack must close the turn's audio context"

            # Turn 2 begins with a brand new context; must not error or
            # reuse turn 1's (now-closed) context.
            turn2_ctx = "turn-2-ctx"
            await tts.create_audio_context(turn2_ctx)
            async for _ in tts.run_tts("Great question.", turn2_ctx):
                pass
            await drain(tts)

            assert tts.audio_context_available(turn2_ctx)

            await tts.flush_audio(turn2_ctx)
            await fake_client.deliver_flushed()
            await drain(tts)
            assert not tts.audio_context_available(turn2_ctx)
        finally:
            await pipeline_task.queue_frame(EndFrame())
            await asyncio.wait_for(run_task, timeout=5)

    @pytest.mark.asyncio
    async def test_interruption_sends_clear_and_supers_cleanup(self):
        """on_audio_context_interrupted() must send Clear to Deepgram and
        still call the base-class cleanup (super()) so audio-context state
        doesn't leak across the interruption."""
        from pipecat.frames.frames import EndFrame

        tts = make_tts()
        pipeline_task, run_task = await bootstrap_tts_in_pipeline(tts)
        fake_client = FakeBidiClient(auto_flush=False)
        await attach_fake_client(tts, fake_client)

        context_id = "turn-1-ctx"
        await tts.create_audio_context(context_id)
        tts._playing_context_id = context_id

        with patch(
            "pipecat.services.tts_service.TTSService.on_audio_context_interrupted",
            new=AsyncMock(),
        ) as super_mock:
            await tts.on_audio_context_interrupted(context_id)

        clear_messages = [m for m in fake_client.sent if m.get("type") == "Clear"]
        assert len(clear_messages) == 1
        super_mock.assert_awaited_once_with(context_id)

        await pipeline_task.queue_frame(EndFrame())
        await run_task


# ---------------------------------------------------------------------------
# Full-pipeline regression test: proves the exact reported symptom is fixed.
# ---------------------------------------------------------------------------


class TestFullPipelineTurnBoundary:
    """Drives the real Pipecat TTSService/Pipeline/PipelineTask machinery
    with a fake BiDi client across two consecutive turns, the same way the
    reporter's local-browser-mode setup would. This is the most faithful
    reproduction of the reported bug and its fix available without live
    SageMaker STT/TTS."""

    @pytest.mark.asyncio
    async def test_two_sentence_turn_emits_one_started_stopped_pair(self):
        """BUG (before fix): a 2-sentence turn emitted TWO
        TTSStartedFrame/TTSStoppedFrame pairs (one per sentence), which is
        what caused RTVIObserver to defer the last sentence's report to the
        next turn. FIX: exactly one pair per turn, regardless of sentence
        count.
        """
        from pipecat.frames.frames import (
            EndFrame,
            LLMFullResponseStartFrame,
            LLMFullResponseEndFrame,
            TextFrame,
            TTSStartedFrame,
            TTSStoppedFrame,
        )
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.task import PipelineTask, PipelineParams
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

        class EventCounter(FrameProcessor):
            def __init__(self):
                super().__init__()
                self.events = []

            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if direction == FrameDirection.DOWNSTREAM and isinstance(
                    frame, (TTSStartedFrame, TTSStoppedFrame)
                ):
                    self.events.append(type(frame).__name__)
                await self.push_frame(frame, direction)

        tts = make_tts()
        counter = EventCounter()
        pipeline = Pipeline([tts, counter])
        task = PipelineTask(
            pipeline, params=PipelineParams(), cancel_on_idle_timeout=False
        )

        fake_client = FakeBidiClient()

        async def fake_connect():
            tts._client = fake_client
            tts._response_task = tts.create_task(tts._process_responses())

        tts._connect = fake_connect
        tts._disconnect = AsyncMock()

        async def driver():
            await asyncio.sleep(0.05)
            await task.queue_frame(LLMFullResponseStartFrame())
            # Trailing space so the sentence aggregator's NLTK lookahead
            # confirms "Hello there." as a complete sentence boundary.
            await task.queue_frame(TextFrame("Hello there. "))
            await task.queue_frame(TextFrame("How can I help you today?"))
            await task.queue_frame(LLMFullResponseEndFrame())
            await asyncio.sleep(0.4)
            await task.queue_frame(EndFrame())

        runner = PipelineRunner()
        await asyncio.gather(runner.run(task), driver())

        assert counter.events.count("TTSStartedFrame") == 1, (
            f"Expected exactly 1 TTSStartedFrame for a 2-sentence turn, "
            f"got {counter.events.count('TTSStartedFrame')}. Events: {counter.events}"
        )
        assert counter.events.count("TTSStoppedFrame") == 1, (
            f"Expected exactly 1 TTSStoppedFrame for a 2-sentence turn, "
            f"got {counter.events.count('TTSStoppedFrame')}. Events: {counter.events}"
        )

    @pytest.mark.asyncio
    async def test_rtvi_observer_mechanism_matches_fix(self):
        """Directly demonstrates the RTVIObserver mechanism this bug
        exploited, using the real RTVIObserver against the two frame
        patterns at issue:

        - BUGGY pattern: TTSStartedFrame/TTSStoppedFrame once per SENTENCE
          (what the old run_tts() implementation produced -- 2 pairs for a
          2-sentence turn).
        - FIXED pattern: TTSStartedFrame/TTSStoppedFrame once per TURN (what
          the fixed service produces regardless of sentence count -- proven
          directly against the real service in
          test_two_sentence_turn_emits_one_started_stopped_pair above).

        RTVIObserver only flushes queued AggregatedTextFrame/TTSTextFrame on
        the next BotStartedSpeakingFrame. Under the buggy pattern, the
        mid-turn TTSStoppedFrame causes a premature "bot stopped speaking",
        so the second sentence's TTSTextFrame (arriving after that) gets
        queued and isn't flushed until turn 2's BotStartedSpeakingFrame --
        reproducing the exact symptom from the issue. Under the fixed
        pattern, both sentences arrive while the bot is still marked as
        speaking, so both are reported within turn 1.
        """
        from pipecat.frames.frames import (
            AggregationType,
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            TTSTextFrame,
        )
        from pipecat.observers.base_observer import FramePushed
        from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIObserverParams
        from pipecat.transports.base_output import BaseOutputTransport

        # Identity-only stand-in flagged as a BaseOutputTransport instance,
        # matching how RTVIObserver requires TTSTextFrame's *source* to be a
        # transport before treating it as ready-to-report (see
        # RTVIObserver.on_push_frame()'s `isinstance(src, BaseOutputTransport)`
        # check) -- this is exactly what a real transport's re-push of a
        # sync frame through its MediaSender looks like from the observer's
        # point of view.
        transport_source = BaseOutputTransport.__new__(BaseOutputTransport)

        async def run_observer(frames_per_sentence_boundary: bool) -> list:
            rtvi = RTVIProcessor()
            observer = rtvi.create_rtvi_observer(params=RTVIObserverParams())
            reported = []

            async def spy(frame):
                reported.append(frame.text)

            observer._send_aggregated_llm_text = spy

            async def push(frame):
                await observer.on_push_frame(
                    FramePushed(
                        source=transport_source,
                        destination=None,
                        frame=frame,
                        direction=None,
                        timestamp=0,
                    )
                )

            sentence1 = TTSTextFrame(
                "Hello there.", aggregated_by=AggregationType.SENTENCE
            )
            sentence2 = TTSTextFrame(
                "How can I help you today?", aggregated_by=AggregationType.SENTENCE
            )
            sentence3 = TTSTextFrame(
                "Great question.", aggregated_by=AggregationType.SENTENCE
            )

            if frames_per_sentence_boundary:
                # BUGGY pattern: Started/Stopped bracket EACH sentence.
                await push(BotStartedSpeakingFrame())
                await push(sentence1)
                await push(BotStoppedSpeakingFrame())  # mid-turn (the bug)

                await push(BotStartedSpeakingFrame())
                await push(BotStoppedSpeakingFrame())  # stopped before text arrives
                await push(sentence2)  # queued: bot not speaking right now
            else:
                # FIXED pattern: Started/Stopped bracket the WHOLE turn.
                await push(BotStartedSpeakingFrame())
                await push(sentence1)
                await push(sentence2)
                await push(BotStoppedSpeakingFrame())  # only at end of turn

            reported_after_turn1 = list(reported)

            # Turn 2 begins.
            await push(BotStartedSpeakingFrame())
            await push(sentence3)
            await push(BotStoppedSpeakingFrame())

            return reported_after_turn1, list(reported)

        # BUG reproduction: confirms our understanding of *why* the issue
        # happened, using the buggy per-sentence Started/Stopped pattern.
        buggy_after_turn1, buggy_total = await run_observer(
            frames_per_sentence_boundary=True
        )
        assert buggy_after_turn1 == ["Hello there."], (
            "Under the buggy per-sentence framing, only the FIRST sentence "
            f"should be reported before turn 2 starts; got {buggy_after_turn1}"
        )
        assert buggy_total == [
            "Hello there.",
            "How can I help you today?",
            "Great question.",
        ], (
            "The second sentence must still show up eventually, but only "
            f"once turn 2 starts (the bug); got {buggy_total}"
        )

        # FIX verification: with per-turn framing, both sentences of turn 1
        # are reported before turn 2 starts.
        fixed_after_turn1, fixed_total = await run_observer(
            frames_per_sentence_boundary=False
        )
        assert fixed_after_turn1 == ["Hello there.", "How can I help you today?"], (
            "Under per-turn framing (the fix), BOTH sentences of turn 1 must "
            f"be reported before turn 2 starts; got {fixed_after_turn1}"
        )
        assert fixed_total == [
            "Hello there.",
            "How can I help you today?",
            "Great question.",
        ]
