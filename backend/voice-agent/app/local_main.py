"""Local prototyping entrypoint for the voice agent.

Runs a FastAPI server with WebRTC signalling and a prebuilt browser UI,
allowing developers to test the full voice pipeline (STT -> LLM -> TTS)
from a browser on localhost without any SIP/PSTN infrastructure.

Prerequisites:
    - Cloud resources deployed (Bedrock access, Deepgram/Cartesia API keys)
    - pip install pipecat-ai[webrtc,runner]

Usage:
    cd backend/voice-agent
    cp .env.example .env  # configure API keys
    python -m app.local_main

    Then open http://localhost:7860 in your browser and click Connect.

Environment variables:
    LOCAL_PORT: Port for the FastAPI server (default: 7860)
    SYSTEM_PROMPT: Custom system prompt (optional)
    VOICE_ID: Cartesia voice ID (optional)
    AWS_REGION: AWS region for Bedrock (default: us-east-1)
    STT_PROVIDER: STT provider (default: deepgram)
    TTS_PROVIDER: TTS provider (default: cartesia)
    DEEPGRAM_API_KEY: Deepgram API key (required for cloud STT)
    CARTESIA_API_KEY: Cartesia API key (required for cloud TTS)
    ENABLE_TOOL_CALLING: Enable tool calling (default: false)
    ENABLE_FILLER_PHRASES: Enable filler phrases (default: true)
"""

import asyncio
import logging
import os
import sys
import uuid
import warnings

import structlog
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, Request, Response
from fastapi.responses import RedirectResponse

from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    ConnectionMode,
    IceCandidate,
    SmallWebRTCRequestHandler,
    SmallWebRTCRequest,
    SmallWebRTCPatchRequest,
)

# Load .env file before any other imports that read env vars
load_dotenv()

# Configure stdlib logging
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=getattr(logging, log_level),
)
logging.getLogger("pipecat").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Suppress pipecat's loguru-based DEBUG logs
from loguru import logger as _loguru_logger

_loguru_logger.disable("pipecat")

# Suppress pipecat-internal deprecation warnings
warnings.filterwarnings(
    "ignore",
    message=r"OpenAILLMContext is deprecated",
    category=DeprecationWarning,
    module=r"pipecat\.processors\.aggregators\.openai_llm_context",
)
import pipecat.services as _pipecat_services

_pipecat_services._warned_modules.add(("deepgram", "deepgram.[stt,tts]"))

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


from app.pipeline_local import create_local_pipeline, LocalPipelineConfig

# =====================
# FastAPI Application
# =====================

app = FastAPI(title="Voice Agent - Local Prototyping")

# WebRTC signalling handler (single connection mode for local dev)
small_webrtc_handler = SmallWebRTCRequestHandler(
    connection_mode=ConnectionMode.SINGLE,
)

# Mount the prebuilt browser UI at /client
try:
    from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

    app.mount("/client", SmallWebRTCPrebuiltUI, name="client")
    logger.info("prebuilt_ui_mounted", path="/client")
except ImportError:
    logger.warning(
        "prebuilt_ui_not_available",
        hint="Install pipecat-ai[runner] for the browser UI",
    )


@app.get("/")
async def root():
    """Redirect to the browser UI."""
    return RedirectResponse(url="/client/index.html")


# The prebuilt UI (pipecat-ai-small-webrtc-prebuilt 2.5.0) speaks the two-phase
# Pipecat Cloud protocol: POST /start to open a session, then
# POST /sessions/{id}/api/offer for the SDP exchange. This module only
# implemented the older single-phase POST /api/offer, so Connect failed with 404
# then 422. These two routes bridge the new client to the existing handler.
_active_sessions: dict[str, dict] = {}


@app.post("/start")
async def rtvi_start(request: Request):
    """Phase 1: open a session (mirrors Pipecat Cloud's /start)."""
    try:
        request_data = await request.json()
    except Exception:
        request_data = {}

    session_id = str(uuid.uuid4())
    _active_sessions[session_id] = request_data.get("body", {})

    result: dict = {"sessionId": session_id}
    if request_data.get("enableDefaultIceServers"):
        result["iceConfig"] = {
            "iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]
        }
    return result


