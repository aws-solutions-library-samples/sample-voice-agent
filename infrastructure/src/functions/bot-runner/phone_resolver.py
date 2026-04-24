"""Phase 7B — resolve a dialed phone number to the assigned agent.

Given the ``To`` number from Daily's inbound webhook, invoke the
voice-api Lambda's ``GET /api/phone-numbers/lookup`` endpoint to find
the ``inbound_agent_id`` for that number. On any failure (DB
unreachable, Lambda cold-start timeout, row missing, etc.) we fall
back to the ``DEFAULT_INBOUND_AGENT`` env var so calls still land on
*some* agent rather than dropping.

Why Lambda invoke instead of an HTTP call?
------------------------------------------

The ECS task role uses ``lambda:InvokeFunction`` for the same Lambda
(see ecs-stack.ts). Reusing that pattern means:

  1. No API key handoff (IAM-native auth)
  2. No API Gateway hop (lower latency — sub-100 ms warm)
  3. No VPC egress through NAT for a same-account call

The bot-runner Lambda was granted the matching policy in
WebhookApiConstruct.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import boto3

logger = logging.getLogger(__name__)

VOICE_API_LAMBDA_NAME_ENV = "VOICE_API_LAMBDA_NAME"
DEFAULT_INBOUND_AGENT_ENV = "DEFAULT_INBOUND_AGENT"

_LAMBDA = boto3.client("lambda")


def resolve_inbound_agent_id(to_number: str, request_id: str) -> Optional[str]:
    """Look up the inbound agent for a dialed number.

    Returns:
        Agent identifier (UUID preferred; falls back to agent name; or
        the DEFAULT_INBOUND_AGENT env var on any failure) or ``None``
        if nothing can be resolved.

    Never raises. Lookup failures are logged + downgraded to the
    fallback so a brief Aurora blip doesn't take down the whole inbound
    path.
    """
    default_agent = os.environ.get(DEFAULT_INBOUND_AGENT_ENV, "").strip() or None

    if not to_number:
        logger.warning(
            f"[{request_id}] resolve_inbound_agent_id: empty to_number, "
            f"using default={default_agent!r}"
        )
        return default_agent

    lambda_name = os.environ.get(VOICE_API_LAMBDA_NAME_ENV, "").strip()
    if not lambda_name:
        logger.warning(
            f"[{request_id}] resolve_inbound_agent_id: "
            f"{VOICE_API_LAMBDA_NAME_ENV} not set, using default={default_agent!r}"
        )
        return default_agent

    try:
        payload = {
            "httpMethod": "GET",
            "path": "/api/phone-numbers/lookup",
            "queryStringParameters": {"number": to_number},
            "headers": {},
            "body": None,
        }
        resp = _LAMBDA.invoke(
            FunctionName=lambda_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        raw = resp.get("Payload")
        if raw is None:
            logger.error(
                f"[{request_id}] resolve_inbound_agent_id: "
                f"invoke returned no payload, using default={default_agent!r}"
            )
            return default_agent

        envelope = json.loads(raw.read())
        status = envelope.get("statusCode", 0)
        body_str = envelope.get("body", "{}") or "{}"
        try:
            body = json.loads(body_str)
        except (TypeError, json.JSONDecodeError):
            body = {}

        if status == 200:
            agent_id = _extract_inbound_agent(body)
            if agent_id:
                logger.info(
                    f"[{request_id}] resolve_inbound_agent_id: "
                    f"{to_number} → {agent_id} (from voice_phone_numbers)"
                )
                return agent_id
            # Row exists but no inbound_agent_id is set — intentional
            # routing gap. Use the fallback so the call still connects.
            logger.warning(
                f"[{request_id}] resolve_inbound_agent_id: "
                f"{to_number} provisioned but has no inbound_agent, "
                f"using default={default_agent!r}"
            )
            return default_agent

        if status == 404:
            logger.warning(
                f"[{request_id}] resolve_inbound_agent_id: "
                f"{to_number} not in voice_phone_numbers, "
                f"using default={default_agent!r}"
            )
            return default_agent

        # Any other status (400 malformed, 500 DB error) — don't trust
        # the response, fall back to default so the call still works.
        logger.error(
            f"[{request_id}] resolve_inbound_agent_id: "
            f"unexpected status={status} body={body_str[:200]}, "
            f"using default={default_agent!r}"
        )
        return default_agent

    except Exception as exc:  # pragma: no cover — logged for diagnosis
        logger.exception(
            f"[{request_id}] resolve_inbound_agent_id crashed: {exc}"
        )
        return default_agent


def _extract_inbound_agent(body: dict[str, Any]) -> Optional[str]:
    """Pull the agent identifier from a lookup response body.

    Prefers the UUID (stable across renames); falls back to the name.
    Returns ``None`` when no inbound agent is assigned.
    """
    inbound = body.get("inbound_agent")
    if not isinstance(inbound, dict):
        return None
    uid = inbound.get("id")
    if isinstance(uid, str) and uid:
        return uid
    name = inbound.get("name")
    if isinstance(name, str) and name:
        return name
    return None
