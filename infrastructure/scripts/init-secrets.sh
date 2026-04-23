#!/bin/bash
# Initialize secrets for Voice Agent POC
# This script helps configure API keys in Secrets Manager
#
# Supports:
#   - DEEPGRAM_API_KEY: For STT (Speech-to-Text)
#   - ELEVENLABS_API_KEY: For TTS (Text-to-Speech)
#   - DAILY_API_KEY: For Daily.co WebRTC transport

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Configuration
AWS_REGION=${AWS_REGION:-us-east-1}
PROJECT_NAME="voice-agent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../backend/voice-agent/.env"

echo -e "${CYAN}======================================"
echo "Voice Agent Secrets Initialization"
echo -e "======================================${NC}"
echo ""

# Get secret ARN from SSM
echo "Fetching secret ARN from SSM..."
SECRET_ARN=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/storage/api-key-secret-arn" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)

if [ -z "$SECRET_ARN" ] || [ "$SECRET_ARN" == "None" ]; then
    echo -e "${RED}Error: Secret ARN not found in SSM${NC}"
    echo "Make sure the Storage stack is deployed first:"
    echo "  ./deploy.sh deploy-stack VoiceAgentStorage"
    exit 1
fi

echo -e "Secret ARN: ${CYAN}$SECRET_ARN${NC}"
echo ""

# Try to load from .env file if it exists
if [ -f "$ENV_FILE" ]; then
    echo -e "${GREEN}Found .env file at: $ENV_FILE${NC}"
    echo "Loading API keys from .env..."

    # Source the .env file to get variables
    set -a
    source "$ENV_FILE"
    set +a
    echo ""
fi

# validate_hmac_b64 <value> — returns 0 if the string is a plausible base64
# HMAC secret (non-empty, non-"null", decodes cleanly, decodes to >= 16 bytes).
# Returns 1 otherwise. Keeps us from persisting the literal string "null" that
# setup-daily.sh used to emit when its jq path was wrong (docs/lambda-patches).
validate_hmac_b64() {
    local value="$1"
    if [ -z "$value" ] || [ "$value" = "null" ] || [ "$value" = "None" ]; then
        return 1
    fi
    if ! printf '%s' "$value" | base64 -d >/dev/null 2>&1; then
        return 1
    fi
    local decoded_len
    decoded_len=$(printf '%s' "$value" | base64 -d 2>/dev/null | wc -c | tr -d ' ')
    [ "$decoded_len" -ge 16 ]
}

# Check if running interactively and keys not set
if [ -t 0 ]; then
    # Interactive mode - prompt for missing values
    echo -e "${YELLOW}Enter your API keys (press Enter to keep existing value):${NC}"
    echo ""

    # Deepgram API Key (STT)
    if [ -n "$DEEPGRAM_API_KEY" ]; then
        echo -e "Deepgram API Key: ${GREEN}[loaded from .env]${NC}"
    else
        echo -n "Deepgram API Key: "
        read -s DEEPGRAM_API_KEY
        echo ""
    fi

    # ElevenLabs API Key (TTS)
    if [ -n "$ELEVENLABS_API_KEY" ]; then
        echo -e "ElevenLabs API Key: ${GREEN}[loaded from .env]${NC}"
    else
        echo -n "ElevenLabs API Key: "
        read -s ELEVENLABS_API_KEY
        echo ""
    fi

    # Daily API Key (optional)
    if [ -n "$DAILY_API_KEY" ]; then
        echo -e "Daily API Key: ${GREEN}[loaded from .env]${NC}"
    else
        echo -n "Daily API Key (optional, press Enter to skip): "
        read -s DAILY_API_KEY
        echo ""
    fi

    # Daily HMAC secret (required for webhook verification — the Lambda
    # rejects requests whose signature doesn't match this value).
    if [ -n "$DAILY_HMAC_SECRET" ] && [ "$DAILY_HMAC_SECRET" != "null" ]; then
        echo -e "Daily HMAC Secret: ${GREEN}[loaded from .env]${NC}"
    else
        echo -n "Daily HMAC Secret (base64, from setup-daily.sh output): "
        read -s DAILY_HMAC_SECRET
        echo ""
    fi
    echo ""
fi

# Validate required keys
if [ -z "$DEEPGRAM_API_KEY" ] && [ -z "$ELEVENLABS_API_KEY" ]; then
    echo -e "${RED}Error: At least DEEPGRAM_API_KEY or ELEVENLABS_API_KEY must be set${NC}"
    echo "Set them in backend/voice-agent/.env or as environment variables"
    exit 1
