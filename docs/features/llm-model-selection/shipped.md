# LLM Model Selection

## Summary

Adds support for multiple LLM providers beyond the default Claude Haiku 4.5, including NVIDIA Nemotron Super 120B and OpenAI GPT-5.6 Luna. Introduces an LLM factory that routes models to the appropriate Bedrock endpoint.

## Motivation

Voice agents benefit from model selection flexibility:
- **Low latency** — GPT-5.6 Luna delivers <180ms TTFT, ideal for real-time voice
- **Cost efficiency** — Nemotron Super activates only 12B of 120B params (MoE), delivering 7x throughput
- **Quality** — Claude Haiku 4.5 remains the proven default with excellent instruction following

Community benchmarks at [voice-ai-benchmarks](https://wirjo.github.io/voice-ai-benchmarks/) provide guidance on STT/LLM selection for voice agents.

## Architecture

Models route to different Bedrock endpoints based on their API support:

```
                    ┌─────────────────────────────────┐
                    │       LLM Factory               │
                    │   create_llm_service()          │
                    └───────────┬─────────────────────┘
                                │
              ┌─────────────────┼─────────────────────┐
              │                 │                      │
              ▼                 ▼                      ▼
    ┌─────────────────┐ ┌─────────────────┐  ┌──────────────────┐
    │ Claude Haiku    │ │ Nemotron Super  │  │ GPT-5.6 Luna     │
    │ (Converse API)  │ │ (Converse API)  │  │ (Responses API)  │
    │ bedrock-runtime │ │ bedrock-runtime │  │ bedrock-mantle   │
    └─────────────────┘ └─────────────────┘  └──────────────────┘
```

## Configuration

Set the model via SSM parameter or environment variable:

| Model | SSM Value (`/voice-agent/config/llm-model-id`) | Endpoint | Notes |
|---|---|---|---|
| Claude Haiku 4.5 (default) | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | bedrock-runtime | Proven, low latency, tool calling |
| NVIDIA Nemotron Super 120B | `nvidia.nemotron-super-3-120b` | bedrock-runtime | 7x throughput, MoE (12B active), 256K context |
| OpenAI GPT-5.6 Luna | `openai.gpt-5.6-luna` | bedrock-mantle | Lowest TTFT (<180ms), cheapest GPT tier |
| OpenAI GPT-5.6 Terra | `openai.gpt-5.6-terra` | bedrock-mantle | Balanced quality/cost |
| OpenAI GPT-5.6 Sol | `openai.gpt-5.6-sol` | bedrock-mantle | Highest quality, highest latency |

### Additional Requirements for bedrock-mantle models

- `BEDROCK_API_KEY` environment variable (create via Bedrock console)
- Model access enabled in your Region

## Model Selection for Voice Agents

For voice agents, TTFT (time to first token) is the most critical LLM metric because it directly adds to perceived response time:

```
Total turn latency = STT finalization + LLM TTFT + TTS TTFT + network
```

**Recommended models by priority:**

1. **Claude Haiku 4.5** — Default. Proven low-latency, excellent tool calling, Converse API (no extra key needed)
2. **GPT-5.6 Luna** — Lowest TTFT (<180ms). Near-identical instruction following to Terra (50.3 vs 50.4 on Agents' Last Exam). Server-side tool calling supported.
3. **Nemotron Super 120B** — Best for high-concurrency deployments. 7x throughput means more concurrent calls per dollar. Client-side tool calling only.

## Limitations

- **GPT-5.6 models** require a Bedrock API key (`BEDROCK_API_KEY`) — not needed for Claude/Nemotron
- **GPT-5.6 models** use a different endpoint path (`/openai/v1`) from other mantle models (`/v1`)
- **Nemotron Super** only supports client-side tool calling on bedrock-mantle; use bedrock-runtime (Converse) for server-side tool calling
- **GPT-5.6 Terra/Sol** have higher TTFT than Luna — avoid for latency-sensitive voice unless using Sol Fast mode

## References

- [Voice AI Benchmarks](https://wirjo.github.io/voice-ai-benchmarks/) — Community STT/LLM benchmarks for voice agents
- [Amazon Bedrock Mantle docs](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [NVIDIA Nemotron Super model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-nvidia-nemotron-super-3-120b.html)
- [GPT-5.6 Terra model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-terra.html)

## Status

| Item | Status |
|---|---|
| LLM factory implementation | ✅ Complete |
| Unit tests | ✅ Complete |
| Pipeline integration (ECS + local) | ✅ Complete |
| Integration tested | ⏳ Pending deployment |
