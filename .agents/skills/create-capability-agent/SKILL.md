---
name: create-capability-agent
description: Scaffolds a new A2A capability agent with Python application, Dockerfile, requirements.txt, and CDK stack. Use when adding a new remote tool or service that the voice agent discovers via CloudMap.
---

## What I Do

Scaffold all the files needed for a new A2A capability agent in the voice agent platform. This includes:

1. Python agent application (`backend/agents/{name}/main.py`)
2. Dependencies (`backend/agents/{name}/requirements.txt`)
3. Container image (`backend/agents/{name}/Dockerfile`)
4. CDK infrastructure stack (`infrastructure/src/stacks/{name}-agent-stack.ts`)
5. Stack registration (exports in `index.ts`, instantiation in `main.ts`)

## When to Use Me

Use this skill when you need to create a new capability agent that will be discovered via CloudMap and invoked over the A2A protocol by the voice agent.

## Reference

Read `docs/guides/adding-a-capability-agent.md` for the complete developer guide. All templates below are derived from that guide and the shipped KB and CRM agent implementations.

## Steps

### 1. Gather Requirements

Ask the user for:
- **Agent name** (kebab-case, e.g., `inventory-agent`). This becomes the directory name and CloudMap service name.
- **Tool descriptions** -- what tools should the agent expose? For each tool, get:
  - Function name (snake_case, must be unique -- check existing names below)
  - What it does (this becomes the `@tool` docstring, which is critical for LLM tool selection)
  - Parameters and return type
- **Execution pattern** -- single tool (DirectToolExecutor, ~300ms) or multi-tool (StrandsA2AExecutor, ~2-3s)?
- **AWS services needed** -- does it call Bedrock, DynamoDB, S3, or external APIs? This determines IAM policies and env vars.
- **Backend dependencies** -- does it depend on other CDK stacks (e.g., a database stack)?

Existing tool names (avoid conflicts):

| Name | Source | Agent |
|------|--------|-------|
| `get_current_time` | Local | Voice Agent |
| `hangup_call` | Local | Voice Agent |
| `transfer_to_agent` | Local | Voice Agent |
| `search_knowledge_base` | A2A | KB Agent |
| `lookup_customer` | A2A | CRM Agent |
| `create_support_case` | A2A | CRM Agent |
| `add_case_note` | A2A | CRM Agent |
| `verify_account_number` | A2A | CRM Agent |
| `verify_recent_transaction` | A2A | CRM Agent |

### 2. Create the Agent Directory

```bash
mkdir -p backend/agents/{name}/tests/
```

### 3. Create main.py

Follow the template in `docs/guides/adding-a-capability-agent.md` (Step 2). Key requirements:

- Use `from strands.multiagent.a2a import A2AServer` (NOT `A2AStarletteApplication`)
- Use `from strands.models import BedrockModel` (NOT `from strands.models.bedrock`)
- Include `_get_task_private_ip()` function (required boilerplate for Agent Card URL)
- Write detailed `@tool` docstrings -- the voice agent's LLM uses these for tool selection
- All tools must return `dict` (JSON-serializable)
- For single-tool agents, include `DirectToolExecutor` and swap it after creating the server:
  ```python
  server.request_handler.agent_executor = DirectToolExecutor(my_tool)
  ```
- Add warm-up in `main()`: pre-initialize boto3 clients and optionally probe the Strands agent

### 4. Create requirements.txt

Base dependencies (always required):
```
strands-agents[a2a]>=1.27.0
requests>=2.31.0
```

Add `boto3>=1.34.0` if calling AWS services. Add `cachetools>=5.3.0` if implementing result caching.

Also create `requirements-test.txt`:
```
-r requirements.txt
pytest>=8.0.0
pytest-asyncio>=0.23.0
requests-mock>=1.11.0
```

### 5. Create Dockerfile

Use the exact template from `docs/guides/adding-a-capability-agent.md` (Step 4). Critical requirements:
- Base image: `python:3.12-slim`
- Install `curl` (required for health check)
- Create `appuser` (non-root)
- HEALTHCHECK must target `/.well-known/agent-card.json` (NOT `/health`)
- CMD: `["python", "main.py"]`

### 6. Create CDK Stack

Create `infrastructure/src/stacks/{name}-agent-stack.ts` following the template in `docs/guides/adding-a-capability-agent.md` (Step 5). Key patterns:
- Import `VoiceAgentConfig` from `../config`
- Import `SSM_PARAMS` from `../ssm-parameters`
- Import `CapabilityAgentConstruct` from `../constructs`
- VPC: `ssm.StringParameter.valueFromLookup` (synth-time)
- All other SSM params: `ssm.StringParameter.valueForStringParameter` (deploy-time)
- Use `ecr_assets.DockerImageAsset` with `Platform.LINUX_AMD64`
- Use `CapabilityAgentConstruct` with agent-specific config
- Grant ECR pull: `containerImage.repository.grantPull(agent.taskDefinition.executionRole!)`

### 7. Register the Stack

Add export to `infrastructure/src/stacks/index.ts`:
```typescript
export { MyAgentStack, MyAgentStackProps } from './{name}-agent-stack';
```

Add instantiation to `infrastructure/src/main.ts`:
```typescript
const myAgentStack = new MyAgentStack(app, 'VoiceAgentMyAgent', {
  env, config,
  description: 'Voice Agent - My Capability Agent',
  tags: { Project: config.projectName, Environment: config.environment },
});
myAgentStack.addDependency(ecsStack);
```

### 8. Write Tests

Create `backend/agents/{name}/tests/__init__.py` (empty) and `backend/agents/{name}/tests/test_{name}.py`.

Follow patterns from existing tests:
- `backend/agents/crm-agent/tests/test_crm_client.py` -- HTTP interactions
- `backend/agents/knowledge-base-agent/tests/test_knowledge_base.py` -- DirectToolExecutor

Run tests:
```bash
cd backend/agents/{name} && pip install -r requirements-test.txt && pytest tests/ -v
```

### 9. Verify Checklist

- [ ] Port 8000, Agent Card endpoint at `/.well-known/agent-card.json`
- [ ] `_get_task_private_ip()` function included
- [ ] Unique tool names (no conflicts with table above)
- [ ] All tools return `dict`
- [ ] Dockerfile health check targets `/.well-known/agent-card.json`
- [ ] `curl` installed in container, non-root user
- [ ] `strands-agents[a2a]>=1.27.0` in requirements
- [ ] ECR pull grant in CDK stack
- [ ] `ecsStack` dependency declared
- [ ] Tests pass
