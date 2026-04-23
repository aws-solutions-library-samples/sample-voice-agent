#!/bin/bash
# setup-daily.sh - Configure Daily.co for Voice Agent
#
# This script automates the Daily.co setup process:
# 1. Loads DAILY_API_KEY from .env
# 2. Fetches webhook URL from SSM (deployed infrastructure)
# 3. Lists available phone numbers in your region
# 4. Purchases the first available number
# 5. Configures pinless dial-in with your webhook URL
# 6. Saves DAILY_PHONE_NUMBER and DAILY_HMAC_SECRET to .env
#
# Prerequisites:
#   - DAILY_API_KEY in backend/voice-agent/.env
#   - BotRunner stack deployed (for webhook URL)
#   - jq installed for JSON parsing
#
# Usage:
#   ./setup-daily.sh
#   PHONE_REGION=NY ./setup-daily.sh  # Use different region

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
PHONE_REGION="${PHONE_REGION:-CA}"
AWS_PROFILE=${AWS_PROFILE:-default}

# Export AWS profile for CLI commands
export AWS_PROFILE
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../backend/voice-agent/.env"

echo -e "${CYAN}======================================"
echo "Daily.co Phone Number Setup"
echo -e "======================================${NC}"
echo ""

# Check for jq
if ! command -v jq &> /dev/null; then
    echo -e "${RED}Error: jq is required but not installed.${NC}"
    echo "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
    exit 1
fi

# Load DAILY_API_KEY from .env
if [ -f "$ENV_FILE" ]; then
    echo -e "Loading configuration from ${CYAN}$ENV_FILE${NC}"
    set -a
    source "$ENV_FILE"
    set +a
else
    echo -e "${RED}Error: .env file not found at $ENV_FILE${NC}"
    echo "Create it first with your DAILY_API_KEY"
    exit 1
fi

# Validate DAILY_API_KEY
if [ -z "$DAILY_API_KEY" ]; then
    echo -e "${RED}Error: DAILY_API_KEY not found in .env${NC}"
    echo "Add DAILY_API_KEY=your-key to $ENV_FILE"
    exit 1
fi
echo -e "DAILY_API_KEY: ${GREEN}[loaded from .env]${NC}"

# Fetch webhook URL from SSM
echo ""
echo "Fetching webhook URL from SSM..."
WEBHOOK_URL=$(aws ssm get-parameter \
    --name "/${PROJECT_NAME}/botrunner/webhook-url" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "")

if [ -z "$WEBHOOK_URL" ] || [ "$WEBHOOK_URL" == "None" ]; then
    echo -e "${RED}Error: Webhook URL not found in SSM${NC}"
    echo "Make sure the BotRunner stack is deployed first:"
    echo "  ./deploy.sh deploy-stack VoiceAgentBotRunner"
    exit 1
fi
echo -e "Webhook URL: ${GREEN}$WEBHOOK_URL${NC}"
echo ""

# Step 1: List available numbers
echo -e "${CYAN}Step 1: Finding available phone numbers in $PHONE_REGION...${NC}"
NUMBERS=$(curl -s --request GET \
  --url "https://api.daily.co/v1/list-available-numbers?region=$PHONE_REGION" \
  --header "Authorization: Bearer $DAILY_API_KEY")

# Check for errors
if echo "$NUMBERS" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${RED}Error listing numbers: $(echo $NUMBERS | jq -r '.error')${NC}"
    exit 1
fi

FIRST_NUMBER=$(echo "$NUMBERS" | jq -r '.data[0].number')

if [ "$FIRST_NUMBER" == "null" ] || [ -z "$FIRST_NUMBER" ]; then
    echo -e "${YELLOW}No phone numbers available in region $PHONE_REGION${NC}"
    echo "Try a different region: PHONE_REGION=NY ./setup-daily.sh"
    exit 1
fi

echo -e "Found: ${GREEN}$FIRST_NUMBER${NC}"
echo ""

# Confirm purchase
echo -e "${YELLOW}This will purchase the phone number $FIRST_NUMBER${NC}"
echo -n "Continue? [y/N] "
read -r CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi
echo ""

