"""Local prototyping pipeline using SmallWebRTCTransport.

Creates the same voice pipeline as pipeline_ecs.py (STT -> LLM -> TTS) but
replaces DailyTransport with pipecat's SmallWebRTCTransport for browser-based
testing on localhost. No Daily account, phone number, or SIP infrastructure
needed -- just a browser microphone.

Prerequisites:
    - Cloud resources must be deployed (Bedrock access, Deepgram/Cartesia keys)
    - pip install pipecat-ai[webrtc,runner]

Architecture:
    Browser Mic/Speaker --WebRTC--> SmallWebRTCTransport --> STT -> LLM -> TTS
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import structlog
from openai._types import NOT_GIVEN
from openai.types.chat import ChatCompletionSystemMessageParam

if TYPE_CHECKING:
    from app.observability import MetricsCollector

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    LLMMessagesUpdateFrame,
    EndFrame,
    FunctionCallResultProperties,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams
from pipecat.observers.base_observer import BaseObserver
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

logger = structlog.get_logger(__name__)

# Default audio sample rate for browser WebRTC (pipecat resamples 48kHz -> 16kHz)
LOCAL_SAMPLE_RATE = 16000


@dataclass
class LocalPipelineConfig:
    """Configuration for the local prototyping pipeline."""

    session_id: str
    system_prompt: str
    voice_id: str
    aws_region: str
    stt_provider: str = "deepgram"
    tts_provider: str = "cartesia"
    stt_endpoint: str = ""
    tts_endpoint: str = ""


def _get_config():
    """Get configuration from ConfigService if available.

    Returns None if config has not been loaded, avoiding RuntimeError.
    For local prototyping, ConfigService may not be initialized (no SSM),
    so this gracefully returns None.
    """
    try:
        from app.services import get_config_service

        svc = get_config_service()
        if not svc.is_configured():
            return None
        return svc.config
    except Exception:
        return None


def _get_env_bool(name: str, default: bool) -> bool:
    """Read a boolean from environment variable."""
    val = os.environ.get(name, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


def _get_llm_model_id() -> str:
    """Get LLM model ID from config or environment."""
    cfg = _get_config()
    if cfg is not None:
        return cfg.llm.model_id
    return os.environ.get(
        "LLM_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )


async def create_local_pipeline(
    config: LocalPipelineConfig,
    webrtc_connection: SmallWebRTCConnection,
    collector: Optional["MetricsCollector"] = None,
) -> Tuple[PipelineTask, SmallWebRTCTransport]:
    """Create and configure the voice pipeline for local browser-based prototyping.

    Uses the same STT/LLM/TTS pipeline as production but with
    SmallWebRTCTransport instead of DailyTransport. No SIP, no Daily
    account, no phone number needed.

    Args:
        config: Local pipeline configuration
        webrtc_connection: Pre-established WebRTC connection from signalling
        collector: Optional MetricsCollector for timing metrics

    Returns:
        Tuple of (PipelineTask, SmallWebRTCTransport)
    """
    logger.info(
        "creating_local_pipeline",
        session_id=config.session_id,
        stt_provider=config.stt_provider,
        tts_provider=config.tts_provider,
        sample_rate=LOCAL_SAMPLE_RATE,
    )

    # =====================
    # WebRTC Transport Setup
    # =====================
    vad_analyzer = SileroVADAnalyzer(
        params=VADParams(
            stop_secs=0.3,
        )
    )

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=LOCAL_SAMPLE_RATE,
            audio_out_sample_rate=LOCAL_SAMPLE_RATE,
        ),
    )
    logger.info("webrtc_transport_created", sample_rate=LOCAL_SAMPLE_RATE)

    # =====================
    # STT Service (via factory)
    # =====================
    # Import the ECS pipeline config for factory compatibility
    from app.pipeline_ecs import PipelineConfig

    factory_config = PipelineConfig(
        room_url="",  # Not used by STT/TTS factories
        room_token="",
        session_id=config.session_id,
        system_prompt=config.system_prompt,
        voice_id=config.voice_id,
        aws_region=config.aws_region,
        stt_provider=config.stt_provider,
        tts_provider=config.tts_provider,
        stt_endpoint=config.stt_endpoint,
        tts_endpoint=config.tts_endpoint,
    )

    from app.services.factory import create_stt_service

    stt = create_stt_service(factory_config)
    logger.info("stt_service_created", provider=config.stt_provider)

    # =====================
    # LLM Service (via factory)
    # =====================
    llm_model_id = _get_llm_model_id()

    from app.services.llm_factory import create_llm_service

    llm = create_llm_service(model_id=llm_model_id, region=config.aws_region)

    # =====================
    # Tool Calling Setup
    # =====================
    enable_tools = _get_env_bool("ENABLE_TOOL_CALLING", False)
    tools_list: List[Any] = []

    # Deferred reference to PipelineTask for tool-initiated frame queuing
    task_ref: Dict[str, Optional[PipelineTask]] = {"task": None}

    async def _queue_frame_for_tools(frame: Any) -> None:
        """Queue a frame into the pipeline on behalf of a tool."""
        task_instance = task_ref["task"]
        if task_instance is None:
            logger.error("queue_frame_called_before_task_created")
            raise RuntimeError("Pipeline task not yet created")
        await task_instance.queue_frame(frame)

    if enable_tools:
        from app.tools.capabilities import detect_capabilities

        # No SIP session tracker in local mode -- SIP tools auto-excluded
        available_capabilities = detect_capabilities(
            transport=transport,
            sip_session_tracker=None,
            config=_get_config(),
        )

        tools_list = _register_local_tools(
            llm,
            config.session_id,
            transport,
            collector,
            available_capabilities,
            queue_frame=_queue_frame_for_tools,
        )
        logger.info(
            "tool_calling_enabled",
            tool_count=len(tools_list),
        )

    # =====================
    # TTS Service (via factory)
    # =====================
    from app.services.factory import create_tts_service

    tts = create_tts_service(factory_config)
    logger.info("tts_service_created", provider=config.tts_provider)

    # =====================
    # LLM Context Setup
    # =====================
    system_content = (
        f"{config.system_prompt} "
        "Your responses will be read aloud via text-to-speech, so keep them "
        "concise and conversational - typically 1-3 sentences. "
        "Avoid special characters, URLs, or formatting. "
        "When the user joins, greet them warmly and ask how you can help."
    )

    messages: list = [
        ChatCompletionSystemMessageParam(
            role="system",
            content=system_content,
        ),
    ]

    tools_schema: ToolsSchema | None = None
    if tools_list:
        tools_schema = ToolsSchema(standard_tools=tools_list)

    context = LLMContext(
        messages,
        tools=tools_schema if tools_schema else NOT_GIVEN,
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=vad_analyzer),
    )
    logger.info("llm_context_created", tools_enabled=bool(tools_list))

    # =====================
    # Filler Processor Setup
    # =====================
    filler_processor = None
    if _get_env_bool("ENABLE_FILLER_PHRASES", True):
        try:
            from app.function_call_filler_processor import FunctionCallFillerProcessor

            filler_processor = FunctionCallFillerProcessor(enabled=True)
            logger.info("filler_processor_enabled")
        except ImportError:
            pass

    # =====================
    # Pipeline Assembly
    # =====================
    pipeline_components = [
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
    ]

    if filler_processor:
        pipeline_components.append(filler_processor)

    pipeline_components.extend(
        [
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    pipeline = Pipeline(pipeline_components)
    logger.info("pipeline_assembled", filler_enabled=bool(filler_processor))

    # =====================
    # Observers Setup
    # =====================
    observers: List[BaseObserver] = []
    if collector:
        from app.observability import (
            MetricsObserver,
            ConversationObserver,
            AudioQualityObserver,
            STTQualityObserver,
            LLMQualityObserver,
            ConversationFlowObserver,
        )

        observers.append(MetricsObserver(collector))
        if _get_env_bool("ENABLE_AUDIO_QUALITY_MONITORING", True):
            observers.append(AudioQualityObserver(collector, enabled=True))
        observers.append(STTQualityObserver(collector, enabled=True))
        observers.append(LLMQualityObserver(collector, enabled=True))
        observers.append(ConversationFlowObserver(collector, enabled=True))
        if _get_env_bool("ENABLE_CONVERSATION_LOGGING", False):
            observers.append(ConversationObserver(collector, enabled=True))

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=observers,
    )

    # Wire the deferred task reference for tools
    task_ref["task"] = task

    # =====================
    # Event Handlers
    # =====================

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, data):
        logger.info("browser_client_connected", session_id=config.session_id)
        # Trigger initial greeting
        greeting_messages = [
            {
                "role": "system",
                "content": config.system_prompt,
            },
            {
                "role": "user",
                "content": "[The user has just joined the call. Greet them warmly.]",
            },
        ]
        await task.queue_frames(
            [LLMMessagesUpdateFrame(greeting_messages, run_llm=True)]
        )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, data):
        logger.info("browser_client_disconnected", session_id=config.session_id)
        await task.queue_frame(EndFrame())

    logger.info("local_pipeline_created", session_id=config.session_id)

    return task, transport


def _register_local_tools(
    llm,
    session_id: str,
    transport: BaseTransport,
    collector: Optional["MetricsCollector"] = None,
    available_capabilities: Optional[Any] = None,
    queue_frame: Optional[Any] = None,
) -> List[Any]:
    """Register local tools with the LLM for local prototyping.

    Same logic as pipeline_ecs._register_tools() but without SSM config
    dependency for disabled-tools list (not available in local mode).

    Args:
        llm: The Bedrock LLM service
        session_id: Session ID for tool context
        transport: Transport instance (SmallWebRTCTransport)
        collector: Optional metrics collector
        available_capabilities: Frozenset of detected capabilities
        queue_frame: Async callback to queue frames into the pipeline

    Returns:
        List of FunctionSchema objects for ToolsSchema
    """
    from app.tools import (
        ToolContext,
        ToolExecutor,
        ToolRegistry,
        PipelineCapability,
    )
    from app.tools.builtin.catalog import ALL_LOCAL_TOOLS

    if available_capabilities is None:
        available_capabilities = frozenset({PipelineCapability.BASIC})

    # Filter tools by capability requirements
    registry = ToolRegistry()
    skipped_tools = []

    for tool in ALL_LOCAL_TOOLS:
        tool_requires = tool.requires or frozenset({PipelineCapability.BASIC})
        if tool_requires <= available_capabilities:
            registry.register(tool)
        else:
            missing = tool_requires - available_capabilities
            skipped_tools.append(
                {
                    "name": tool.name,
                    "reason": "missing_capabilities",
                    "missing": sorted(c.value for c in missing),
                }
            )
            logger.info(
                "tool_skipped_missing_capabilities",
                tool_name=tool.name,
                missing_capabilities=sorted(c.value for c in missing),
            )

    if skipped_tools:
        logger.info(
            "tools_filtered",
            registered=registry.get_tool_names(),
            skipped=skipped_tools,
        )

    registry.lock()

    executor = ToolExecutor(registry, collector)

    function_schemas = []
    for tool_def in registry.get_all_definitions():
        function_schemas.append(tool_def.to_function_schema())

    turn_counter = {"count": 0}

    def make_tool_handler(tool_name: str):
        """Factory to create a handler closure for a specific tool."""

        async def tool_handler(params: FunctionCallParams) -> None:
            turn_counter["count"] += 1

            tool_context = ToolContext(
                call_id=session_id,
                session_id=session_id,
                turn_number=turn_counter["count"],
                metrics_collector=collector,
                transport=transport,
                sip_session_id=None,  # No SIP in local mode
                queue_frame=queue_frame,
            )

            logger.info(
                "tool_handler_called",
                tool_name=tool_name,
                args=params.arguments,
                session_id=session_id,
            )

            result = await executor.execute(
                tool_name=tool_name,
                arguments=dict(params.arguments),
                context=tool_context,
            )

            properties = None
            if result.run_llm is not None:
                properties = FunctionCallResultProperties(
                    run_llm=result.run_llm,
                )

            if result.is_success():
                await params.result_callback(result.content, properties=properties)
            else:
                await params.result_callback(
                    {
                        "error": True,
                        "error_code": result.error_code or "UNKNOWN_ERROR",
                        "error_message": result.error_message
                        or "Tool execution failed",
                    },
                    properties=properties,
                )

            if (
                result.is_success()
                and result.run_llm is False
                and result.spoken_response
            ):
                if queue_frame:
                    await queue_frame(TTSSpeakFrame(text=result.spoken_response))

        return tool_handler

    for tool_def in registry.get_all_definitions():
        tool_name = tool_def.name
        handler = make_tool_handler(tool_name)
        llm.register_function(
            function_name=tool_name,
            handler=handler,
        )
        logger.info(
            "tool_registered_with_llm",
            tool_name=tool_name,
        )

    logger.info(
        "tools_registration_complete",
        tool_count=len(registry),
        tools=registry.get_tool_names(),
    )

    return function_schemas
