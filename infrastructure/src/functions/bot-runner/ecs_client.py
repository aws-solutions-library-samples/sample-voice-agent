"""
ECS Client

Handles starting ECS Fargate tasks for Pipecat voice sessions.
"""

import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class EcsClient:
    """Client for AWS ECS to start Pipecat voice sessions."""

    def __init__(
        self,
        cluster_arn: str | None = None,
        task_definition_arn: str | None = None,
        security_group_id: str | None = None,
    ):
        """
        Initialize ECS client.

        Args:
            cluster_arn: ECS cluster ARN. If not provided, read from environment.
            task_definition_arn: Task definition ARN. If not provided, read from environment.
            security_group_id: Security group ID for tasks. If not provided, read from environment.
        """
        self._cluster_arn = cluster_arn or os.environ.get("ECS_CLUSTER_ARN")
        self._task_definition_arn = task_definition_arn or os.environ.get(
            "ECS_TASK_DEFINITION_ARN"
        )
        self._security_group_id = security_group_id or os.environ.get("ECS_TASK_SG_ID")
        self._subnet_ids = os.environ.get("PRIVATE_SUBNET_IDS", "").split(",")

        if not self._cluster_arn:
            raise ValueError("ECS_CLUSTER_ARN not set")
        if not self._task_definition_arn:
            raise ValueError("ECS_TASK_DEFINITION_ARN not set")

        # Extract region from ARN
        arn_parts = self._cluster_arn.split(":")
        self._region = arn_parts[3] if len(arn_parts) > 3 else "us-east-1"

        # Configure client with retry settings
        config = Config(
            retries={
                "max_attempts": 3,
                "mode": "adaptive",
            },
            connect_timeout=10,
            read_timeout=30,
        )

        self._client = boto3.client(
            "ecs",
            region_name=self._region,
            config=config,
        )

    def start_task(
        self,
        session_id: str,
        room_url: str,
        room_token: str,
        caller_id: str,
        system_prompt: str | None = None,
        voice_id: str = "79a125e8-cd45-4c13-8a67-188112f4dd22",
        dialin_settings: dict | None = None,
    ) -> dict[str, Any]:
        """
        Start an ECS Fargate task for a voice session.

        This starts a new Pipecat container that connects to the Daily room
        and handles the voice conversation.

        Args:
            session_id: Unique identifier for this voice session
            room_url: Daily room URL for WebRTC connection
            room_token: Daily meeting token for bot authentication
            caller_id: Phone number or identifier of the caller
            system_prompt: Custom system prompt for Claude
            voice_id: TTS voice ID (ElevenLabs voice ID for cloud TTS)
            dialin_settings: Dict with call_id, call_domain, sip_uri for pinless dial-in

        Returns:
            Response with task ARN and status
        """
        # Build environment variable overrides for the container
        env_overrides = [
            {"name": "ROOM_URL", "value": room_url},
            {"name": "ROOM_TOKEN", "value": room_token},
            {"name": "SESSION_ID", "value": session_id},
            {"name": "VOICE_ID", "value": voice_id},
        ]

        if system_prompt:
            env_overrides.append({"name": "SYSTEM_PROMPT", "value": system_prompt})

        if dialin_settings:
            if dialin_settings.get("call_id"):
                env_overrides.append(
                    {"name": "DIALIN_CALL_ID", "value": dialin_settings["call_id"]}
                )
            if dialin_settings.get("call_domain"):
                env_overrides.append(
                    {
                        "name": "DIALIN_CALL_DOMAIN",
                        "value": dialin_settings["call_domain"],
                    }
                )
            if dialin_settings.get("sip_uri"):
                env_overrides.append(
                    {"name": "DIALIN_SIP_URI", "value": dialin_settings["sip_uri"]}
                )

        logger.info(f"Starting ECS task for session: {session_id}")

        try:
            # Build run_task parameters
            run_params = {
                "cluster": self._cluster_arn,
                "taskDefinition": self._task_definition_arn,
                "launchType": "FARGATE",
                "count": 1,
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": "pipecat",  # Must match container name in task def
                            "environment": env_overrides,
                        }
                    ],
                },
                "tags": [
                    {"key": "SessionId", "value": session_id},
                    {"key": "CallerId", "value": caller_id or "unknown"},
                ],
            }

            # Add network configuration if we have subnet and security group info
            if self._subnet_ids and self._subnet_ids[0] and self._security_group_id:
                run_params["networkConfiguration"] = {
                    "awsvpcConfiguration": {
                        "subnets": [s for s in self._subnet_ids if s],
                        "securityGroups": [self._security_group_id],
                        "assignPublicIp": "DISABLED",
                    }
                }

            response = self._client.run_task(**run_params)

            # Check if task started successfully
            tasks = response.get("tasks", [])
            failures = response.get("failures", [])

            if failures:
                failure_reasons = [
                    f"{f.get('arn', 'unknown')}: {f.get('reason', 'unknown')}"
                    for f in failures
                ]
                logger.error(f"ECS task failures: {failure_reasons}")
                return {
                    "status": "failed",
                    "session_id": session_id,
                    "error": failure_reasons,
                }

            if tasks:
                task = tasks[0]
                task_arn = task.get("taskArn", "")
                logger.info(f"ECS task started: {task_arn}")
                return {
                    "status": "started",
                    "session_id": session_id,
                    "task_arn": task_arn,
                    "last_status": task.get("lastStatus", "PENDING"),
                }

            return {
                "status": "unknown",
                "session_id": session_id,
                "message": "No tasks or failures returned",
            }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_message = e.response.get("Error", {}).get("Message", str(e))
            logger.error(f"ECS run_task failed: {error_code} - {error_message}")
            raise

    def get_task_status(self, task_arn: str) -> dict:
        """
        Get status of a running task.

        Args:
            task_arn: ARN of the task to check

        Returns:
            Task status information
        """
        try:
            response = self._client.describe_tasks(
                cluster=self._cluster_arn,
                tasks=[task_arn],
            )

            tasks = response.get("tasks", [])
            if tasks:
                task = tasks[0]
                return {
                    "status": task.get("lastStatus", "UNKNOWN"),
                    "task_arn": task_arn,
                    "desired_status": task.get("desiredStatus"),
                    "started_at": str(task.get("startedAt", "")),
                    "stopped_at": str(task.get("stoppedAt", "")),
                    "stop_code": task.get("stopCode"),
                    "stopped_reason": task.get("stoppedReason"),
                }

            return {
                "status": "NOT_FOUND",
                "task_arn": task_arn,
            }

        except ClientError as e:
            logger.error(f"Failed to get task status: {e}")
            return {
                "status": "ERROR",
                "task_arn": task_arn,
                "error": str(e),
            }

    def stop_task(self, task_arn: str, reason: str = "Session ended") -> dict:
        """
        Stop a running task.

        Args:
            task_arn: ARN of the task to stop
            reason: Reason for stopping

        Returns:
            Stop confirmation
        """
        try:
            response = self._client.stop_task(
                cluster=self._cluster_arn,
                task=task_arn,
                reason=reason,
            )

            task = response.get("task", {})
            return {
                "status": "stopping",
                "task_arn": task_arn,
                "last_status": task.get("lastStatus"),
            }

        except ClientError as e:
            logger.error(f"Failed to stop task: {e}")
            return {
                "status": "error",
                "task_arn": task_arn,
                "error": str(e),
            }
