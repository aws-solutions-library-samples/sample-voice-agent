"""
Service factory for STT and TTS providers.

Supports switching between cloud APIs and SageMaker endpoints via configuration:
- STT_PROVIDER: "deepgram" (default, cloud API) or "sagemaker" (Deepgram on SageMaker)
- TTS_PROVIDER: "elevenlabs" (default, cloud API) or "sagemaker" (Deepgram Aura on SageMaker)

Cloud APIs are the default for simpler deployment without SageMaker endpoints.
SageMaker providers use HTTP/2 bidirectional streaming for low-latency, VPC-local inference.
"""

import os
from typing import Any, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.pipeline_ecs import PipelineConfig

logger = structlog.get_logger(__name__)


# Default ElevenLabs voice + model for cloud-API mode. Override per-call via
# PipelineConfig.voice_id (flows through from agent-level config in ECS).
_ELEVENLABS_DEFAULT_VOICE_ID = "vW1NxlzqX8WROgpQAghR"
_ELEVENLABS_DEFAULT_MODEL = "eleven_flash_v2_5"


def create_stt_service(config: "PipelineConfig"):
    """
    Create STT service based on provider configuration.

    Supports:
    - "deepgram": Cloud WebSocket API (requires DEEPGRAM_API_KEY)
    - "sagemaker": Pipecat's built-in DeepgramSageMakerSTTService using HTTP/2 BiDi streaming

    Args:
        config: Pipeline configuration with provider, endpoint names, and region

    Returns:
        STT service instance

    Raises:
        ValueError: If required configuration is missing for the selected provider
    """
    provider = config.stt_provider.lower()

    if provider == "sagemaker":
        from app.services.deepgram_sagemaker_stt import DeepgramSageMakerSTTService
        from deepgram import LiveOptions

        from app.services.sagemaker_credentials import patch_sagemaker_bidi_credentials

        patch_sagemaker_bidi_credentials()

        if not config.stt_endpoint:
            raise ValueError(
                "STT_ENDPOINT_NAME is required when STT_PROVIDER=sagemaker"
            )

        logger.info(
            "stt_provider_selected",
            provider="sagemaker",
            endpoint=config.stt_endpoint,
            region=config.aws_region,
        )
        return DeepgramSageMakerSTTService(
            endpoint_name=config.stt_endpoint,
            region=config.aws_region,
            live_options=LiveOptions(
                model="nova-3",
                language="en",
                interim_results=True,
                punctuate=True,
                encoding="linear16",
                sample_rate=8000,
                channels=1,
            ),
        )

    else:
        # Default to Deepgram cloud API
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.transcriptions.language import Language

        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise ValueError("DEEPGRAM_API_KEY environment variable required for STT")

        # Phase 7A-followup (resolved 2026-04-27): wire stt_language +
        # stt_keywords from Aurora into Deepgram's Settings API.
        # Pipecat 0.0.108 exposes both via DeepgramSTTService.Settings
        # (model + language inherited from STTSettings, keywords on the
        # Deepgram-specific subclass). language is a StrEnum keyed on
        # BCP-47 codes ("en", "en-US", "es", …); we coerce via
        # Language(<str>) and fall back to default if Aurora supplies
        # something the enum doesn't know.
        raw_language = (getattr(config, "stt_language", "") or "en").strip()
        keywords = list(getattr(config, "stt_keywords", []) or [])

        try:
            language_enum = Language(raw_language) if raw_language else None
        except ValueError:
            logger.warning(
                "stt_language_unknown_fallback_to_default",
                provider="deepgram",
                supplied=raw_language,
            )
            language_enum = None

        # Build Settings only when we actually have something non-default
        # to apply — keeps the deployment indistinguishable from pre-7A
        # for agents with empty stt_language / stt_keywords (Chris).
        stt_settings = None
        if language_enum is not None or keywords:
            settings_kwargs: dict[str, Any] = {}
            if language_enum is not None:
                settings_kwargs["language"] = language_enum
            if keywords:
                # Deepgram accepts keywords as list[str] OR comma-separated
                # str; the SDK normalizes. We pass a list so any Aurora-
                # configured single-word entries don't get split.
                settings_kwargs["keywords"] = keywords
            stt_settings = DeepgramSTTService.Settings(**settings_kwargs)

        logger.info(
            "stt_provider_selected",
            provider="deepgram",
            language=raw_language,
            keywords_count=len(keywords),
            keywords_wired=stt_settings is not None,
        )

        kwargs: dict[str, Any] = {"api_key": api_key, "sample_rate": 8000}
        if stt_settings is not None:
            kwargs["settings"] = stt_settings
        return DeepgramSTTService(**kwargs)


