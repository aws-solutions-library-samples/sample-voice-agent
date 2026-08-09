---
name: deploy-sagemaker
description: Deploys the voice agent to AWS with self-hosted Deepgram STT/TTS on SageMaker GPU endpoints. Guides through GPU quota checks, Marketplace subscriptions, model package ARN configuration, and CDK deployment. Use for production deployments or when audio must stay within the VPC.
---

# Deploy — SageMaker Mode

You are guiding the user through deploying the voice agent with self-hosted Deepgram STT/TTS on SageMaker GPU endpoints. Audio never leaves the VPC.

## When This Skill Activates
- User wants a production deployment
- User mentions SageMaker, self-hosted, data residency, or VPC-only
- User has already subscribed to Deepgram on AWS Marketplace

## What To Do

### Phase 1: Pre-Flight Checks

Run the same checks as deploy-cloud-api (AWS credentials, Node.js, Docker, Bedrock access) and additionally:

1. **Confirm account:**
   ```bash
   aws sts get-caller-identity
   ```
   Show account ID and region. **Get explicit confirmation this is the right account.**

2. **Check SageMaker GPU quotas:**
   ```bash
   aws service-quotas get-service-quota --service-code sagemaker --quota-code "L-1B43B3DD" --query 'Quota.Value' --output text
   aws service-quotas get-service-quota --service-code sagemaker --quota-code "L-E460AE79" --query 'Quota.Value' --output text
   ```
   - STT needs `ml.g6.2xlarge` quota >= 2
   - TTS needs `ml.g6.12xlarge` quota >= 2
   - If insufficient: suggest deploying cloud API mode first while quotas are pending (24-48 hours)

Report all results as a summary checklist.

### Phase 2: Verify Marketplace Subscriptions

Ask for two model package ARNs. If the user doesn't have them, direct them to `docs/reference/deepgram-marketplace-setup.md`.

Expected format:
- STT: `arn:aws:sagemaker:<region>:865070037744:model-package/deepgram-streaming-stt-...`
- TTS: `arn:aws:sagemaker:<region>:865070037744:model-package/deepgram-streaming-tts-...`

Validate: both must start with `arn:aws:sagemaker:` and contain `model-package/`. Region must match deployment region.

### Phase 3: Configure Environment

```bash
cd infrastructure && cp .env.example .env
```

Set model package ARNs in `.env`. Region in the ARNs must match `AWS_REGION`.

### Phase 4: Explain What Will Be Created

Same resources as cloud-api mode, plus:

| Resource | Purpose |
|----------|---------|
| SageMaker STT (ml.g6.2xlarge) | Deepgram Nova-3 on 1x L4 GPU |
| SageMaker TTS (ml.g6.12xlarge) | Deepgram Aura on 4x L4 GPU |

Deployment takes 20-25 minutes (SageMaker endpoints ~15 min).

> **Cost responsibility:** The user is responsible for all AWS charges incurred by these resources. SageMaker GPU endpoints incur charges while running. Remind them to use the **destroy-project** skill to tear down resources when done.

**Get explicit confirmation before deploying.**

### Phase 5: Deploy Foundation + Configure Secrets + Deploy Remaining

Deploy in two stages so the ECS container picks up the Daily API key on first boot.

1. **Install and bootstrap:**
   ```bash
   cd infrastructure && npm install
   ```
   Check if CDK is bootstrapped; if not, run `npx cdk bootstrap`.

2. **Deploy foundation stacks (Network + Storage):**
   ```bash
   npx cdk deploy VoiceAgentNetwork VoiceAgentStorage --require-approval never
   ```

3. **Configure secrets now, before deploying ECS:**
   SageMaker mode only needs `DAILY_API_KEY` (no Deepgram/Cartesia cloud keys).
   Write to `backend/voice-agent/.env`, then push:
   ```bash
   ./scripts/init-secrets.sh
   ```

4. **Deploy remaining stacks (SageMaker + ECS + BotRunner):**
   ```bash
   npx cdk deploy VoiceAgentSageMaker VoiceAgentEcs VoiceAgentBotRunner --require-approval never
   ```
   SageMaker endpoints take 10-15 minutes to provision. This is normal.
   - **ResourceLimitExceeded** = GPU quota insufficient
   - **Model package not found** = wrong ARN or region mismatch

### Phase 6: Verify SageMaker Endpoints

```bash
STT_ENDPOINT=$(aws ssm get-parameter --name "/voice-agent/sagemaker/stt-endpoint-name" --query 'Parameter.Value' --output text)
TTS_ENDPOINT=$(aws ssm get-parameter --name "/voice-agent/sagemaker/tts-endpoint-name" --query 'Parameter.Value' --output text)

aws sagemaker describe-endpoint --endpoint-name "$STT_ENDPOINT" --query 'EndpointStatus' --output text
aws sagemaker describe-endpoint --endpoint-name "$TTS_ENDPOINT" --query 'EndpointStatus' --output text
```

Both should show "InService". If "Creating", wait and recheck.

### Phase 7: Show Progress and Next Steps

Same as deploy-cloud-api Phase 7 -- show progress checklist, direct to configure-daily. Remind user to use the **destroy-project** skill when done to release Daily phone numbers and tear down all AWS resources.
