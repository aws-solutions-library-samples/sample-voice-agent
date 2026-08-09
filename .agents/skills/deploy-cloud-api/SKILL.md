---
name: deploy-cloud-api
description: Deploys the voice agent to AWS using Deepgram and Cartesia cloud APIs. Guides through prerequisites, environment setup, CDK deployment, and secrets configuration. Use when deploying for the first time, setting up a dev environment, or when SageMaker is not needed.
---

# Deploy — Cloud API Mode

You are guiding the user through deploying the voice agent using Deepgram and Cartesia cloud APIs. This is the simpler deployment path — no SageMaker, no GPU quotas, no Marketplace subscriptions.

## When This Skill Activates
- User wants to deploy the voice agent
- User asks about getting started
- User wants the simplest deployment path
- User mentions cloud APIs, Deepgram cloud, or Cartesia

## What To Do

### Phase 1: Pre-Flight Checks

Run these checks and explain results conversationally:

1. **AWS credentials:**
   ```bash
   aws sts get-caller-identity
   ```
   - If succeeds: "You're connected to AWS account **[ID]** in region **[region]**. Is this the right account for your deployment?"
   - If fails (expired token, invalid credentials, or no credentials configured): list available AWS profiles and offer to use one:
     ```bash
     aws configure list-profiles
     ```
     - If profiles exist: show the list and ask the user which profile to use. Then authenticate with that profile:
       ```bash
       export AWS_PROFILE=<chosen-profile>
       aws sts get-caller-identity
       ```
       If `get-caller-identity` still fails (e.g. expired SSO session), prompt the user to refresh:
       ```bash
       aws sso login --profile <chosen-profile>
       ```
       Then retry `get-caller-identity`.
     - If no profiles exist: walk them through `aws configure` step by step to create one.
   - Once authenticated, confirm the account ID and region with the user.
   - **Wait for the user to confirm the account is correct before proceeding.**

2. **Node.js:**
   ```bash
   node --version
   ```
   Require 18+.

3. **Docker:**
   ```bash
   docker --version 2>/dev/null || finch --version 2>/dev/null
   ```
   Either Docker or finch must be installed and running.

4. **Bedrock model access:**
   ```bash
   aws bedrock list-foundation-models --query "modelSummaries[?modelId=='anthropic.claude-3-5-haiku-20241022-v1:0'].modelId" --output text --region us-east-1
   ```
   - If empty: "You need to enable Claude models in Bedrock. Go to the AWS Console → Amazon Bedrock → Model access → Request access to Anthropic Claude models. This is usually approved instantly."
   - **Don't proceed until Bedrock access is confirmed.**

Report all results in a summary:
```
Pre-flight check results:
✅ AWS credentials — Account 123456789012, us-east-1
✅ Node.js — v20.11.0
✅ Docker — Docker Desktop 4.28.0
✅ Bedrock — Claude models accessible
```

### Phase 2: Gather API Keys

Ask the user for three API keys. Explain what each service does in this project:

1. **Daily.co** — Phone/WebRTC platform that bridges calls to the voice agent. Key from [dashboard.daily.co](https://dashboard.daily.co) -> Developers -> API Keys.
2. **Deepgram** — Speech-to-text (caller's voice -> text for Claude). Key from [console.deepgram.com](https://console.deepgram.com).
3. **Cartesia** — Text-to-speech (Claude's responses -> natural voice). Key from [play.cartesia.ai](https://play.cartesia.ai).

Confirm the user has all three keys before proceeding. They should not paste keys into chat — keys go into `.env` files and Secrets Manager.

### Phase 3: Configure Environment

```bash
cd infrastructure
cp .env.example .env
```

Only two values matter for cloud API mode:
- `ENVIRONMENT` — resource name prefix (default `poc` is fine)
- `AWS_REGION` — must have Bedrock Claude access (default `us-east-1`)

The Deepgram model package ARNs stay as placeholders in cloud API mode.

Show the user the final `.env` and confirm.

### Phase 4: Explain What Will Be Created

Before deploying, show what will be created:

| Resource | Purpose |
|----------|---------|
| VPC + NAT Gateway | Private networking |
| ECS Fargate (2 vCPU, 4GB) | Voice agent container |
| Network Load Balancer | Routes call requests |
| Secrets Manager + KMS | API key storage |
| Lambda + API Gateway | Webhook handler |
| CloudWatch Dashboard | Monitoring |

> **Cost responsibility:** The user is responsible for all AWS charges incurred by these resources. Remind them to use the **destroy-project** skill to tear down resources when done.

**Get explicit confirmation before deploying.** Show the account ID and region.

### Phase 5: Deploy Foundation + Configure Secrets + Deploy Remaining

Deploy in two stages so the ECS container picks up real API keys on first boot (no forced redeployment needed).

1. **Install and bootstrap:**
   ```bash
   cd infrastructure && npm install
   ```
   Check if CDK is bootstrapped; if not, run `npx cdk bootstrap`.

2. **Deploy foundation stacks (Network + Storage):**
   ```bash
   USE_CLOUD_APIS=true npx cdk deploy VoiceAgentNetwork VoiceAgentStorage --require-approval never
   ```
   This creates the VPC and Secrets Manager. Takes ~3-5 minutes.

3. **Configure secrets now, before deploying ECS:**
   Write API keys to `backend/voice-agent/.env`, then push to Secrets Manager:
   ```bash
   ./scripts/init-secrets.sh
   ```
   Verify secrets were stored:
   ```bash
   SECRET_ARN=$(aws ssm get-parameter --name "/voice-agent/storage/api-key-secret-arn" --query 'Parameter.Value' --output text)
   aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query 'SecretString' --output text | python3 -c "import json,sys; [print(f'{k}: {len(v)} chars') for k,v in json.loads(sys.stdin.read()).items() if v]"
   ```

4. **Deploy remaining stacks (ECS + BotRunner):**
   ```bash
   USE_CLOUD_APIS=true npx cdk deploy VoiceAgentSageMaker VoiceAgentEcs VoiceAgentBotRunner --require-approval never
   ```
   The `VoiceAgentSageMaker` stack is included because other stacks depend on it, but in cloud API mode it only writes placeholder SSM parameters — no SageMaker endpoints or GPU resources are created. The ECS container starts with real API keys on first boot. No forced redeployment needed. Takes ~8-10 minutes.

5. **Verify each phase succeeds** before proceeding. If any phase fails:
   - **Docker build timeout**: Container is large; ensure Docker has 4GB+ memory
   - **Access Denied**: IAM role needs administrator access
   - **ExpiredToken**: Re-authenticate and rerun; CDK resumes where it left off

### Phase 6: Show Progress and Next Steps

Fetch and display the webhook URL:
```bash
aws ssm get-parameter --name "/voice-agent/botrunner/webhook-url" --query 'Parameter.Value' --output text
```

Show progress checklist:
```
Deployment Progress:
✅ Infrastructure deployed (Network, Storage, SageMaker, ECS, BotRunner)
✅ Secrets configured
🔲 Daily.co phone number — use /configure-daily
🔲 Verification — use /verify-deployment
🔲 Capability agents (optional) — use /deploy-capability-agents
```

Explain what is deployed and what is not yet:

> **What's running now:** The voice agent is live with Claude (Bedrock) for conversation and two built-in tools: `get_current_time` and `hangup_call`. Tool calling is enabled by default.
>
> **Not yet deployed:** The Knowledge Base and CRM capability agents are optional. After setting up your phone number and testing the basic agent, use `/deploy-capability-agents` to add them.

Remind user: use the **destroy-project** skill when done to release Daily phone numbers and tear down all AWS resources.
