"""Service mode entrypoint for Pipecat voice pipeline.

Runs an async HTTP server that accepts call requests, allowing the container
to stay warm and handle multiple calls without cold starts.

Uses aiohttp to run both the HTTP server and pipecat pipelines in the same
event loop - this is required because pipecat's Daily transport uses signals
that only work in the main thread.

Environment variables (minimal - only deployment-specific):
  SERVICE_PORT: Port to listen on (default: 8080)
  AWS_REGION: AWS region for SDK clients
"""

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
import warnings
from typing import Any, Optional

import structlog
from aiohttp import web
from pipecat.pipeline.runner import PipelineRunner

# Configure stdlib logging as safety net for any non-structlog modules
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=getattr(logging, log_level),
)
# Suppress pipecat's internal DEBUG-level pipe-formatted logs.
# Our MetricsObserver already captures these metrics as structured JSON.
logging.getLogger("pipecat").setLevel(logging.WARNING)
# Suppress httpx INFO logs (e.g., "HTTP Request: POST http://...").
# A2A tool calls are already captured in our structured logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
# Suppress A2A SDK task lifecycle logs ("New task created with id: ...").
# Our a2a/tool_adapter.py logs these events with structured context.
logging.getLogger("a2a").setLevel(logging.WARNING)

# Suppress pipecat's loguru-based DEBUG logs (loguru ignores stdlib logging levels).
# These are pipe-linking/VAD-loading messages that add ~48 lines of noise per call.
from loguru import logger as _loguru_logger

_loguru_logger.disable("pipecat")

# Suppress pipecat-internal deprecation warnings that we cannot fix at source:
# 1. OpenAILLMContext — Bedrock adapter internally creates AWSBedrockLLMContext
#    which inherits from deprecated OpenAILLMContext (pipecat/services/aws/llm.py)
# 2. DeprecatedModuleProxy — fires even when importing from the correct
#    sub-module path (pipecat.services.deepgram.stt_sagemaker)
# These are pipecat bugs; our code already uses the recommended APIs.
warnings.filterwarnings(
    "ignore",
    message=r"OpenAILLMContext is deprecated",
    category=DeprecationWarning,
    module=r"pipecat\.processors\.aggregators\.openai_llm_context",
)
# The deepgram DeprecatedModuleProxy uses `simplefilter("always")` inside a
# `catch_warnings` context, defeating any filterwarnings() call. Pre-seed
# its _warned_modules set so the warning is never emitted.
import pipecat.services as _pipecat_services

_pipecat_services._warned_modules.add(("deepgram", "deepgram.[stt,tts]"))

# Configure structured logging with context support
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


# Error categories for classification
class ErrorCategory:
    """Error categories for pipeline failures."""

    STT = "stt_error"
    LLM = "llm_error"
    TTS = "tts_error"
    TRANSPORT = "transport_error"
    CONFIG = "config_error"
    UNKNOWN = "unknown_error"


def categorize_error(error: Exception) -> str:
    """Categorize an error based on its type and message."""
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    # Check for STT-related errors
    if any(
        kw in error_str or kw in error_type
        for kw in ["deepgram", "stt", "transcri", "speech-to-text", "sagemaker_stt"]
    ):
        return ErrorCategory.STT

    # Check for LLM-related errors
    if any(
        kw in error_str or kw in error_type
        for kw in ["bedrock", "llm", "claude", "anthropic", "model"]
    ):
        return ErrorCategory.LLM

    # Check for TTS-related errors
    if any(
        kw in error_str or kw in error_type
        for kw in ["cartesia", "tts", "text-to-speech", "synthesis", "sagemaker_tts"]
    ):
        return ErrorCategory.TTS

    # Check for transport-related errors
    if any(
        kw in error_str or kw in error_type
        for kw in ["daily", "transport", "webrtc", "connection", "network"]
    ):
        return ErrorCategory.TRANSPORT

    # Check for config-related errors
    if any(
        kw in error_str or kw in error_type
        for kw in ["config", "environment", "api_key", "missing", "required"]
    ):
        return ErrorCategory.CONFIG

    return ErrorCategory.UNKNOWN


# Import pipeline creation
from app.pipeline_ecs import create_voice_pipeline, PipelineConfig, DialinSettings
from app.secrets_loader import load_secrets_from_aws
from app.observability import create_metrics_collector, EMFLogger
from app.session_tracker import SessionTracker, get_ecs_task_id
from app.task_protection import TaskProtection
from app.services import load_config, AppConfig, get_config_service

