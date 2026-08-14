"""
Deepgram text-to-speech service for AWS SageMaker.

This module provides a Pipecat TTS service that connects to Deepgram Aura models
deployed on AWS SageMaker endpoints. Uses HTTP/2 bidirectional streaming for
low-latency real-time speech synthesis.

Deepgram TTS Protocol (via SageMaker BiDi):
- Send text: {"type": "Speak", "text": "..."}
- Flush (force generation): {"type": "Flush"}
- Clear buffer (interruption): {"type": "Clear"}
- Close connection: {"type": "Close"}
- Receive: binary audio chunks (linear16/mulaw/alaw)

Turn vs. sentence boundaries:
Pipecat's TTSService base class aggregates LLM output into per-sentence
AggregatedTextFrames but expects a *single* audio context (bracketed by one
TTSStartedFrame/TTSStoppedFrame pair) per LLM turn, not per sentence.
Sentences within the same turn share one context_id (see
TTSService.create_context_id() / _reuse_context_id_within_turn) and are
routed through create_audio_context()/append_to_audio_context() so the
transport only reports "bot started/stopped speaking" once per turn.

This service participates in that per-turn audio-context lifecycle instead
of yielding its own ad hoc TTSStartedFrame/TTSStoppedFrame from run_tts() on
every call (i.e. per sentence). run_tts() only sends the "Speak" message;
the base class calls flush_audio() (which sends "Flush") once per turn, when
the LLM response ends. Audio bytes and the terminal TTSStoppedFrame arrive
asynchronously on the response-processor task and are appended directly to
the turn's audio context via append_to_audio_context(), matching the pattern
used by Pipecat's own DeepgramSageMakerTTSService
(pipecat.services.deepgram.sagemaker.tts). Emitting Started/Stopped per
sentence instead of per turn was the root cause of
https://github.com/aws-solutions-library-samples/sample-voice-agent/issues/29:
each mid-turn TTSStoppedFrame made the transport fire "bot stopped speaking",
which made RTVIObserver queue the *next* AggregatedTextFrame/TTSTextFrame
instead of reporting it immediately (it only flushes queued text on the next
"bot started speaking"). For the last sentence of a turn, that next
"started speaking" doesn't happen until the following turn's first audio
chunk, so the report for that sentence -- and, once the frame reaches the
transport, its audio -- surfaces at the start of the next turn.

Reference:
- Deepgram TTS WebSocket docs: https://developers.deepgram.com/docs/tts-websocket
- Pipecat DeepgramSageMakerSTTService: pipecat.services.deepgram.stt_sagemaker
"""

import asyncio
import json
from typing import AsyncGenerator, Optional

import structlog

logger = structlog.get_logger(__name__)

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.services.tts_service import TTSService

try:
    from pipecat.services.aws.sagemaker.bidi_client import SageMakerBidiClient
except ModuleNotFoundError as e:
    logger.error("sagemaker_module_missing", error=str(e))
    logger.error(
        "sagemaker_install_required",
        hint="pip install pipecat-ai[sagemaker], requires Python >= 3.12",
    )
    raise Exception(f"Missing module: {e}")


