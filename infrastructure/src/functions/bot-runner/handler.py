"""
Bot Runner Lambda Handler

Handles Daily dial-in webhooks and routes to ECS voice service.

Architecture:
- Daily webhook triggers this Lambda
- Lambda creates Daily room and generates tokens
- Lambda calls always-on ECS service with room config
- ECS service runs pipecat and connects to Daily room
- PSTN caller is routed to room via SIP
"""

import json
import logging
import os
import time
import uuid
from typing import Any

from daily_client import DailyClient
from hmac_verifier import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    VerificationError,
    load_hmac_secret,
    verify_signature,
)
from phone_resolver import resolve_inbound_agent_id
from service_client import EcsServiceClient

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# HMAC verification is on by default. Set DAILY_HMAC_VERIFY=false to bypass
# (e.g., for local dev or during HMAC secret rotation). In production, do NOT
# disable this — the /start endpoint is internet-exposed.
_HMAC_VERIFY_ENABLED = os.environ.get("DAILY_HMAC_VERIFY", "true").lower() not in (
    "false",
    "0",
    "no",
)


def route(event: dict, context: Any) -> dict:
    """Dispatch the API Gateway event to the right handler.

    The bot-runner Lambda is wired to two API Gateway routes:

      POST /start    — Daily inbound webhook (HMAC-verified). Goes to
                       start_session.
      POST /dial-out — Outbound dialing trigger (Phase 7D). IAM-auth
                       only; goes to start_dial_out.

    Lambda exposes one handler entry per function, so we route on the
    request path here. Lambda functions called via lambda:Invoke
    (e.g. from the SQS consumer) can either pass an event with
    ``rawPath="/dial-out"`` or omit the path and set ``invocation_type``
    in the body — we accept both.
    """
    raw_path = (
        event.get("rawPath")
        or event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path")
        or ""
    )
    body_for_check = event.get("body")
    if isinstance(body_for_check, str):
        try:
            parsed = json.loads(body_for_check)
        except Exception:
            parsed = {}
    else:
        parsed = body_for_check or {}

    # Allow callers (e.g. SQS consumer doing direct lambda:Invoke
    # without an API Gateway event shape) to ask for dial-out via the
    # body field. Convenient for ad-hoc operator scripts too.
    invocation_type = (parsed or {}).get("invocation_type") if isinstance(parsed, dict) else None

    if raw_path.endswith("/dial-out") or invocation_type == "dial_out":
        return start_dial_out(event, context)

    # Default: inbound webhook. Includes /start and any unrecognized
    # path so existing callers don't break.
    return start_session(event, context)