# Global A2A registry - initialized in main() when capability registry is enabled
_a2a_registry: Optional[Any] = None
_a2a_poll_interval: int = 30
_config_refresh_task: Optional[asyncio.Task] = None


class PipelineManager:
    """Manages running voice pipelines."""

    def __init__(self, config: AppConfig):
        """Initialize the PipelineManager with configuration.

        Args:
            config: Application configuration loaded from SSM
        """
        self.config = config
        self.active_sessions: dict[str, asyncio.Task] = {}
        self._emf_logger = EMFLogger(environment=config.environment)

        # Auto-scaling support
        self._task_protection = TaskProtection()
        self._draining = False
        self._max_concurrent = int(os.environ.get("MAX_CONCURRENT_CALLS", "4"))

        # Initialize session tracker
        self._task_id: Optional[str] = get_ecs_task_id()
        if config.session_table_name:
            self._session_tracker: Optional[SessionTracker] = SessionTracker(
                table_name=config.session_table_name,
                task_id=self._task_id,
            )
            logger.info(
                "session_tracker_enabled",
                table_name=config.session_table_name,
                task_id=self._task_id,
            )
        else:
            self._session_tracker = None
            logger.info(
                "session_tracker_disabled", reason="session_table_name not configured"
            )

    async def start_heartbeat_loop(self) -> None:
        """Start the session tracker heartbeat loop with protection renewal."""
        if self._session_tracker:
            await self._session_tracker.start_heartbeat_loop(
                get_count_fn=self._heartbeat_with_protection_renewal
            )

    def _heartbeat_with_protection_renewal(self) -> int:
        """Heartbeat callback that also schedules protection renewal.

        Returns the current active session count for the heartbeat,
        and schedules an async protection renewal if there are active sessions.
        """
        count = len(self.active_sessions)
        # Schedule async protection renewal (cannot await from sync callback)
        if count > 0 and self._task_protection.is_protected:
            asyncio.ensure_future(self._task_protection.renew_if_protected())
        return count

    async def start_call(
        self,
        room_url: str,
        room_token: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        dialin_settings: Optional[dict] = None,
    ) -> dict:
        """Start a new call pipeline."""
        # Reject calls when draining (SIGTERM received)
        if self._draining:
            logger.warning(
                "call_rejected_draining",
                active_sessions=len(self.active_sessions),
            )
            return {
                "status": "rejected",
                "error": "Service is draining, not accepting new calls",
                "http_status": 503,
            }

        # Reject calls when at capacity
        if len(self.active_sessions) >= self._max_concurrent:
            logger.warning(
                "call_rejected_at_capacity",
                active_sessions=len(self.active_sessions),
                max_concurrent=self._max_concurrent,
            )
            return {
                "status": "rejected",
                "error": "At capacity, try another instance",
                "http_status": 503,
            }

        if session_id in self.active_sessions:
            return {"status": "error", "error": f"Session {session_id} already active"}

        # Generate correlation ID for this call
        call_id = str(uuid.uuid4())

        logger.info(
            "starting_call",
            call_id=call_id,
            session_id=session_id,
            room_url=room_url,
        )

        # Record session start in DynamoDB (required - blocks on failure)
        if self._session_tracker:
            try:
                await self._session_tracker.start_session(session_id, call_id)
            except Exception as e:
                logger.error(
                    "session_start_failed",
                    session_id=session_id,
                    call_id=call_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                return {
                    "status": "error",
                    "error": "Service temporarily unavailable",
                    "details": "Failed to initialize session tracking",
                }

        # Enable task scale-in protection on first call (0 → 1 transition)
        if len(self.active_sessions) == 0:
            success = await self._task_protection.set_protected(True, retry=True)
            if not success:
                logger.warning(
                    "task_protection_enable_failed",
                    note="Accepting call without scale-in protection",
                )

        # Create the task but don't await it - let it run in background
        task = asyncio.create_task(
            self._run_pipeline(
                room_url=room_url,
                room_token=room_token,
                session_id=session_id,
                call_id=call_id,
                system_prompt=system_prompt,
                dialin_settings=dialin_settings,
            )
        )
        self.active_sessions[session_id] = task

        # Emit session health metric on call start
        self._emf_logger.emit_session_health(
            active_sessions=len(self.active_sessions),
            task_id=self._task_id,
        )

        return {
            "status": "started",
            "session_id": session_id,
            "call_id": call_id,
        }

    async def _run_pipeline(
        self,
        room_url: str,
        room_token: str,
        session_id: str,
        call_id: str,
        system_prompt: Optional[str],
        dialin_settings: Optional[dict],
    ):
        """Run a voice pipeline for a call."""
        # Bind call_id to all logs in this context
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(call_id=call_id, session_id=session_id)

        # Create metrics collector for this call
        collector = create_metrics_collector(
            call_id=call_id,
            session_id=session_id,
        )

        transport = None
        error_category = None  # Track error category for session health emission
        end_status = "completed"  # Track end status for session tracking

        try:
            logger.info("pipeline_starting")

            # Activate session in DynamoDB (mark as active)
            if self._session_tracker:
                await self._session_tracker.activate_session(session_id)

            # Build pipeline config
            dialin = None
            if dialin_settings:
                dialin = DialinSettings(
                    call_id=dialin_settings.get("call_id", ""),
                    call_domain=dialin_settings.get("call_domain", ""),
                    sip_uri=dialin_settings.get("sip_uri", ""),
                )

            config = PipelineConfig(
                room_url=room_url,
                room_token=room_token,
                session_id=session_id,
                system_prompt=system_prompt or "You are a helpful AI assistant.",
                voice_id=self.config.providers.voice_id,
                aws_region=os.environ.get("AWS_REGION", "us-east-1"),
                dialin_settings=dialin,
                stt_provider=os.environ.get(
                    "STT_PROVIDER", self.config.providers.stt_provider
                ),
                tts_provider=os.environ.get(
                    "TTS_PROVIDER", self.config.providers.tts_provider
                ),
                stt_endpoint=os.environ.get("STT_ENDPOINT_NAME", ""),
                tts_endpoint=os.environ.get("TTS_ENDPOINT_NAME", ""),
                flux_stt_endpoint=os.environ.get(
                    "FLUX_STT_ENDPOINT_NAME",
                    self.config.providers.flux_stt_endpoint_name,
                ),
            )

            # Create the pipeline with metrics collector
            task, transport = await create_voice_pipeline(
                config, collector, a2a_registry=_a2a_registry
            )

            # Run the pipeline
            runner = PipelineRunner()
            await runner.run(task)

            # Finalize metrics on successful completion
            collector.finalize(status="completed")
            logger.info("pipeline_completed")

        except asyncio.CancelledError:
            end_status = "cancelled"
            collector.finalize(status="cancelled")
            logger.info("pipeline_cancelled")
            # error_category stays None for cancellation

        except Exception as e:
            end_status = "error"
            error_category = categorize_error(e)
            collector.finalize(status="error", error_category=error_category)
            logger.error(
                "pipeline_error",
                error=str(e),
                error_category=error_category,
                error_type=type(e).__name__,
            )

        finally:
            # Remove from active sessions
            self.active_sessions.pop(session_id, None)

            # Disable task scale-in protection when last call ends (1 → 0 transition)
            # Race-safe: pop() and len() are synchronous, no await between them
            if len(self.active_sessions) == 0 and self._task_protection.is_protected:
                await self._task_protection.set_protected(False)

            # End session in DynamoDB
            if self._session_tracker:
                try:
                    await self._session_tracker.end_session(
                        session_id=session_id,
                        end_status=end_status,
                        turn_count=collector.turn_count,
                        error_category=error_category,
                    )
                except Exception as e:
                    logger.warning(
                        "session_end_failed",
                        session_id=session_id,
                        error=str(e),
                        error_type=type(e).__name__,
                        end_status=end_status,
                        turn_count=collector.turn_count,
                    )

            # Emit session health metric on call end
            self._emf_logger.emit_session_health(
                active_sessions=len(self.active_sessions),
                error_count=1 if error_category else 0,
                error_category=error_category,
                task_id=self._task_id,
            )

            # Clean up transport
            if transport:
                try:
                    await transport.cleanup()
                except Exception:
                    pass

            # Clear context vars
            structlog.contextvars.clear_contextvars()

    def get_status(self) -> dict:
        """Get the current status of the service."""
        return {
            "status": "draining" if self._draining else "healthy",
            "active_sessions": len(self.active_sessions),
            "session_ids": list(self.active_sessions.keys()),
            "draining": self._draining,
            "protected": self._task_protection.is_protected,
            "capacity_remaining": self._max_concurrent - len(self.active_sessions),
        }


# Global pipeline manager - initialized in main()
pipeline_manager: Optional[PipelineManager] = None


# HTTP Handlers
async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response(
        pipeline_manager.get_status()
        if pipeline_manager
        else {"status": "initializing"}
    )


async def handle_status(request: web.Request) -> web.Response:
    """Status endpoint."""
    return web.json_response(
        pipeline_manager.get_status()
        if pipeline_manager
        else {"status": "initializing"}
    )


async def handle_ready(request: web.Request) -> web.Response:
    """NLB readiness check.

    Returns 503 when draining or at capacity to stop NLB from routing
    new calls to this task. Separate from /health (ECS liveness) to
    prevent ECS from killing at-capacity containers.
    """
    if pipeline_manager is None:
        return web.json_response({"status": "initializing"}, status=503)

    if pipeline_manager._draining:
        return web.json_response(
            {
                "status": "draining",
                "active_sessions": len(pipeline_manager.active_sessions),
            },
            status=503,
        )

    active = len(pipeline_manager.active_sessions)
    if active >= pipeline_manager._max_concurrent:
        return web.json_response(
            {
                "status": "at_capacity",
                "active_sessions": active,
            },
            status=503,
        )

    return web.json_response(
        {
            "status": "ready",
            "active_sessions": active,
            "capacity_remaining": pipeline_manager._max_concurrent - active,
            "protected": pipeline_manager._task_protection.is_protected,
        }
    )


async def handle_call(request: web.Request) -> web.Response:
    """Handle new call requests."""
    if pipeline_manager is None:
        return web.json_response(
            {"status": "error", "error": "Service not initialized"}, status=503
        )

    try:
        data = await request.json()

        room_url = data.get("room_url", "")
        room_token = data.get("room_token", "")
        session_id = data.get("session_id", "")

        if not room_url or not room_token or not session_id:
            return web.json_response(
                {
                    "status": "error",
                    "error": "Missing required fields: room_url, room_token, session_id",
                },
                status=400,
            )

        result = await pipeline_manager.start_call(
            room_url=room_url,
            room_token=room_token,
            session_id=session_id,
            system_prompt=data.get("system_prompt"),
            dialin_settings=data.get("dialin_settings"),
        )

        status_code = result.pop(
            "http_status", 200 if result.get("status") == "started" else 400
        )
        return web.json_response(result, status=status_code)

    except Exception as e:
        logger.error("request_error", error=str(e))
        return web.json_response({"status": "error", "error": str(e)}, status=500)


def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/call", handle_call)
    return app


async def _config_refresh_loop(region: str) -> None:
    """Periodically refresh SSM config and lazily init/teardown the A2A registry."""
    global _a2a_registry, _a2a_poll_interval

    while True:
        await asyncio.sleep(30)
        try:
            config_service = get_config_service()
            await config_service.refresh()
            cfg = config_service.config

            want_registry = (
                cfg.features.enable_capability_registry and bool(cfg.a2a.namespace)
            )
            have_registry = _a2a_registry is not None

            if want_registry and not have_registry:
                from app.a2a import AgentRegistry

                _a2a_registry = AgentRegistry(
                    namespace=cfg.a2a.namespace,
                    region=region,
                    a2a_timeout=cfg.a2a.tool_timeout_seconds,
                )
                _a2a_poll_interval = cfg.a2a.poll_interval_seconds
                await _a2a_registry.start_polling(
                    interval_seconds=_a2a_poll_interval
                )
                logger.info(
                    "a2a_registry_lazy_initialized",
                    namespace=cfg.a2a.namespace,
                    poll_interval=_a2a_poll_interval,
                )

            elif not want_registry and have_registry:
                try:
                    await _a2a_registry.stop_polling()
                except Exception:
                    pass
                _a2a_registry = None
                logger.info("a2a_registry_torn_down")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "config_refresh_loop_error",
                error=str(e),
                error_type=type(e).__name__,
            )


