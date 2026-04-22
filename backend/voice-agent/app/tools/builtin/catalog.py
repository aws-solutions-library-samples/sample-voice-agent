"""Tool catalog -- the single registry of all local tools.

This module provides the complete list of tools that can be registered
with the voice agent pipeline. The pipeline's capability detection system
filters this list at runtime, only registering tools whose requirements
are satisfied by the current deployment.

To add a new local tool:
    1. Create the tool file in app/tools/builtin/ (e.g., hangup_tool.py)
    2. Define the ToolDefinition with appropriate `requires` capabilities
    3. Import and add it to ALL_LOCAL_TOOLS below
    4. The capability system handles the rest -- no pipeline code changes needed

This catalog is strictly for tools that run inside the voice agent
container and may need direct access to pipeline internals (transport,
SIP session, etc.).
"""

from typing import List

from ..schema import ToolDefinition
from .time_tool import time_tool
from .transfer_tool import transfer_tool
from .hangup_tool import hangup_tool


# Complete list of local tools available to the pipeline.
# Each tool declares its own `requires` set of PipelineCapability values.
# The pipeline filters this list based on detected capabilities at startup.
#
# Order doesn't matter -- tools are registered by name, not position.
ALL_LOCAL_TOOLS: List[ToolDefinition] = [
    time_tool,
    transfer_tool,
    hangup_tool,
    # Future tools:
    # collect_dtmf_tool,    # requires={TRANSPORT, DTMF_COLLECTION}
    # pause_recording_tool, # requires={TRANSPORT, RECORDING_CONTROL}
]
