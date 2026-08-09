"""Tests for LLM service factory."""

import os
from unittest.mock import patch, MagicMock

import pytest

from app.services.llm_factory import (
    is_mantle_model,
    create_llm_service,
    _get_mantle_base_url,
    CONVERSE_MODELS,
    MANTLE_MODELS,
    OPENAI_NATIVE_MODELS,
)


class TestModelClassification:
    """Test model routing logic."""

    def test_claude_is_converse_model(self):
        assert not is_mantle_model("us.anthropic.claude-haiku-4-5-20251001-v1:0")

    def test_nemotron_is_converse_model(self):
        assert not is_mantle_model("nvidia.nemotron-super-3-120b")

    def test_gpt_luna_is_mantle_model(self):
        assert is_mantle_model("openai.gpt-5.6-luna")

    def test_gpt_terra_is_mantle_model(self):
        assert is_mantle_model("openai.gpt-5.6-terra")

    def test_gpt_sol_is_mantle_model(self):
        assert is_mantle_model("openai.gpt-5.6-sol")

    def test_gpt_oss_is_mantle_model(self):
        assert is_mantle_model("openai.gpt-oss-120b")

    def test_unknown_model_is_converse(self):
        """Unknown models default to Converse API (bedrock-runtime)."""
        assert not is_mantle_model("some.unknown-model-v1")


class TestMantleBaseUrl:
    """Test bedrock-mantle URL generation."""

    def test_openai_native_models_use_openai_path(self):
        url = _get_mantle_base_url("us-east-1", "openai.gpt-5.6-luna")
        assert url == "https://bedrock-mantle.us-east-1.api.aws/openai/v1"

    def test_openai_terra_uses_openai_path(self):
        url = _get_mantle_base_url("us-west-2", "openai.gpt-5.6-terra")
        assert url == "https://bedrock-mantle.us-west-2.api.aws/openai/v1"

    def test_gpt_oss_uses_standard_path(self):
        url = _get_mantle_base_url("us-east-1", "openai.gpt-oss-120b")
        assert url == "https://bedrock-mantle.us-east-1.api.aws/v1"

    def test_region_substitution(self):
        url = _get_mantle_base_url("eu-west-1", "openai.gpt-5.6-sol")
        assert url == "https://bedrock-mantle.eu-west-1.api.aws/openai/v1"


class TestCreateLlmService:
    """Test factory function creates correct service types."""

    @patch("pipecat.services.aws.llm.AWSBedrockLLMService")
    def test_claude_creates_bedrock_service(self, mock_bedrock_class):
        mock_bedrock_class.InputParams = MagicMock()
        mock_bedrock_class.return_value = MagicMock()

        result = create_llm_service(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            region="us-east-1",
        )

        mock_bedrock_class.assert_called_once()
        call_kwargs = mock_bedrock_class.call_args[1]
        assert call_kwargs["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert call_kwargs["region"] == "us-east-1"

    @patch("pipecat.services.aws.llm.AWSBedrockLLMService")
    def test_nemotron_creates_bedrock_service(self, mock_bedrock_class):
        mock_bedrock_class.InputParams = MagicMock()
        mock_bedrock_class.return_value = MagicMock()

        result = create_llm_service(
            model_id="nvidia.nemotron-super-3-120b",
            region="us-east-1",
        )

        mock_bedrock_class.assert_called_once()
        call_kwargs = mock_bedrock_class.call_args[1]
        assert call_kwargs["model"] == "nvidia.nemotron-super-3-120b"

    @patch.dict(os.environ, {"BEDROCK_API_KEY": "test-bedrock-key"})
    @patch("pipecat.services.openai.llm.OpenAILLMService")
    def test_gpt_luna_creates_openai_service(self, mock_openai_class):
        mock_openai_class.InputParams = MagicMock()
        mock_openai_class.return_value = MagicMock()

        result = create_llm_service(
            model_id="openai.gpt-5.6-luna",
            region="us-east-1",
        )

        mock_openai_class.assert_called_once()
        call_kwargs = mock_openai_class.call_args[1]
        assert call_kwargs["model"] == "openai.gpt-5.6-luna"
        assert call_kwargs["api_key"] == "test-bedrock-key"
        assert call_kwargs["base_url"] == "https://bedrock-mantle.us-east-1.api.aws/openai/v1"

    @patch.dict(os.environ, {}, clear=True)
    def test_gpt_luna_requires_api_key(self):
        os.environ.pop("BEDROCK_API_KEY", None)

        with pytest.raises(ValueError, match="BEDROCK_API_KEY"):
            create_llm_service(
                model_id="openai.gpt-5.6-luna",
                region="us-east-1",
            )

    @patch.dict(os.environ, {"BEDROCK_API_KEY": "test-key"})
    @patch("pipecat.services.openai.llm.OpenAILLMService")
    def test_gpt_oss_uses_standard_mantle_path(self, mock_openai_class):
        mock_openai_class.InputParams = MagicMock()
        mock_openai_class.return_value = MagicMock()

        create_llm_service(
            model_id="openai.gpt-oss-120b",
            region="us-west-2",
        )

        call_kwargs = mock_openai_class.call_args[1]
        assert call_kwargs["base_url"] == "https://bedrock-mantle.us-west-2.api.aws/v1"

    @patch("pipecat.services.aws.llm.AWSBedrockLLMService")
    def test_unknown_model_defaults_to_bedrock(self, mock_bedrock_class):
        """Unknown models not in MANTLE_MODELS default to Converse/bedrock-runtime."""
        mock_bedrock_class.InputParams = MagicMock()
        mock_bedrock_class.return_value = MagicMock()

        result = create_llm_service(
            model_id="some.unknown-model-v1",
            region="us-east-1",
        )

        mock_bedrock_class.assert_called_once()
        call_kwargs = mock_bedrock_class.call_args[1]
        assert call_kwargs["model"] == "some.unknown-model-v1"
        assert call_kwargs["region"] == "us-east-1"