@app.post("/sessions/{session_id}/api/offer")
async def session_offer(
    session_id: str, request: Request, background_tasks: BackgroundTasks
):
    """Phase 2: SDP exchange, proxied to the existing offer handler."""
    if session_id not in _active_sessions:
        return Response(content="Invalid or not-yet-ready session_id", status_code=404)
    data = await request.json()
    return await offer(
        SmallWebRTCRequest(
            sdp=data["sdp"],
            type=data["type"],
            pc_id=data.get("pc_id"),
            restart_pc=data.get("restart_pc"),
            request_data=data.get("request_data")
            or data.get("requestData")
            or _active_sessions[session_id],
        ),
        background_tasks,
    )


@app.patch("/sessions/{session_id}/api/offer")
async def session_offer_patch(session_id: str, request: Request):
    """Phase 2 (trickle ICE): proxied to the existing patch handler.

    The client PATCHes additional ICE candidates to the session-scoped path as
    they are discovered. Without this the PATCH returns 405 and the connection
    falls back to only the candidates present in the initial offer, which can
    fail on more restrictive networks.
    """
    if session_id not in _active_sessions:
        return Response(content="Invalid or not-yet-ready session_id", status_code=404)
    data = await request.json()
    return await offer_patch(
        SmallWebRTCPatchRequest(
            pc_id=data["pc_id"],
            candidates=[IceCandidate(**c) for c in data.get("candidates", [])],
        )
    )


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    """Handle WebRTC SDP offer from browser client.

    Creates a new WebRTC connection, builds the voice pipeline, and
    starts it as a background task. Returns the SDP answer for the
    browser to complete the WebRTC handshake.
    """

    async def webrtc_connection_callback(webrtc_connection: SmallWebRTCConnection):
        """Called when a new WebRTC connection is established."""
        session_id = str(uuid.uuid4())
        logger.info("new_webrtc_session", session_id=session_id)

        config = LocalPipelineConfig(
            session_id=session_id,
            system_prompt=os.environ.get(
                "SYSTEM_PROMPT",
                "You are a helpful AI assistant. Respond concisely and naturally.",
            ),
            voice_id=os.environ.get(
                "VOICE_ID", "79a125e8-cd45-4c13-8a67-188112f4dd22"
            ),
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            stt_provider=os.environ.get("STT_PROVIDER", "deepgram"),
            tts_provider=os.environ.get("TTS_PROVIDER", "cartesia"),
            stt_endpoint=os.environ.get("STT_ENDPOINT_NAME", ""),
            tts_endpoint=os.environ.get("TTS_ENDPOINT_NAME", ""),
        )

        try:
            task, transport = await create_local_pipeline(
                config=config,
                webrtc_connection=webrtc_connection,
            )

            runner = PipelineRunner()
            await runner.run(task)

            logger.info("pipeline_completed", session_id=session_id)
        except Exception as e:
            logger.error(
                "pipeline_error",
                session_id=session_id,
                error=str(e),
                error_type=type(e).__name__,
            )

    answer = await small_webrtc_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=lambda conn: background_tasks.add_task(
            webrtc_connection_callback, conn
        ),
    )
    return answer


@app.patch("/api/offer")
async def offer_patch(request: SmallWebRTCPatchRequest):
    """Handle ICE candidate trickle from browser client."""
    await small_webrtc_handler.handle_patch_request(request)
    return {"status": "ok"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "mode": "local"}


async def _shutdown():
    """Clean up WebRTC connections on shutdown."""
    await small_webrtc_handler.close()


app.add_event_handler("shutdown", _shutdown)


def main():
    """Run the local prototyping server."""
    port = int(os.environ.get("LOCAL_PORT", "7860"))

    logger.info(
        "local_server_starting",
        port=port,
        url=f"http://localhost:{port}",
    )
    print(f"\n  Voice Agent - Local Prototyping")
    print(f"  Open http://localhost:{port} in your browser")
    print(f"  Press Ctrl+C to stop\n")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
