"""Tests for ECS auto-scaling integration in PipelineManager.

Tests the interaction between PipelineManager, TaskProtection, and the
/ready endpoint for auto-scaling behavior.
"""

import os

# Set env vars before imports (must happen before app module imports)
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("VOICE_ID", "test-voice-id")

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
    import structlog  # noqa: F401 - required transitively by app modules
except ImportError:
    pytest.skip(
        "Container-only dependencies not available (aiohttp/structlog)",
        allow_module_level=True,
    )


class TestPipelineManagerDraining:
    """Tests for draining behavior."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock AppConfig."""
        config = MagicMock()
        config.environment = "test"
        config.session_table_name = None  # Disable session tracker
        config.providers = MagicMock()
        config.providers.voice_id = "test-voice"
        config.providers.stt_provider = "deepgram"
        config.providers.tts_provider = "cartesia"
        config.knowledge_base = MagicMock()
        config.knowledge_base.id = None
        config.features = MagicMock()
        return config

    def test_initial_state_not_draining(self, mock_config):
        """PipelineManager starts not draining."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        assert pm._draining is False

    def test_initial_state_no_active_sessions(self, mock_config):
        """PipelineManager starts with no active sessions."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        assert len(pm.active_sessions) == 0

    def test_max_concurrent_from_env(self, mock_config):
        """MAX_CONCURRENT_CALLS env var is respected."""
        with patch.dict(os.environ, {"MAX_CONCURRENT_CALLS": "6"}):
            from app.service_main import PipelineManager

            pm = PipelineManager(mock_config)
            assert pm._max_concurrent == 6

    def test_max_concurrent_default(self, mock_config):
        """Default MAX_CONCURRENT_CALLS is 4."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_CONCURRENT_CALLS", None)
            from app.service_main import PipelineManager

            pm = PipelineManager(mock_config)
            assert pm._max_concurrent == 4

    @pytest.mark.asyncio
    async def test_reject_call_when_draining(self, mock_config):
        """Calls are rejected with error when service is draining."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        pm._draining = True

        result = await pm.start_call(
            room_url="https://example.daily.co/room",
            room_token="token",
            session_id="test-session",
        )
        assert result["status"] == "rejected"
        assert "draining" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_reject_call_at_capacity(self, mock_config):
        """Calls are rejected when at max capacity."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        pm._max_concurrent = 2
        # Simulate 2 active sessions
        pm.active_sessions = {"s1": MagicMock(), "s2": MagicMock()}

        result = await pm.start_call(
            room_url="https://example.daily.co/room",
            room_token="token",
            session_id="test-session",
        )
        assert result["status"] == "rejected"
        assert "capacity" in result["error"].lower()


class TestPipelineManagerProtection:
    """Tests for task protection lifecycle."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock AppConfig."""
        config = MagicMock()
        config.environment = "test"
        config.session_table_name = None
        config.providers = MagicMock()
        config.providers.voice_id = "test-voice"
        config.providers.stt_provider = "deepgram"
        config.providers.tts_provider = "cartesia"
        config.knowledge_base = MagicMock()
        config.knowledge_base.id = None
        config.features = MagicMock()
        return config

    @pytest.mark.asyncio
    async def test_protection_enabled_on_first_call(self, mock_config):
        """Task protection is enabled when first call starts (0 -> 1)."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        pm._task_protection.set_protected = AsyncMock(return_value=True)

        # Mock pipeline creation to prevent actual pipeline run
        with patch(
            "app.service_main.create_voice_pipeline", new_callable=AsyncMock
        ) as mock_create:
            mock_task = MagicMock()
            mock_transport = MagicMock()
            mock_transport.cleanup = AsyncMock()
            mock_create.return_value = (mock_task, mock_transport)

            with patch("app.service_main.create_metrics_collector") as mock_collector:
                mock_collector.return_value = MagicMock()
                mock_collector.return_value.turn_count = 0

                result = await pm.start_call(
                    room_url="https://example.daily.co/room",
                    room_token="token",
                    session_id="session-1",
                )

        assert result["status"] == "started"
        pm._task_protection.set_protected.assert_called_once_with(True, retry=True)

    @pytest.mark.asyncio
    async def test_protection_not_called_on_subsequent_calls(self, mock_config):
        """Task protection is NOT re-called when additional calls start."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        pm._task_protection.set_protected = AsyncMock(return_value=True)
        # Simulate one existing session
        pm.active_sessions = {"existing-session": MagicMock()}

        with patch(
            "app.service_main.create_voice_pipeline", new_callable=AsyncMock
        ) as mock_create:
            mock_task = MagicMock()
            mock_transport = MagicMock()
            mock_transport.cleanup = AsyncMock()
            mock_create.return_value = (mock_task, mock_transport)

            with patch("app.service_main.create_metrics_collector") as mock_collector:
                mock_collector.return_value = MagicMock()
                mock_collector.return_value.turn_count = 0

                result = await pm.start_call(
                    room_url="https://example.daily.co/room",
                    room_token="token",
                    session_id="session-2",
                )

        assert result["status"] == "started"
        # Protection should NOT have been called (already have active sessions)
        pm._task_protection.set_protected.assert_not_called()

    @pytest.mark.asyncio
    async def test_protection_failure_still_accepts_call(self, mock_config):
        """Call is accepted even if protection enable fails (degraded mode)."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        pm._task_protection.set_protected = AsyncMock(return_value=False)

        with patch(
            "app.service_main.create_voice_pipeline", new_callable=AsyncMock
        ) as mock_create:
            mock_task = MagicMock()
            mock_transport = MagicMock()
            mock_transport.cleanup = AsyncMock()
            mock_create.return_value = (mock_task, mock_transport)

            with patch("app.service_main.create_metrics_collector") as mock_collector:
                mock_collector.return_value = MagicMock()
                mock_collector.return_value.turn_count = 0

                result = await pm.start_call(
                    room_url="https://example.daily.co/room",
                    room_token="token",
                    session_id="session-1",
                )

        assert result["status"] == "started"  # Call accepted despite protection failure


class TestGetStatus:
    """Tests for enhanced get_status()."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock AppConfig."""
        config = MagicMock()
        config.environment = "test"
        config.session_table_name = None
        config.providers = MagicMock()
        config.providers.voice_id = "test-voice"
        config.providers.stt_provider = "deepgram"
        config.providers.tts_provider = "cartesia"
        config.knowledge_base = MagicMock()
        config.knowledge_base.id = None
        config.features = MagicMock()
        return config

    def test_status_includes_scaling_fields(self, mock_config):
        """Status includes draining, protected, capacity_remaining."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        status = pm.get_status()

        assert "draining" in status
        assert "protected" in status
        assert "capacity_remaining" in status
        assert status["draining"] is False
        assert status["protected"] is False
        assert status["capacity_remaining"] == pm._max_concurrent

    def test_status_reflects_draining(self, mock_config):
        """Status shows draining state."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        pm._draining = True
        status = pm.get_status()
        assert status["status"] == "draining"
        assert status["draining"] is True

    def test_status_capacity_decreases_with_sessions(self, mock_config):
        """Capacity remaining decreases as sessions are added."""
        from app.service_main import PipelineManager

        pm = PipelineManager(mock_config)
        pm._max_concurrent = 4
        pm.active_sessions = {"s1": MagicMock(), "s2": MagicMock()}
        status = pm.get_status()
        assert status["active_sessions"] == 2
        assert status["capacity_remaining"] == 2