async def run_server(port: int):
    """Run the HTTP server."""
    app = create_app()
    # Disable aiohttp access logging -- health check polls from ELB every 10s
    # produce ~8,640 log lines/day of pure noise. All meaningful request events
    # (call start, errors, session lifecycle) are already captured by structlog.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # Start session tracker heartbeat loop
    if pipeline_manager:
        await pipeline_manager.start_heartbeat_loop()

    # Start A2A capability registry polling if configured
    if _a2a_registry:
        try:
            await _a2a_registry.start_polling(interval_seconds=_a2a_poll_interval)
        except Exception as e:
            logger.error("a2a_registry_start_failed", error=str(e))

    # Start config refresh loop to pick up SSM changes at runtime
    global _config_refresh_task
    region = os.environ.get("AWS_REGION", "us-east-1")
    _config_refresh_task = asyncio.create_task(_config_refresh_loop(region=region))

    logger.info(
        "service_ready",
        port=port,
        endpoint=f"http://0.0.0.0:{port}",
    )

    # Keep the server running
    try:
        while True:
            await asyncio.sleep(3600)  # Sleep for an hour at a time
    except asyncio.CancelledError:
        pass
    finally:
        # Stop config refresh loop
        if _config_refresh_task:
            _config_refresh_task.cancel()
        # Stop A2A registry polling
        if _a2a_registry:
            try:
                await _a2a_registry.stop_polling()
            except Exception:
                pass
        await runner.cleanup()


