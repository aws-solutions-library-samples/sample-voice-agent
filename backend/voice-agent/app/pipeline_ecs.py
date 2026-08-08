"""
Pipecat Voice Pipeline - Clean ECS Version

This is the clean pipeline implementation for ECS Fargate.
No debugging hacks - just the standard pipecat patterns that work correctly
when running with asyncio.run().

Architecture:
- Daily transport handles WebRTC audio I/O
- Silero VAD detects speech boundaries
- Deepgram STT converts speech to text
- Claude on Bedrock generates responses (with tool calling support)
- Cartesia TTS converts text to speech
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import structlog
from openai._types import NOT_GIVEN
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
)

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
from pipecat.transports.daily.transport import (
    DailyDialinSettings,
    DailyParams,
    DailyTransport,
)

logger = structlog.get_logger(__name__)


def _parse_dialin_data(data: Any) -> dict:
    """Parse SIP dialin event data into structured log fields.

    Extracts known SIP fields from the event data dict. Falls back to
    a truncated raw string for unexpected formats.

    Args:
        data: Dialin event data from Daily transport

    Returns:
        Dict of structured fields for logging
    """
    if not data:
        return {}

    if not isinstance(data, dict):
        return {"raw_data": str(data)[:200]}

    fields: dict = {}
    # Extract known SIP fields
    for key in (
        "sessionId",
        "callId",
        "sipCallId",
        "from",
        "to",
        "statusCode",
        "reason",
        "callDomain",
    ):
        if key in data:
            fields[key] = data[key]

    # Include raw_data as fallback if no known fields were found
    if not fields:
        fields["raw_data"] = str(data)[:200]

    return fields


def _get_config():
    """Get configuration from ConfigService.

    Returns None if config has not been loaded yet, avoiding the RuntimeError
    that ConfigService.config raises when accessed before loading.
    """
    from app.services import get_config_service

    svc = get_config_service()
    if not svc.is_configured():
        return None
    return svc.config


def _get_enable_filler_phrases() -> bool:
    """Get filler phrases enabled status from config."""
    cfg = _get_config()
    if cfg is None:
        return False
    return cfg.features.enable_filler_phrases


def _get_enable_tool_calling() -> bool:
    """Get tool calling enabled status from config."""
    cfg = _get_config()
    if cfg is None:
        return False
    return cfg.features.enable_tool_calling


def _get_enable_audio_quality() -> bool:
    """Get audio quality monitoring enabled status from config."""
    cfg = _get_config()
    if cfg is None:
        return True
    return cfg.features.enable_audio_quality_monitoring


def _get_enable_conversation_logging() -> bool:
    """Get conversation logging enabled status from config."""
    cfg = _get_config()
    if cfg is None:
        return False
    return cfg.features.enable_conversation_logging


def _get_enable_capability_registry() -> bool:
    """Get capability registry enabled status from config."""
    cfg = _get_config()
    if cfg is None:
        return False
    return cfg.features.enable_capability_registry


def _get_llm_model_id() -> str:
    """Get LLM model ID from config."""
    cfg = _get_config()
    if cfg is None:
        return os.environ.get(
            "LLM_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
    return cfg.llm.model_id


@dataclass
class DialinSettings:
    """Settings for Daily pinless dial-in."""

    call_id: str
    call_domain: str
    sip_uri: str


@dataclass
class PipelineConfig:
    """Configuration for the voice pipeline."""

    room_url: str
    room_token: str
    session_id: str
    system_prompt: str
    voice_id: str
    aws_region: str
    dialin_settings: DialinSettings | None = None
    stt_provider: str = "deepgram"
    tts_provider: str = "cartesia"
    stt_endpoint: str = ""
    tts_endpoint: str = ""


async def create_voice_pipeline(
    config: PipelineConfig,
    collector: Optional["MetricsCollector"] = None,
    a2a_registry: Optional[Any] = None,
) -> Tuple[PipelineTask, DailyTransport]:
    """
    Create and configure the voice pipeline for ECS.

    This uses pipecat's standard patterns:
    1. DailyTransport for WebRTC audio I/O
    2. Silero VAD for speech detection
    3. Deepgram STT for transcription
    4. Bedrock Claude for LLM responses
    5. Cartesia TTS for speech synthesis

    Args:
        config: Pipeline configuration
        collector: Optional MetricsCollector for timing metrics
        a2a_registry: Optional AgentRegistry for A2A capability discovery

    Returns:
        Tuple of (PipelineTask, DailyTransport)
    """
    logger.info(
        "creating_pipeline",
        session_id=config.session_id,
        room_url=config.room_url,
        stt_provider=config.stt_provider,
        tts_provider=config.tts_provider,
        stt_endpoint=config.stt_endpoint or "(cloud)",
        tts_endpoint=config.tts_endpoint or "(cloud)",
    )

    # =====================
    # Daily Transport Setup
    # =====================
    daily_dialin = None
    if config.dialin_settings:
        daily_dialin = DailyDialinSettings(
            call_id=config.dialin_settings.call_id,
            call_domain=config.dialin_settings.call_domain,
        )
        logger.info(
            "dialin_configured",
            call_id=config.dialin_settings.call_id,
            call_domain=config.dialin_settings.call_domain,
        )

    daily_api_key = os.environ.get("DAILY_API_KEY")
    if not daily_api_key:
        raise ValueError("DAILY_API_KEY environment variable required")

    # VAD analyzer is configured on LLMUserAggregatorParams (not DailyParams)
    # since pipecat v0.0.101 deprecated the transport-level vad_analyzer param.
    vad_analyzer = SileroVADAnalyzer(
        params=VADParams(
            stop_secs=0.3,  # Slightly longer pause for natural conversation
        )
    )

    transport = DailyTransport(
        config.room_url,
        config.room_token,
        "Voice Assistant",
        params=DailyParams(
            api_key=daily_api_key,
            dialin_settings=daily_dialin,
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=8000,  # PSTN audio rate
            audio_out_sample_rate=8000,
            audio_out_end_silence_secs=0,  # No trailing silence after hangup
        ),
    )
    logger.info("daily_transport_created")

    # Track SIP session ID for transfers (mutable container for closure access)
    # This will be populated when dialin_connected event fires
    sip_session_tracker: Dict[str, Optional[str]] = {"session_id": None}

    # =====================
    # STT Service (via factory)
    # =====================
    from app.services.factory import create_stt_service

    stt = create_stt_service(config)
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
    enable_tools = _get_enable_tool_calling()
    enable_registry = _get_enable_capability_registry()
    tools_list: List[Any] = []

    # Deferred reference to PipelineTask for tool-initiated frame queuing.
    # Tools (e.g., hangup_call) need to push EndFrame into the pipeline,
    # but _register_tools() runs before the PipelineTask is created.
    # This mutable container is captured by the closure and populated later.
    task_ref: Dict[str, Optional[PipelineTask]] = {"task": None}

    async def _queue_frame_for_tools(frame: Any) -> None:
        """Queue a frame into the pipeline on behalf of a tool.

        This closure captures task_ref and delegates to the real
        PipelineTask.queue_frame() once it's been assigned. By the time
        any tool executes (a participant must join first), the task will
        always be populated.
        """
        task_instance = task_ref["task"]
        if task_instance is None:
            logger.error("queue_frame_called_before_task_created")
            raise RuntimeError("Pipeline task not yet created -- cannot queue frame")
        await task_instance.queue_frame(frame)

    if enable_tools:
        # Detect what pipeline capabilities are available in this deployment.
        # This drives which local tools get registered -- tools whose
        # requirements aren't met are silently skipped.
        from app.tools.capabilities import detect_capabilities

        available_capabilities = detect_capabilities(
            transport=transport,
            sip_session_tracker=sip_session_tracker,
            config=_get_config(),
        )

        if enable_registry and a2a_registry:
            # Use capability registry: local tools + remote A2A capabilities
            tools_list = _register_capabilities(
                llm,
                config.session_id,
                transport,
                collector,
                sip_session_tracker,
                a2a_registry,
                available_capabilities,
                queue_frame=_queue_frame_for_tools,
            )
            logger.info(
                "tool_calling_enabled_with_capabilities",
                tool_count=len(tools_list),
                filler_phrases_enabled=_get_enable_filler_phrases(),
            )
        else:
            # Local tools only
            tools_list = _register_tools(
                llm,
                config.session_id,
                transport,
                collector,
                sip_session_tracker,
                available_capabilities,
                queue_frame=_queue_frame_for_tools,
            )
            logger.info(
                "tool_calling_enabled",
                tool_count=len(tools_list),
                filler_phrases_enabled=_get_enable_filler_phrases(),
            )

    # =====================
    # TTS Service (via factory)
    # =====================
    from app.services.factory import create_tts_service

    tts = create_tts_service(config)
    logger.info("tts_service_created", provider=config.tts_provider)

    # =====================
    # LLM Context Setup
    # =====================
    # Build system prompt with optional KB instructions
    system_content = (
        f"{config.system_prompt} "
        "Your responses will be read aloud via text-to-speech, so keep them "
        "concise and conversational - typically 1-3 sentences. "
        "Avoid special characters, URLs, or formatting. "
        "When the user joins, greet them warmly and ask how you can help."
    )

    # Add KB instructions if knowledge base is configured
    kb_id = os.environ.get("KB_KNOWLEDGE_BASE_ID")
    if kb_id:
        system_content += (
            " When answering questions about products, policies, or procedures, "
            "use the search_knowledge_base tool to find accurate information. "
            "Synthesize the retrieved information naturally into your response. "
            "When citing sources, mention the document name conversationally, "
            "for example: 'According to our FAQ...' or 'Our return policy states...'"
        )

    messages: list = [
        ChatCompletionSystemMessageParam(
            role="system",
            content=system_content,
        ),
    ]

    # Pass tools to context so LLM knows what tools are available.
    # ToolsSchema wraps FunctionSchema objects for provider-agnostic tool
    # definitions. Pipecat's Bedrock adapter converts these to the native
    # Converse API toolConfig format automatically.
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
    # FunctionCallFillerProcessor injects filler phrases when tool calls start.
    # This is cleaner than post-hoc audio injection because:
    # 1. The filler is spoken as part of the normal pipeline flow
    # 2. It becomes part of conversation context (appropriate for "let me look that up")
    # 3. No timing/race condition issues with tool responses
    filler_processor = None
    if _get_enable_filler_phrases():
        from app.function_call_filler_processor import FunctionCallFillerProcessor

        filler_processor = FunctionCallFillerProcessor(enabled=True)
        logger.info("filler_processor_enabled")

    # =====================
    # Pipeline Assembly
    # =====================
    # Standard pipecat pipeline:
    # Input → STT → User Context → LLM → [Filler] → TTS → Output
    # (Metrics collected via observer, NOT in pipeline)

    pipeline_components = [
        transport.input(),  # Audio from caller
        stt,  # Speech to text
        context_aggregator.user(),  # Add user message to context
        llm,  # Generate response
    ]

    # Add filler processor after LLM, before TTS (if enabled)
    if filler_processor:
        pipeline_components.append(filler_processor)

    pipeline_components.extend(
        [
            tts,  # Text to speech
            transport.output(),  # Audio to caller
            context_aggregator.assistant(),  # Add assistant response to context
        ]
    )

    pipeline = Pipeline(pipeline_components)
    logger.info("pipeline_assembled", filler_enabled=bool(filler_processor))

    # =====================
    # Observers Setup
    # =====================
    # Use pipecat's observer pattern for non-intrusive monitoring.
    # Observers watch frames WITHOUT intercepting them - they run in separate
    # async tasks and cannot block the pipeline.
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

        # Metrics observer for timing/latency metrics
        observers.append(MetricsObserver(collector))
        logger.info("metrics_observer_added")

        # Audio quality observer for RMS/peak/silence metrics
        if _get_enable_audio_quality():
            observers.append(AudioQualityObserver(collector, enabled=True))
            logger.info("audio_quality_observer_added")

        # STT quality observer for confidence scores
        observers.append(STTQualityObserver(collector, enabled=True))
        logger.info("stt_quality_observer_added")

        # LLM quality observer for token counts
        observers.append(LLMQualityObserver(collector, enabled=True))
        logger.info("llm_quality_observer_added")

        # Conversation flow observer for turn-taking analysis
        observers.append(ConversationFlowObserver(collector, enabled=True))
        logger.info("conversation_flow_observer_added")

        # Conversation observer for content logging (if enabled)
        if _get_enable_conversation_logging():
            observers.append(ConversationObserver(collector, enabled=True))
            logger.info("conversation_observer_added")

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,  # Allow barge-in
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        observers=observers,
    )

    # Wire the deferred task reference so tools can queue frames.
    # This must happen before any participant joins (which is guaranteed
    # because Daily transport events fire only after the runner starts).
    task_ref["task"] = task

    # =====================
    # Event Handlers
    # =====================

    @transport.event_handler("on_joined")
    async def on_joined(transport, data):
        logger.info("daily_joined", data=str(data)[:200])

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(
            "participant_joined",
            session_id=config.session_id,
            participant_id=participant.get("id"),
        )
        # Trigger initial greeting by sending context with a user message
        # Bedrock's Converse API requires conversations to start with a user message
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

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.info(
            "participant_left",
            session_id=config.session_id,
            participant_id=participant.get("id"),
            reason=reason,
        )
        # End the session when caller hangs up
        await task.queue_frame(EndFrame())

    @transport.event_handler("on_dialin_ready")
    async def on_dialin_ready(transport, data):
        logger.info("dialin_ready", data=str(data)[:200])

    @transport.event_handler("on_dialin_connected")
    async def on_dialin_connected(transport, data):
        dialin_fields = _parse_dialin_data(data)
        logger.info("dialin_connected", **dialin_fields)
        # Store the SIP session ID for transfer operations
        if data and "sessionId" in data:
            sip_session_tracker["session_id"] = data["sessionId"]
            logger.info(
                "sip_session_id_stored", session_id=sip_session_tracker["session_id"]
            )

    @transport.event_handler("on_dialin_stopped")
    async def on_dialin_stopped(transport, data):
        dialin_fields = _parse_dialin_data(data)
        logger.info("dialin_stopped", **dialin_fields)
        await task.queue_frame(EndFrame())

    @transport.event_handler("on_dialin_warning")
    async def on_dialin_warning(transport, data):
        dialin_fields = _parse_dialin_data(data)
        logger.warning("dialin_warning", **dialin_fields)

    @transport.event_handler("on_dialin_error")
    async def on_dialin_error(transport, data):
        dialin_fields = _parse_dialin_data(data)
        logger.error("dialin_error", **dialin_fields)
        await task.queue_frame(EndFrame())

    logger.info("pipeline_created", session_id=config.session_id)

    return task, transport


def _register_tools(
    llm,
    session_id: str,
    transport: DailyTransport,
    collector: Optional["MetricsCollector"] = None,
    sip_session_tracker: Optional[Dict[str, Optional[str]]] = None,
    available_capabilities: Optional[Any] = None,
    queue_frame: Optional[Any] = None,
) -> List[Any]:
    """
    Register local tools with the LLM service for function calling.

    Uses the capability-based tool catalog: each tool declares the pipeline
    capabilities it requires (e.g., TRANSPORT, SIP_SESSION, TRANSFER_DESTINATION),
    and only tools whose requirements are satisfied by the detected capabilities
    get registered. This prevents the LLM from seeing tools that can't function
    in the current deployment.

    Args:
        llm: The Bedrock LLM service to register tools with
        session_id: Session ID for tool context
        transport: DailyTransport instance for SIP operations (e.g., transfers)
        collector: Optional metrics collector for tool execution metrics
        sip_session_tracker: Mutable dict to track SIP session ID for transfers
        available_capabilities: Frozenset of PipelineCapability values detected
            at pipeline creation time. If None, defaults to {BASIC} only.
        queue_frame: Optional async callback to queue frames into the pipeline.
            Used by tools that need to push frames (e.g., EndFrame for hangup).

    Returns:
        List of tool definitions in Bedrock format for passing to LLM context
    """
    from app.tools import (
        ToolContext,
        ToolExecutor,
        ToolRegistry,
        PipelineCapability,
    )
    from app.tools.builtin.catalog import ALL_LOCAL_TOOLS

    # Default to BASIC-only if no capabilities were detected
    if available_capabilities is None:
        available_capabilities = frozenset({PipelineCapability.BASIC})

    # Load the explicit disabled-tools list from SSM config (if any)
    disabled_tools: set = set()
    cfg = _get_config()
    if cfg is not None:
        disabled_str = cfg.features.disabled_tools
        if disabled_str:
            disabled_tools = {
                name.strip() for name in disabled_str.split(",") if name.strip()
            }
            if disabled_tools:
                logger.info(
                    "tools_disabled_by_config",
                    disabled_tools=sorted(disabled_tools),
                )

    # Filter the catalog: only register tools whose requirements are met
    # and that haven't been explicitly disabled via config
    registry = ToolRegistry()
    skipped_tools = []

    for tool in ALL_LOCAL_TOOLS:
        # Check explicit disable list first
        if tool.name in disabled_tools:
            skipped_tools.append(
                {
                    "name": tool.name,
                    "reason": "disabled_by_config",
                }
            )
            logger.info(
                "tool_disabled_by_config",
                tool_name=tool.name,
            )
            continue

        # Check capability requirements
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

    # Create executor with metrics integration
    executor = ToolExecutor(registry, collector)

    # Build FunctionSchema list for LLMContext/ToolsSchema.
    # Pipecat's Bedrock adapter converts FunctionSchema to native toolConfig
    # format automatically, so we no longer need Bedrock-specific dicts.
    function_schemas = []
    for tool_def in registry.get_all_definitions():
        function_schemas.append(tool_def.to_function_schema())

    # Track turn number for context
    turn_counter = {"count": 0}

    def make_tool_handler(tool_name: str):
        """Factory to create a handler closure for a specific tool."""

        async def tool_handler(params: FunctionCallParams) -> None:
            """Pipecat function handler for tool execution."""
            turn_counter["count"] += 1

            # Create tool context
            # Note: call_id and session_id are the same value in this context.
            # The canonical log field is call_id (bound via structlog.contextvars
            # in service_main.py). session_id is kept for DynamoDB compatibility.
            tool_context = ToolContext(
                call_id=session_id,
                session_id=session_id,
                turn_number=turn_counter["count"],
                metrics_collector=collector,
                transport=transport,
                sip_session_id=sip_session_tracker["session_id"]
                if sip_session_tracker
                else None,
                queue_frame=queue_frame,
            )

            logger.info(
                "tool_handler_called",
                tool_name=tool_name,
                args=params.arguments,
                session_id=session_id,
            )

            # Execute tool
            result = await executor.execute(
                tool_name=tool_name,
                arguments=dict(params.arguments),
                context=tool_context,
            )

            # Return result through Pipecat's callback
            # Build optional properties to control post-tool LLM behavior.
            # Tools like hangup_call set run_llm=False to suppress the
            # redundant LLM re-inference that would otherwise add ~2s of
            # dead air before the pipeline actually disconnects.
            properties = None
            if result.run_llm is not None:
                properties = FunctionCallResultProperties(
                    run_llm=result.run_llm,
                )

            if result.is_success():
                await params.result_callback(result.content, properties=properties)
            else:
                # Return error info so LLM can respond appropriately
                await params.result_callback(
                    {
                        "error": True,
                        "error_code": result.error_code or "UNKNOWN_ERROR",
                        "error_message": result.error_message
                        or "Tool execution failed",
                    },
                    properties=properties,
                )

            # For deterministic tools with a pre-formatted response,
            # push speech directly into the pipeline, skipping the
            # second LLM roundtrip (~1s savings).
            if (
                result.is_success()
                and result.run_llm is False
                and result.spoken_response
            ):
                if queue_frame:
                    await queue_frame(TTSSpeakFrame(text=result.spoken_response))

        return tool_handler

    # Register each tool with Pipecat's function calling
    for tool_def in registry.get_all_definitions():
        tool_name = tool_def.name
        tool_description = tool_def.description

        # Create handler using factory function (proper closure)
        handler = make_tool_handler(tool_name)

        llm.register_function(
            function_name=tool_name,
            handler=handler,
        )
        logger.info(
            "tool_registered_with_llm",
            tool_name=tool_name,
            description=tool_description[:50] + "..."
            if len(tool_description) > 50
            else tool_description,
        )

    logger.info(
        "tools_registration_complete",
        tool_count=len(registry),
        tools=registry.get_tool_names(),
    )

    return function_schemas


def _register_capabilities(
    llm,
    session_id: str,
    transport: DailyTransport,
    collector: Optional["MetricsCollector"] = None,
    sip_session_tracker: Optional[Dict[str, Optional[str]]] = None,
    a2a_registry: Optional[Any] = None,
    available_capabilities: Optional[Any] = None,
    queue_frame: Optional[Any] = None,
) -> List[Any]:
    """Register both local tools and remote A2A capabilities with the LLM.

    This is the capability-registry-aware replacement for _register_tools().
    It registers the same local tools as _register_tools(), then merges in
    any remote A2A capabilities discovered by the AgentRegistry.

    The A2A tools use a different handler path (create_a2a_tool_handler)
    that routes queries to remote agents via the A2A protocol instead of
    executing them locally via ToolExecutor.

    Args:
        llm: The Bedrock LLM service to register tools with
        session_id: Session ID for tool context
        transport: DailyTransport instance for SIP operations
        collector: Optional metrics collector
        sip_session_tracker: Mutable dict to track SIP session ID
        a2a_registry: AgentRegistry instance with discovered capabilities
        available_capabilities: Frozenset of detected PipelineCapability values
        queue_frame: Optional async callback to queue frames into the pipeline

    Returns:
        List of FunctionSchema objects (local + remote combined) for ToolsSchema
    """
    # Start with local tools (capability-filtered) -- returns FunctionSchema list
    local_tools = _register_tools(
        llm,
        session_id,
        transport,
        collector,
        sip_session_tracker,
        available_capabilities,
        queue_frame=queue_frame,
    )

    if not a2a_registry:
        logger.info("capability_registry_no_registry", reason="registry not provided")
        return local_tools

    # Get remote A2A tool definitions (still in Bedrock dict format from registry)
    remote_tool_specs = a2a_registry.get_tool_definitions()

    if not remote_tool_specs:
        logger.info("capability_registry_no_remote_tools")
        return local_tools

    # Build set of local tool names for conflict detection
    # local_tools are FunctionSchema objects with a .name property
    local_tool_names = {schema.name for schema in local_tools}

    # Register A2A tool handlers and collect non-conflicting specs
    from app.a2a import create_a2a_tool_handler
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    a2a_tools_added = []
    a2a_function_schemas = []
    for skill_info in a2a_registry.get_all_skills():
        # Skip A2A skills that conflict with local tool names
        if skill_info.skill_id in local_tool_names:
            logger.warning(
                "capability_registry_skill_shadowed_by_local",
                skill_id=skill_info.skill_id,
                agent=skill_info.agent_name,
                reason="local tool takes precedence",
            )
            continue

        entry = a2a_registry.get_agent_for_skill(skill_info.skill_id)
        if not entry:
            continue

        handler = create_a2a_tool_handler(
            skill_id=skill_info.skill_id,
            agent=entry.agent,
            timeout_seconds=float(a2a_registry.a2a_timeout),
            collector=collector,
        )

        llm.register_function(
            function_name=skill_info.skill_id,
            handler=handler,
        )

        a2a_tools_added.append(skill_info.skill_id)
        logger.info(
            "a2a_tool_registered_with_llm",
            skill_id=skill_info.skill_id,
            agent=skill_info.agent_name,
        )

    # Convert non-conflicting A2A remote specs from Bedrock format to FunctionSchema
    for spec in remote_tool_specs:
        tool_spec = spec.get("toolSpec", {})
        tool_name = tool_spec.get("name", "")
        if tool_name in a2a_tools_added:
            input_schema = tool_spec.get("inputSchema", {}).get("json", {})
            a2a_function_schemas.append(
                FunctionSchema(
                    name=tool_name,
                    description=tool_spec.get("description", ""),
                    properties=input_schema.get("properties", {}),
                    required=input_schema.get("required", []),
                )
            )

    # Merge local + remote FunctionSchema lists
    combined_tools = list(local_tools) + a2a_function_schemas

    logger.info(
        "capability_registration_complete",
        local_tools=len(local_tools),
        a2a_tools=len(a2a_tools_added),
        total_tools=len(combined_tools),
        a2a_skill_ids=a2a_tools_added,
    )

    return combined_tools
