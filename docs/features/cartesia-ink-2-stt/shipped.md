# Cartesia Ink 2 STT Integration

## Summary

Adds Cartesia Ink 2 as an alternative STT provider, leveraging native turn detection for more natural conversations. Also supports Cartesia ink-whisper for standard VAD-driven transcription.

## Motivation

Cartesia Ink 2 provides:
- **Lowest word error rate** of any streaming STT model
- **Native turn detection** — the model knows when users start and finish speaking (no fixed silence threshold)
- **Structured data handling** — phone numbers, dates, emails, currencies transcribed correctly
- **Keyterms** — domain-specific vocabulary biasing for better accuracy on product names/jargon

This eliminates the reliance on fixed VAD silence thresholds for determining end-of-turn, resulting in more natural conversations where the agent doesn't cut in prematurely or wait too long.

## Configuration

| SSM Parameter | Value | Description |
|---|---|---|
| `/voice-agent/config/stt-provider` | `cartesia` | Ink-whisper model, standard VAD-driven |
| `/voice-agent/config/stt-provider` | `cartesia-turns` | Ink-2 model, native turn detection |

Both require `CARTESIA_API_KEY` environment variable (same key used for TTS).

No SageMaker endpoint needed — Cartesia STT is cloud-only via WebSocket.

## Architecture

```
[Audio In] → [Silero VAD] → [CartesiaTurnsSTTService (ink-2)] → [LLM] → [TTS] → [Audio Out]
                                        ↕
                              Cartesia WebSocket API
                              (turn.start, turn.update,
                               turn.eager_end, turn.end)
```

With `cartesia-turns`, Silero VAD is still present for interruption detection, but turn boundaries are driven server-side by ink-2.

## Provider Comparison

| Provider | Model | Turn Detection | VAD Required | Endpoint |
|---|---|---|---|---|
| `deepgram` | Nova-3 | External (Silero VAD) | Yes | Cloud WebSocket |
| `cartesia` | ink-whisper | External (Silero VAD) | Yes | Cloud WebSocket |
| `cartesia-turns` | ink-2 | Native (server-driven) | For interrupts only | Cloud WebSocket |
| `sagemaker` | Nova-3 | External (Silero VAD) | Yes | SageMaker HTTP/2 BiDi |

## Dependencies

- `pipecat-ai[cartesia]` >= 1.7.0 (includes CartesiaTurnsSTTService + keyterm support)
- Cartesia API key with ink-2 model access

## Status

| Item | Status |
|---|---|
| Implementation | ✅ Complete |
| Unit tests | ✅ Complete |
| Integration tested | ⏳ Pending deployment |