# Step 2: Buy the number
echo -e "${CYAN}Step 2: Purchasing $FIRST_NUMBER...${NC}"
PURCHASE=$(curl -s --request POST \
  --url 'https://api.daily.co/v1/buy-phone-number' \
  --header "Authorization: Bearer $DAILY_API_KEY" \
  --header 'Content-Type: application/json' \
  --data "{\"number\": \"$FIRST_NUMBER\"}")

# Check for errors
if echo "$PURCHASE" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${RED}Error purchasing number: $(echo $PURCHASE | jq -r '.error')${NC}"
    exit 1
fi

PHONE_ID=$(echo "$PURCHASE" | jq -r '.id')
echo -e "Purchased! ID: ${GREEN}$PHONE_ID${NC}"
echo ""

# Step 3: Configure pinless dialin
echo -e "${CYAN}Step 3: Configuring webhook...${NC}"
CONFIG=$(curl -s --request POST \
  --url 'https://api.daily.co/v1' \
  --header "Authorization: Bearer $DAILY_API_KEY" \
  --header 'Content-Type: application/json' \
  --data "{
    \"properties\": {
      \"pinless_dialin\": [{
        \"phone_number\": \"$FIRST_NUMBER\",
        \"room_creation_api\": \"$WEBHOOK_URL\"
      }]
    }
  }")

# Check for errors
if echo "$CONFIG" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${RED}Error configuring webhook: $(echo $CONFIG | jq -r '.error')${NC}"
    exit 1
fi

# Daily's response shape for POST /v1 is {domain_name, domain_id, config: {...}}
# — NOT {properties: {...}} (the request body uses `properties` but the response
# uses `config`). Previous versions of this script read .properties.* and got
# null, which got saved to .env as the literal string "null" and broke HMAC
# verification downstream.
HMAC=$(echo "$CONFIG" | jq -r '.config.pinless_dialin[0].hmac // empty')

if [ -z "$HMAC" ] || [ "$HMAC" = "null" ]; then
    echo -e "${RED}Error: Daily did not return an HMAC secret in the response.${NC}"
    echo "Response was:"
    echo "$CONFIG" | jq '.' 2>/dev/null || echo "$CONFIG"
    echo ""
    echo -e "${YELLOW}This breaks webhook signature verification. Aborting so we don't"
    echo -e "persist a bogus secret. If Daily's response shape has changed, update"
    echo -e "the jq path in setup-daily.sh.${NC}"
    exit 1
fi

echo -e "Webhook configured with HMAC verification"
echo ""

# Step 4: Save to .env
echo -e "${CYAN}Step 4: Saving configuration to .env...${NC}"

# Remove old values if they exist
sed -i.bak '/^DAILY_PHONE_NUMBER=/d' "$ENV_FILE" 2>/dev/null || true
sed -i.bak '/^DAILY_HMAC_SECRET=/d' "$ENV_FILE" 2>/dev/null || true
rm -f "${ENV_FILE}.bak"

# Append new values
echo "" >> "$ENV_FILE"
echo "# Daily.co Phone Configuration (added by setup-daily.sh)" >> "$ENV_FILE"
echo "DAILY_PHONE_NUMBER=$FIRST_NUMBER" >> "$ENV_FILE"
echo "DAILY_HMAC_SECRET=$HMAC" >> "$ENV_FILE"

echo -e "${GREEN}Saved to $ENV_FILE${NC}"
echo ""

echo -e "${GREEN}======================================"
echo "Setup Complete"
echo -e "======================================${NC}"
echo ""
echo "Configuration:"
echo -e "  Phone Number:  ${CYAN}$FIRST_NUMBER${NC}"
echo -e "  Webhook URL:   ${CYAN}$WEBHOOK_URL${NC}"
echo -e "  HMAC Secret:   ${CYAN}[saved to .env]${NC}"
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/init-secrets.sh to sync secrets to AWS"
echo "  2. Test by calling the phone number"
echo ""
