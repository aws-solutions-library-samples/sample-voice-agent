---
id: deepgram-flux-stt-integration
status: in-progress
created: 2026-08-08
---

# Deepgram Flux STT Integration

## Problem

The current STT pipeline uses Deepgram Nova-3 on SageMaker with Silero VAD for turn detection. While functional, Silero VAD uses a fixed silence threshold (0.3s) that can cut off mid-sentence pauses or feel unresponsive. Deepgram Flux offers native, prosody-aware turn detection that better understands conversational speech patterns.

## Proposed Solution

Add Deepgram Flux as an alternative STT provider (`STT_PROVIDER=flux-sagemaker`) that runs on a separate SageMaker endpoint. Flux provides:

- **Native turn detection** — prosody-aware end-of-turn signals instead of fixed silence thresholds
- **Eager end-of-turn events** — faster response triggering for snappier conversations
- **Multilingual support** — `flux-general-multi` model for non-English callers
- **Backward compatible** — Nova-3 remains the default; Flux is opt-in via config

## Architecture

Same BiDi HTTP/2 streaming pattern as Nova-3, using a separate SageMaker endpoint:

```
Caller -> Daily -> ECS -> SageMaker Flux STT Endpoint (BiDi HTTP/2, port 8443)
                       -> SageMaker TTS Endpoint (Aura-2, unchanged)
                       -> Bedrock LLM (unchanged)
```

Flux STT handles turn boundaries natively. Silero VAD remains as the audio passthrough filter on the aggregator but defers to Flux's smarter turn detection.

## Implementation

- New service wrapper: `app/services/deepgram_flux_sagemaker_stt.py`
- Factory support: `STT_PROVIDER=flux-sagemaker` in `factory.py`
- Config: `FLUX_STT_ENDPOINT_NAME` env var / SSM parameter
- Infrastructure: Optional Flux SageMaker endpoint in CDK (separate Marketplace subscription)
- Pipecat version: bumped to >=0.0.108 (Flux SageMaker support added in that release)

## Trade-offs

| Aspect | Nova-3 (current) | Flux (new option) |
|--------|-------------------|-------------------|
| Turn detection | Silero VAD (0.3s silence) | Native prosody-aware |
| Latency (TTFB) | ~140ms | TBD (expected similar or better) |
| GPU requirements | ml.g6.2xlarge (1x L4) | TBD (may need larger instance) |
| Multilingual | English only (nova-3) | Multi-language (flux-general-multi) |
| Maturity | Production-proven | Newer, less battle-tested |
| Cost | Known | Separate Marketplace subscription |

## Open Questions

1. What instance type does Flux STT require on SageMaker?
2. Is the Flux model package available on AWS Marketplace yet?
3. Should we support a hybrid mode (Flux turn detection + Nova-3 transcription)?
