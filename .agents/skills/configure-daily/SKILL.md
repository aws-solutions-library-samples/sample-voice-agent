---
name: configure-daily
description: Sets up Daily.co phone number and webhook for PSTN dial-in. Guides through API key verification, phone number purchase, pinless dial-in configuration, and secrets sync. Use after deploying infrastructure, when setting up a phone number, or when configuring dial-in.
---

# Configure Daily.co — Get a Phone Number

You are guiding the user through setting up Daily.co so they can call their voice agent from a real phone. This is the step that makes the deployment actually usable.

## When This Skill Activates
- User says "set up Daily", "configure phone number", "I want to call the agent"
- User completed deployment and needs a phone number
- User asks about PSTN, dial-in, or telephony

## Daily API Reference

All Daily REST API calls require HTTPS and an `Authorization: Bearer <key>` header. The base URL is `https://api.daily.co/v1`.

**Endpoints used by this skill:**

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/` | Verify API key (returns domain info) |
| GET | `/purchased-phone-numbers` | List existing phone numbers |
| GET | `/list-available-numbers?region=XX` | Find numbers to buy by US state |
| POST | `/buy-phone-number` | Purchase a number |
| POST | `/` | Set domain config (pinless dial-in) |

**Important curl guidelines:**

Shell variable interpolation inside `--header` values in multi-line curl commands is fragile. To avoid `authorization-header-error` responses from Daily:

1. **Always assign the API key to a variable first**, then reference it:
   ```bash
   AUTH_HEADER="Authorization: Bearer $DAILY_API_KEY"
   ```

2. **Use the `-H` short flag with the variable in double quotes:**
   ```bash
   curl -s -H "$AUTH_HEADER" 'https://api.daily.co/v1/purchased-phone-numbers'
   ```

3. **For POST requests with JSON bodies**, build the JSON payload separately to avoid nested quoting issues:
   ```bash
   JSON_BODY=$(python3 -c "import json; print(json.dumps({\"key\": \"$VALUE\"}))")
   curl -s -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$JSON_BODY" 'https://api.daily.co/v1/endpoint'
   ```

4. **Always single-quote URLs** to prevent glob or variable expansion.

## What To Do

### Phase 1: Check Prerequisites

1. **Verify BotRunner is deployed:**
   ```bash
   WEBHOOK_URL=$(aws ssm get-parameter --name "/voice-agent/botrunner/webhook-url" --query 'Parameter.Value' --output text 2>/dev/null)
   ```
   If fails: BotRunner stack must be deployed first.

2. **Check for existing Daily API key** in `backend/voice-agent/.env`.

### Phase 2: Daily.co Setup

Confirm the user has:
- A Daily.co account at [dashboard.daily.co](https://dashboard.daily.co)
- A credit card on file (required for phone numbers)
- An API key from Developers -> API Keys

Set up the auth header variable and verify the key:
```bash
AUTH_HEADER="Authorization: Bearer $DAILY_API_KEY"
curl -s -w "\n%{http_code}" -H "$AUTH_HEADER" 'https://api.daily.co/v1' | tail -1
```

200 = key is valid. 400 = `Authorization` header missing/malformed. 401 = key is invalid.

### Phase 3: Get a Phone Number

#### Step 1: Check for existing purchased numbers

Before buying a new number, check if the account already has one that can be reused:

```bash
curl -s -H "$AUTH_HEADER" 'https://api.daily.co/v1/purchased-phone-numbers' | python3 -c "
import json, sys
data = json.load(sys.stdin)
numbers = data.get('data', [])
if not numbers:
    print('No existing phone numbers found.')
else:
    print(f'Found {len(numbers)} existing number(s):')
    for n in numbers:
        print(f'  {n[\"number\"]} (id: {n[\"id\"]})')
"
```

- **If numbers exist:** Present them to the user and ask: "You already have a phone number on your Daily account. Would you like to use **[number]** for this deployment, or purchase a new one?"
  - If they choose an existing number, set `PHONE_NUMBER` to that number and **skip to Phase 4** (Configure Webhook).
  - If they want a new number, continue to Step 2.
- **If no numbers exist:** Continue to Step 2.

#### Step 2: Purchase a new number

Ask the user for a preferred US state abbreviation (CA, NY, TX, etc.).

```bash
curl -s -H "$AUTH_HEADER" "https://api.daily.co/v1/list-available-numbers?region=$PHONE_REGION" | python3 -c "
import json, sys
data = json.load(sys.stdin)
numbers = data.get('data', [])[:5]
print(f'Found {data.get(\"total_count\", 0)} available numbers:')
for n in numbers: print(f'  {n[\"number\"]}')
"
```

**Get confirmation before purchasing.**

> **14-day hold:** A purchased phone number **cannot be released within 14 days** of purchase. Calling `DELETE /release-phone-number/:id` before this period expires returns an error. Make sure the user understands this before confirming.

```bash
JSON_BODY=$(python3 -c "import json; print(json.dumps({'number': '$PHONE_NUMBER'}))")
curl -s -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$JSON_BODY" 'https://api.daily.co/v1/buy-phone-number'
```

### Phase 4: Configure Webhook

Configure pinless dial-in to route calls to the voice agent:

```bash
WEBHOOK_URL=$(aws ssm get-parameter --name "/voice-agent/botrunner/webhook-url" --query 'Parameter.Value' --output text)

JSON_BODY=$(python3 -c "
import json
print(json.dumps({
    'properties': {
        'pinless_dialin': [{
            'phone_number': '$PHONE_NUMBER',
            'room_creation_api': '$WEBHOOK_URL'
        }]
    }
}))
")

CONFIG=$(curl -s -H "$AUTH_HEADER" -H 'Content-Type: application/json' -d "$JSON_BODY" 'https://api.daily.co/v1/')
```

The call flow: Caller dials number -> Daily holds caller -> Webhook fires -> Lambda creates room -> ECS bot joins -> Caller connected.

### Phase 5: Save and Sync

Extract HMAC secret from the config response and save to `backend/voice-agent/.env`. Then sync to AWS:

```bash
cd infrastructure && ./scripts/init-secrets.sh
```

### Phase 6: Verify

Test the webhook:
```bash
curl -s -o /dev/null -w "%{http_code}" -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -d '{"type": "dialin.connected", "callId": "test-verify", "callDomain": "test.daily.co"}'
```

200 or 400 = webhook is responding (400 is expected for test data).

### Phase 7: Report

Show results:
```
Daily.co Setup Complete:
✅ Phone number: [number]
✅ Webhook: [url]
✅ Secrets synced

Call [number] to talk to the voice agent.
```

**Note:** It can take a few minutes for Daily.co to fully provision the phone number and activate dial-in routing. If the first call doesn't connect, wait 2-3 minutes and try again.

### Phase 8: What's Running and Next Steps

Explain what the voice agent can do right now:

```
What's deployed now:
━━━━━━━━━━━━━━━━━━━
Your voice agent is running with these tools:
  • get_current_time — Ask "What time is it?"
  • hangup_call      — Say "Goodbye" or ask to hang up

Try it: Call [number] and ask "What time is it?" or have a conversation
and then ask the agent to end the call.
```

Then point to next steps:

> **Want more capabilities?** Use `/deploy-capability-agents` to add a Knowledge Base (RAG search) and/or CRM (customer lookup) to your voice agent. These run as separate containers and are discovered automatically — no pipeline changes needed.

Remind user to use the **destroy-project** skill to tear down all resources when done.
