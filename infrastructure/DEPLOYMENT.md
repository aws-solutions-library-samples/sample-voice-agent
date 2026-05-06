# Deployment Guide

Complete guide for deploying the SIP Voice Agent. Choose your deployment mode:

- **[Cloud API Mode](#path-a-cloud-api-mode)** -- Uses Deepgram and Cartesia cloud APIs. Simpler setup, no GPU quotas or Marketplace subscriptions needed. Best for getting started and development.
- **[SageMaker Mode](#path-b-sagemaker-mode)** -- Runs Deepgram STT/TTS on self-hosted SageMaker GPU endpoints. Audio stays in your VPC. Best for production workloads with data residency requirements.

Both modes use the same core infrastructure (VPC, ECS, Lambda, API Gateway) and differ only in how STT/TTS is handled.

---

## Prerequisites (Both Modes)

### AWS Account Setup

1. **AWS Account** with administrative access
2. **AWS CLI** installed and configured with credentials:
   ```bash
   aws sts get-caller-identity  # Verify credentials work
   ```
3. **Node.js 18+** installed (check with `node --version`)
4. **Docker** installed and running (or [finch](https://github.com/runfinch/finch) as an alternative)

### Bedrock Model Access

Enable Claude models in your AWS account:

1. Go to **AWS Console** -> **Amazon Bedrock** -> **Model access**
2. Request access to:
   - `anthropic.claude-3-5-haiku-20241022`
   - `anthropic.claude-3-haiku-20240307`
3. Wait for approval (usually instant for these models)

### Daily.co Account

1. Create an account at [dashboard.daily.co](https://dashboard.daily.co)
2. Add a credit card (required for purchasing phone numbers)
3. Go to **Developers** -> **API Keys** and generate an API key
4. Save the API key -- you'll need it for secrets configuration

---

## Path A: Cloud API Mode

This mode uses Deepgram and Cartesia cloud APIs for STT/TTS. No SageMaker endpoints, no GPU quotas, no Marketplace subscriptions.

### Additional Prerequisites (Cloud API Mode)

You will need API keys for:
- **[Deepgram](https://console.deepgram.com/)** -- Speech-to-Text (Nova-3 model)
- **[Cartesia](https://play.cartesia.ai/)** -- Text-to-Speech (Sonic model) *(default)*
- **[ElevenLabs](https://elevenlabs.io/)** -- Text-to-Speech (Multilingual v2 model) *(alternative — set `TTS_PROVIDER=elevenlabs`)*

### Step 1: Configure Environment

```bash
cd infrastructure

# Copy the environment template
cp .env.example .env
```

Edit `.env` with your settings:
```bash
# .env
ENVIRONMENT=poc
AWS_REGION=us-east-1

# Leave the Deepgram model package ARNs as-is (not needed in cloud API mode)
```

### Step 2: Install Dependencies and Deploy Foundation

```bash
# Install CDK dependencies
npm install

# Deploy foundation stacks first (Network + Storage)
USE_CLOUD_APIS=true npx cdk deploy VoiceAgentNetwork VoiceAgentStorage --require-approval never
```

This creates the VPC and Secrets Manager (~3-5 minutes).

### Step 3: Configure Secrets

Add your API keys to Secrets Manager **before deploying ECS**, so the container picks them up on first boot:

```bash
# Create backend/voice-agent/.env with your keys:
cat > ../backend/voice-agent/.env << 'EOF'
DEEPGRAM_API_KEY=your-deepgram-api-key
CARTESIA_API_KEY=your-cartesia-api-key
DAILY_API_KEY=your-daily-api-key
# Optional: Use ElevenLabs instead of Cartesia for TTS
# TTS_PROVIDER=elevenlabs
# ELEVENLABS_API_KEY=your-elevenlabs-api-key
EOF

# Push to Secrets Manager
./scripts/init-secrets.sh

# Or run interactively (prompts for keys):
./scripts/init-secrets.sh
```

### Step 4: Deploy Remaining Stacks

```bash
# Deploy remaining stacks (ECS + BotRunner)
USE_CLOUD_APIS=true npx cdk deploy VoiceAgentSageMaker VoiceAgentEcs VoiceAgentBotRunner --require-approval never
```

The ECS container starts with real API keys on first boot. No forced redeployment needed.

This deploys 3 remaining stacks:
1. **VoiceAgentSageMaker** -- Writes placeholder SSM parameters only (no SageMaker resources in cloud API mode)
2. **VoiceAgentEcs** -- ECS Fargate cluster, NLB, CloudWatch dashboard
3. **VoiceAgentBotRunner** -- Lambda webhook handler, API Gateway

Total deployment time: ~8-10 minutes.

### Step 5: Set Up Daily.co Phone Number

Purchase a phone number and configure the webhook:

```bash
# Automated setup (recommended)
./scripts/setup-daily.sh

# Or specify a region for the phone number
PHONE_REGION=NY ./scripts/setup-daily.sh
```

The script will:
1. Load your `DAILY_API_KEY` from `backend/voice-agent/.env`
2. Fetch the webhook URL from SSM (deployed in Step 2)
3. List available phone numbers and purchase one (with confirmation)
4. Configure pinless dial-in with your webhook URL
5. Save `DAILY_PHONE_NUMBER` and `DAILY_HMAC_SECRET` to `.env`

After running, sync the updated secrets to AWS:
```bash
./scripts/init-secrets.sh
```

For manual setup or troubleshooting, see [Daily.co Setup Guide](../docs/reference/daily-setup.md).

> **Note:** It can take a few minutes for Daily.co to fully provision the phone number and activate dial-in routing. If the first call doesn't connect, wait 2-3 minutes and try again.

### Step 6: Verify and Test

```bash
# Test the webhook endpoint
./deploy.sh webhook

# Or run the full integration test suite
./deploy.sh verify
```

Then **call your phone number** -- you should hear the voice assistant respond.

---

## Path B: SageMaker Mode

This mode runs Deepgram STT/TTS on dedicated SageMaker GPU endpoints inside your VPC. Audio never leaves AWS.

### Additional Prerequisites (SageMaker Mode)

#### Service Quotas

Request these quotas in the [Service Quotas console](https://console.aws.amazon.com/servicequotas/) before deployment:

| Service | Quota | Value Needed |
|---------|-------|--------------|
| SageMaker | ml.g6.2xlarge for endpoint usage | 2+ |
| SageMaker | ml.g6.12xlarge for endpoint usage | 2+ |
| VPC | Elastic IPs | 2+ |
| VPC | NAT Gateways per AZ | 1+ |

Quota increases can take 24-48 hours for GPU instances.

#### Deepgram Marketplace Subscriptions

Subscribe to Deepgram model packages on AWS Marketplace. See the [Deepgram Marketplace Setup Guide](../docs/reference/deepgram-marketplace-setup.md) for detailed step-by-step instructions.

After subscribing, you will have two model package ARNs:
- **STT**: `arn:aws:sagemaker:<region>:865070037744:model-package/deepgram-streaming-stt-...`
- **TTS**: `arn:aws:sagemaker:<region>:865070037744:model-package/deepgram-streaming-tts-...`

### Step 1: Configure Environment

```bash
cd infrastructure

# Copy the environment template
cp .env.example .env
```

Edit `.env` with your settings:
```bash
# .env
ENVIRONMENT=poc
AWS_REGION=us-east-1

# Deepgram Model Package ARNs (from Marketplace subscription)
DEEPGRAM_STT_MODEL_PACKAGE_ARN=arn:aws:sagemaker:us-east-1:865070037744:model-package/deepgram-streaming-stt-...
DEEPGRAM_TTS_MODEL_PACKAGE_ARN=arn:aws:sagemaker:us-east-1:865070037744:model-package/deepgram-streaming-tts-...
```

### Step 2: Install Dependencies and Deploy Foundation

```bash
# Install CDK dependencies
npm install

# Deploy foundation stacks first (Network + Storage)
npx cdk deploy VoiceAgentNetwork VoiceAgentStorage --require-approval never
```

### Step 3: Configure Secrets

Add your Daily API key to Secrets Manager **before deploying ECS**:

```bash
# Create backend/voice-agent/.env with your Daily API key
cat > ../backend/voice-agent/.env << 'EOF'
DAILY_API_KEY=your-daily-api-key
EOF

# Push to Secrets Manager
./scripts/init-secrets.sh
```

In SageMaker mode, you only need the `DAILY_API_KEY`. Deepgram and Cartesia keys are not needed because STT/TTS runs on SageMaker endpoints within your VPC.

### Step 4: Deploy Remaining Stacks

```bash
# Deploy SageMaker endpoints + ECS + BotRunner
npx cdk deploy VoiceAgentSageMaker VoiceAgentEcs VoiceAgentBotRunner --require-approval never
```

This deploys 3 remaining stacks:
1. **VoiceAgentSageMaker** -- Deepgram STT + TTS SageMaker endpoints (~15 minutes)
2. **VoiceAgentEcs** -- ECS Fargate cluster, NLB, CloudWatch dashboard
3. **VoiceAgentBotRunner** -- Lambda webhook handler, API Gateway

Total deployment time: ~20-25 minutes (SageMaker endpoint provisioning is the bottleneck).

### Step 5: Set Up Daily.co Phone Number

Same as Cloud API mode:
```bash
./scripts/setup-daily.sh
./scripts/init-secrets.sh
```

### Step 6: Verify and Test

```bash
./deploy.sh verify
```

Call your phone number to test.

---

## Post-Deployment Options

### Deploy Capability Agents (A2A)

The Knowledge Base and CRM capability agents extend the voice agent with RAG and customer data. They are optional.

```bash
# Deploy Knowledge Base agent
./deploy.sh deploy-stack VoiceAgentKnowledgeBase
./deploy.sh deploy-stack VoiceAgentKbAgent

# Deploy CRM agent
./deploy.sh deploy-stack VoiceAgentCRM
./deploy.sh deploy-stack VoiceAgentCrmAgent
```

Enable the capability registry so the voice agent discovers these agents:
```bash
aws ssm put-parameter \
  --name "/voice-agent/config/enable-capability-registry" \
  --value "true" \
  --type String \
  --overwrite
```

After enabling, the voice agent discovers capability agents on its next CloudMap poll cycle (default: 30 seconds). See [Adding a Capability Agent](../docs/guides/adding-a-capability-agent.md) for building custom agents.

#### Seed CRM Demo Data

The CRM DynamoDB tables are created empty. You must seed them with demo data to test CRM lookups:

```bash
CRM_URL=$(aws ssm get-parameter --name "/voice-agent/crm/api-url" --query 'Parameter.Value' --output text)
curl -s -X POST "$CRM_URL/admin/seed" | python3 -m json.tool
```

Expected response: `{"message": "Demo data seeded successfully", "customers_seeded": 3, "cases_seeded": 2}`

This loads 3 demo customers:

| Customer | Phone | Account Type |
|----------|-------|-------------|
| John Smith | 555-0100 | premium |
| Sarah Johnson | 555-0101 | basic |
| Michael Chen | 555-0102 | enterprise |

Test by calling your phone number and saying: *"Look up the account for 555-0100"*

> **Without seeding, all CRM lookups return "customer not found."**

To reset and re-seed: `curl -s -X DELETE "$CRM_URL/admin/reset"` followed by `curl -s -X POST "$CRM_URL/admin/seed"`

### Enable Call Transfers

The voice agent can transfer calls to human agents via SIP REFER. See [Call Transfers](../docs/reference/call-transfers.md) for setup instructions.

### Tool Calling

Tool calling is **enabled by default**. The voice agent registers built-in tools (`get_current_time`, `hangup_call`) automatically. If you have deployed capability agents and enabled the capability registry, their tools are also registered.

To disable tool calling (conversation-only mode with no tools):
```bash
aws ssm put-parameter \
  --name "/voice-agent/config/enable-tool-calling" \
  --value "false" \
  --type String \
  --overwrite
```

This takes effect on the next incoming call — no container restart needed.

---

## Auto-Scaling Configuration

The ECS service auto-scales based on `SessionsPerTask` (average active voice calls per container).

| Parameter | Default | CDK Context | Env Var | Description |
|-----------|---------|-------------|---------|-------------|
| `minCapacity` | `1` | `-c voice-agent:minCapacity=2` | `MIN_CAPACITY` | Minimum ECS tasks (always running) |
| `maxCapacity` | `12` | `-c voice-agent:maxCapacity=20` | `MAX_CAPACITY` | Maximum ECS tasks |
| `targetSessionsPerTask` | `3` | `-c voice-agent:targetSessionsPerTask=2` | `TARGET_SESSIONS_PER_TASK` | Target tracking metric (validated 1-10) |
| `sessionCapacityPerTask` | `10` | `-c voice-agent:sessionCapacityPerTask=8` | `SESSION_CAPACITY_PER_TASK` | Per-container capacity limit (validated 1-50) |

Example:
```bash
npx cdk deploy VoiceAgentEcs \
  -c voice-agent:minCapacity=2 \
  -c voice-agent:maxCapacity=20 \
  -c voice-agent:targetSessionsPerTask=2
```

Each container supports up to `sessionCapacityPerTask` concurrent voice calls. When at capacity, the `/ready` endpoint returns 503, causing the NLB to stop routing new calls to that container.

### Scaling Policies

| Policy | Trigger | Action |
|--------|---------|--------|
| **Target Tracking** | `SessionsPerTask` deviates from target | Proportional scale-out (scale-in disabled) |
| **Burst Step Scaling** | >target+0.5 sessions/task | +10 tasks; >target+1.0 adds +25 tasks |
| **Scale-In Step Scaling** | <1.0 sessions/task for 3 periods | Remove 2 tasks (300s cooldown) |

Tasks with active voice calls are protected from termination via ECS Task Scale-in Protection.

---

## Updating the Deployment

### Update Infrastructure

```bash
cd infrastructure
./deploy.sh diff    # Preview changes
./deploy.sh deploy  # Apply changes
```

### Update Voice Agent Container

The container is built from `backend/voice-agent/Dockerfile` as a CDK Docker image asset. To update:

```bash
cd infrastructure
npx cdk deploy VoiceAgentEcs
```

CDK automatically rebuilds and pushes the container image when source files change, then triggers an ECS service update.

---

## Cleanup

### Release Daily.co Phone Numbers

Release your phone numbers **before** destroying infrastructure (the Daily API key is stored in Secrets Manager which CDK will delete):

```bash
# Load your Daily API key
source ../backend/voice-agent/.env

# List purchased numbers
AUTH_HEADER="Authorization: Bearer $DAILY_API_KEY"
curl -s -H "$AUTH_HEADER" 'https://api.daily.co/v1/purchased-phone-numbers' | python3 -c "
import json, sys
data = json.load(sys.stdin)
for n in data.get('data', []):
    print(f'{n[\"number\"]} (id: {n[\"id\"]})')
"

# Release a phone number (replace PHONE_ID with the id from above)
curl -s -X DELETE -H "$AUTH_HEADER" "https://api.daily.co/v1/release-phone-number/PHONE_ID"

# Clear the webhook configuration
curl -s -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
  -d '{"properties": {"pinless_dialin": []}}' 'https://api.daily.co/v1/'
```

> **Note:** A phone number cannot be released within 14 days of purchase. If you get an error, release it manually at [dashboard.daily.co](https://dashboard.daily.co) after the hold period.

### Destroy AWS Resources

```bash
cd infrastructure
./deploy.sh destroy
```

> **Warning**: This deletes all AWS resources including VPC, SageMaker endpoints, secrets, and data. You are responsible for all charges incurred while resources are running.

---

## Deployed Resources

| Component | Cloud API Mode | SageMaker Mode |
|-----------|---------------|----------------|
| VPC + NAT Gateway (2 AZs) | Yes | Yes |
| ECS Fargate (2 vCPU / 4 GB) | Yes | Yes |
| Network Load Balancer | Yes | Yes |
| Secrets Manager + KMS | Yes | Yes |
| Lambda + API Gateway | Yes | Yes |
| CloudWatch Dashboard + Alarms | Yes | Yes |
| SageMaker STT (ml.g6.2xlarge) | No | Yes |
| SageMaker TTS (ml.g6.12xlarge) | No | Yes |
| Bedrock Claude Haiku | Yes (pay-per-use) | Yes (pay-per-use) |
| Daily.co | Yes (third-party) | Yes (third-party) |
| Deepgram Cloud STT | Yes (third-party) | No (self-hosted) |
| Cartesia Cloud TTS | Yes (third-party, default) | No (self-hosted) |
| ElevenLabs Cloud TTS | Yes (third-party, alternative) | No (self-hosted) |

**Cloud API mode** does not deploy SageMaker endpoints but routes audio through the public internet. **SageMaker mode** keeps all audio within your VPC.

> **You are responsible for all AWS and third-party service charges incurred by deploying and running this project.**