def main():
    """Main entry point for service mode."""
    port = int(os.environ.get("SERVICE_PORT", "8080"))
    region = os.environ.get("AWS_REGION", "us-east-1")

    logger.info(
        "service_starting",
        port=port,
        region=region,
    )

    # Load configuration from SSM Parameter Store
    try:
        config = asyncio.run(load_config())
        logger.info(
            "config_loaded",
            environment=config.environment,
            kb_configured=bool(config.knowledge_base.id),
        )
    except Exception as e:
        logger.error("config_load_failed", error=str(e))
        sys.exit(1)

    # Initialize pipeline manager with config
    global pipeline_manager
    pipeline_manager = PipelineManager(config)

    # Initialize A2A capability registry if enabled
    global _a2a_registry, _a2a_poll_interval
    if config.features.enable_capability_registry and config.a2a.namespace:
        from app.a2a import AgentRegistry

        _a2a_registry = AgentRegistry(
            namespace=config.a2a.namespace,
            region=region,
            a2a_timeout=config.a2a.tool_timeout_seconds,
        )
        _a2a_poll_interval = config.a2a.poll_interval_seconds
        logger.info(
            "a2a_registry_initialized",
            namespace=config.a2a.namespace,
            poll_interval=config.a2a.poll_interval_seconds,
            tool_timeout=config.a2a.tool_timeout_seconds,
        )
    elif config.features.enable_capability_registry:
        logger.warning(
            "a2a_registry_not_initialized",
            reason="enable_capability_registry is true but no namespace configured",
        )

    # Load secrets from AWS
    secrets_loaded = load_secrets_from_aws()
    if secrets_loaded:
        logger.info("secrets_loaded_from_aws")
    else:
        logger.warning("no_secrets_loaded", message="Check API_KEY_SECRET_ARN")

    # Set up signal handlers with graceful drain
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def graceful_drain(sig_name: str) -> None:
        """Graceful drain: stop accepting calls, wait for active to complete."""
        logger.info("drain_started", signal=sig_name)
        if pipeline_manager:
            pipeline_manager._draining = True

            # Wait for active sessions to complete (up to Fargate stop timeout)
            drain_start = time.time()
            while pipeline_manager.active_sessions:
                elapsed = time.time() - drain_start
                remaining = len(pipeline_manager.active_sessions)
                logger.info(
                    "drain_waiting",
                    active_sessions=remaining,
                    elapsed_seconds=round(elapsed),
                )
                if elapsed > 110:  # Leave 10s buffer before Fargate kills at 120s
                    logger.warning(
                        "drain_timeout_approaching",
                        active_sessions=remaining,
                        elapsed_seconds=round(elapsed),
                    )
                    break
                await asyncio.sleep(5)

            # Clear protection and close HTTP session
            await pipeline_manager._task_protection.set_protected(False)
            await pipeline_manager._task_protection.close()

        logger.info("drain_complete")
        # Cancel remaining tasks to trigger server cleanup
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    def shutdown_handler(sig_name: str) -> None:
        asyncio.ensure_future(graceful_drain(sig_name))

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler, sig.name)

    # Run the server
    try:
        loop.run_until_complete(run_server(port))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        logger.info("service_stopped")


if __name__ == "__main__":
    main()
