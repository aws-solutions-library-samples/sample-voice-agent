"""
Service factory for STT and TTS providers.

Supports switching between cloud APIs and SageMaker endpoints via configuration:
- STT_PROVIDER: "deepgram" (default, cloud API) or "sagemaker" (Deepgram on SageMaker)
- TTS_PROVIDER: "cartesia" (default, cloud API) or "sagemaker" (Deepgram Aura on SageMaker)

Cloud APIs are the default for simpler deployment without SageMaker endpoints.
SageMaker providers use HTTP/2 bidirectional streaming for low-latency, VPC-local inference.
"""

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.pipeline_ecs import PipelineConfig

logger = structlog.get_logger(__name__)


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

    if provider == "flux-sagemaker":
        from app.services.deepgram_flux_sagemaker_stt import DeepgramFluxSageMakerSTTService

        from app.services.sagemaker_credentials import patch_sagemaker_bidi_credentials

        patch_sagemaker_bidi_credentials()

        endpoint_name = config.flux_stt_endpoint or config.stt_endpoint
        if not endpoint_name:
            raise ValueError(
                "FLUX_STT_ENDPOINT_NAME (or STT_ENDPOINT_NAME) is required when STT_PROVIDER=flux-sagemaker"
            )

        logger.info(
            "stt_provider_selected",
            provider="flux-sagemaker",
            endpoint=endpoint_name,
            region=config.aws_region,
        )
        return DeepgramFluxSageMakerSTTService(
            endpoint_name=endpoint_name,
            region=config.aws_region,
            settings=DeepgramFluxSageMakerSTTService.Settings(min_confidence=0.3),
        )

    elif provider == "sagemaker":
        from app.services.deepgram_sagemaker_stt import DeepgramSageMakerSTTService

        # deepgram-sdk 6.x removed LiveOptions; Pipecat ships a compat wrapper.
        from pipecat.services.deepgram.stt import LiveOptions

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
                # Must match the transport's sample rate. Local WebRTC runs at
                # 16000 (LOCAL_SAMPLE_RATE in pipeline_local.py); 8000 is the
                # PSTN/telephony rate. A mismatch makes the BiDi handshake hang
                # silently — Pipecat's built-in STT service has no timeout.
                sample_rate=int(os.environ.get("STT_SAMPLE_RATE", "16000")),
                channels=1,
            ),
        )

    else:
        # Default to Deepgram cloud API
        from pipecat.services.deepgram.stt import DeepgramSTTService

        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise ValueError("DEEPGRAM_API_KEY environment variable required for STT")

        logger.info("stt_provider_selected", provider="deepgram")
        return DeepgramSTTService(
            api_key=api_key,
            sample_rate=8000,
        )


def create_tts_service(config: "PipelineConfig"):
    """
    Create TTS service based on provider configuration.

    Supports:
    - "cartesia": Cloud HTTP API (requires CARTESIA_API_KEY)
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

        # For SageMaker TTS, voice_id should be a Deepgram Aura voice name
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
        # Default to Cartesia cloud API
        from pipecat.services.cartesia.tts import CartesiaTTSService

        api_key = os.getenv("CARTESIA_API_KEY")
        if not api_key:
            raise ValueError("CARTESIA_API_KEY environment variable required for TTS")

        # Map voice ID to Cartesia format if needed
        voice_id = _map_voice_id_to_cartesia(config.voice_id)

        logger.info("tts_provider_selected", provider="cartesia", voice_id=voice_id)
        return CartesiaTTSService(
            api_key=api_key,
            voice_id=voice_id,
            sample_rate=8000,
        )


def _resolve_voice_for_sagemaker(voice_id: str | None) -> str:
    """
    Resolve a voice ID to a Deepgram Aura voice name for SageMaker TTS.

    If the voice_id is a Cartesia UUID, maps it to an equivalent Deepgram Aura voice.
    If it's already a Deepgram Aura voice name, returns it directly.

    Args:
        voice_id: Cartesia UUID or Deepgram Aura voice name

    Returns:
        Deepgram Aura voice name (e.g., "aura-2-thalia-en")
    """
    default_voice = "aura-2-thalia-en"

    if not voice_id:
        return default_voice

    # If it starts with "aura", it's already a Deepgram voice name
    if voice_id.startswith("aura"):
        return voice_id

    # Map Cartesia UUIDs to Deepgram Aura equivalents
    cartesia_to_deepgram = {
        # Female voices
        "79a125e8-cd45-4c13-8a67-188112f4dd22": "aura-2-thalia-en",  # British Lady -> Thalia
        "b7d50908-b17c-442d-ad8d-810c63997ed9": "aura-2-luna-en",  # California Girl -> Luna
        "5345cf08-6f37-424d-a5d9-8ae1101b9377": "aura-2-asteria-en",  # Sweet Lady -> Asteria
        # Male voices
        "a0e99841-438c-4a64-b679-ae501e7d6091": "aura-2-arcas-en",  # Barbershop Man -> Arcas
        "fb26447f-308b-471e-8b00-8e9f04284eb5": "aura-2-orpheus-en",  # Doctor Mischief -> Orpheus
    }

    return cartesia_to_deepgram.get(voice_id, default_voice)


def _map_voice_id_to_cartesia(voice_id: str | None) -> str:
    """
    Map voice IDs to Cartesia format.

    If it's already a Cartesia UUID, returns it directly.
    If it's a Deepgram Aura voice name, maps it to a similar Cartesia voice.

    Args:
        voice_id: Deepgram voice ID or Cartesia voice ID

    Returns:
        Cartesia voice ID
    """
    # Default Cartesia voice (British Lady - clear and professional)
    default_voice = "79a125e8-cd45-4c13-8a67-188112f4dd22"

    if not voice_id:
        return default_voice

    # If it looks like a Cartesia UUID, use it directly
    if len(voice_id) == 36 and voice_id.count("-") == 4:
        return voice_id

    # Map Deepgram Aura voices to similar Cartesia voices
    voice_mapping = {
        # Female voices
        "aura-asteria-en": "79a125e8-cd45-4c13-8a67-188112f4dd22",
        "aura-luna-en": "b7d50908-b17c-442d-ad8d-810c63997ed9",
        "aura-stella-en": "5345cf08-6f37-424d-a5d9-8ae1101b9377",
        "aura-athena-en": "79a125e8-cd45-4c13-8a67-188112f4dd22",
        "aura-2-thalia-en": "79a125e8-cd45-4c13-8a67-188112f4dd22",
        "aura-2-asteria-en": "5345cf08-6f37-424d-a5d9-8ae1101b9377",
        "aura-2-luna-en": "b7d50908-b17c-442d-ad8d-810c63997ed9",
        # Male voices
        "aura-orion-en": "a0e99841-438c-4a64-b679-ae501e7d6091",
        "aura-arcas-en": "fb26447f-308b-471e-8b00-8e9f04284eb5",
        "aura-perseus-en": "a0e99841-438c-4a64-b679-ae501e7d6091",
        "aura-orpheus-en": "a0e99841-438c-4a64-b679-ae501e7d6091",
        "aura-2-arcas-en": "fb26447f-308b-471e-8b00-8e9f04284eb5",
        "aura-2-orpheus-en": "a0e99841-438c-4a64-b679-ae501e7d6091",
    }

    return voice_mapping.get(voice_id, default_voice)
