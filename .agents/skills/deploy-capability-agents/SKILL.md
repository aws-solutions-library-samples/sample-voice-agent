---
name: deploy-capability-agents
description: Deploys optional A2A capability agents (Knowledge Base, CRM) that extend the voice agent with new skills. Explains the A2A architecture, enables the capability registry, deploys agent stacks, and verifies discovery. Use after the core deployment and Daily setup are complete.
---

# Deploy Capability Agents

You are guiding the user through extending their voice agent with A2A (Agent-to-Agent) capability agents. This is optional — the voice agent works without them, but they add powerful features.

## When This Skill Activates
- User says "deploy agents", "add KB", "add CRM", "capability agents"
- User finished `/configure-daily` and wants to extend the agent
- User asks about knowledge base, RAG, customer lookup, or A2A

## Prerequisites

The core deployment must be complete:
- `/deploy-cloud-api` or `/deploy-sagemaker` has been run
- `/configure-daily` has been run (phone number active)
- The user has tested the basic agent (time check, hangup)

## What's Running Now

Before deploying anything, set context for the user:

```
Current voice agent capabilities:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • Natural conversation — powered by Claude on Bedrock
  • get_current_time    — "What time is it?"
  • hangup_call         — "Goodbye" / ask to end the call

Tool calling is enabled by default. The agent can hold conversations
and use the two built-in tools above. No other tools are active yet.
```

## How Capability Agents Work

Explain the architecture before deploying:

```
A2A Architecture:
━━━━━━━━━━━━━━━━
                    ┌─────────────────┐
                    │   Voice Agent   │
                    │   (ECS hub)     │
                    └────────┬────────┘
                             │ polls every 30s
                    ┌────────▼────────┐
                    │    CloudMap     │
                    │  (discovery)    │
                    └──┬──────────┬──┘
                       │          │
              ┌────────▼──┐  ┌───▼────────┐
              │  KB Agent  │  │ CRM Agent  │
              │ (ECS spoke)│  │(ECS spoke) │
              └────────────┘  └────────────┘
```

Key points:
- Each capability agent runs as a **separate ECS container** (its own task definition, scaling, and logs)
- Agents register themselves in **CloudMap** under a shared namespace
- The voice agent **polls CloudMap every 30 seconds** for new agents
- When discovered, the agent's skills are **automatically registered as LLM tools** — no pipeline code changes needed
- If an agent goes away, its tools are automatically deregistered after a grace period

## What To Do

### Phase 1: Check Current State

Verify the core deployment is healthy and check whether agents are already deployed:

```bash
# Core health
aws ssm get-parameter --name "/voice-agent/ecs/cluster-arn" --query 'Parameter.Value' --output text 2>/dev/null

# Capability registry status
aws ssm get-parameter --name "/voice-agent/config/enable-capability-registry" --query 'Parameter.Value' --output text 2>/dev/null || echo "not-set (defaults to false)"

# Agent stacks
aws cloudformation describe-stacks --stack-name VoiceAgentKbAgent --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "not-deployed"
aws cloudformation describe-stacks --stack-name VoiceAgentCrmAgent --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "not-deployed"
```

### Phase 2: Choose Agents

Present the available agents and let the user choose:

| Agent | What It Does | Stacks Deployed | Test Prompt |
|-------|-------------|-----------------|-------------|
| **Knowledge Base** | RAG search over documents in `resources/` (FAQ, policies, product info) | `VoiceAgentKnowledgeBase` + `VoiceAgentKbAgent` | "What's your return policy?" |
| **CRM** | Customer lookup, case creation, case updates via DynamoDB | `VoiceAgentCRM` + `VoiceAgentCrmAgent` | "Can you look up my account?" |

Ask: "Which agents would you like to deploy? You can deploy one or both."

### Phase 3: Enable the Capability Registry

This is required **once** before deploying any agents. Skip if already enabled.

```bash
aws ssm put-parameter \
  --name "/voice-agent/config/enable-capability-registry" \
  --value "true" --type String --overwrite
```

The voice agent reads this flag at the start of each call. No container restart is needed — the next incoming call will start the CloudMap polling loop.

### Phase 4: Deploy Selected Agents

**Get confirmation before deploying.** Show the account ID and region.

#### Knowledge Base Agent

```bash
USE_CLOUD_APIS=true npx cdk deploy VoiceAgentKnowledgeBase VoiceAgentKbAgent --require-approval never
```

