---
name: destroy-project
description: Tears down all AWS infrastructure and releases Daily.co phone numbers. Guides through Daily phone number release, pinless dial-in cleanup, CDK stack destruction, and output file cleanup. Use when done experimenting, cleaning up resources, or shutting down the deployment.
---

# Destroy Project — Full Teardown

You are guiding the user through a complete teardown of the voice agent deployment. This includes releasing Daily.co phone numbers (which CDK cannot manage) and destroying all AWS CloudFormation stacks.

## When This Skill Activates
- User says "destroy", "tear down", "clean up", "delete everything", "remove deployment"
- User asks about stopping charges or cleaning up resources
- User is done experimenting and wants to shut down

## What To Do

### Phase 1: Confirm Intent

**This is destructive and irreversible.** Make sure the user understands:
- All AWS resources (VPC, ECS, Lambda, SageMaker endpoints, Secrets Manager, DynamoDB tables) will be deleted
- Daily.co phone numbers will be released and cannot be recovered
- The `.env` files with secrets will remain on disk but the AWS-side secrets will be gone

Ask: "This will destroy all AWS resources and release your Daily.co phone number. Are you sure you want to proceed?"

### Phase 2: Release Daily.co Phone Number

This step **must happen before** CDK destroy, because the Daily API key is stored in Secrets Manager which CDK will delete.

1. **Load Daily API key and phone number from `.env`:**
   ```bash
   ENV_FILE="backend/voice-agent/.env"
   if [ -f "$ENV_FILE" ]; then
     set -a && source "$ENV_FILE" && set +a
   fi
   ```

2. **Check if a Daily API key exists:**
   - If `DAILY_API_KEY` is empty: warn that you cannot release phone numbers automatically. Tell user to release manually at [dashboard.daily.co](https://dashboard.daily.co) under Phone Numbers, then skip to Phase 3.

3. **List purchased phone numbers:**
   ```bash
   AUTH_HEADER="Authorization: Bearer $DAILY_API_KEY"
   curl -s -H "$AUTH_HEADER" 'https://api.daily.co/v1/purchased-phone-numbers' | python3 -c "
   import json, sys
   data = json.load(sys.stdin)
   numbers = data.get('data', [])
   if not numbers:
       print('No purchased phone numbers found.')
   else:
       print(f'Found {len(numbers)} purchased number(s):')
       for n in numbers:
           print(f'  {n[\"number\"]} (id: {n[\"id\"]})')
   "
   ```

4. **Show the numbers and get confirmation before releasing.** Warn: "A number cannot be released within 14 days of purchase. If you purchased recently, you may get an error."

5. **Release each phone number:**
   ```bash
   curl -s -X DELETE -H "$AUTH_HEADER" "https://api.daily.co/v1/release-phone-number/$PHONE_ID"
   ```
   Check for errors in the response. A 14-day restriction error is non-fatal -- inform the user the number will need to be released later from the Daily dashboard.

6. **Remove pinless dial-in configuration from the domain:**
   ```bash
   curl -s -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d '{"properties": {"pinless_dialin": []}}' 'https://api.daily.co/v1/'
   ```
   This clears the webhook configuration so Daily stops sending requests to the now-deleted API Gateway.

### Phase 3: Destroy AWS Infrastructure

```bash
cd infrastructure && ./deploy.sh destroy
```

This runs `npx cdk destroy --all --force` and removes all 10 CDK stacks:
- VoiceAgentNetwork (VPC, NAT, security groups)
- VoiceAgentStorage (Secrets Manager, KMS)
- VoiceAgentSageMaker (GPU endpoints, if deployed)
- VoiceAgentKnowledgeBase (Bedrock KB, S3 bucket)
- VoiceAgentEcs (Fargate cluster, NLB, CloudWatch dashboard)
- VoiceAgentBotRunner (Lambda, API Gateway)
- VoiceAgentCRM (DynamoDB tables)
- VoiceAgentKbAgent (KB capability agent)
- VoiceAgentCrmAgent (CRM capability agent)

Wait for the destroy to complete. This typically takes 5-10 minutes. SageMaker endpoints may take longer.

If destroy fails partway through:
- **"Stack is in DELETE_FAILED state"** -- Some resources may have deletion protection or dependencies. Re-run `npx cdk destroy --all --force` or delete the stuck stack manually in CloudFormation console.
- **"Role does not exist"** -- The IAM role was already deleted. This usually resolves on retry.

### Phase 4: Clean Up Local Files

Remove deployment output files:
```bash
cd infrastructure
rm -f outputs.json outputs-*.json cdk.out
```

Optionally remind the user:
- The `.env` files in `backend/voice-agent/` and `infrastructure/` still contain API keys on disk. These are gitignored but the user may want to clear them.
- The CDK bootstrap stack (`CDKToolkit`) is NOT deleted by `cdk destroy --all`. It can be reused for future deployments or removed manually via CloudFormation if the account is being decommissioned.

### Phase 5: Report

Show final status:

```
Teardown Complete:
[check/x] Daily phone number(s) released
[check/x] Pinless dial-in webhook cleared
[check/x] AWS infrastructure destroyed (all CDK stacks)
[check/x] Local output files cleaned up

Remaining items (manual):
- CDK bootstrap stack (CDKToolkit) still exists in the account
- Local .env files still contain API keys (gitignored)
- Daily.co account still active (no ongoing charges without active resources)
```

If any phone numbers could not be released due to the 14-day restriction, include a reminder:
```
Action required:
- Phone number [number] cannot be released until [date].
  Release it manually at https://dashboard.daily.co or re-run this
  cleanup after the 14-day hold period.
```
