"""Tests for Cartesia STT service factory integration."""

import os
from unittest.mock import patch, MagicMock

import pytest


class TestCartesiaSTTFactory:
    """Test the factory routes correctly for Cartesia STT providers."""

    def _make_config(self, stt_provider: str):
        """Create a minimal PipelineConfig-like object for factory testing."""
        from dataclasses import dataclass

        @dataclass
        class FakeConfig:
            stt_provider: str
            stt_endpoint: str = ""
            tts_provider: str = "cartesia"
            tts_endpoint: str = ""
            voice_id: str = "79a125e8-cd45-4c13-8a67-188112f4dd22"
            aws_region: str = "us-east-1"

        return FakeConfig(stt_provider=stt_provider)

    @patch.dict(os.environ, {"CARTESIA_API_KEY": "test-key-123"})
    @patch("pipecat.services.cartesia.stt.CartesiaSTTService")
    def test_cartesia_provider_creates_stt_service(self, mock_stt_class):
        """STT_PROVIDER=cartesia should create CartesiaSTTService with ink-whisper."""
        mock_stt_class.Settings = MagicMock()
        mock_stt_class.return_value = MagicMock()

        from app.services.factory import create_stt_service

        config = self._make_config("cartesia")
        result = create_stt_service(config)

        mock_stt_class.assert_called_once()
        call_kwargs = mock_stt_class.call_args[1]
        assert call_kwargs["api_key"] == "test-key-123"
        assert call_kwargs["sample_rate"] == 8000

    @patch.dict(os.environ, {"CARTESIA_API_KEY": "test-key-456"})
    @patch("pipecat.services.cartesia.turns.stt.CartesiaTurnsSTTService")
    def test_cartesia_turns_provider_creates_turns_service(self, mock_turns_class):
        """STT_PROVIDER=cartesia-turns should create CartesiaTurnsSTTService with ink-2."""
        mock_turns_class.Settings = MagicMock()
        mock_turns_class.return_value = MagicMock()

        from app.services.factory import create_stt_service

        config = self._make_config("cartesia-turns")
        result = create_stt_service(config)

        mock_turns_class.assert_called_once()
        call_kwargs = mock_turns_class.call_args[1]
        assert call_kwargs["api_key"] == "test-key-456"
        assert call_kwargs["sample_rate"] == 8000

    @patch.dict(os.environ, {}, clear=True)
    def test_cartesia_provider_requires_api_key(self):
        """STT_PROVIDER=cartesia without CARTESIA_API_KEY should raise ValueError."""
        # Remove the key if present
        os.environ.pop("CARTESIA_API_KEY", None)

        from app.services.factory import create_stt_service

        config = self._make_config("cartesia")
        with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
            create_stt_service(config)

    @patch.dict(os.environ, {}, clear=True)
    def test_cartesia_turns_provider_requires_api_key(self):
        """STT_PROVIDER=cartesia-turns without CARTESIA_API_KEY should raise ValueError."""
        os.environ.pop("CARTESIA_API_KEY", None)

        from app.services.factory import create_stt_service

        config = self._make_config("cartesia-turns")
        with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
            create_stt_service(config)


class TestCartesiaSTTModule:
    """Test the cartesia_stt module directly."""

    @patch.dict(os.environ, {"CARTESIA_API_KEY": "direct-test-key"})
    @patch("pipecat.services.cartesia.stt.CartesiaSTTService")
    def test_create_cartesia_stt_service_defaults(self, mock_stt_class):
        """Default creation uses ink-whisper at 8000 Hz."""
        mock_stt_class.Settings = MagicMock()
        mock_stt_class.return_value = MagicMock()

        from app.services.cartesia_stt import create_cartesia_stt_service

        result = create_cartesia_stt_service()

        mock_stt_class.assert_called_once()
        call_kwargs = mock_stt_class.call_args[1]
        assert call_kwargs["api_key"] == "direct-test-key"
        assert call_kwargs["sample_rate"] == 8000

    @patch.dict(os.environ, {"CARTESIA_API_KEY": "direct-test-key"})
    @patch("pipecat.services.cartesia.stt.CartesiaSTTService")
    def test_create_cartesia_stt_service_custom_sample_rate(self, mock_stt_class):
        """Custom sample rate is passed through."""
        mock_stt_class.Settings = MagicMock()
        mock_stt_class.return_value = MagicMock()

        from app.services.cartesia_stt import create_cartesia_stt_service

        create_cartesia_stt_service(sample_rate=16000)

        call_kwargs = mock_stt_class.call_args[1]
        assert call_kwargs["sample_rate"] == 16000

    @patch.dict(os.environ, {"CARTESIA_API_KEY": "turns-test-key"})
    @patch("pipecat.services.cartesia.turns.stt.CartesiaTurnsSTTService")
    def test_create_cartesia_turns_with_keyterms(self, mock_turns_class):
        """Keyterms are passed to CartesiaTurnsSTTService settings."""
        mock_settings = MagicMock()
        mock_turns_class.Settings = MagicMock(return_value=mock_settings)
        mock_turns_class.return_value = MagicMock()

        from app.services.cartesia_stt import create_cartesia_turns_stt_service

        create_cartesia_turns_stt_service(keyterms=["Pipecat", "AWS", "voice agent"])

        # Settings should have been created with keyterms
        mock_turns_class.Settings.assert_called_once_with(
            keyterm=["Pipecat", "AWS", "voice agent"]
        )
        call_kwargs = mock_turns_class.call_args[1]
        assert call_kwargs["settings"] == mock_settings

    @patch.dict(os.environ, {"CARTESIA_API_KEY": "turns-test-key"})
    @patch("pipecat.services.cartesia.turns.stt.CartesiaTurnsSTTService")
    def test_create_cartesia_turns_without_keyterms(self, mock_turns_class):
        """Without keyterms, settings is None."""
        mock_turns_class.return_value = MagicMock()

        from app.services.cartesia_stt import create_cartesia_turns_stt_service

        create_cartesia_turns_stt_service()

        call_kwargs = mock_turns_class.call_args[1]
        assert call_kwargs["settings"] is None