def create_tts_service(config: "PipelineConfig"):
    """
    Create TTS service based on provider configuration.

    Supports:
    - "elevenlabs": Cloud WebSocket streaming API (requires ELEVENLABS_API_KEY)
    - "sagemaker": Custom DeepgramSageMakerTTSService using HTTP/2 BiDi streaming

    Args:
        config: Pipeline configuration with provider, endpoint names, region, and voice_id

    Returns:
        TTS service instance

    Raises:
        ValueError: If required configuration is missing for the selected provider
    """
    provider = config.tts_provider.lower()

    if provider == "sagemaker":
        from app.services.deepgram_sagemaker_tts import DeepgramSageMakerTTSService

        from app.services.sagemaker_credentials import patch_sagemaker_bidi_credentials

        patch_sagemaker_bidi_credentials()

        if not config.tts_endpoint:
            raise ValueError(
                "TTS_ENDPOINT_NAME is required when TTS_PROVIDER=sagemaker"
            )

        # For SageMaker TTS, voice_id should be a Deepgram Aura voice name.
        voice = _resolve_voice_for_sagemaker(config.voice_id)

        logger.info(
            "tts_provider_selected",
            provider="sagemaker",
            endpoint=config.tts_endpoint,
            voice=voice,
            region=config.aws_region,
        )
        return DeepgramSageMakerTTSService(
            endpoint_name=config.tts_endpoint,
            region=config.aws_region,
            voice=voice,
            sample_rate=8000,
            encoding="linear16",
        )

    else:
        # Default to ElevenLabs cloud API (WebSocket streaming with word
        # timestamps and interruption support).
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            raise ValueError(
                "ELEVENLABS_API_KEY environment variable required for TTS"
            )

        # Voice-id resolution in order: agent-config tts_voice_id →
        # legacy config.voice_id → hardcoded default. Phase 7A starts
        # populating tts_voice_id from the Lambda.
        voice_id = (
            getattr(config, "tts_voice_id", "") or config.voice_id
            or _ELEVENLABS_DEFAULT_VOICE_ID
        )
        tts_model = getattr(config, "tts_model", "") or _ELEVENLABS_DEFAULT_MODEL

        logger.info(
            "tts_provider_selected",
            provider="elevenlabs",
            voice_id=voice_id,
            model=tts_model,
            stability=getattr(config, "tts_stability", None),
            similarity_boost=getattr(config, "tts_similarity_boost", None),
            style=getattr(config, "tts_style", None),
            use_speaker_boost=getattr(config, "tts_use_speaker_boost", None),
            speed=getattr(config, "tts_speed", None),
        )

        # Flat kwargs work on all pipecat versions (>=0.0.102). On 0.0.106+
        # the Settings inner class is also available, but flat kwargs are
        # forward-compatible and simpler. Don't use `settings=Settings(...)`:
        # it was a shim added late and isn't the canonical path.
        #
        # Per-agent voice tuning (stability / similarity / style / speaker
        # boost / speed) is passed via `params=` using pipecat's InputParams
        # inner class. Build only the subset the agent actually set so None
        # values pass through to ElevenLabs defaults rather than clobbering
        # them.
        voice_params_kwargs: dict[str, Any] = {}
        for attr in (
            "tts_stability",
            "tts_similarity_boost",
            "tts_style",
            "tts_use_speaker_boost",
            "tts_speed",
        ):
            v = getattr(config, attr, None)
            if v is not None:
                # Attr names: tts_stability → stability, tts_similarity_boost → similarity_boost, etc.
                voice_params_kwargs[attr.removeprefix("tts_")] = v

        service_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "voice_id": voice_id,
            "model": tts_model,
        }
        if voice_params_kwargs:
            try:
                service_kwargs["params"] = ElevenLabsTTSService.InputParams(**voice_params_kwargs)
            except Exception as exc:  # noqa: BLE001 — fall back to defaults, don't block call
                logger.warning(
                    "elevenlabs_input_params_rejected",
                    error=str(exc),
                    attempted=voice_params_kwargs,
                )

        return ElevenLabsTTSService(**service_kwargs)


def _resolve_voice_for_sagemaker(voice_id: str | None) -> str:
    """
    Resolve a voice ID to a Deepgram Aura voice name for SageMaker TTS.

    If the voice_id is already a Deepgram Aura voice name (starts with
    "aura"), returns it directly. Otherwise falls back to a sensible
    default.

    Args:
        voice_id: ElevenLabs voice ID or Deepgram Aura voice name

    Returns:
        Deepgram Aura voice name (e.g., "aura-2-thalia-en")
    """
    default_voice = "aura-2-thalia-en"

    if not voice_id:
        return default_voice

    # If it starts with "aura", it's already a Deepgram Aura voice name.
    if voice_id.startswith("aura"):
        return voice_id

    # Any other value (e.g. an ElevenLabs voice ID) does not apply to the
    # SageMaker Deepgram Aura endpoint. Fall back to the default.
    return default_voice
