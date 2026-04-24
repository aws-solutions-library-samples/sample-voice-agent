"""Prompt hydration — replace {{Variable_Name}} placeholders with runtime
values and prepend the shared voice-specific system instructions.

Ported from the OG voiceagent (core/hydrator.py) so Cosentus agent prompts
behave identically across the OG EC2 pipeline and this fork. The OG prompts
rely on {{current_time}} and per-case placeholders like {{Service_Date}},
{{Patient_First_Name}}, {{Claim_Number}} etc. Without hydration those
placeholders ship to Bedrock as literal text, which confuses Claude.

For inbound (no case context) calls, pass an empty ``case_data`` dict —
placeholders are then stripped to empty strings. For outbound batch
dialing (Phase 7D), ``case_data`` will be populated from the batch row.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Mapping


VOICE_WRAPPER: str = (
    "# GLOBAL VOICE SYSTEM INSTRUCTIONS\n"
    "You are a specialized voice AI connected to a live, low-latency phone call.\n"
    "- NEVER use markdown formatting (no asterisks, bolding, or bullet points).\n"
    "- ALWAYS speak in short, punchy, conversational sentences.\n"
    "- If you must provide a long string of data (like spelling an NPI or Claim "
    "number), that is the ONLY thing you should say in that turn. Do not add "
    "conversational filler to data readouts.\n"
    "---\n"
)


_PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}")


def hydrate_prompt(prompt_text: str, case_data: Mapping[str, object] | None = None) -> str:
    """Replace ``{{Variable}}`` placeholders in a prompt and prepend the
    global voice system instructions wrapper.

    Semantics match OG voiceagent/core/hydrator.py exactly:

      * ``{{current_time}}`` is always injected with a formatted datetime
        (e.g. "Wednesday, March 25, 2026 04:30 PM"). The caller does not
        need to include it in ``case_data``.
      * For every key in ``case_data``, any ``{{key}}`` occurrence is
        replaced with ``str(value)`` (or an empty string if the value is
        falsy).
      * Any remaining ``{{…}}`` placeholders in the prompt are stripped
        (collapsed to empty string). This keeps stray or unknown
        placeholders from leaking into the LLM context.
      * VOICE_WRAPPER is prepended unconditionally.

    Args:
        prompt_text: The raw prompt template from Aurora (may or may not
            contain placeholders).
        case_data: Optional mapping of placeholder name → value. Pass
            ``None`` or ``{}`` for inbound calls where no case context
            exists; the function will still replace ``{{current_time}}``
            and strip unknown placeholders.

    Returns:
        Hydrated prompt with VOICE_WRAPPER prepended. Safe to pass
        directly into Bedrock/Converse as system content.
    """
    if case_data is None:
        case_data = {}

    prompt = prompt_text or ""

    prompt = prompt.replace(
        "{{current_time}}",
        datetime.now().strftime("%A, %B %d, %Y %I:%M %p"),
    )

    for key, value in case_data.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", str(value) if value else "")

    prompt = _PLACEHOLDER_RE.sub("", prompt)

    return VOICE_WRAPPER + prompt
