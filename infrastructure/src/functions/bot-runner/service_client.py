"""
ECS Service Client for Voice Agent.

Makes HTTP calls to the always-on ECS service to handle voice calls.
This replaces the task-based approach that had cold start issues.

The ECS service endpoint is read from SSM Parameter Store at runtime,
allowing the endpoint to be updated without redeploying the Lambda.
"""

import json
import logging
import os
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import boto3

logger = logging.getLogger(__name__)

# SSM Parameter Store path for ECS service endpoint
ECS_ENDPOINT_PARAM = "/voice-agent/ecs/service-endpoint"


class EcsServiceClient:
    """Client for calling the ECS voice service."""

    def __init__(self):
        """Initialize the client with ECS service endpoint from SSM."""
        # Try to get endpoint from SSM Parameter Store first
        self.service_endpoint = self._get_endpoint_from_ssm()

        # Fallback to environment variable (for backwards compatibility)
        if not self.service_endpoint:
            self.service_endpoint = os.environ.get("ECS_SERVICE_ENDPOINT", "")
            if self.service_endpoint:
                logger.info("Using ECS endpoint from environment variable")

        if not self.service_endpoint:
            raise ValueError(
                f"ECS service endpoint not found. "
                f"Please ensure SSM parameter {ECS_ENDPOINT_PARAM} is set "
                f"or ECS_SERVICE_ENDPOINT environment variable is configured."
            )

        # Remove trailing slash
        self.service_endpoint = self.service_endpoint.rstrip("/")
        logger.info(f"ECS Service endpoint: {self.service_endpoint}")

    def _get_endpoint_from_ssm(self) -> Optional[str]:
        """
        Retrieve ECS service endpoint from SSM Parameter Store.

        Returns:
            The endpoint URL or None if not found
        """
        try:
            ssm = boto3.client("ssm")
            response = ssm.get_parameter(Name=ECS_ENDPOINT_PARAM, WithDecryption=False)
            endpoint = response["Parameter"]["Value"]
            logger.info(f"Retrieved ECS endpoint from SSM: {ECS_ENDPOINT_PARAM}")
            return endpoint
        except Exception as e:
            logger.warning(f"Failed to retrieve endpoint from SSM: {e}")
            return None

    def start_call(
        self,
        room_url: str,
        room_token: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        dialin_settings: Optional[dict] = None,
        agent_id: Optional[str] = None,
        case_data: Optional[dict] = None,
    ) -> dict:
        """
        Start a voice call by sending request to the ECS service.

        Args:
            room_url: Daily room URL
            room_token: Bot meeting token
            session_id: Unique session identifier
            system_prompt: Optional custom system prompt
            dialin_settings: Optional dial-in configuration. Pass None
                for outbound calls (Phase 7D) — the pipeline runs
                identically but joins a room that's been pre-bridged
                via Daily's dialOut, not waiting for an inbound SIP.
            agent_id: Optional agent UUID or name. When set, the Fargate
                pipeline's Phase 7A runtime-config loader pulls that
                agent's full config from the voice-api Lambda. Omitted
                for SIP / legacy callers that still provide a raw
                ``system_prompt``.
            case_data: Optional placeholder dict for the hydrator (Phase
                7A). Outbound batch calls fill this from the batch row's
                case_data column so {{Service_Date}} / {{Patient_Name}}
                etc render against real values before reaching Bedrock.

        Returns:
            Response from the service
        """
        url = f"{self.service_endpoint}/call"

        payload = {
            "room_url": room_url,
            "room_token": room_token,
            "session_id": session_id,
        }

        if system_prompt:
            payload["system_prompt"] = system_prompt

        if agent_id:
            payload["agent_id"] = agent_id

        if dialin_settings:
            payload["dialin_settings"] = dialin_settings  # type: ignore

        if case_data:
            payload["case_data"] = case_data  # type: ignore

        logger.info(f"Sending call request to service: session_id={session_id}")

        try:
            request = Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urlopen(request, timeout=10) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                logger.info(f"Service response: {response_data}")
                return response_data

        except HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else str(e)
            logger.error(f"Service returned error {e.code}: {error_body}")
            return {
                "status": "error",
                "error": f"Service error: {e.code}",
                "details": error_body,
            }

        except URLError as e:
            logger.error(f"Failed to connect to service: {e.reason}")
            return {
                "status": "error",
                "error": f"Connection failed: {e.reason}",
            }

        except Exception as e:
            logger.error(f"Unexpected error calling service: {e}")
            return {
                "status": "error",
                "error": str(e),
            }

    def get_health(self) -> dict:
        """Check the health of the ECS service."""
        url = f"{self.service_endpoint}/health"

        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {
                "status": "unhealthy",
                "error": str(e),
            }