class DeepgramSageMakerTTSService(TTSService):
    """Deepgram text-to-speech service for AWS SageMaker.

    Provides real-time speech synthesis using Deepgram Aura models deployed on
    AWS SageMaker endpoints. Uses HTTP/2 bidirectional streaming for low-latency
    audio generation with streaming output.

    Requirements:

    - AWS credentials configured (via environment variables, AWS CLI, or instance metadata)
    - A deployed SageMaker endpoint with Deepgram Aura TTS model
    - Python >= 3.12 (for aws_sdk_sagemaker_runtime_http2)

    Example::

        tts = DeepgramSageMakerTTSService(
            endpoint_name="my-deepgram-tts-endpoint",
            region="us-east-2",
            voice="aura-2-thalia-en",
            sample_rate=8000,
            encoding="linear16",
        )
    """

    def __init__(
        self,
        *,
        endpoint_name: str,
        region: str,
        voice: str = "aura-2-thalia-en",
        sample_rate: int = 8000,
        encoding: str = "linear16",
        **kwargs,
    ):
        """Initialize the Deepgram SageMaker TTS service.

        Args:
            endpoint_name: Name of the SageMaker endpoint with Deepgram Aura model.
            region: AWS region where the endpoint is deployed.
            voice: Deepgram Aura voice name (e.g., "aura-2-thalia-en").
            sample_rate: Output audio sample rate in Hz (default: 8000 for PSTN).
            encoding: Audio encoding format ("linear16", "mulaw", "alaw").
            **kwargs: Additional arguments passed to the parent TTSService.
        """
        super().__init__(
            sample_rate=sample_rate,
            # Let the base class own the audio-context lifecycle: it will
            # create the context and yield TTSStartedFrame on the first
            # sentence of a turn (push_start_frame) and emit TTSStoppedFrame
            # when the context is closed (push_stop_frames), rather than us
            # yielding those frames per sentence from run_tts().
            push_start_frame=True,
            push_stop_frames=True,
            **kwargs,
        )

        self._endpoint_name = endpoint_name
        self._region = region
        self._voice = voice
        self._sample_rate = sample_rate
        self._encoding = encoding

        self._client: Optional[SageMakerBidiClient] = None
        self._response_task: Optional[asyncio.Task] = None

        # Turn-boundary bookkeeping (see module docstring for full rationale).
        # All sentences in one LLM turn share the same audio context
        # (context_id), so completion of that context is driven by Deepgram's
        # "Flushed" acknowledgement to our flush_audio() call at the end of
        # the turn -- not by any individual sentence's audio finishing.
        self._active_context_id: Optional[str] = None

        # Pipecat 0.0.108 replaced the synchronous set_model_name() with an
        # async set_model(), which can't be awaited from __init__. Assign the
        # setting directly and sync metrics, matching how Pipecat's own
        # DeepgramTTSService seeds the model in its constructor.
        self._settings.model = voice
        self._sync_model_name_to_metrics()

    def can_generate_metrics(self) -> bool:
        """Check if this service can generate processing metrics."""
        return True

    async def set_voice(self, voice: str):
        """Set the Deepgram Aura voice.

        Note: Voice changes take effect on the next connection. If a session is
        active, it will need to be disconnected and reconnected.

        Async to match TTSService.set_voice() in the base class, which this
        previously shadowed with a sync method.

        Args:
            voice: Deepgram Aura voice name (e.g., "aura-2-thalia-en").
        """
        logger.info("tts_voice_switching", voice=voice)
        self._voice = voice
        await self.set_model(voice)  # set_model_name removed in pipecat 0.0.108

    async def start(self, frame: StartFrame):
        """Start the Deepgram SageMaker TTS service."""
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        """Stop the Deepgram SageMaker TTS service."""
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        """Cancel the Deepgram SageMaker TTS service."""
        await super().cancel(frame)
        await self._disconnect()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Send text to Deepgram for synthesis within the current turn's audio context.

        Unlike a one-shot HTTP TTS call, this only sends the "Speak" message.
        Deepgram accumulates text across all sentences of a turn in its own
        buffer; we do not force generation here so the resulting audio keeps
        flowing into the same per-turn audio context (identified by
        context_id, shared across sentences via
        TTSService.create_context_id()). Generation is triggered once per
        turn by flush_audio(), which the base class calls after the last
        sentence of an LLM response (see
        TTSService.on_turn_context_completed()). Audio bytes and the
        terminal TTSStoppedFrame arrive asynchronously via
        _process_responses(), which appends them directly to this context.

        Args:
            text: Text to synthesize.
            context_id: TTS context ID shared by all sentences in this turn
                (Pipecat v0.0.102+).

        Yields:
            Frame: Nothing on the success path (audio arrives out-of-band via
            the response processor); an ErrorFrame if the client is not
            connected or the Speak message could not be sent.
        """
        if not text.strip():
            return

        if not self._client or not self._client.is_active:
            logger.warning("tts_client_not_connected", action="attempting_reconnect")
            await self._connect()
            if not self._client or not self._client.is_active:
                logger.error("tts_reconnect_failed")
                yield ErrorFrame(error="TTS SageMaker client not connected")
                return

        logger.debug("tts_synthesizing", text_preview=text[:80])

        self._active_context_id = context_id

        try:
            await self._client.send_json({"type": "Speak", "text": text})
        except Exception as e:
            logger.error(
                "tts_synthesis_error",
                error=str(e),
                error_type=type(e).__name__,
                text_length=len(text),
                endpoint_name=self._endpoint_name,
            )
            yield ErrorFrame(error=f"TTS synthesis failed: {e}")

    async def flush_audio(self, context_id: Optional[str] = None):
        """Trigger audio generation for all text sent so far in this turn.

        Called once per LLM turn by the base class (TTSService), after the
        last sentence has been sent to run_tts() -- via
        on_turn_context_completed(), which fires on LLMFullResponseEndFrame
        or a standalone TTSSpeakFrame. Sends the Deepgram "Flush" command so
        all buffered text is generated as audio. The corresponding "Flushed"
        acknowledgement (handled in _process_responses()) is what closes the
        turn's audio context -- not any individual sentence's audio
        finishing -- which is what keeps one turn's Started/Stopped frame
        pair spanning all of its sentences.
        """
        if not self._client or not self._client.is_active:
            return

        try:
            await self._client.send_json({"type": "Flush"})
        except Exception as e:
            logger.error("tts_flush_failed", error=str(e))

    async def _connect(self):
        """Connect to the SageMaker endpoint and start the BiDi session."""
        logger.debug("tts_sagemaker_connecting")

        # Build query string for Deepgram TTS
        query_params = {
            "model": self._voice,
            "encoding": self._encoding,
            "sample_rate": str(self._sample_rate),
            "container": "none",
        }
        query_string = "&".join(f"{k}={v}" for k, v in query_params.items())

        # Create BiDi client
        self._client = SageMakerBidiClient(
            endpoint_name=self._endpoint_name,
            region=self._region,
            model_invocation_path="v1/speak",
            model_query_string=query_string,
        )

        try:
            await asyncio.wait_for(self._client.start_session(), timeout=30.0)

            # Start processing responses in the background
            self._response_task = self.create_task(self._process_responses())

            logger.info("tts_sagemaker_connected")

        except asyncio.TimeoutError:
            logger.error(
                "tts_sagemaker_connection_timeout",
                endpoint_name=self._endpoint_name,
                bidi_endpoint=self._client.bidi_endpoint,
                hint="Check security group rules allow port 8443 from ECS tasks to VPC endpoint",
            )
            await self.push_error(
                error_msg="SageMaker TTS connection timed out (port 8443 may be blocked)"
            )
        except Exception as e:
            logger.error(
                "tts_sagemaker_connection_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            await self.push_error(
                error_msg=f"SageMaker TTS connection failed: {e}", exception=e
            )

    async def _disconnect(self):
        """Disconnect from the SageMaker endpoint.

        Uses a graceful shutdown sequence to avoid InvalidStateError from the
        AWS CRT library. The CRT's native HTTP/2 layer has pending callbacks
        (_on_body, _on_complete) that fire after Python-side futures are
        cancelled, causing "CANCELLED: <Future ...>" tracebacks. To prevent
        this, we close the BiDi session first (which signals the CRT to stop
        sending), give the response task a grace period to exit naturally, and
        only force-cancel as a last resort.
        """
        if self._client and self._client.is_active:
            logger.debug("tts_sagemaker_disconnecting")

            # 1. Send Close message to Deepgram (tells the model to stop)
            try:
                await self._client.send_json({"type": "Close"})
            except Exception as e:
                logger.debug("tts_close_message_failed", error=str(e))

            # 2. Close the BiDi session BEFORE cancelling tasks.
            #    This sets is_active=False and closes the input stream, which
            #    signals the CRT to drain pending callbacks and stop sending.
            #    The response loop will exit naturally on the next iteration
            #    (is_active check) or when receive_response() returns None.
            await self._client.close_session()

            # 3. Give the response task a grace period to finish naturally.
            #    This avoids cancelling futures that the CRT still references.
            if self._response_task and not self._response_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._response_task), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    logger.debug(
                        "tts_response_task_grace_timeout", action="force_cancel"
                    )
                    await self.cancel_task(self._response_task)
                except (asyncio.CancelledError, Exception):
                    # Task finished with error or was already cancelled — fine
                    pass

            logger.debug("tts_sagemaker_disconnected")

    async def _process_responses(self):
        """Process streaming responses from Deepgram TTS on SageMaker.

        Deepgram TTS returns:
        - Binary audio chunks (PayloadPart with bytes)
        - JSON control messages (Flushed, Warning, Error, Close)

        Audio chunks are appended directly to the active turn's audio
        context (append_to_audio_context()) so they flow through Pipecat's
        per-turn Started/Stopped and ordering logic instead of a local
        queue drained by run_tts(). A "Flushed" acknowledgement closes the
        context (remove_audio_context()), which is what actually ends the
        turn's TTSStartedFrame/TTSStoppedFrame pair -- this only happens
        once per turn, when flush_audio() has been called and Deepgram
        confirms all buffered text has been generated.
        """
        try:
            while self._client and self._client.is_active:
                result = await self._client.receive_response()

                if result is None:
                    break

                if not hasattr(result, "value"):
                    continue

                payload = result.value

                # Check for binary audio data
                if hasattr(payload, "bytes_") and payload.bytes_:
                    raw_bytes = payload.bytes_

                    # Try to detect if this is a JSON control message or audio
                    # Deepgram sends JSON messages (Flushed, Error, etc.) as text
                    # and audio as raw binary
                    try:
                        text_data = raw_bytes.decode("utf-8")
                        parsed = json.loads(text_data)

                        # Handle JSON control messages
                        msg_type = parsed.get("type", "")

                        if msg_type == "Flushed":
                            # All audio for the current turn's Flush has been
                            # delivered. Close the audio context now -- this
                            # is the ONE place a turn's context should end,
                            # regardless of how many sentences it contained.
                            context_id = self._active_context_id
                            if context_id and self.audio_context_available(context_id):
                                await self.remove_audio_context(context_id)
                            self._active_context_id = None

                        elif msg_type == "Warning":
                            logger.warning(
                                "deepgram_tts_warning",
                                warn_msg=parsed.get("warn_msg", ""),
                            )

                        elif msg_type == "Error":
                            logger.error(
                                "deepgram_tts_error",
                                err_msg=parsed.get("err_msg", ""),
                            )
                            context_id = self._active_context_id
                            if context_id and self.audio_context_available(context_id):
                                await self.remove_audio_context(context_id)
                            self._active_context_id = None

                        elif msg_type == "Close":
                            logger.debug("deepgram_tts_connection_closed_by_server")
                            break

                        # Other JSON messages (Metadata, etc.) — ignore
                        continue

                    except (UnicodeDecodeError, json.JSONDecodeError):
                        # Not JSON — this is raw audio data
                        pass

                    context_id = self._active_context_id
                    if context_id:
                        await self.append_to_audio_context(
                            context_id,
                            TTSAudioRawFrame(
                                audio=raw_bytes,
                                sample_rate=self._sample_rate,
                                num_channels=1,
                                context_id=context_id,
                            ),
                        )

        except asyncio.CancelledError:
            logger.debug("tts_response_processor_cancelled")
        except RuntimeError:
            # Expected during graceful shutdown: close_session() sets
            # is_active=False, so receive_response() raises RuntimeError
            # ("BiDi session not active"). This is the normal exit path.
            logger.debug("tts_response_processor_session_closed")
        except Exception as e:
            # During graceful shutdown, close_session() closes the input
            # stream which may cause the SageMaker endpoint to terminate
            # the output stream with a ModelStreamError. This is expected
            # and should not be logged at error level.
            if self._client and not self._client.is_active:
                logger.debug(
                    "tts_response_processor_teardown",
                    error=str(e),
                    error_type=type(e).__name__,
                )
            else:
                logger.error(
                    "tts_response_processor_error",
                    error=str(e),
                    error_type=type(e).__name__,
                    endpoint_name=self._endpoint_name,
                )
            # Make sure a stuck turn's context still closes on error so the
            # transport doesn't wait indefinitely for a TTSStoppedFrame.
            context_id = self._active_context_id
            if context_id and self.audio_context_available(context_id):
                try:
                    await self.append_to_audio_context(
                        context_id, TTSStoppedFrame(context_id=context_id)
                    )
                    await self.remove_audio_context(context_id)
                except Exception:
                    pass
            self._active_context_id = None
        finally:
            logger.debug("tts_response_processor_stopped")

    async def on_audio_context_interrupted(self, context_id: str):
        """Handle barge-in by clearing the Deepgram text buffer.

        Called by the base class when the user starts speaking during TTS
        playback (see TTSService._handle_interruption()). Sends a Clear
        message to discard any queued/generating text for the interrupted
        context.
        """
        if self._client and self._client.is_active:
            try:
                await self._client.send_json({"type": "Clear"})
            except Exception as e:
                logger.debug("tts_clear_message_failed", error=str(e))
        if self._active_context_id == context_id:
            self._active_context_id = None
        await super().on_audio_context_interrupted(context_id)
