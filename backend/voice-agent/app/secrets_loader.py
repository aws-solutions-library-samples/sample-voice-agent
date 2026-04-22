"""
AWS Secrets Manager Loader

Fetches API keys from AWS Secrets Manager and sets them as environment variables.
This is used when running in ECS to securely inject secrets at runtime.
"""

import json
import os

import boto3
import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger(__name__)


def load_secrets_from_aws() -> bool:
    """
    Load API keys from AWS Secrets Manager and set as environment variables.

    The secret ARN is read from API_KEY_SECRET_ARN environment variable.
    Expected secret format:
    {
        "DAILY_API_KEY": "...",
        "DEEPGRAM_API_KEY": "...",
        "ELEVENLABS_API_KEY": "..."
    }

    Returns:
        True if secrets were loaded successfully, False otherwise
    """
    secret_arn = os.environ.get("API_KEY_SECRET_ARN")
    if not secret_arn:
        logger.info("secrets_load_skipped", reason="API_KEY_SECRET_ARN not set")
        return False

    region = os.environ.get("AWS_REGION", "us-east-1")

    try:
        client = boto3.client("secretsmanager", region_name=region)

        logger.info("fetching_secrets", secret_arn=secret_arn)
        response = client.get_secret_value(SecretId=secret_arn)
        secret_string = response.get("SecretString", "{}")
        secrets = json.loads(secret_string)

        # Set API keys as environment variables
        keys_loaded = []
        for key in ["DAILY_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"]:
            if key in secrets and secrets[key]:
                os.environ[key] = secrets[key]
                keys_loaded.append(key)
                logger.info("secret_key_loaded", key_name=key)

        if keys_loaded:
            logger.info("secrets_loaded", count=len(keys_loaded), keys=keys_loaded)
            return True
        else:
            logger.warning("no_api_keys_found_in_secret")
            return False

    except ClientError as e:
        logger.error(
            "secrets_fetch_failed",
            error=str(e),
            error_type=type(e).__name__,
            secret_arn=secret_arn,
            region=region,
        )
        return False
    except json.JSONDecodeError as e:
        logger.error(
            "secrets_json_parse_failed",
            error=str(e),
            error_type=type(e).__name__,
            secret_arn=secret_arn,
        )
        return False
    except Exception as e:
        logger.error(
            "secrets_load_unexpected_error",
            error=str(e),
            error_type=type(e).__name__,
            secret_arn=secret_arn,
        )
        return False