This deploys:
- **VoiceAgentKnowledgeBase** — Bedrock Knowledge Base + S3 bucket with sample documents from `resources/`
- **VoiceAgentKbAgent** — ECS Fargate container that exposes a `search_knowledge_base` skill via A2A

Takes ~5-8 minutes. The KB agent uses a direct tool executor (bypasses inner LLM) for low-latency queries (~300ms).

#### CRM Agent

```bash
USE_CLOUD_APIS=true npx cdk deploy VoiceAgentCRM VoiceAgentCrmAgent --require-approval never
```

This deploys:
- **VoiceAgentCRM** — DynamoDB tables (empty) + CRM API Lambda for customers and cases
- **VoiceAgentCrmAgent** — ECS Fargate container that exposes 5 CRM tools via A2A (customer lookup, case creation, case update, case listing, case detail)

Takes ~5-8 minutes.

**Seed the CRM with demo data** — the DynamoDB tables are created empty. The CRM API has a `/admin/seed` endpoint that loads demo customers and cases:

```bash
CRM_URL=$(aws ssm get-parameter --name "/voice-agent/crm/api-url" --query 'Parameter.Value' --output text)
curl -s -X POST "$CRM_URL/admin/seed" | python3 -m json.tool
```

Expected response: `{"message": "Demo data seeded successfully", "customers_seeded": 3, "cases_seeded": 2}`

The seed data includes:

| Customer | Phone | Account Type | Test Prompt |
|----------|-------|-------------|-------------|
| John Smith | 555-0100 | premium | "Look up the account for 555-0100" |
| Sarah Johnson | 555-0101 | basic | "Can you find Sarah Johnson?" |
| Michael Chen | 555-0102 | enterprise | "Look up customer 555-0102" |

There are also 2 demo support cases (a billing dispute for John Smith and a service outage for Michael Chen).

> **Without seeding, all CRM lookups return "customer not found."** This step is required for testing.

To reset and re-seed later: `DELETE /admin/reset` followed by `POST /admin/seed`.

### Phase 5: Verify Agent Discovery

After deployment, wait ~1 minute for the agents to register in CloudMap and be discovered. Then verify:

```bash
# Check CloudMap namespace for registered services
NAMESPACE=$(aws ssm get-parameter --name "/voice-agent/ecs/cloudmap-namespace" --query 'Parameter.Value' --output text 2>/dev/null)
if [ -n "$NAMESPACE" ]; then
  NAMESPACE_ID=$(aws servicediscovery list-namespaces --query "Namespaces[?Name=='$NAMESPACE'].Id" --output text)
  aws servicediscovery list-services --filters "Name=NAMESPACE_ID,Values=$NAMESPACE_ID,Condition=EQ" \
    --query 'Services[].Name' --output text
fi
```

You should see the agent service names listed. The voice agent will discover them on the next CloudMap poll (within 30 seconds).

### Phase 6: Test

Prompt the user to make a test call:

```
Capability agents deployed:
━━━━━━━━━━━━━━━━━━━━━━━━━━
  [status] Knowledge Base — "What's your return policy?"
  [status] CRM           — "Look up the account for 555-0100"

Call [phone number] and try one of the prompts above.
The voice agent discovers new agents automatically — no restart needed.
```

If the CRM agent was deployed, confirm the seed data is loaded:
```bash
CRM_URL=$(aws ssm get-parameter --name "/voice-agent/crm/api-url" --query 'Parameter.Value' --output text)
curl -s "$CRM_URL/customers" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if not data:
    print('WARNING: No customers found. Run POST /admin/seed to load demo data.')
else:
    print(f'{len(data)} customer(s) loaded:')
    for c in data:
        print(f'  {c.get(\"name\", \"?\")} — {c.get(\"phone\", \"?\")}')
"
```

If a tool isn't responding on the first call, remind the user:
- The registry polls every 30 seconds — wait a moment and try again
- Check ECS console to confirm the agent container is running and healthy
- Check CloudWatch logs for the agent container if it's not registering

### Phase 7: Report

```
Capability Agent Deployment Complete:
✅ Capability registry enabled
[status] Knowledge Base agent — deployed / skipped
[status] CRM agent — deployed / skipped
✅ Agents discoverable via CloudMap

Your voice agent now has [N] additional tools available.
Run /verify-deployment to confirm full system health.
```

Remind user to use the **destroy-project** skill to tear down all resources when done.
