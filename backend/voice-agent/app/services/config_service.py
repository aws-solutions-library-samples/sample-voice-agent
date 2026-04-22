"""Configuration Service for reading application settings from SSM Parameter Store.

This module provides a centralized way to read and cache configuration values
from AWS Systems Manager Parameter Store. All configuration is loaded at startup
to avoid repeated SSM API calls during runtime.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import boto3
import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger(__name__)


@dataclass
class KnowledgeBaseConfig:
    """Knowledge Base configuration."""

    id: str
    arn: str
    bucket_name: str
    max_results: int = 3
    min_confidence: float = 0.3


@dataclass
class ProviderConfig:
    """Provider configuration for STT/TTS."""

    stt_provider: str = "deepgram"
    tts_provider: str = "cartesia"
    voice_id: str = "79a125e8-cd45-4c13-8a67-188112f4dd22"


@dataclass
class FeatureFlags:
    """Feature toggle configuration."""

    enable_tool_calling: bool = True
    enable_filler_phrases: bool = False
    enable_conversation_logging: bool = True
    enable_audio_quality_monitoring: bool = True
    disabled_tools: str = ""  # Comma-separated list of tool names to disable


@dataclass
class LLMConfig:
    """LLM model configuration."""

    model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


@dataclass
class AppConfig:
    """Complete application configuration."""

    environment: str = "production"
    log_level: str = "INFO"
    session_table_name: str = ""
    api_key_secret_arn: str = ""
    knowledge_base: KnowledgeBaseConfig = field(
        default_factory=lambda: KnowledgeBaseConfig("", "", "")
    )
    providers: ProviderConfig = field(default_factory=ProviderConfig)
    features: FeatureFlags = field(default_factory=FeatureFlags)
    llm: LLMConfig = field(default_factory=LLMConfig)


class ConfigService:
    """Service for loading and caching configuration from SSM Parameter Store.

    This service reads all configuration from SSM at startup and caches it
    in memory. This eliminates the need for environment variables and allows
    configuration changes without container redeployment.

    Example:
        >>> config = ConfigService()
        >>> await config.load()
        >>> kb_id = config.knowledge_base.id
        >>> log_level = config.log_level
    """

    # SSM parameter paths
    BASE_PATH = "/voice-agent"
    KNOWLEDGE_BASE_PATH = f"{BASE_PATH}/knowledge-base"
    CONFIG_PATH = f"{BASE_PATH}/config"
    SESSIONS_PATH = f"{BASE_PATH}/sessions"
    STORAGE_PATH = f"{BASE_PATH}/storage"

    def __init__(self, region: Optional[str] = None):
        """Initialize the ConfigService.

        Args:
            region: AWS region. If not provided, reads from AWS_REGION env var
                   or defaults to us-east-1.
        """
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._ssm = boto3.client("ssm", region_name=self.region)
        self._config: Optional[AppConfig] = None
        self._loaded = False
        self._refreshing: bool = False

    async def load(self) -> AppConfig:
        """Load all configuration from SSM Parameter Store.

        This method reads all configuration parameters from SSM and caches
        them in memory. It should be called once at application startup.

        Returns:
            AppConfig object with all configuration values

        Raises:
            RuntimeError: If required parameters cannot be loaded
        """
        if self._loaded and self._config is not None:
            logger.debug("config_already_loaded")
            return self._config

        logger.info("loading_config_from_ssm", region=self.region)

        try:
            # Load all parameters in batches (max 10 per call)
            param_names = self._get_all_param_names()
            params = {}

            if param_names:
                # SSM GetParameters has a limit of 10 parameters per call
                batch_size = 10
                for i in range(0, len(param_names), batch_size):
                    batch = param_names[i : i + batch_size]
                    response = self._ssm.get_parameters(
                        Names=batch, WithDecryption=False
                    )

                    # Add to params dictionary
                    params.update(
                        {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
                    )

                    # Log any missing parameters in this batch
                    missing = response.get("InvalidParameters", [])
                    if missing:
                        logger.warning("missing_ssm_parameters", parameters=missing)

            # Build configuration
            self._config = self._build_config(params)
            self._loaded = True

            logger.info(
                "config_loaded_successfully",
                kb_configured=bool(self._config.knowledge_base.id),
                environment=self._config.environment,
            )

            return self._config

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            logger.error(
                "failed_to_load_config",
                error_code=error_code,
                error=str(e),
            )
            raise RuntimeError(f"Failed to load configuration from SSM: {e}")

    async def refresh(self) -> AppConfig:
        """Re-fetch all SSM parameters and update the cached config.

        Unlike load(), this can be called repeatedly. On SSM failure the
        previous config is kept and a warning is logged.

        Returns:
            The (possibly updated) AppConfig.
        """
        if self._refreshing:
            return self.config

        if not self._loaded or self._config is None:
            return await self.load()

        self._refreshing = True
        try:
            param_names = self._get_all_param_names()
            params: Dict[str, str] = {}

            batch_size = 10
            for i in range(0, len(param_names), batch_size):
                batch = param_names[i : i + batch_size]
                response = self._ssm.get_parameters(
                    Names=batch, WithDecryption=False
                )
                params.update(
                    {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
                )

            new_config = self._build_config(params)

            # Log feature flag changes
            old_flags = self._config.features
            new_flags = new_config.features
            for flag_name in vars(old_flags):
                old_val = getattr(old_flags, flag_name)
                new_val = getattr(new_flags, flag_name)
                if old_val != new_val:
                    logger.info(
                        "config_flag_changed",
                        flag=flag_name,
                        old_value=old_val,
                        new_value=new_val,
                    )

            self._config = new_config

        except ClientError as e:
            logger.warning(
                "config_refresh_failed",
                error=str(e),
                note="keeping_previous_config",
            )
        except Exception as e:
            logger.warning(
                "config_refresh_unexpected_error",
                error=str(e),
                error_type=type(e).__name__,
                note="keeping_previous_config",
            )
        finally:
            self._refreshing = False

        return self._config

    def _get_all_param_names(self) -> List[str]:
        """Get list of all SSM parameter names to load."""
        return [
            # Knowledge Base
            f"{self.KNOWLEDGE_BASE_PATH}/id",
            f"{self.KNOWLEDGE_BASE_PATH}/arn",
            f"{self.KNOWLEDGE_BASE_PATH}/bucket-name",
            f"{self.CONFIG_PATH}/kb-max-results",
            f"{self.CONFIG_PATH}/kb-min-confidence",
            # App Config
            f"{self.CONFIG_PATH}/log-level",
            f"{self.CONFIG_PATH}/stt-provider",
            f"{self.CONFIG_PATH}/tts-provider",
            f"{self.CONFIG_PATH}/voice-id",
            f"{self.CONFIG_PATH}/enable-tool-calling",
            f"{self.CONFIG_PATH}/enable-filler-phrases",
            f"{self.CONFIG_PATH}/enable-conversation-logging",
            f"{self.CONFIG_PATH}/enable-audio-quality-monitoring",
            f"{self.CONFIG_PATH}/disabled-tools",
            # LLM
            f"{self.CONFIG_PATH}/llm-model-id",
            # Infrastructure
            f"{self.SESSIONS_PATH}/table-name",
            f"{self.STORAGE_PATH}/api-key-secret-arn",
        ]

    def _build_config(self, params: Dict[str, str]) -> AppConfig:
        """Build AppConfig from SSM parameters."""

        # Knowledge Base config
        kb_config = KnowledgeBaseConfig(
            id=params.get(f"{self.KNOWLEDGE_BASE_PATH}/id", ""),
            arn=params.get(f"{self.KNOWLEDGE_BASE_PATH}/arn", ""),
            bucket_name=params.get(f"{self.KNOWLEDGE_BASE_PATH}/bucket-name", ""),
            max_results=int(params.get(f"{self.CONFIG_PATH}/kb-max-results", "3")),
            min_confidence=float(
                params.get(f"{self.CONFIG_PATH}/kb-min-confidence", "0.3")
            ),
        )

        # Provider config
        provider_config = ProviderConfig(
            stt_provider=params.get(f"{self.CONFIG_PATH}/stt-provider", "deepgram"),
            tts_provider=params.get(f"{self.CONFIG_PATH}/tts-provider", "cartesia"),
            voice_id=params.get(
                f"{self.CONFIG_PATH}/voice-id", "79a125e8-cd45-4c13-8a67-188112f4dd22"
            ),
        )

        # Feature flags
        features = FeatureFlags(
            enable_tool_calling=params.get(
                f"{self.CONFIG_PATH}/enable-tool-calling", "true"
            ).lower()
            == "true",
            enable_filler_phrases=params.get(
                f"{self.CONFIG_PATH}/enable-filler-phrases", "false"
            ).lower()
            == "true",
            enable_conversation_logging=params.get(
                f"{self.CONFIG_PATH}/enable-conversation-logging", "true"
            ).lower()
            == "true",
            enable_audio_quality_monitoring=params.get(
                f"{self.CONFIG_PATH}/enable-audio-quality-monitoring", "true"
            ).lower()
            == "true",
            disabled_tools=params.get(f"{self.CONFIG_PATH}/disabled-tools", ""),
        )

        # LLM config - SSM parameter with env var fallback
        default_model = os.environ.get(
            "LLM_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        llm_config = LLMConfig(
            model_id=params.get(f"{self.CONFIG_PATH}/llm-model-id", default_model),
        )

        # Build complete config
        config = AppConfig(
            environment=os.environ.get("ENVIRONMENT", "production"),
            log_level=params.get(f"{self.CONFIG_PATH}/log-level", "INFO"),
            session_table_name=params.get(f"{self.SESSIONS_PATH}/table-name", ""),
            api_key_secret_arn=params.get(
                f"{self.STORAGE_PATH}/api-key-secret-arn", ""
            ),
            knowledge_base=kb_config,
            providers=provider_config,
            features=features,
            llm=llm_config,
        )

        return config

    @property
    def config(self) -> AppConfig:
        """Get the loaded configuration.

        Raises:
            RuntimeError: If configuration has not been loaded yet
        """
        if not self._loaded or self._config is None:
            raise RuntimeError("Configuration not loaded. Call load() first.")
        return self._config

    @property
    def knowledge_base(self) -> KnowledgeBaseConfig:
        """Get Knowledge Base configuration."""
        return self.config.knowledge_base

    @property
    def providers(self) -> ProviderConfig:
        """Get provider configuration."""
        return self.config.providers

    @property
    def features(self) -> FeatureFlags:
        """Get feature flags."""
        return self.config.features

    @property
    def llm(self) -> LLMConfig:
        """Get LLM configuration."""
        return self.config.llm

    def is_configured(self) -> bool:
        """Check if configuration has been loaded."""
        return self._loaded and self._config is not None


# Global instance for singleton pattern
_config_service: Optional[ConfigService] = None


def get_config_service() -> ConfigService:
    """Get the global ConfigService instance.

    This function provides a singleton pattern for the ConfigService,
    ensuring that configuration is only loaded once per process.

    Returns:
        ConfigService instance
    """
    global _config_service
    if _config_service is None:
        _config_service = ConfigService()
    return _config_service


async def load_config() -> AppConfig:
    """Convenience function to load configuration.

    This is a shortcut for:
        >>> config = await get_config_service().load()

    Returns:
        AppConfig with all configuration values
    """
    return await get_config_service().load()
