"""
Deepgram Flux STT service wrapper for AWS SageMaker with graceful teardown.

Subclasses pipecat's DeepgramFluxSageMakerSTTService to fix the AWS CRT
InvalidStateError race condition during session teardown (same pattern as
our Nova STT wrapper in deepgram_sagemaker_stt.py).

Deepgram Flux provides:
- Native turn detection (eager end-of-turn events)
- Lower-latency transcription with streaming confidence
- Multilingual support (with flux-general-multi model)
- Improved prosody-aware speech boundaries

Reference:
- Pipecat docs: https://docs.pipecat.ai/server/services/stt/deepgram
- Example: https://github.com/pipecat-ai/pipecat/blob/main/examples/voice/voice-deepgram-flux-sagemaker.py
"""

import asyncio

import structlog

logger = structlog.get_logger(__name__)

from pipecat.services.deepgram.flux.sagemaker.stt import (
    DeepgramFluxSageMakerSTTService as _BaseDeepgramFluxSageMakerSTTService,
)


class DeepgramFluxSageMakerSTTService(_BaseDeepgramFluxSageMakerSTTService):
    """Deepgram Flux SageMaker STT with graceful BiDi teardown.

    Overrides _disconnect() to close the BiDi session before cancelling
    background tasks, preventing InvalidStateError from the AWS CRT
    HTTP/2 layer.

    Uses getattr guards to handle attribute differences between
    Pipecat versions (e.g. _keepalive_task vs _watchdog_task).
    """

    async def _disconnect(self):
        """Disconnect from the SageMaker endpoint gracefully.

        Uses a graceful shutdown sequence: close the BiDi session first,
        then wait for background tasks to exit naturally before
        force-cancelling as a last resort.
        """
        if self._client and self._client.is_active:
            logger.debug("flux_stt_sagemaker_disconnecting")

            # 1. Send CloseStream to Deepgram Flux
            try:
                await self._client.send_json({"type": "CloseStream"})
            except Exception:
                logger.debug("flux_stt_close_stream_failed", exc_info=True)

            # 2. Close BiDi session BEFORE cancelling tasks.
            await self._client.close_session()

            # 3. Gracefully stop keepalive/watchdog task.
            #    Flux base may use _watchdog_task instead of _keepalive_task.
            bg_task = getattr(self, "_keepalive_task", None) or getattr(
                self, "_watchdog_task", None
            )
            if bg_task and not bg_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(bg_task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    await self.cancel_task(bg_task)

            # 4. Give response task a grace period to finish naturally.
            response_task = getattr(self, "_response_task", None)
            if response_task and not response_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(response_task), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    logger.debug(
                        "flux_stt_response_task_grace_timeout",
                        action="force_cancel",
                    )
                    await self.cancel_task(response_task)
                except (asyncio.CancelledError, Exception):
                    pass

            # 5. Clear connection state if Flux base class exposes it.
            conn_event = getattr(self, "_connection_established_event", None)
            if conn_event:
                conn_event.clear()
            reset_fn = getattr(self, "_reset_configure_state", None)
            if reset_fn:
                reset_fn()

            logger.debug("flux_stt_sagemaker_disconnected")
            await self._call_event_handler("on_disconnected")
