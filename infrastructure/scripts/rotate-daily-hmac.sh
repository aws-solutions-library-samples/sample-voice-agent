#!/bin/bash
#
# Rotate the DAILY_HMAC_SECRET end-to-end.
#
# Use when:
#   - You've changed the webhook URL (new API Gateway stage, new region)
#   - Daily's pinless_dialin config got nuked / reset
#   - You need to rotate the secret for compliance / incident response
#
# What it does (5 steps, all idempotent):
#   1. Read the current DAILY_API_KEY from Secrets Manager
#   2. Read the webhook URL from SSM (/voice-agent/botrunner/webhook-url)
#   3. POST to Daily to configure pinless_dialin for the target phone
#      number, capturing the returned `hmac` field
#   4. MERGE the new HMAC into the existing secret JSON (doesn't clobber
#      DEEPGRAM_API_KEY / ELEVENLABS_API_KEY / DAILY_API_KEY)
#   5. Set DAILY_HMAC_VERIFY=true on the bot-runner Lambda
#
# Why this script exists:
#   The previous interactive setup-daily.sh + init-secrets.sh flow had a
#   jq-path bug that once persisted the literal string "null" as the
#   HMAC secret. That broke every inbound webhook silently (bot-runner
#   fails closed). Those scripts are now fixed, but this command is
#   scriptable end-to-end and non-interactive, so CI/infra engineers
#   don't have to paste values.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="voice-agent"
PHONE_NUMBER="${1:-}"

if [ -z "$PHONE_NUMBER" ]; then
    echo "Usage: $0 <E.164 phone number>"
    echo "Example: $0 +12098075018"
    exit 1
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}=== Rotating Daily HMAC for $PHONE_NUMBER ===${NC}"

# 1. Secret ARN
SECRET_ARN=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/storage/api-key-secret-arn" \
    --region "$AWS_REGION" --query 'Parameter.Value' --output text)
if [ -z "$SECRET_ARN" ] || [ "$SECRET_ARN" = "None" ]; then
    echo -e "${RED}Error: Storage stack not deployed (SSM param missing).${NC}"
    exit 1
fi

# 2. Daily API key from the existing secret
DAILY_API_KEY=$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_ARN" --region "$AWS_REGION" \
    --query SecretString --output text | \
    python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('DAILY_API_KEY',''))")

if [ -z "$DAILY_API_KEY" ]; then
    echo -e "${RED}Error: DAILY_API_KEY missing from Secrets Manager.${NC}"
    echo "Run ./init-secrets.sh first."
    exit 1
fi

# 3. Webhook URL
WEBHOOK_URL=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/botrunner/webhook-url" \
    --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")
if [ -z "$WEBHOOK_URL" ]; then
    echo -e "${RED}Error: BotRunner stack not deployed (SSM webhook-url missing).${NC}"
    exit 1
fi
echo -e "  webhook: ${CYAN}$WEBHOOK_URL${NC}"

# 4. POST pinless_dialin, capture HMAC
echo "  calling Daily..."
RESP=$(curl -sS -X POST 'https://api.daily.co/v1' \
    -H "Authorization: Bearer $DAILY_API_KEY" \
    -H 'Content-Type: application/json' \
    -d "{\"properties\":{\"pinless_dialin\":[{\"phone_number\":\"$PHONE_NUMBER\",\"room_creation_api\":\"$WEBHOOK_URL\"}]}}")

if echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'config' in d else 1)" 2>/dev/null; then
    HMAC=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
pld = d.get('config', {}).get('pinless_dialin') or []
for entry in pld:
    if entry.get('phone_number') == '$PHONE_NUMBER' and entry.get('hmac'):
        print(entry['hmac'])
        break
")
else
    echo -e "${RED}Error: Daily response missing 'config':${NC}"
    echo "$RESP" | python3 -m json.tool 2>&1 | head -20
    exit 1
fi

if [ -z "$HMAC" ] || [ "$HMAC" = "null" ]; then
    echo -e "${RED}Error: Daily did not return an HMAC for $PHONE_NUMBER.${NC}"
    echo "$RESP" | python3 -m json.tool 2>&1 | head -20
    exit 1
fi

# Sanity-check: base64 → ≥16 bytes
DECODED_LEN=$(printf '%s' "$HMAC" | base64 -d 2>/dev/null | wc -c | tr -d ' ' || echo 0)
if [ "$DECODED_LEN" -lt 16 ]; then
    echo -e "${RED}Error: Returned HMAC decoded to only $DECODED_LEN bytes (<16).${NC}"
    exit 1
fi
echo -e "  HMAC: ${GREEN}captured ($DECODED_LEN bytes decoded)${NC}"

# 5. Merge into existing secret JSON without clobbering other keys
aws secretsmanager get-secret-value \
    --secret-id "$SECRET_ARN" --region "$AWS_REGION" \
    --query SecretString --output text | \
    HMAC="$HMAC" python3 -c "
import sys, json, os
d = json.loads(sys.stdin.read())
d['DAILY_HMAC_SECRET'] = os.environ['HMAC']
print(json.dumps(d))" > /tmp/rotate-hmac.json

aws secretsmanager put-secret-value \
    --secret-id "$SECRET_ARN" --region "$AWS_REGION" \
    --secret-string file:///tmp/rotate-hmac.json \
    --query 'VersionId' --output text > /dev/null
rm -f /tmp/rotate-hmac.json
echo -e "  secret updated: ${GREEN}ok${NC}"

# 6. Flip DAILY_HMAC_VERIFY=true on the bot-runner Lambda if it was off.
BOT_RUNNER_NAME=$(aws lambda list-functions --region "$AWS_REGION" \
    --query 'Functions[?starts_with(FunctionName, `VoiceAgentBotRunner-WebhookApiBotRunnerFunction`)].FunctionName' \
    --output text | head -1)
if [ -n "$BOT_RUNNER_NAME" ]; then
    CURRENT=$(aws lambda get-function-configuration \
        --function-name "$BOT_RUNNER_NAME" --region "$AWS_REGION" \
        --query 'Environment.Variables.DAILY_HMAC_VERIFY' --output text)
    if [ "$CURRENT" != "true" ]; then
        aws lambda get-function-configuration \
            --function-name "$BOT_RUNNER_NAME" --region "$AWS_REGION" \
            --query 'Environment.Variables' --output json > /tmp/rotate-env.json
        python3 -c "
import json
env = json.load(open('/tmp/rotate-env.json'))
env['DAILY_HMAC_VERIFY'] = 'true'
json.dump({'Variables': env}, open('/tmp/rotate-env-new.json','w'))"
        aws lambda update-function-configuration \
            --function-name "$BOT_RUNNER_NAME" --region "$AWS_REGION" \
            --environment file:///tmp/rotate-env-new.json \
            --query 'Environment.Variables.DAILY_HMAC_VERIFY' --output text > /dev/null
        rm -f /tmp/rotate-env.json /tmp/rotate-env-new.json
        echo -e "  DAILY_HMAC_VERIFY: ${GREEN}flipped 'false' → 'true' on $BOT_RUNNER_NAME${NC}"
    else
        echo -e "  DAILY_HMAC_VERIFY: already ${GREEN}'true'${NC}"
    fi
fi

echo ""
echo -e "${GREEN}=== HMAC rotation complete ===${NC}"
echo ""
echo "Next inbound call will verify with the new secret. If you see"
echo "'HMAC verification failed' in bot-runner logs, Daily is still"
echo "sending signatures from the previous secret — allow up to 30s"
echo "for their cache to flush, then retry."
