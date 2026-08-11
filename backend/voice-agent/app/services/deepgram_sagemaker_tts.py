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
    TTSStartedFrame,
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
        super().__init__(sample_rate=sample_rate, **kwargs)

        self._endpoint_name = endpoint_name
        self._region = region
        self._voice = voice
        self._sample_rate = sample_rate
        self._encoding = encoding

        self._client: Optional[SageMakerBidiClient] = None
        self._response_task: Optional[asyncio.Task] = None

        # Queue for receiving audio chunks from the response processor
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()

        # Track whether we're in an active synthesis
        self._synthesizing = False

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
        """Convert text to speech via SageMaker BiDi streaming.

        Sends text to the Deepgram Aura model on SageMaker and yields audio
        frames as they arrive. Uses the Deepgram TTS WebSocket protocol:
        1. Send {"type": "Speak", "text": text}
        2. Send {"type": "Flush"} to trigger generation
        3. Receive binary audio chunks from the response stream

        Args:
            text: Text to synthesize.
            context_id: TTS context ID for tracking (Pipecat v0.0.102+).

        Yields:
            Frame: TTSStartedFrame, TTSAudioRawFrame chunks, TTSStoppedFrame.
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

        try:
            # Signal TTS start
            yield TTSStartedFrame()

            # Clear any leftover audio from previous synthesis
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            self._synthesizing = True

            # Send text to Deepgram for synthesis
            await self._client.send_json({"type": "Speak", "text": text})

            # Send Flush to trigger audio generation
            await self._client.send_json({"type": "Flush"})

            # Yield audio chunks as they arrive from the response processor
            # The response processor puts audio bytes into _audio_queue.
            # A None sentinel signals that a Flushed event was received
            # (all audio for this Flush has been delivered).
            while self._synthesizing:
                try:
                    audio_data = await asyncio.wait_for(
                        self._audio_queue.get(), timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "tts_audio_receive_timeout",
                        timeout_seconds=10.0,
                        endpoint_name=self._endpoint_name,
                    )
                    break

                if audio_data is None:
                    # Flushed sentinel — all audio for this text delivered
                    break

                # Yield audio chunk as a raw frame
                yield TTSAudioRawFrame(
                    audio=audio_data,
                    sample_rate=self._sample_rate,
                    num_channels=1,
                )

            self._synthesizing = False

            # Signal TTS complete
            yield TTSStoppedFrame()

        except Exception as e:
            self._synthesizing = False
            logger.error(
                "tts_synthesis_error",
                error=str(e),
                error_type=type(e).__name__,
                text_length=len(text),
                endpoint_name=self._endpoint_name,
            )
            yield ErrorFrame(error=f"TTS synthesis failed: {e}")
            yield TTSStoppedFrame()

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

            # 4. Signal any waiting synthesis to stop
            self._synthesizing = False
            try:
                self._audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

            logger.debug("tts_sagemaker_disconnected")

    async def _process_responses(self):
        """Process streaming responses from Deepgram TTS on SageMaker.

        Deepgram TTS returns:
        - Binary audio chunks (PayloadPart with bytes)
        - JSON control messages (Flushed, Warning, Error, Close)

        Audio chunks are placed into _audio_queue for run_tts() to consume.
        A None sentinel is queued when a Flushed event is received.
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
                            # All audio for the current Flush has been delivered
                            await self._audio_queue.put(None)

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
                            # Signal error to synthesis
                            await self._audio_queue.put(None)

                        elif msg_type == "Close":
                            logger.debug("deepgram_tts_connection_closed_by_server")
                            break

                        # Other JSON messages (Metadata, etc.) — ignore
                        continue

                    except (UnicodeDecodeError, json.JSONDecodeError):
                        # Not JSON — this is raw audio data
                        pass

                    # Queue audio data for run_tts() to consume
                    await self._audio_queue.put(raw_bytes)

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
            # Signal error to any waiting synthesis
            try:
                self._audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        finally:
            logger.debug("tts_response_processor_stopped")

    async def handle_interruption(self):
        """Handle barge-in by clearing the Deepgram text buffer.

        Called when the user starts speaking during TTS playback.
        Sends a Clear message to discard any queued text.
        """
        if self._client and self._client.is_active:
            try:
                await self._client.send_json({"type": "Clear"})
                self._synthesizing = False
                # Drain the audio queue
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            except Exception as e:
                logger.debug("tts_clear_message_failed", error=str(e))
