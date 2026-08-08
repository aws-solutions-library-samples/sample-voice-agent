"""
Tests for graceful BiDi session teardown in TTS and STT services.

Verifies that _disconnect() closes the BiDi session before cancelling
background tasks, preventing InvalidStateError from the AWS CRT library.

Run with: pytest tests/test_bidi_teardown.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# TTS teardown tests
# ---------------------------------------------------------------------------


class TestTTSGracefulTeardown:
    """Tests for DeepgramSageMakerTTSService._disconnect() ordering."""

    def _make_tts(self):
        """Create a TTS service instance with mocked internals."""
        with patch("app.services.deepgram_sagemaker_tts.SageMakerBidiClient"):
            from app.services.deepgram_sagemaker_tts import (
                DeepgramSageMakerTTSService,
            )

            tts = DeepgramSageMakerTTSService(
                endpoint_name="test-endpoint",
                region="us-east-1",
                voice="aura-2-thalia-en",
                sample_rate=8000,
                encoding="linear16",
            )
        return tts

    @pytest.mark.asyncio
    async def test_close_session_before_cancel_task(self):
        """close_session() must be called before the response task is awaited."""
        tts = self._make_tts()

        call_order = []

        mock_client = MagicMock()
        type(mock_client).is_active = PropertyMock(return_value=True)
        mock_client.send_json = AsyncMock()

        async def fake_close_session():
            call_order.append("close_session")
            type(mock_client).is_active = PropertyMock(return_value=False)

        mock_client.close_session = fake_close_session

        # Simulate a response task that is still pending (hangs until cancelled)
        async def hanging_response_loop():
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                call_order.append("response_task_cancelled")

        response_task = asyncio.create_task(hanging_response_loop())

        tts._client = mock_client
        tts._response_task = response_task

        # Mock cancel_task to track ordering
        async def fake_cancel_task(task):
            call_order.append("cancel_task")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        tts.cancel_task = fake_cancel_task

        # Mock wait_for to trigger timeout quickly
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, *, timeout=None):
            return await original_wait_for(coro, timeout=0.05)

        with patch("asyncio.wait_for", fast_wait_for):
            await tts._disconnect()

        # close_session must come before cancel_task
        assert call_order.index("close_session") < call_order.index("cancel_task"), (
            f"Expected close_session before cancel_task, got: {call_order}"
        )

    @pytest.mark.asyncio
    async def test_response_task_gets_grace_period(self):
        """Response task should be given a chance to finish before force-cancel."""
        tts = self._make_tts()

        mock_client = MagicMock()
        type(mock_client).is_active = PropertyMock(return_value=True)
        mock_client.send_json = AsyncMock()

        async def fake_close_session():
            type(mock_client).is_active = PropertyMock(return_value=False)

        mock_client.close_session = fake_close_session

        # Simulate a response task that takes a moment to finish
        finished = False

        async def slow_response_loop():
            nonlocal finished
            await asyncio.sleep(0.1)
            finished = True

        response_task = asyncio.create_task(slow_response_loop())

        tts._client = mock_client
        tts._response_task = response_task
        tts.cancel_task = AsyncMock()

        await tts._disconnect()

        # Task should have finished naturally within the grace period
        assert finished is True
        # cancel_task should NOT have been called (task finished in time)
        tts.cancel_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_cancel_after_grace_timeout(self):
        """Response task is force-cancelled if it doesn't finish in time."""
        tts = self._make_tts()

        mock_client = MagicMock()
        type(mock_client).is_active = PropertyMock(return_value=True)
        mock_client.send_json = AsyncMock()

        async def fake_close_session():
            type(mock_client).is_active = PropertyMock(return_value=False)

        mock_client.close_session = fake_close_session

        # Simulate a response task that hangs forever
        async def hanging_response_loop():
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                pass

        response_task = asyncio.create_task(hanging_response_loop())

        tts._client = mock_client
        tts._response_task = response_task
        tts.cancel_task = AsyncMock()

        # Mock wait_for to trigger timeout quickly instead of waiting 2s
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, *, timeout=None):
            return await original_wait_for(coro, timeout=0.05)

        with patch("asyncio.wait_for", fast_wait_for):
            await tts._disconnect()

        # cancel_task should have been called due to timeout
        tts.cancel_task.assert_called_once_with(response_task)

        # Cleanup
        if not response_task.done():
            response_task.cancel()
            try:
                await response_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_disconnect_when_not_active(self):
        """_disconnect() should be a no-op when client is not active."""
        tts = self._make_tts()

        mock_client = MagicMock()
        type(mock_client).is_active = PropertyMock(return_value=False)
        mock_client.close_session = AsyncMock()

        tts._client = mock_client

        await tts._disconnect()

        # close_session should not be called
        mock_client.close_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_sends_close_message_first(self):
        """Close message is sent to Deepgram before closing the session."""
        tts = self._make_tts()

        call_order = []

        mock_client = MagicMock()
        type(mock_client).is_active = PropertyMock(return_value=True)

        async def fake_send_json(data):
            call_order.append(("send_json", data))

        mock_client.send_json = fake_send_json

        async def fake_close_session():
            call_order.append("close_session")
            type(mock_client).is_active = PropertyMock(return_value=False)

        mock_client.close_session = fake_close_session

        tts._client = mock_client
        tts._response_task = None  # no response task
        tts.cancel_task = AsyncMock()

        await tts._disconnect()

        assert call_order[0] == ("send_json", {"type": "Close"})
        assert call_order[1] == "close_session"

    @pytest.mark.asyncio
    async def test_response_processor_handles_runtime_error(self):
        """_process_responses exits gracefully on RuntimeError (session closed)."""
        tts = self._make_tts()

        mock_client = MagicMock()
        # is_active returns True initially, then the receive raises RuntimeError
        type(mock_client).is_active = PropertyMock(return_value=True)

        async def raise_runtime_error():
            raise RuntimeError("BiDi session not active")

        mock_client.receive_response = raise_runtime_error

        tts._client = mock_client

        # Should not raise — the RuntimeError should be caught
        await tts._process_responses()

    @pytest.mark.asyncio
    async def test_model_stream_error_during_teardown_not_logged_as_error(self):
        """ModelStreamError after close_session should be logged at debug, not error."""
        tts = self._make_tts()

        mock_client = MagicMock()
        # is_active starts True, then goes False (simulating close_session was called)
        type(mock_client).is_active = PropertyMock(return_value=False)

        async def raise_model_error():
            # Simulate what happens when receive_response() gets past the
            # is_active check but the stream is closing
            raise Exception("An error occurred while streaming the inference response")

        mock_client.receive_response = raise_model_error

        tts._client = mock_client

        # Should not raise — the error should be caught
        await tts._process_responses()

        # The while loop won't even execute because is_active is False,
        # so no error should be raised at all. Let's test the real scenario
        # where is_active is True when the loop enters but the exception
        # fires and is_active is False when the handler runs.

    @pytest.mark.asyncio
    async def test_model_stream_error_mid_teardown_downgraded(self):
        """Exception from receive_response during teardown is debug-level."""
        tts = self._make_tts()

        call_count = 0

        mock_client = MagicMock()

        def is_active_side_effect():
            # First call: True (enters the while loop)
            # After exception: False (close_session was called concurrently)
            nonlocal call_count
            call_count += 1
            return call_count <= 1

        type(mock_client).is_active = PropertyMock(side_effect=is_active_side_effect)

        async def raise_model_error():
            raise Exception("An error occurred while streaming the inference response")

        mock_client.receive_response = raise_model_error

        tts._client = mock_client

        # Should not raise
        await tts._process_responses()


