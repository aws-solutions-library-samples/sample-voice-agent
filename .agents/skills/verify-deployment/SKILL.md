---
name: verify-deployment
description: Runs post-deployment health checks against all voice agent infrastructure. Tests SSM parameters, ECS service, Secrets Manager, webhook endpoint, and SageMaker endpoints. Use after deploying, when troubleshooting issues, or to confirm everything is working.
---

# Verify — Is Everything Working?

You are running a comprehensive health check against the deployed voice agent infrastructure. Your job is to test every component, explain results clearly, and help the user fix any issues.

## When This Skill Activates
- User says "verify", "check deployment", "is everything working?"
- User just finished deploying and wants to confirm
- User is having issues and wants to diagnose
- User starts a new session and has an existing deployment

## What To Do

### Phase 1: Discover Deployment State

"Let me check your deployment. I'll test each component and report what I find."

Run all checks and collect results before presenting them:

1. **SSM Parameters** (proves stacks deployed):
   ```bash
   for param in \
     "/voice-agent/network/vpc-id" \
     "/voice-agent/storage/api-key-secret-arn" \
     "/voice-agent/sagemaker/stt-endpoint-name" \
     "/voice-agent/sagemaker/tts-endpoint-name" \
     "/voice-agent/ecs/cluster-arn" \
     "/voice-agent/ecs/service-endpoint" \
     "/voice-agent/botrunner/webhook-url"; do
     VALUE=$(aws ssm get-parameter --name "$param" --query 'Parameter.Value' --output text 2>/dev/null)
     if [ $? -eq 0 ]; then
       echo "OK|$param|${VALUE:0:60}"
     else
       echo "MISSING|$param|"
     fi
   done
   ```

2. **ECS Service health:**
   ```bash
   CLUSTER=$(aws ssm get-parameter --name "/voice-agent/ecs/cluster-arn" --query 'Parameter.Value' --output text 2>/dev/null)
   if [ -n "$CLUSTER" ]; then
     SERVICE_ARN=$(aws ecs list-services --cluster "$CLUSTER" --query 'serviceArns[0]' --output text 2>/dev/null)
     aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE_ARN" \
       --query 'services[0].{Status:status,Running:runningCount,Desired:desiredCount,Pending:pendingCount}' --output json 2>/dev/null
   fi
   ```

3. **Secrets populated:**
   ```bash
   SECRET_ARN=$(aws ssm get-parameter --name "/voice-agent/storage/api-key-secret-arn" --query 'Parameter.Value' --output text 2>/dev/null)
   if [ -n "$SECRET_ARN" ]; then
     aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query 'SecretString' --output text 2>/dev/null | python3 -c "
   import json, sys
   try:
     secret = json.loads(sys.stdin.read())
     for key in ['DAILY_API_KEY', 'DEEPGRAM_API_KEY', 'CARTESIA_API_KEY']:
       val = secret.get(key, '')
       if not val or 'PLACEHOLDER' in val:
         print(f'MISSING|{key}')
       else:
         print(f'OK|{key}|{len(val)} chars')
   except: print('ERROR|Could not parse secrets')
   "
   fi
   ```

4. **Webhook endpoint:**
   ```bash
   WEBHOOK_URL=$(aws ssm get-parameter --name "/voice-agent/botrunner/webhook-url" --query 'Parameter.Value' --output text 2>/dev/null)
   if [ -n "$WEBHOOK_URL" ]; then
     HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$WEBHOOK_URL" \
       -H "Content-Type: application/json" \
       -d '{"type": "dialin.connected", "callId": "health-check", "callDomain": "test.daily.co"}' 2>/dev/null)
     echo "WEBHOOK|$HTTP_CODE|$WEBHOOK_URL"
   fi
   ```

