"""Phase 7E: post-call analyses on Bedrock.

Ports voiceagent/core/post_call.py from Anthropic-native API to AWS
Bedrock so the fork can run the same structured-extraction LLM pass
the OG pipeline runs without needing an ANTHROPIC_API_KEY secret.

Behavior parity is the goal — same prompt, same fallback model
chain, same JSON-only response contract, same validation of selector
choices. The only thing that changes is the LLM client and the model
ID format (short name like "claude-sonnet-4-6" gets resolved through
``resolve_bedrock_model_id`` to the inference-profile ARN, same as
the live conversation LLM in Phase 7A).

Output is written into ``voice_calls.post_call_analyses`` (JSONB) by
``call_writer.write_call_record``. Lambda's POST /api/auto-actions
endpoint then reads that column to populate ``voice_call_costs``,
``voice_call_scores``, and ``voice_auto_actions`` (task creation,
denial routing, AR-call logging).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import boto3
import structlog

if TYPE_CHECKING:
    from app.services.agent_config import PostCallConfig, PostCallField

logger = structlog.get_logger(__name__)

# Synchronous Bedrock client — same pattern as agent_config.py /
# call_writer.py. Use asyncio.to_thread to call from async code.
_BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")

# Fallback model when the agent's configured PCA model is unavailable
# in our region or isn't yet in our _SHORT_TO_BEDROCK map. Mirrors OG
# behavior (core/post_call.py:76).
_FALLBACK_MODEL_SHORT = "claude-sonnet-4-6"

# OG (anthropic) hits a NotFoundError when the model identifier is
# wrong; Bedrock surfaces that as a ValidationException (or sometimes
# AccessDeniedException for un-entitled models). Both should trigger
# fallback.
_BEDROCK_RETRYABLE_ERRORS = (
    "ValidationException",
    "AccessDeniedException",
    "ResourceNotFoundException",
)


def _format_transcript(transcript: list[dict[str, Any]]) -> str:
    """Render the in-memory transcript as a flat dialogue text the LLM
    can read.

    Accepts the shape ConversationObserver appends:
      ``{"turn_number", "speaker", "content", "timestamp"}``.
    Also tolerates the OG shape ``{"role", "content"}`` so a future
    consumer that pulls transcripts from a different source still
    works without us reformatting upstream.
    """
    lines: list[str] = []
    for turn in transcript:
        speaker = turn.get("speaker") or turn.get("role") or ""
        content = turn.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                item if isinstance(item, str) else str(item.get("text", item))
                for item in content
            )
        if speaker == "assistant":
            label = "Agent"
        elif speaker == "user":
            label = "Caller"
        else:
            label = speaker or "?"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


def _build_field_instructions(fields: List["PostCallField"]) -> str:
    """Produce the field list the LLM extracts. Identical to OG's
    formatting so prompt-cache compatibility holds.
    """
    parts: list[str] = []
    for i, field in enumerate(fields, 1):
        if field.type == "selector":
            choices_str = ", ".join(field.choices)
            line = f'{i}. {field.name} (select ONE of: {choices_str}): {field.description}'
        else:
            line = f'{i}. {field.name} (text): {field.description}'
            if field.format_examples:
                line += f'\n   Example format: "{field.format_examples[0]}"'
        parts.append(line)
    return "\n".join(parts)


def _build_prompt(
    transcript_text: str,
    field_instructions: str,
    case_data: Dict[str, Any],
) -> str:
    """Match OG core/post_call.py prompt byte-for-byte.

    Identical structure means an analyses run on a fork-handled call
    produces the same fields the post-call frontend already knows how
    to render.
    """
    case_block = json.dumps(case_data, indent=2) if case_data else "No case data provided."
    return (
        "You are analyzing a completed phone call. "
        "Read the transcript and extract the following fields.\n\n"
        f"Transcript:\n{transcript_text}\n\n"
        f"Case data:\n{case_block}\n\n"
        f"Fields to extract:\n\n{field_instructions}\n\n"
        "Respond ONLY with a valid JSON object mapping each field name to its value. "
        "For selector fields, use exactly one of the provided choices. "
        "For text fields, write a concise response.\n\n"
        "Output JSON only — no markdown, no backticks, no explanation."
    )


def _resolve_bedrock_id(short_or_full: str) -> str:
    """Wrapper around agent_config.resolve_bedrock_model_id so PCA
    calls get the same model-ID translation rules the conversation
    LLM uses (Phase 7A)."""
    from app.services.agent_config import resolve_bedrock_model_id

    return resolve_bedrock_model_id(short_or_full)


async def _invoke_bedrock(
    *,
    model_id: str,
    prompt: str,
    max_tokens: int = 1000,
) -> str:
    """Call Bedrock's Converse API with a single user message; return
    the assistant's text reply. Synchronous boto3 wrapped in
    to_thread to avoid the aiobotocore connection-drop issues we hit
    in Phase 7A.

    Raises whatever Bedrock raises on failure — caller decides whether
    to fall back to a different model.
    """
    request_params: Dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {"role": "user", "content": [{"text": prompt}]},
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            # Lower than conversation temperature — analyses should
            # be deterministic-ish. Matches OG's anthropic call which
            # didn't set a temperature (defaulted to 1.0 there); we
            # opt for 0.0 here because OG's structured extraction is
            # demonstrably temperature-tolerant down to deterministic.
            "temperature": 0.0,
        },
    }
    response = await asyncio.to_thread(
        _BEDROCK.converse, **request_params
    )

    # Converse returns: {output: {message: {role, content: [{text}]}}}
    output = response.get("output", {})
    message = output.get("message", {})
    blocks = message.get("content") or []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, str) and text:
            return text
    return ""


async def run_post_call_analyses(
    pca_config: Optional["PostCallConfig"],
    transcript: List[Dict[str, Any]],
    case_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Run all post-call analysis fields in a single batched LLM call.

    Returns a dict mapping ``field.name -> extracted value``. On any
    failure (no fields configured, empty transcript, both primary +
    fallback model errors, malformed JSON), returns either ``{}`` or
    ``{"_error": "..."}`` — never raises.

    Args:
        pca_config: Agent's post_call_analyses config (model + fields).
            None or empty fields → returns ``{}`` (no-op, log only).
        transcript: List of turn dicts. Empty → ``{}``.
        case_data: Hydrator dict (Service_Date, Patient_First_Name…).
            Available to the LLM as additional context.

    Returns:
        Dict suitable for ``voice_calls.post_call_analyses`` JSONB.
    """
    if not pca_config or not pca_config.fields:
        logger.debug(
            "post_call_skip_no_fields_configured",
            field_count=0,
        )
        return {}

    if not transcript:
        logger.info("post_call_skip_empty_transcript")
        return {}

    transcript_text = _format_transcript(transcript)
    field_instructions = _build_field_instructions(pca_config.fields)
    prompt = _build_prompt(transcript_text, field_instructions, case_data or {})

    primary_model_short = pca_config.model or _FALLBACK_MODEL_SHORT
    candidates: List[str] = []
    seen: set[str] = set()
    for short in (primary_model_short, _FALLBACK_MODEL_SHORT):
        if short and short not in seen:
            seen.add(short)
            candidates.append(short)

    last_error: Optional[str] = None
    for short_id in candidates:
        bedrock_id = _resolve_bedrock_id(short_id)
        try:
            text = await _invoke_bedrock(model_id=bedrock_id, prompt=prompt)
        except Exception as exc:
            err_name = type(exc).__name__
            last_error = f"{err_name}: {str(exc)[:300]}"
            # Bedrock SDK raises botocore.exceptions.ClientError; the
            # actual API error code is in exc.response['Error']['Code'].
            # Fall back if we hit a known retryable error on a non-
            # final candidate.
            api_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            is_retryable = api_code in _BEDROCK_RETRYABLE_ERRORS
            if short_id != candidates[-1] and is_retryable:
                logger.warning(
                    "post_call_model_failed_falling_back",
                    primary_model=short_id,
                    bedrock_id=bedrock_id,
                    error=last_error,
                    api_code=api_code,
                )
                continue
            logger.error(
                "post_call_invoke_failed",
                model_short=short_id,
                bedrock_id=bedrock_id,
                error=last_error,
                api_code=api_code,
            )
            return {"_error": last_error or "Bedrock invocation failed"}

        # Strip common markdown fences the model sometimes wraps
        # despite our explicit instruction. Same defensive parse OG
        # uses.
        cleaned = text.strip().replace("```json", "").replace("```", "").strip()
        if not cleaned:
            logger.error(
                "post_call_empty_response", model_short=short_id
            )
            last_error = "Empty Bedrock response"
            continue

        try:
            results = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_error = f"JSON parse: {exc}"
            logger.error(
                "post_call_parse_failed",
                model_short=short_id,
                error=last_error,
                response_excerpt=cleaned[:300],
            )
            return {"_error": "Failed to parse analysis response"}

        if not isinstance(results, dict):
            logger.error(
                "post_call_non_dict_response",
                model_short=short_id,
                received_type=type(results).__name__,
            )
            return {"_error": "Analysis response was not a JSON object"}

        # Validate selector fields against allowed choices. Match OG
        # behavior: invalid choices get a "invalid: <value>" prefix
        # rather than being silently dropped, so analyses still
        # surface in the UI for operator triage.
        for field in pca_config.fields:
            if field.type == "selector" and field.name in results:
                if results[field.name] not in field.choices:
                    results[field.name] = f"invalid: {results[field.name]}"

        if short_id != primary_model_short:
            logger.warning(
                "post_call_used_fallback_model",
                primary_model_short=primary_model_short,
                fallback_used=short_id,
            )
        logger.info(
            "post_call_analysis_complete",
            model_short=short_id,
            field_count=len(pca_config.fields),
            extracted_keys=list(results.keys()),
        )
        return results

    logger.error(
        "post_call_exhausted_models",
        last_error=last_error,
        tried=candidates,
    )
    return {"_error": last_error or "Post-call analysis exhausted all model options"}
