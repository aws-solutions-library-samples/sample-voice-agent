---
name: create-local-tool
description: Scaffolds a new local tool for the voice agent pipeline with capability-based registration, executor function, and tests. Use when adding a tool that runs inside the voice agent container and may need transport or SIP session access.
---

## What I Do

Scaffold all the files needed for a new local tool in the voice agent pipeline. This includes:

1. Tool implementation (`backend/voice-agent/app/tools/builtin/{name}_tool.py`)
2. Catalog registration (add to `ALL_LOCAL_TOOLS` in `catalog.py`)
3. Unit tests (`backend/voice-agent/tests/test_{name}_tool.py`)

No pipeline code or CDK changes are needed -- the capability system handles registration automatically.

## When to Use Me

Use this skill when you need to create a new tool that runs **inside the voice agent container** and may need access to pipeline internals (transport, SIP session, DTMF, recording control). For remote tools that run as separate services, use the `create-capability-agent` skill instead.

## Reference

Read `docs/guides/adding-a-local-tool.md` for the complete developer guide. Templates below are derived from that guide and the shipped `time_tool` and `transfer_tool` implementations.

## Steps

### 1. Gather Requirements

Ask the user for:
- **Tool name** (snake_case, e.g., `hangup_call`). This becomes the function name the LLM sees.
- **What it does** -- this becomes the `description` field, which is critical for LLM tool selection.
- **Parameters** -- for each parameter: name, type, description, required?, enum values?
- **Capabilities needed** -- does it need transport, SIP session, DTMF, recording control, or env vars? See the capability table in the guide.
- **Return value** -- what data should the tool return to the LLM?

Check existing tool names to avoid conflicts:

| Name | Source | Agent |
|------|--------|-------|
| `get_current_time` | Local | Voice Agent |
| `hangup_call` | Local | Voice Agent |
| `transfer_to_agent` | Local | Voice Agent |
| `search_knowledge_base` | A2A | KB Agent |
| `lookup_customer` | A2A | CRM Agent |
| `create_support_case` | A2A | CRM Agent |
| `add_case_note` | A2A | CRM Agent |
| `verify_account_number` | A2A | CRM Agent |
| `verify_recent_transaction` | A2A | CRM Agent |

### 2. Create the Tool File

Create `backend/voice-agent/app/tools/builtin/{name}_tool.py`:

```python
"""Description of this tool.

Capability requirements:
    - List each capability and why it's needed
"""

from typing import Any, Dict

from ..capabilities import PipelineCapability
from ..context import ToolContext
from ..result import ToolResult, success_result, error_result
from ..schema import ToolCategory, ToolDefinition, ToolParameter


async def {name}_executor(
    arguments: Dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    """Execute the tool.

    Args:
        arguments: Validated parameters from the LLM
        context: ToolContext with call_id, session_id, transport, etc.

    Returns:
        ToolResult via success_result() or error_result()
    """
    # Tool logic here
    return success_result({"key": "value"})


{name}_tool = ToolDefinition(
    name="{tool_name}",
    description="...",
    category=ToolCategory.SYSTEM,
    parameters=[...],
    executor={name}_executor,
    timeout_seconds=5.0,
    requires=frozenset({PipelineCapability.BASIC}),
)
```

Key requirements:
- Executor must be `async def`
- Return `ToolResult` via `success_result()` or `error_result()`
- Write a specific `description` -- the LLM uses this to decide when to call the tool
- Set `requires` to the **minimum** set of capabilities needed
- If the tool needs transport access, use `context.transport`
- If it needs the SIP session ID, use `context.sip_session_id`

### 3. Add to the Catalog

Edit `backend/voice-agent/app/tools/builtin/catalog.py`:

```python
from .{name}_tool import {name}_tool

ALL_LOCAL_TOOLS: List[ToolDefinition] = [
    time_tool,
    transfer_tool,
    hangup_tool,
    {name}_tool,  # <-- add here
]
```

### 4. Write Tests

Create `backend/voice-agent/tests/test_{name}_tool.py`:

```python
"""Tests for {name}_tool."""

import pytest
from app.tools.builtin.{name}_tool import {name}_tool, {name}_executor
from app.tools.capabilities import PipelineCapability
from app.tools.context import ToolContext


class TestToolDefinition:
    def test_name(self):
        assert {name}_tool.name == "{tool_name}"

    def test_requires(self):
        assert {name}_tool.requires == frozenset({PipelineCapability.BASIC})

    def test_in_catalog(self):
        from app.tools.builtin.catalog import ALL_LOCAL_TOOLS
        assert {name}_tool in ALL_LOCAL_TOOLS


class TestToolExecutor:
    @pytest.fixture
    def context(self):
        return ToolContext(call_id="test-call", session_id="test-session")

    @pytest.mark.asyncio
    async def test_basic_execution(self, context):
        result = await {name}_executor({}, context)
        assert result.success is True
```

### 5. Run Tests

```bash
cd backend/voice-agent && python -m pytest tests/test_{name}_tool.py -v
```

Also run the full suite to verify no regressions:
```bash
cd backend/voice-agent && python -m pytest tests/ -v
```

### 6. Verify Checklist

- [ ] Tool name is unique (not in the conflict table above)
- [ ] Executor is `async def` and returns `ToolResult`
- [ ] `requires` declares the minimum capabilities needed
- [ ] `description` is specific enough for the LLM to know when to use it
- [ ] Tool is added to `ALL_LOCAL_TOOLS` in `catalog.py`
- [ ] Tests cover definition, capabilities, and executor logic
- [ ] Tests pass