def start_session(event: dict, context: Any) -> dict:
    """
    Handle Daily dial-in webhook and spawn voice session.

    Supports two modes:
    1. PSTN via Daily webhook: Has callId, callDomain, from fields
    2. SIP via direct request: Has source='sip', caller_id fields

    Expected PSTN webhook payload:
    {
        "callId": "string",
        "callDomain": "string",
        "from": "+15551234567",
        "to": "+15559876543",
        "direction": "inbound"
    }

    Expected SIP request payload:
    {
        "source": "sip",
        "caller_id": "web-client-001",
        "caller_number": "sip:100@asterisk.local"
    }

    Returns:
        API Gateway response with session details or error
    """
    request_id = context.aws_request_id if context else str(uuid.uuid4())
    logger.info(f"[{request_id}] Received webhook event")

    try:
        # Verify HMAC signature BEFORE parsing body — an attacker with no
        # knowledge of the HMAC secret must be rejected before we spend any
        # effort processing the payload.
        if _HMAC_VERIFY_ENABLED:
            try:
                _verify_request(event)
            except VerificationError as exc:
                logger.warning(
                    f"[{request_id}] HMAC verification failed: {exc}",
                )
                return _error_response(401, "Unauthorized")

        # Parse request body
        body = _parse_body(event)
        logger.info(f"[{request_id}] Parsed body: {body}")

        # Detect request type: PSTN webhook vs SIP direct request
        call_id = body.get("callId")
        call_domain = body.get("callDomain")
        source = body.get("source", "pstn")

        # Handle SIP requests differently
        if source == "sip" or (not call_id and not call_domain):
            logger.info(f"[{request_id}] Detected SIP request")
            return _handle_sip_request(body, request_id)

        # PSTN flow - validate required fields
        from_number = body.get("from", "unknown")
        to_number = body.get("To") or body.get("to") or ""

        if not call_id:
            return _error_response(400, "Missing required field: callId")
        if not call_domain:
            return _error_response(400, "Missing required field: callDomain")

        # Phase 7B: resolve the dialed number → agent via voice-api
        # Lambda. resolve_inbound_agent_id never raises — returns the
        # DEFAULT_INBOUND_AGENT fallback on lookup failure. Passing None
        # drops us into the legacy hardcoded-prompt path, which is only
        # useful if the fallback env var is also unset.
        agent_id = resolve_inbound_agent_id(to_number, request_id)

        # Initialize clients
        daily_client = DailyClient()
        service_client = EcsServiceClient()

        # Generate unique session ID
        session_id = f"voice-{call_id}-{uuid.uuid4().hex[:8]}"
        logger.info(f"[{request_id}] Created session_id: {session_id}")

        # Step 1: Create Daily room with SIP enabled
        logger.info(f"[{request_id}] Creating Daily room")
        room = daily_client.create_room(
            name=f"voice-{call_id}",
            properties={
                "enable_chat": False,
                "enable_screenshare": False,
                "enable_recording": False,
                "enable_transcription": False,
                "sip": {
                    "display_name": "Voice Assistant",
                    "video": False,
                    "sip_mode": "dial-in",
                },
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        room_url = room["url"]
        room_name = room["name"]
        logger.info(f"[{request_id}] Created room: {room_name}")

        # Step 2: Generate meeting token for the bot
        logger.info(f"[{request_id}] Generating bot token")
        bot_token = daily_client.create_meeting_token(
            room_name=room_name,
            properties={
                "is_owner": True,
                "user_name": "Voice Assistant",
                "enable_screenshare": False,
                "start_video_off": True,
                "start_audio_off": False,
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        logger.info(f"[{request_id}] Generated bot token")

        # Step 3: Get SIP URI for call routing
        sip_uri = daily_client.get_sip_uri(room_name)
        logger.info(f"[{request_id}] SIP URI: {sip_uri}")

        # Step 4: Call the always-on ECS service to handle the call.
        # When agent_id is set (Phase 7B path — resolved from the dialed
        # number), the Fargate pipeline loads that agent's full config
        # from Aurora and the legacy ``system_prompt`` kwarg is ignored.
        # We still pass _get_system_prompt() as a safety net for the
        # edge case where resolve_inbound_agent_id returns None
        # (fallback env var unset AND no DB row) — in that scenario the
        # Fargate side falls back to the generic "You are a helpful
        # voice assistant" prompt instead of hanging up.
        logger.info(
            f"[{request_id}] Calling ECS service "
            f"(agent_id={agent_id!r}, to={to_number!r}, from={from_number!r})"
        )
        service_response = service_client.start_call(
            room_url=room_url,
            room_token=bot_token,
            session_id=session_id,
            system_prompt=None if agent_id else _get_system_prompt(from_number),
            dialin_settings={
                "call_id": call_id,
                "call_domain": call_domain,
                "sip_uri": sip_uri,
            },
            agent_id=agent_id,
        )
        logger.info(
            f"[{request_id}] Service response: {service_response.get('status')}"
        )

        # Check if voice agent accepted the call
        if service_response.get("status") not in ("started",):
            logger.error(
                f"[{request_id}] Voice agent rejected call: {service_response}"
            )
            return _error_response(
                503,
                f"Voice agent unavailable: {service_response.get('error', 'unknown')}",
            )

        # Step 5: Return SIP transfer response to Daily
        # Daily expects a sipUri field to route the call
        response_body = {
            "sessionId": session_id,
            "roomUrl": room_url,
            "sipUri": sip_uri,
            "status": service_response.get("status", "started"),
            "message": "Voice session started successfully",
        }

        logger.info(f"[{request_id}] Session started successfully")
        return _success_response(200, response_body)

    except ValueError as e:
        logger.error(f"[{request_id}] Validation error: {e}")
        return _error_response(400, str(e))
    except Exception as e:
        logger.exception(f"[{request_id}] Unexpected error: {e}")
        return _error_response(500, "Internal server error")


def start_dial_out(event: dict, context: Any) -> dict:
    """
    Phase 7D: outbound batch dialing.

    Creates a Daily room, generates a bot token, asks Daily to ring
    the target PSTN number from that room, and POSTs the room
    handles to the ECS pipeline so the agent is in the room and
    speaking the moment the target picks up.

    Expected request body (from the SQS consumer or any other caller
    with lambda:Invoke permission on this function):

        {
          "to_number":   "+15551234567",       (required, E.164)
          "from_number": "+12098075018",       (required, E.164 — must be
                                                a number provisioned in
                                                Daily for outbound)
          "agent_id":    "chris-claim-status", (required; UUID or name)
          "case_data":   {...},                (optional; hydrator dict)
          "session_id":  "..."                 (optional; auto-generated
                                                from a UUID if absent)
        }

    Returns:

        {
          "session_id": "voice-out-...",
          "room_url":   "https://cosentus.daily.co/voice-out-...",
          "dial_out":   <Daily dialOut response>,
          "status":     "started"
        }

    Auth: this handler is NOT exposed via the HMAC-verified webhook
    path. It's expected to be invoked via lambda:InvokeFunction (IAM
    auth) by trusted same-account callers — the SQS consumer in
    Phase 7D PR 2, ad-hoc operator scripts, etc.
    """
    request_id = context.aws_request_id if context else str(uuid.uuid4())
    logger.info(f"[{request_id}] dial-out request received")

    try:
        body = _parse_body(event)

        to_number = (body.get("to_number") or "").strip()
        # from_number (caller ID) is optional. When omitted, Daily picks
        # whatever default caller ID is assigned to the domain. When
        # provided, it MUST be a number Daily recognizes as ours
        # (purchased via /buy-phone-number) — using an unrelated number
        # surfaces "Incorrect callerID! No phone number maps to..."
        # from start_dialout.
        from_number = (body.get("from_number") or "").strip() or None
        agent_id = (body.get("agent_id") or "").strip() or None
        case_data = body.get("case_data") or {}

        if not to_number:
            return _error_response(400, "Missing required field: to_number")
        if not agent_id:
            return _error_response(400, "Missing required field: agent_id")
        if not isinstance(case_data, dict):
            return _error_response(400, "case_data must be an object")

        session_id = body.get("session_id") or f"voice-out-{uuid.uuid4().hex[:12]}"

        daily_client = DailyClient()
        service_client = EcsServiceClient()

        # Step 1: create Daily room. Daily's only valid sip_mode is
        # ``dial-in`` (verified 2026-04-27 against
        # https://docs.daily.co/reference/rest-api/rooms/config —
        # there is no "dial-out" mode). The dialOut leg is created
        # separately via Daily's POST /dialout endpoint and works
        # from any room with SIP enabled. So we use sip_mode=dial-in
        # here even for outbound calls; the room never actually
        # receives an inbound SIP leg, only the dialOut bridge.
        logger.info(f"[{request_id}] creating outbound Daily room")
        room = daily_client.create_room(
            name=f"voice-out-{uuid.uuid4().hex[:12]}",
            properties={
                "enable_chat": False,
                "enable_screenshare": False,
                "enable_recording": False,
                "enable_transcription": False,
                "sip": {
                    "display_name": "Voice Assistant",
                    "video": False,
                    "sip_mode": "dial-in",
                },
                "exp": int(time.time()) + 3600,
            },
        )
        room_url = room["url"]
        room_name = room["name"]
        logger.info(f"[{request_id}] room created: {room_name}")

        # Step 2: bot meeting token (owner — Daily's dialOut requires
        # owner privilege to initiate from the bot's connection).
        bot_token = daily_client.create_meeting_token(
            room_name=room_name,
            properties={
                "is_owner": True,
                "user_name": "Voice Assistant",
                "enable_screenshare": False,
                "start_video_off": True,
                "start_audio_off": False,
                "exp": int(time.time()) + 3600,
            },
        )
        logger.info(f"[{request_id}] bot token issued")

        # Step 3: hand off to ECS. We pass dialout_settings (the
        # phone number to ring + caller id) — the pipeline itself
        # will call transport.start_dialout() after joining the
        # room. Daily's dialOut mechanism only works from a
        # connected SDK client (the bot in the room), NOT from a
        # REST API call, so the Lambda can't initiate it.
        logger.info(
            f"[{request_id}] handing off to ECS "
            f"(agent_id={agent_id!r}, to=****{to_number[-4:]})"
        )
        service_response = service_client.start_call(
            room_url=room_url,
            room_token=bot_token,
            session_id=session_id,
            # No system_prompt override on outbound — the agent_id
            # path supplies the full Aurora config (including
            # first_message which is what the target hears on pickup).
            system_prompt=None,
            # dialin_settings=None signals OUTBOUND in pipeline_ecs.
            dialin_settings=None,
            # dialout_settings tells the pipeline to call
            # start_dialout() after joining; the bot will then ring
            # the target from inside the room. caller_id is included
            # only when supplied by the caller (Daily picks a default
            # otherwise — typically a Daily-assigned number).
            dialout_settings=(
                {"phone_number": to_number, "caller_id": from_number}
                if from_number
                else {"phone_number": to_number}
            ),
            agent_id=agent_id,
            case_data=case_data,
        )

        if service_response.get("status") not in ("started",):
            logger.error(
                f"[{request_id}] ECS rejected outbound: {service_response}"
            )
            return _error_response(
                503,
                f"Voice agent unavailable: {service_response.get('error', 'unknown')}",
            )

        logger.info(
            f"[{request_id}] outbound session handed off; "
            f"pipeline will dial out from inside the room"
        )
        return _success_response(
            200,
            {
                "session_id": session_id,
                "room_url": room_url,
                "status": "started",
            },
        )

    except ValueError as e:
        logger.error(f"[{request_id}] dial-out validation error: {e}")
        return _error_response(400, str(e))
    except Exception as e:
        logger.exception(f"[{request_id}] dial-out unexpected error: {e}")
        return _error_response(500, "Internal server error")


def _handle_sip_request(body: dict, request_id: str) -> dict:
    """
    Handle SIP-initiated voice session.

    Creates a Daily room and starts bot without pinless dial-in configuration.
    Returns SIP URI for Asterisk to dial.
    """
    try:
        caller_id = body.get("caller_id", "unknown")
        caller_number = body.get("caller_number", "unknown")

        logger.info(f"[{request_id}] Processing SIP request from: {caller_id}")

        # Initialize clients
        daily_client = DailyClient()
        service_client = EcsServiceClient()

        # Generate unique session ID for SIP
        session_id = f"sip-{uuid.uuid4().hex[:8]}"
        logger.info(f"[{request_id}] Created session_id: {session_id}")

        # Step 1: Create Daily room with SIP enabled (no pinless dial-in)
        logger.info(f"[{request_id}] Creating Daily room for SIP")
        room = daily_client.create_room(
            name=f"sip-{session_id}",
            properties={
                "enable_chat": False,
                "enable_screenshare": False,
                "enable_recording": False,
                "enable_transcription": False,
                "sip": {
                    "display_name": "Voice Assistant",
                    "video": False,
                    "sip_mode": "dial-in",
                },
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        room_url = room["url"]
        room_name = room["name"]
        logger.info(f"[{request_id}] Created room: {room_name}")

        # Step 2: Generate meeting token for the bot
        logger.info(f"[{request_id}] Generating bot token")
        bot_token = daily_client.create_meeting_token(
            room_name=room_name,
            properties={
                "is_owner": True,
                "user_name": "Voice Assistant",
                "enable_screenshare": False,
                "start_video_off": True,
                "start_audio_off": False,
                "exp": int(time.time()) + 3600,  # 1 hour from now
            },
        )
        logger.info(f"[{request_id}] Generated bot token")

        # Step 3: Get SIP URI for call routing
        sip_uri = daily_client.get_sip_uri(room_name)
        logger.info(f"[{request_id}] SIP URI: {sip_uri}")

        # Step 4: Call the always-on ECS service to handle the call
        # For SIP calls, we don't pass dialin_settings (no pinless dial-in)
        logger.info(f"[{request_id}] Calling ECS service")
        service_response = service_client.start_call(
            room_url=room_url,
            room_token=bot_token,
            session_id=session_id,
            system_prompt=_get_system_prompt(caller_number),
            # dialin_settings is None for SIP calls
        )
        logger.info(
            f"[{request_id}] Service response: {service_response.get('status')}"
        )

        # Check if voice agent accepted the call
        if service_response.get("status") not in ("started",):
            logger.error(
                f"[{request_id}] Voice agent rejected call: {service_response}"
            )
            return _error_response(
                503,
                f"Voice agent unavailable: {service_response.get('error', 'unknown')}",
            )

        # Step 5: Return response with SIP URI
        # Asterisk will use this SIP URI to dial the room
        response_body = {
            "sessionId": session_id,
            "roomUrl": room_url,
            "sipUri": sip_uri,
            "status": service_response.get("status", "started"),
            "message": "Voice session started successfully",
        }

        logger.info(f"[{request_id}] SIP session started successfully")
        return _success_response(200, response_body)

    except ValueError as e:
        logger.error(f"[{request_id}] SIP validation error: {e}")
        return _error_response(400, str(e))
    except Exception as e:
        logger.exception(f"[{request_id}] SIP unexpected error: {e}")
        return _error_response(500, "Internal server error")


def _parse_body(event: dict) -> dict:
    """Parse request body from API Gateway event."""
    body = event.get("body", "{}")

    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e}")

    return body if isinstance(body, dict) else {}


def _verify_request(event: dict) -> None:
    """
    Verify the API Gateway event carries a valid Daily HMAC signature.

    Raises VerificationError on any failure.

    API Gateway v1 (REST) lowercases header names in the `headers` dict but
    preserves case in `multiValueHeaders`. We check both to be safe.
    """
    headers = event.get("headers") or {}
    multi_headers = event.get("multiValueHeaders") or {}

    def _header(name: str) -> str:
        lookups = [name, name.lower(), name.upper()]
        for key in lookups:
            if key in headers and headers[key]:
                return headers[key]
            if key in multi_headers and multi_headers[key]:
                return multi_headers[key][0]
        return ""

    signature = _header(SIGNATURE_HEADER)
    timestamp = _header(TIMESTAMP_HEADER)

    # API Gateway delivers the body as a string (possibly base64-encoded
    # if isBase64Encoded=true). Daily sends JSON so base64 encoding is
    # unexpected, but handle it defensively.
    raw_body = event.get("body") or ""
    if isinstance(raw_body, dict):
        # Test event with a pre-parsed dict. Serialize to bytes — NOT
        # byte-identical to Daily's wire format, so signatures for these
        # events must be recomputed by the test, not copied from a real call.
        body_bytes = json.dumps(raw_body, separators=(",", ":")).encode("utf-8")
    elif event.get("isBase64Encoded"):
        import base64

        body_bytes = base64.b64decode(raw_body)
    else:
        body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else b""

    secret = load_hmac_secret()
    verify_signature(
        body=body_bytes,
        signature=signature,
        timestamp=timestamp,
        hmac_secret_b64=secret or "",
    )


def _get_system_prompt(caller_id: str) -> str:
    """
    Generate system prompt for the voice assistant.

    Can be customized based on caller ID, time of day, etc.
    """
    return """You are a helpful voice assistant powered by Claude.

Your role is to have natural, conversational interactions with callers.
Be concise but friendly - remember this is a phone call, not a text chat.

Guidelines:
- Keep responses brief and conversational (1-3 sentences typically)
- Use natural speech patterns, not formal writing
- Ask clarifying questions when needed
- Be helpful and patient

Tool Usage:
- When using tools, call them directly without explaining what you're doing first
- After the tool returns, respond naturally with the result
- Do NOT say "Let me..." or "I'll use..." before calling a tool

The caller is reaching you via phone. Greet them warmly and ask how you can help."""


def _success_response(status_code: int, body: dict) -> dict:
    """Create successful API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _error_response(status_code: int, message: str) -> dict:
    """Create error API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(
            {
                "error": message,
                "status": "error",
            }
        ),
    }
