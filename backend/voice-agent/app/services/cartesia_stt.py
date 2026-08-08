"""
Cartesia Ink 2 STT service for turn-aware speech recognition.

Uses Pipecat's CartesiaTurnsSTTService which connects to Cartesia's v2
WebSocket API with the ink-2 model. The server drives turn boundaries
natively — no external VAD is needed for turn detection.

Key features of ink-2:
- Lowest word error rate of any streaming STT model
- Native turn detection (turn.start, turn.update, turn.eager_end, turn.resume, turn.end)
- Handles structured data (phone numbers, dates, emails) natively
- Supports keyterms for domain-specific vocabulary biasing

Usage:
    Set STT_PROVIDER=cartesia-turns in SSM or environment.
    Requires CARTESIA_API_KEY (same key used for TTS).
"""

import os

import structlog

logger = structlog.get_logger(__name__)


def create_cartesia_stt_service(*, sample_rate: int = 8000):
    """Create a CartesiaSTTService (ink-whisper) for standard VAD-driven STT.

    This uses the standard Cartesia WebSocket API with the ink-whisper model.
    Requires an external VAD (Silero) for turn boundary detection.

    Args:
        sample_rate: Audio sample rate in Hz (default 8000 for PSTN)

    Returns:
        CartesiaSTTService instance

    Raises:
        ValueError: If CARTESIA_API_KEY is not set
    """
    from pipecat.services.cartesia.stt import CartesiaSTTService

    api_key = os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise ValueError(
            "CARTESIA_API_KEY environment variable required for Cartesia STT"
        )

    logger.info("creating_cartesia_stt", model="ink-whisper", sample_rate=sample_rate)

    return CartesiaSTTService(
        api_key=api_key,
        settings=CartesiaSTTService.Settings(
            model="ink-whisper",
            language="en",
        ),
        sample_rate=sample_rate,
    )


def create_cartesia_turns_stt_service(
    *,
    sample_rate: int = 8000,
    keyterms: list[str] | None = None,
):
    """Create a CartesiaTurnsSTTService (ink-2) with native turn detection.

    This uses Cartesia's v2 WebSocket API with the ink-2 model. The server
    drives turn boundaries via structured events, so no external VAD is needed
    for determining when the user has finished speaking.

    Args:
        sample_rate: Audio sample rate in Hz (default 8000 for PSTN)
        keyterms: Optional list of domain-specific terms to bias transcription

    Returns:
        CartesiaTurnsSTTService instance

    Raises:
        ValueError: If CARTESIA_API_KEY is not set
    """
    from pipecat.services.cartesia.turns.stt import CartesiaTurnsSTTService

    api_key = os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise ValueError(
            "CARTESIA_API_KEY environment variable required for Cartesia STT"
        )

    settings = None
    if keyterms:
        settings = CartesiaTurnsSTTService.Settings(keyterm=keyterms)
        logger.info(
            "creating_cartesia_turns_stt",
            model="ink-2",
            sample_rate=sample_rate,
            keyterms_count=len(keyterms),
        )
    else:
        logger.info(
            "creating_cartesia_turns_stt",
            model="ink-2",
            sample_rate=sample_rate,
        )

    return CartesiaTurnsSTTService(
        api_key=api_key,
        sample_rate=sample_rate,
        settings=settings,
    )