5. **Deployment mode + SageMaker (if applicable):**
   ```bash
   STT_ENDPOINT=$(aws ssm get-parameter --name "/voice-agent/sagemaker/stt-endpoint-name" --query 'Parameter.Value' --output text 2>/dev/null)
   if [[ "$STT_ENDPOINT" == *"cloud-api-mode"* ]]; then
     echo "MODE|cloud-api"
   else
     echo "MODE|sagemaker"
     aws sagemaker describe-endpoint --endpoint-name "$STT_ENDPOINT" --query 'EndpointStatus' --output text 2>/dev/null
     TTS_ENDPOINT=$(aws ssm get-parameter --name "/voice-agent/sagemaker/tts-endpoint-name" --query 'Parameter.Value' --output text 2>/dev/null)
     aws sagemaker describe-endpoint --endpoint-name "$TTS_ENDPOINT" --query 'EndpointStatus' --output text 2>/dev/null
   fi
   ```

### Phase 2: Present Results

Show a clear, visual summary:

```
Voice Agent Health Check
========================

Infrastructure:
  ✅ VPC and networking — deployed
  ✅ Secrets Manager — deployed
  ✅ ECS Fargate — 1/1 tasks running
  ✅ Bot Runner (Lambda + API Gateway) — deployed

Deployment Mode: Cloud API
  ℹ️  STT: Deepgram cloud API
  ℹ️  TTS: Cartesia cloud API

Secrets:
  ✅ DAILY_API_KEY — configured (42 chars)
  ✅ DEEPGRAM_API_KEY — configured (40 chars)
  ✅ CARTESIA_API_KEY — configured (38 chars)

Webhook:
  ✅ Endpoint responding — HTTP 200
  📍 URL: https://xxxxx.execute-api.us-east-1.amazonaws.com/poc/start

Overall: ✅ All checks passed
```

### Phase 3: Diagnose Failures

For each failure, provide specific, actionable remediation:

**Missing SSM parameters:**
- "The [Stack Name] stack hasn't been deployed yet. Run `cd infrastructure && ./deploy.sh deploy-stack [StackName]`."

**ECS tasks not running:**
- "The voice agent container isn't running. Check the ECS console for task failure reasons. Common causes:"
  - "Container failed to pull from ECR — check IAM permissions"
  - "Container crashed on startup — check CloudWatch logs"
  - "Insufficient memory — the container needs 4GB minimum"

**Secrets missing:**
- "API keys haven't been configured. Run `cd infrastructure && ./scripts/init-secrets.sh` to set them up."
- If specifically DAILY_API_KEY is missing: "You need a Daily.co API key. Run the configure-daily skill to set up your phone number and API key."

**Webhook not responding:**
- "The Lambda function isn't responding. Check:"
  - "Lambda function exists in the AWS Console"
  - "API Gateway is deployed and has a stage"
  - "Lambda has VPC access to reach the ECS service"

**SageMaker endpoints not InService:**
- "Creating" → "SageMaker endpoints take 10-15 minutes to provision. Wait a few more minutes and check again."
- "Failed" → "Check CloudWatch logs at `/aws/sagemaker/Endpoints/`. Common causes: wrong model package ARN, insufficient quota."

### Phase 4: Suggest Next Steps

Based on results:

- If all passing and no Daily phone: "Everything looks healthy! You just need a phone number. Use `/configure-daily` to set one up."
- If all passing with phone: "You're fully deployed! Call **[phone number]** to talk to your voice agent."
- If failures: "Let's fix these issues one at a time. I recommend starting with [most critical failure]."

**After core verification passes**, check for capability agents:

```bash
# Check if capability registry is enabled
aws ssm get-parameter --name "/voice-agent/config/enable-capability-registry" --query 'Parameter.Value' --output text 2>/dev/null || echo "not-set"
```

Include a capability agent status section in the report:

```
Capability Agents:
  [status] Registry: enabled / disabled
  [status] KB Agent: deployed / not deployed
  [status] CRM Agent: deployed / not deployed
```

To check whether agent stacks are deployed, look for their SSM parameters or CloudFormation stacks:
```bash
aws cloudformation describe-stacks --stack-name VoiceAgentKbAgent --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "not-deployed"
aws cloudformation describe-stacks --stack-name VoiceAgentCrmAgent --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "not-deployed"
```

If agents are not deployed, mention:
> "The Knowledge Base and CRM capability agents are optional. Use `/deploy-capability-agents` to add RAG search or customer lookup to your voice agent."

"You can run this check anytime by asking me to verify the deployment."
