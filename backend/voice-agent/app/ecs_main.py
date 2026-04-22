"""
Pipecat Voice Pipeline - ECS Entrypoint

This is the simplified entrypoint for running pipecat in ECS Fargate.
It runs pipecat directly with asyncio.run() - the pattern that pipecat
is designed for.

Environment Variables (passed by ECS task override):
    ROOM_URL: Daily room URL for the voice session
    ROOM_TOKEN: Daily room token for authentication
    SESSION_ID: Unique session identifier
    SYSTEM_PROMPT: Optional custom system prompt for the LLM
    DIALIN_CALL_ID: Daily call ID (for pinless dial-in)
    DIALIN_CALL_DOMAIN: Daily call domain (for pinless dial-in)
    DIALIN_SIP_URI: SIP URI for the room

Environment Variables (set in task definition):
    API_KEY_SECRET_ARN: ARN of AWS Secrets Manager secret
    AWS_REGION: AWS region for Bedrock
    LOG_LEVEL: Logging level (default: INFO)
"""

import asyncio
import logging
import os
import sys
import warnings

import structlog
from pipecat.pipeline.runner import PipelineRunner

from app.pipeline_ecs import create_voice_pipeline, PipelineConfig, DialinSettings
from app.secrets_loader import load_secrets_from_aws

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Set up logging
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=getattr(logging, log_level),
)
# Suppress pipecat's internal DEBUG-level pipe-formatted logs.
# Our MetricsObserver already captures these metrics as structured JSON.
logging.getLogger("pipecat").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Suppress pipecat's loguru-based DEBUG logs (loguru ignores stdlib logging levels).
from loguru import logger as _loguru_logger

_loguru_logger.disable("pipecat")

# Suppress pipecat-internal deprecation warnings that we cannot fix at source
# (see service_main.py for detailed rationale).
warnings.filterwarnings(
    "ignore",
    message=r"OpenAILLMContext is deprecated",
    category=DeprecationWarning,
    module=r"pipecat\.processors\.aggregators\.openai_llm_context",
)
import pipecat.services as _pipecat_services

_pipecat_services._warned_modules.add(("deepgram", "deepgram.[stt,tts]"))

logger = structlog.get_logger(__name__)


def get_config_from_env() -> PipelineConfig:
    """
    Build pipeline configuration from environment variables.

    ECS tasks receive configuration via container overrides.
    """
    # Required environment variables
    room_url = os.environ.get("ROOM_URL")
    room_token = os.environ.get("ROOM_TOKEN")
    session_id = os.environ.get("SESSION_ID", "ecs-session")

    if not room_url:
        raise ValueError("ROOM_URL environment variable is required")
    if not room_token:
        raise ValueError("ROOM_TOKEN environment variable is required")

    # Optional dial-in settings
    dialin = None
    dialin_call_id = os.environ.get("DIALIN_CALL_ID")
    dialin_call_domain = os.environ.get("DIALIN_CALL_DOMAIN")
    dialin_sip_uri = os.environ.get("DIALIN_SIP_URI")

    if dialin_call_id and dialin_call_domain:
        dialin = DialinSettings(
            call_id=dialin_call_id,
            call_domain=dialin_call_domain,
            sip_uri=dialin_sip_uri or "",
        )
        logger.info(
            "dialin_settings_configured",
            call_id=dialin_call_id,
            call_domain=dialin_call_domain,
        )

    # System prompt (optional, with default)
    system_prompt = os.environ.get(
        "SYSTEM_PROMPT",
        "You are a helpful AI assistant. Respond concisely and naturally in conversation.",
    )

    return PipelineConfig(
        room_url=room_url,
        room_token=room_token,
        session_id=session_id,
        system_prompt=system_prompt,
        voice_id=os.environ.get("VOICE_ID", "vW1NxlzqX8WROgpQAghR"),  # ElevenLabs voice
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        dialin_settings=dialin,
    )


async def run_pipeline(config: PipelineConfig) -> None:
    """
    Run the voice pipeline until completion.

    This is the core async function that creates and runs the pipecat pipeline.
    Running via asyncio.run() ensures proper event loop handling.
    """
    transport = None
    try:
        logger.info(
            "creating_pipeline",
            session_id=config.session_id,
            room_url=config.room_url,
        )

        task, transport = await create_voice_pipeline(config)

        logger.info("pipeline_starting", session_id=config.session_id)

        # Run the pipeline using PipelineRunner
        runner = PipelineRunner()
        await runner.run(task)

        logger.info("pipeline_completed", session_id=config.session_id)

    except asyncio.CancelledError:
        logger.info("pipeline_cancelled", session_id=config.session_id)

    except Exception as e:
        logger.error(
            "pipeline_error",
            session_id=config.session_id,
            error=str(e),
            exc_info=True,
        )
        raise

    finally:
        # Cleanup transport
        if transport:
            logger.info("cleaning_up_transport", session_id=config.session_id)
            await transport.cleanup()


def main() -> None:
    """
    Main entrypoint for ECS task.

    This function:
    1. Loads secrets from AWS Secrets Manager
    2. Builds configuration from environment variables
    3. Runs the voice pipeline with asyncio.run()

    Using asyncio.run() is critical - this is how pipecat expects to be run.
    All of pipecat's internal async tasks will execute properly in this context.
    """
    logger.info(
        "ecs_task_starting",
        region=os.environ.get("AWS_REGION", "unknown"),
        session_id=os.environ.get("SESSION_ID", "unknown"),
    )

    # Load secrets from AWS Secrets Manager
    secrets_loaded = load_secrets_from_aws()
    if secrets_loaded:
        logger.info("secrets_loaded_from_aws")
    else:
        # Check if keys are already in environment (for local testing)
        has_keys = (
            os.environ.get("DEEPGRAM_API_KEY")
            or os.environ.get("ELEVENLABS_API_KEY")
            or os.environ.get("DAILY_API_KEY")
        )
        if has_keys:
            logger.info("using_environment_api_keys")
        else:
            logger.warning("no_api_keys_found")

    try:
        # Build configuration from environment
        config = get_config_from_env()

        logger.info(
            "config_loaded",
            session_id=config.session_id,
            room_url=config.room_url,
            has_dialin=config.dialin_settings is not None,
        )

        # Run the pipeline with asyncio.run()
        # pipecat runs as the main process
        asyncio.run(run_pipeline(config))

        logger.info("ecs_task_completed", session_id=config.session_id)

    except ValueError as e:
        logger.error("configuration_error", error=str(e))
        sys.exit(1)

    except Exception as e:
        logger.error("fatal_error", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
