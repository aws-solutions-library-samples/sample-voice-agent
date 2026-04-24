"""Pipeline capability declarations for the tool calling framework.

This module defines the set of capabilities that the voice agent pipeline
can provide to tools. Each tool declares which capabilities it requires,
and the pipeline only registers tools whose requirements are fully satisfied.

This prevents tools from being registered when they can't function (e.g.,
registering a SIP transfer tool when no TRANSFER_DESTINATION is configured),
which avoids confusing the LLM with unusable tools and eliminates runtime
configuration errors.

Usage:
    >>> from app.tools.capabilities import PipelineCapability, detect_capabilities
    >>>
    >>> # At pipeline creation time:
    >>> available = detect_capabilities(transport, sip_tracker, config)
    >>>
    >>> # Check if a tool's requirements are met:
    >>> if tool.requires <= available:
    ...     registry.register(tool)

Adding new capabilities:
    When building a new tool that needs a pipeline resource not yet represented
    here, add a new enum member and update detect_capabilities() to probe for
    it. Existing tools are unaffected because they don't declare the new
    capability in their `requires` set.
"""

import os
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pipecat.transports.base_transport import BaseTransport

logger = structlog.get_logger(__name__)


class PipelineCapability(Enum):
    """Capabilities that the voice agent pipeline can provide to tools.

    Tools declare a frozenset of these capabilities in their `requires` field.
    At pipeline creation time, detect_capabilities() probes the environment
    and returns the set of capabilities that are actually available. Only tools
    whose requirements are a subset of the available capabilities get registered.

    Capability groups:

    BASIC:
        Always available. Tools with no special requirements (e.g., get_current_time)
        should declare requires=frozenset({PipelineCapability.BASIC}).

    Transport capabilities:
        Require a live transport instance (DailyTransport or SmallWebRTCTransport).
        - TRANSPORT: The transport object exists and is connected.
        - SIP_SESSION: A SIP dial-in connection is present (the pipeline is
          handling a PSTN call, not a WebRTC-only session).
        - DTMF_COLLECTION: The transport supports collecting DTMF tone input
          from the caller (needed for PIN entry, payment card collection).
        - RECORDING_CONTROL: The transport supports pausing/resuming call
          recording (needed for PCI-DSS compliance during payment collection).

    Configuration capabilities:
        (Removed in Phase 7C.) Pre-7C we probed for a TRANSFER_DESTINATION
        env var here because transfer_tool was env-var-driven. 7C moved
        transfer targets to the per-agent Aurora config
        (voice_agents.tools[].settings.targets), so the capability is no
        longer meaningful — transfer_call simply requires TRANSPORT +
        SIP_SESSION, and the Aurora tools_config filter in
        _register_tools handles whether the tool is actually enabled
        for a given agent.
    """

    # Always available -- no special requirements
    BASIC = "basic"

    # Transport capabilities
    TRANSPORT = "transport"
    SIP_SESSION = "sip_session"
    DTMF_COLLECTION = "dtmf_collection"
    RECORDING_CONTROL = "recording_control"


def detect_capabilities(
    transport: Optional["BaseTransport"] = None,
    sip_session_tracker: Optional[Dict[str, Optional[str]]] = None,
    config: Optional[Any] = None,
) -> FrozenSet[PipelineCapability]:
    """Detect which pipeline capabilities are available in this deployment.

    This function probes the pipeline's runtime environment -- the transport
    object, SIP session state, environment variables, and configuration -- to
    determine which capabilities are present. The result is compared against
    each tool's `requires` set to decide which tools to register.

    Args:
        transport: The transport instance (DailyTransport, SmallWebRTCTransport,
            etc.), or None if not available.
        sip_session_tracker: Mutable dict tracking the SIP session ID.
            Presence (not None) indicates this is a SIP-capable pipeline.
        config: AppConfig from ConfigService (currently unused but reserved
            for future configuration-based capabilities).

    Returns:
        Frozen set of available PipelineCapability values.
    """
    caps = {PipelineCapability.BASIC}

    if transport is not None:
        caps.add(PipelineCapability.TRANSPORT)

        # SIP session tracking is enabled when the pipeline is created for
        # a dial-in (PSTN) call. The tracker dict is created regardless of
        # whether the SIP session ID has been populated yet -- its presence
        # signals that we *will* have a SIP session.
        if sip_session_tracker is not None:
            caps.add(PipelineCapability.SIP_SESSION)

        # DTMF collection requires transport methods for tone capture.
        # This will be available when Daily adds DTMF APIs.
        if hasattr(transport, "collect_dtmf"):
            caps.add(PipelineCapability.DTMF_COLLECTION)

        # Recording control requires transport methods for pause/resume.
        if hasattr(transport, "pause_recording"):
            caps.add(PipelineCapability.RECORDING_CONTROL)

    result = frozenset(caps)

    logger.info(
        "pipeline_capabilities_detected",
        capabilities=sorted(c.value for c in result),
        count=len(result),
    )

    return result
