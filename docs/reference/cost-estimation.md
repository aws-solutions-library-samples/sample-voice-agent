# Cost Estimation

You are responsible for the cost of the AWS services used while running this Guidance. As of March 2026, the cost for running this Guidance with the default settings in the US East (N. Virginia) Region is approximately **$135-200 per month** for Cloud API mode, or **$935-1,200 per month** for Amazon SageMaker mode (due to GPU instance costs).

We recommend creating a [Budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html) through [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/) to help manage costs. Prices are subject to change. For full details, refer to the pricing webpage for each AWS service used in this Guidance.

## Sample Cost Table

The following table provides a sample cost breakdown for deploying this Guidance with the default parameters in the US East (N. Virginia) Region for one month.

### Cloud API Mode

| AWS Service | Dimensions | Cost [USD] |
| ----------- | ---------- | ---------- |
| Amazon ECS Fargate | 1 task, 2 vCPU / 4 GB, always-on | ~$70/month |
| NAT Gateway | 1 gateway + data processing | ~$35/month |
| Network Load Balancer | 1 NLB, minimal LCUs | ~$18/month |
| AWS Lambda | ~10,000 invocations/month | ~$0.01/month |
| Amazon API Gateway | ~10,000 requests/month | ~$0.04/month |
| AWS Secrets Manager | 3 secrets | ~$1.20/month |
| Amazon CloudWatch | Logs, metrics, dashboard, alarms | ~$10-15/month |
| Amazon Bedrock | Pay-per-token LLM usage | ~$5-50/month |

### Amazon SageMaker Mode (additional costs)

| AWS Service | Dimensions | Cost [USD] |
| ----------- | ---------- | ---------- |
| Amazon SageMaker STT Endpoint | ml.g6.2xlarge, always-on | ~$350/month |
| Amazon SageMaker TTS Endpoint | ml.g6.12xlarge, always-on | ~$450/month |

> Third-party service costs (Daily.co, Deepgram, Cartesia) vary by usage and are not included above. Refer to each provider's pricing page.

## Additional Considerations

- This Guidance creates a NAT Gateway which incurs hourly charges even when idle.
- Amazon SageMaker endpoints (Amazon SageMaker mode) run on GPU instances that are billed per hour irrespective of usage.
- Third-party services (Daily.co, Deepgram, Cartesia) have their own pricing and usage limits.
- The Amazon ECS Fargate service runs at least one task continuously (always-on architecture) to avoid cold start latency.
- Cloud API mode routes audio through the public internet. Use Amazon SageMaker mode if data residency is required.
