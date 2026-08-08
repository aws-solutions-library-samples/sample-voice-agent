"""
Deepgram STT service wrapper for AWS SageMaker with graceful teardown.

Subclasses pipecat's DeepgramSageMakerSTTService to fix the AWS CRT
InvalidStateError race condition during session teardown. The upstream
implementation cancels the response task before closing the BiDi session,
which leaves CRT native callbacks referencing cancelled futures.

This wrapper reorders _disconnect() to close the session first, then
wait for the response task to exit naturally.
"""

import asyncio

import structlog

logger = structlog.get_logger(__name__)

from pipecat.services.deepgram.sagemaker.stt import (
    DeepgramSageMakerSTTService as _BaseDeepgramSageMakerSTTService,
)


class DeepgramSageMakerSTTService(_BaseDeepgramSageMakerSTTService):
    """Deepgram SageMaker STT with graceful BiDi teardown.

    Overrides ``_disconnect()`` to close the BiDi session before cancelling
    background tasks, preventing ``InvalidStateError`` from the AWS CRT
    HTTP/2 layer.
    """

    async def _disconnect(self):
        """Disconnect from the SageMaker endpoint.

        Uses a graceful shutdown sequence to avoid InvalidStateError from the
        AWS CRT library. The CRT's native HTTP/2 layer has pending callbacks
        (_on_body, _on_complete) that fire after Python-side futures are
        cancelled, causing "CANCELLED: <Future ...>" tracebacks. To prevent
        this, we close the BiDi session first (which signals the CRT to stop
        sending), give background tasks a grace period to exit naturally,
        and only force-cancel as a last resort.
        """
        if self._client and self._client.is_active:
            logger.debug("stt_sagemaker_disconnecting")

            # 1. Send CloseStream message to Deepgram (tells the model to stop)
            try:
                await self._client.send_json({"type": "CloseStream"})
            except Exception as e:
                logger.debug("stt_close_stream_failed", error=str(e))

            # 2. Close the BiDi session BEFORE cancelling tasks.
            #    This sets is_active=False and closes the input stream, which
            #    signals the CRT to drain pending callbacks and stop sending.
            await self._client.close_session()

            # 3. Give the keepalive task a brief moment — it checks is_active
            #    so it should exit on its own quickly.
            if self._keepalive_task and not self._keepalive_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._keepalive_task), timeout=1.0
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    await self.cancel_task(self._keepalive_task)

            # 4. Give the response task a grace period to finish naturally.
            #    This avoids cancelling futures that the CRT still references.
            if self._response_task and not self._response_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._response_task), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    logger.debug(
                        "stt_response_task_grace_timeout", action="force_cancel"
                    )
                    await self.cancel_task(self._response_task)
                except (asyncio.CancelledError, Exception):
                    # Task finished with error or was already cancelled — fine
                    pass

            logger.debug("stt_sagemaker_disconnected")
            await self._call_event_handler("on_disconnected")