class TestReadyEndpoint:
    """Tests for /ready NLB readiness endpoint."""

    @pytest.fixture
    async def client(self, aiohttp_client):
        """Create test client with mock pipeline manager."""
        import app.service_main as sm
        from app.service_main import PipelineManager, create_app

        mock_config = MagicMock()
        mock_config.environment = "test"
        mock_config.session_table_name = None
        mock_config.providers = MagicMock()
        mock_config.providers.voice_id = "test-voice"
        mock_config.providers.stt_provider = "deepgram"
        mock_config.providers.tts_provider = "cartesia"
        mock_config.knowledge_base = MagicMock()
        mock_config.knowledge_base.id = None
        mock_config.features = MagicMock()

        sm.pipeline_manager = PipelineManager(mock_config)

        app = create_app()
        client = await aiohttp_client(app)
        yield client
        sm.pipeline_manager = None

    @pytest.mark.asyncio
    async def test_ready_returns_200_when_healthy(self, client):
        """GET /ready returns 200 when service is healthy with capacity."""
        resp = await client.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ready"
        assert "capacity_remaining" in data

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_draining(self, client):
        """GET /ready returns 503 when service is draining."""
        import app.service_main as sm

        sm.pipeline_manager._draining = True

        resp = await client.get("/ready")
        assert resp.status == 503
        data = await resp.json()
        assert data["status"] == "draining"

    @pytest.mark.asyncio
    async def test_ready_returns_503_at_capacity(self, client):
        """GET /ready returns 503 when at max capacity."""
        import app.service_main as sm

        sm.pipeline_manager._max_concurrent = 2
        sm.pipeline_manager.active_sessions = {"s1": MagicMock(), "s2": MagicMock()}

        resp = await client.get("/ready")
        assert resp.status == 503
        data = await resp.json()
        assert data["status"] == "at_capacity"

    @pytest.mark.asyncio
    async def test_health_returns_200_even_when_draining(self, client):
        """GET /health always returns 200 (ECS liveness, not routing)."""
        import app.service_main as sm

        sm.pipeline_manager._draining = True

        resp = await client.get("/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_health_returns_200_at_capacity(self, client):
        """GET /health returns 200 even at capacity."""
        import app.service_main as sm

        sm.pipeline_manager._max_concurrent = 1
        sm.pipeline_manager.active_sessions = {"s1": MagicMock()}

        resp = await client.get("/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_not_initialized(self, aiohttp_client):
        """GET /ready returns 503 when pipeline_manager is None."""
        import app.service_main as sm
        from app.service_main import create_app

        sm.pipeline_manager = None
        app = create_app()
        client = await aiohttp_client(app)

        resp = await client.get("/ready")
        assert resp.status == 503
        data = await resp.json()
        assert data["status"] == "initializing"
