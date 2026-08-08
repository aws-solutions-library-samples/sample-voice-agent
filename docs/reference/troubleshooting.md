# Troubleshooting

## Deployment Modes

| Mode | STT/TTS | Best For |
| ---- | ------- | -------- |
| **Cloud API** (`USE_CLOUD_APIS=true`) | Deepgram + Cartesia cloud APIs | Getting started, development |
| **Amazon SageMaker** (default) | Self-hosted on GPU instances | Production, data residency |

Cloud API mode requires Deepgram and Cartesia API keys. Amazon SageMaker mode requires [Deepgram Marketplace subscriptions](deepgram-marketplace-setup.md) and GPU quota.

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
| Amazon SageMaker `ml.g6.2xlarge` | Endpoint instances | 0 | Request increase for Amazon SageMaker mode |
| Amazon SageMaker `ml.g6.12xlarge` | Endpoint instances | 0 | Request increase for Amazon SageMaker mode |
| Amazon ECS Fargate | On-demand vCPU | 256 | Sufficient for default configuration |
| Amazon Bedrock | Tokens per minute | Varies | Monitor throttling in Amazon CloudWatch |

Request service limit increases via the [Service Quotas console](https://console.aws.amazon.com/servicequotas/).

For any feedback, questions, or suggestions, please use the [issues tab](https://github.com/aws-samples/sample-voice-agent/issues) under this repo.
