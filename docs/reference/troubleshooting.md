# Troubleshooting

## Deployment Modes

| Mode | STT/TTS | Best For |
| ---- | ------- | -------- |
| **Cloud API** (`USE_CLOUD_APIS=true`) | Deepgram (or Deepgram Flux) + Cartesia cloud APIs | Getting started, development |
| **Amazon SageMaker** (default) | Self-hosted on GPU instances | Production, data residency |

Cloud API mode requires Deepgram and Cartesia API keys. Optionally use Deepgram Flux for prosody-aware turn detection (`STT_PROVIDER=flux-sagemaker`). Amazon SageMaker mode requires [Deepgram Marketplace subscriptions](deepgram-marketplace-setup.md) and GPU quota.

## Known Issues

**No response from agent:**

1. Check AWS Lambda logs for webhook errors
2. Verify API keys are correctly configured in AWS Secrets Manager
3. Check Amazon ECS service is running and healthy

**High latency:**

1. Check Amazon SageMaker endpoint Amazon CloudWatch metrics
2. Review Amazon CloudWatch metrics for Amazon Bedrock latency
3. Verify VPC endpoints are configured correctly

**No audio output:**

1. Verify Daily room configuration (SIP enabled)
2. Check TTS provider API key is valid
3. Review voice agent container logs

## Limitations

- Maximum concurrent calls per container is configurable (default: 10) but bounded by CPU/memory.
- Cold start for new Amazon ECS tasks takes ~90 seconds. Total time from overload to new capacity: ~3-5 minutes.
- The A2A capability agent discovery relies on AWS Cloud Map polling (default: every 30 seconds).

## Service Limits

| Service | Limit | Default | Notes |
| ------- | ----- | ------- | ----- |
| Amazon SageMaker `ml.g6.2xlarge` | Endpoint instances | 0 | STT primary instance type. The STT endpoint also falls back to `ml.g6e.2xlarge` -> `ml.g5.2xlarge` -> `ml.g4dn.2xlarge` on capacity errors (see below), so request quota for all four if you can. |
| Amazon SageMaker `ml.g6.12xlarge` | Endpoint instances | 0 | TTS primary instance type. Fallback order: `ml.g6e.12xlarge` -> `ml.g5.12xlarge` -> `ml.g4dn.12xlarge`. |
| Amazon ECS Fargate | On-demand vCPU | 256 | Sufficient for default configuration |
| Amazon Bedrock | Tokens per minute | Varies | Monitor throttling in Amazon CloudWatch |

Request service limit increases via the [Service Quotas console](https://console.aws.amazon.com/servicequotas/).

### GPU capacity errors (InsufficientInstanceCapacity)

The default GPU instance types (`ml.g6.2xlarge` for STT, `ml.g6.12xlarge` for TTS) can hit `InsufficientInstanceCapacity` in busy regions like `us-east-1`. As of [#30](https://github.com/aws-solutions-library-samples/sample-voice-agent/issues/30), both endpoint configs use SageMaker [instance pools](https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-heterogeneous.html): SageMaker tries the highest-priority instance type first and automatically falls back through a priority-ordered list on capacity errors, instead of failing outright.

- STT pool: `ml.g6.2xlarge` -> `ml.g6e.2xlarge` -> `ml.g5.2xlarge` -> `ml.g4dn.2xlarge`
- TTS pool: `ml.g6.12xlarge` -> `ml.g6e.12xlarge` -> `ml.g5.12xlarge` -> `ml.g4dn.12xlarge`

`g4dn` is intentionally last in both pools -- it's the slowest of the four (measured TTS realtime factor ~1.02 vs ~0.93 on g6/g5/g6e), so it's a fallback, not a first choice.

**Residual risk:** STT and TTS endpoints still deploy in one CloudFormation stack (`VoiceAgentSageMaker`). If *all four* pooled instance types for one endpoint are simultaneously unavailable, that endpoint's creation still fails and CloudFormation still rolls back the stack -- including deleting an already-healthy sibling endpoint. Instance pools make this scenario much rarer (it now requires a capacity shortage across four different GPU families at once, not one), but they don't eliminate it. If this residual failure mode recurs in practice, the next step is splitting STT and TTS into separate stacks (tracked as a possible follow-up) so a capacity failure on one can never touch the other.

If you still hit capacity errors after this fix, request quota for all four instance types per pool, or try a different Availability Zone/Region.

For any feedback, questions, or suggestions, please use the [issues tab](https://github.com/aws-samples/sample-voice-agent/issues) under this repo.
