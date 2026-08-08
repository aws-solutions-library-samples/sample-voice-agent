"""
LLM service factory for voice pipeline.

Supports multiple LLM providers via Amazon Bedrock:
- Claude (default): Via Converse API on bedrock-runtime
- NVIDIA Nemotron Super: Via Converse API on bedrock-runtime
- OpenAI GPT-5.6 Terra: Via OpenAI-compatible Responses API on bedrock-mantle

The bedrock-mantle endpoint provides OpenAI SDK compatibility, allowing models
that only support the Responses/Chat Completions API to be used via Pipecat's
OpenAILLMService with a custom base_url.
"""

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.pipeline_ecs import PipelineConfig

logger = structlog.get_logger(__name__)

# Models that use the Converse API via bedrock-runtime (AWSBedrockLLMService)
CONVERSE_MODELS = {
    # Claude models (default)
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-sonnet-4-20250514-v1:0",
    # NVIDIA Nemotron models
    "nvidia.nemotron-super-3-120b",
    "nvidia.nemotron-nano-3-30b",
    "nvidia.nemotron-nano-3-12b-v2",
    "nvidia.nemotron-nano-3-9b-v2",
}

# Models that use the Responses API via bedrock-mantle (OpenAILLMService)
MANTLE_MODELS = {
    "openai.gpt-5.6-luna",
    "openai.gpt-5.6-terra",
    "openai.gpt-5.6-sol",
    "openai.gpt-oss-120b",
    "openai.gpt-oss-20b",
}

# OpenAI GPT-5.6 models use a different path prefix on bedrock-mantle
OPENAI_NATIVE_MODELS = {
    "openai.gpt-5.6-terra",
    "openai.gpt-5.6-sol",
    "openai.gpt-5.6-luna",
}


def _get_mantle_base_url(region: str, model_id: str) -> str:
    """Get the bedrock-mantle base URL for a given region and model.

    OpenAI GPT-5.6 models use /openai/v1 path, while other mantle models use /v1.

    Args:
        region: AWS region (e.g., "us-east-1")
        model_id: The model identifier

    Returns:
        Full base URL for the bedrock-mantle endpoint
    """
    if model_id in OPENAI_NATIVE_MODELS:
        return f"https://bedrock-mantle.{region}.api.aws/openai/v1"
    return f"https://bedrock-mantle.{region}.api.aws/v1"


def _get_mantle_api_key() -> str:
    """Get the Bedrock API key for bedrock-mantle authentication.

    The bedrock-mantle endpoint requires a Bedrock API key (not AWS credentials).
    Create one via the Bedrock console or API.

    Returns:
        Bedrock API key

    Raises:
        ValueError: If BEDROCK_API_KEY is not set
    """
    api_key = os.getenv("BEDROCK_API_KEY")
    if not api_key:
        raise ValueError(
            "BEDROCK_API_KEY environment variable required for bedrock-mantle models "
            "(OpenAI GPT-5.6, GPT OSS). Create one in the Amazon Bedrock console."
        )
    return api_key


def is_mantle_model(model_id: str) -> bool:
    """Check if a model requires the bedrock-mantle endpoint.

    Args:
        model_id: The model identifier

    Returns:
        True if the model uses bedrock-mantle, False for bedrock-runtime
    """
    return model_id in MANTLE_MODELS


def create_llm_service(model_id: str, region: str):
    """Create the appropriate LLM service for the given model.

    Routes to either:
    - AWSBedrockLLMService (Converse API) for Claude and Nemotron models
    - OpenAILLMService (Chat Completions/Responses) for GPT models via bedrock-mantle

    Args:
        model_id: Bedrock model identifier
        region: AWS region

    Returns:
        Configured LLM service instance
    """
    if is_mantle_model(model_id):
        from pipecat.services.openai.llm import OpenAILLMService

        base_url = _get_mantle_base_url(region, model_id)
        api_key = _get_mantle_api_key()

        logger.info(
            "llm_service_created",
            provider="bedrock-mantle",
            model=model_id,
            base_url=base_url,
        )

        return OpenAILLMService(
            model=model_id,
            api_key=api_key,
            base_url=base_url,
            params=OpenAILLMService.InputParams(
                max_tokens=256,
                temperature=0.7,
            ),
        )

    else:
        from pipecat.services.aws.llm import AWSBedrockLLMService

        logger.info(
            "llm_service_created",
            provider="bedrock-runtime",
            model=model_id,
            region=region,
        )

        return AWSBedrockLLMService(
            model=model_id,
            region=region,
            params=AWSBedrockLLMService.InputParams(
                max_tokens=256,
                temperature=0.7,
            ),
        )