fi

# Validate the HMAC secret if present. We don't hard-fail when it's missing
# (the caller may be running this before setup-daily.sh has completed) but we
# refuse to persist garbage — the Lambda's fail-closed policy would reject
# every webhook and the only way to diagnose is to log into Secrets Manager.
if [ -n "$DAILY_HMAC_SECRET" ]; then
    if ! validate_hmac_b64 "$DAILY_HMAC_SECRET"; then
        echo -e "${RED}Error: DAILY_HMAC_SECRET is not a valid base64-encoded secret.${NC}"
        echo "  Current value starts with: '${DAILY_HMAC_SECRET:0:8}...'"
        echo "  Expected: base64 string decoding to 16+ bytes."
        echo ""
        echo "  The old setup-daily.sh saved the literal string 'null' here due to"
        echo "  a jq path bug. Re-run ./setup-daily.sh with a recent checkout, or"
        echo "  paste the hmac value directly from Daily's API response."
        exit 1
    fi
    echo -e "DAILY_HMAC_SECRET: ${GREEN}[validated, $(printf '%s' "$DAILY_HMAC_SECRET" | base64 -d 2>/dev/null | wc -c | tr -d ' ') bytes decoded]${NC}"
else
    echo -e "${YELLOW}Warning: DAILY_HMAC_SECRET not set. Webhook signature verification${NC}"
    echo -e "${YELLOW}         will reject every request until this is populated.${NC}"
    echo -e "${YELLOW}         Run ./setup-daily.sh first, then re-run this script.${NC}"
fi

# Build secret value JSON. Keep the HMAC out of the JSON entirely when it's
# not set — that way Secrets Manager never sees the key at all, and the
# Lambda's load_hmac_secret() helper treats it as "not configured" rather
# than "configured to empty string".
if [ -n "$DAILY_HMAC_SECRET" ]; then
SECRET_VALUE=$(cat <<EOF
{
  "DEEPGRAM_API_KEY": "${DEEPGRAM_API_KEY:-}",
  "ELEVENLABS_API_KEY": "${ELEVENLABS_API_KEY:-}",
  "DAILY_API_KEY": "${DAILY_API_KEY:-}",
  "DAILY_HMAC_SECRET": "${DAILY_HMAC_SECRET}"
}
EOF
)
else
SECRET_VALUE=$(cat <<EOF
{
  "DEEPGRAM_API_KEY": "${DEEPGRAM_API_KEY:-}",
  "ELEVENLABS_API_KEY": "${ELEVENLABS_API_KEY:-}",
  "DAILY_API_KEY": "${DAILY_API_KEY:-}"
}
EOF
)
fi

# Update secret
echo "Updating secret in Secrets Manager..."

if aws secretsmanager put-secret-value \
    --secret-id "$SECRET_ARN" \
    --secret-string "$SECRET_VALUE" \
    --region "$AWS_REGION" > /dev/null 2>&1; then

    echo -e "${GREEN}✓ Secrets updated successfully${NC}"
    echo ""
    echo "Configured secrets:"
    echo "  - DEEPGRAM_API_KEY (STT):        $([ -n "$DEEPGRAM_API_KEY" ] && echo "✓ Set" || echo "- Not set")"
    echo "  - ELEVENLABS_API_KEY (TTS):      $([ -n "$ELEVENLABS_API_KEY" ] && echo "✓ Set" || echo "- Not set")"
    echo "  - DAILY_API_KEY (WebRTC):        $([ -n "$DAILY_API_KEY" ] && echo "✓ Set" || echo "- Not set (optional)")"
    echo "  - DAILY_HMAC_SECRET (webhook):   $([ -n "$DAILY_HMAC_SECRET" ] && echo "✓ Set (validated)" || echo "- Not set — webhook verification disabled")"
else
    echo -e "${RED}✗ Failed to update secrets${NC}"
    echo "Check your AWS credentials and permissions"
    exit 1
fi

echo ""
echo -e "${GREEN}======================================"
echo "Secrets Configuration Complete"
echo -e "======================================${NC}"
echo ""
echo "The ECS service will load these secrets automatically on startup."
echo ""
echo "Webhook endpoint:"
WEBHOOK_URL=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/botrunner/webhook-url" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "<deploy BotRunner stack first>")
echo "   $WEBHOOK_URL"
echo ""
echo "To verify deployment:"
echo "   ./scripts/test-webhook.sh"