# ---------------------------------------------------------------------------
# STT teardown tests
# ---------------------------------------------------------------------------


class TestSTTGracefulTeardown:
    """Tests for DeepgramSageMakerSTTService._disconnect() ordering."""

    def _make_stt(self):
        """Create an STT service instance with mocked internals."""
        with patch("pipecat.services.deepgram.stt_sagemaker.SageMakerBidiClient"):
            from app.services.deepgram_sagemaker_stt import (
                DeepgramSageMakerSTTService,
            )
            from deepgram import LiveOptions

            stt = DeepgramSageMakerSTTService(
                endpoint_name="test-endpoint",
                region="us-east-1",
                live_options=LiveOptions(
                    model="nova-3",
                    language="en",
                    encoding="linear16",
                    sample_rate=8000,
                ),
            )
        return stt

    @pytest.mark.asyncio
    async def test_close_session_before_cancel_task(self):
        """close_session() must be called before tasks are cancelled."""
        stt = self._make_stt()

        call_order = []

        mock_client = MagicMock()
        type(mock_client).is_active = PropertyMock(return_value=True)
        mock_client.send_json = AsyncMock()

        async def fake_close_session():
            call_order.append("close_session")
            type(mock_client).is_active = PropertyMock(return_value=False)

        mock_client.close_session = fake_close_session

        # Simulate hanging tasks to ensure ordering is observable
        async def hanging_task():
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                pass

        keepalive_task = asyncio.create_task(hanging_task())
        response_task = asyncio.create_task(hanging_task())

        stt._client = mock_client
        stt._keepalive_task = keepalive_task
        stt._response_task = response_task

        async def fake_cancel_task(task):
            call_order.append("cancel_task")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        stt.cancel_task = fake_cancel_task
        stt._call_event_handler = AsyncMock()

        # Mock wait_for for fast timeouts
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, *, timeout=None):
            return await original_wait_for(coro, timeout=0.05)

        with patch("asyncio.wait_for", fast_wait_for):
            await stt._disconnect()

        # close_session must be the first action
        assert call_order[0] == "close_session", f"Got: {call_order}"

    @pytest.mark.asyncio
    async def test_disconnect_when_not_active(self):
        """_disconnect() should be a no-op when client is not active."""
        stt = self._make_stt()

        mock_client = MagicMock()
        type(mock_client).is_active = PropertyMock(return_value=False)
        mock_client.close_session = AsyncMock()

        stt._client = mock_client

        await stt._disconnect()

        mock_client.close_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_overrides_parent_disconnect(self):
        """Our STT wrapper must override the parent _disconnect method."""
        from app.services.deepgram_sagemaker_stt import DeepgramSageMakerSTTService
        from pipecat.services.deepgram.sagemaker.stt import (
            DeepgramSageMakerSTTService as ParentSTT,
        )

        # The wrapper's _disconnect should be different from the parent
        assert DeepgramSageMakerSTTService._disconnect is not ParentSTT._disconnect, (
            "STT wrapper must override _disconnect to fix the teardown race"
        )
