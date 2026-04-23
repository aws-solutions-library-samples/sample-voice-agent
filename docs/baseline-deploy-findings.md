# Baseline Deploy to AWS Dev — Findings

**Date**: 2026-04-22
**Account**: 825269749545 (medcloud dev)
**Region**: us-east-1
**Environment**: `poc`
**Deployer identity**: `medcloud-voice-alex` (IAM user, after Lakshay's AdministratorAccess grant)
**Commit deployed**: `f0eeec3` (one past the PR #2 merge)

---

## What got deployed

Five CDK stacks created clean from scratch:

| Stack | Purpose | Notable outputs |
| --- | --- | --- |
| `VoiceAgentNetwork` | VPC, subnets, NAT GWs, VPC endpoints (SSM, Secrets, Logs, Bedrock, SageMakerRuntime) | `vpc-05a1f6c68c04943ec` |
| `VoiceAgentStorage` | Encryption key + API key secret | `arn:...:secret:SecretsApiKeySecretCEC8F618-MqoY7x3uc0N3-dOqjuy` |
| `VoiceAgentSageMaker` | Stub — real endpoints skipped (cloud APIs mode) | `cloud-api-mode-stt-not-deployed` |
| `VoiceAgentEcs` | Fargate cluster, task def, service, NLB, DynamoDB session table, session counter Lambda | NLB `VoiceA-Servi-QOhEbO6GItrb-e979675235460ad8.elb.us-east-1.amazonaws.com`, cluster `voice-agent-poc-cluster` |
| `VoiceAgentBotRunner` | API Gateway + Lambda webhook for Daily.co pinless dial-in | `https://1v6foyd02m.execute-api.us-east-1.amazonaws.com/poc/start` |

Full deploy time (first run, incl. Docker image builds): **~24 min wall clock.**

Daily number provisioned and wired to the webhook: **`+1 (209) 807-5018`**
(California 209 area code, `DAILY_HMAC_SECRET` saved to `backend/voice-agent/.env`; that file is gitignored.)

---

## What broke (and got fixed)

### Issue 1 — Pre-deploy lint gate

`deploy.sh deploy` runs `npm test && npm run lint` before `cdk deploy`.
On a fresh clone the lint gate fails with **208 errors** because:

- 197 prettier formatting errors in the upstream reference repo's TS source
  (pre-existing, never reformatted when the fork was created).
- 11 `'test' is not defined` errors — the reference repo's `eslint.config.mjs`
  lists `describe`/`it`/`expect`/`jest` as globals but not `test`.

**Fix (applied, committed to main):**
- Added `test: 'readonly'` to the jest globals block.
- Ran `npm run lint:fix` over the reference TS files.
- Renamed one unused variable `maxSessionsPerTaskMetric` → `_maxSessionsPerTaskMetric`
  to match the `argsIgnorePattern: '^_'` rule.

Gate now passes clean on `main`. 86 tests green.

### Issue 2 — ECS task crashed on every pipeline build

Webhook returned 200 and the bot-runner Lambda successfully launched ECS tasks,
but the pipeline died immediately with:

```
type object 'ElevenLabsTTSService' has no attribute 'Settings'
```

**Root cause:** `pipecat-ai.services.elevenlabs.tts.ElevenLabsTTSService.Settings`
was introduced in pipecat-ai **0.0.106**. The fork's `backend/voice-agent/requirements.txt`
pinned `pipecat-ai[...]==0.0.102`. When the Cartesia → ElevenLabs swap was written
(PR #2) the `Settings(voice=, model=)` pattern was carried over verbatim from
the reference repo (which uses 0.0.106). On 0.0.102 that attribute simply
didn't exist.

**Fix (committed in `f0eeec3`):** use flat kwargs, which all pipecat versions
accept:

```python
return ElevenLabsTTSService(
    api_key=api_key,
    voice_id=voice_id,
    model=_ELEVENLABS_DEFAULT_MODEL,
)
```

### Issue 3 — TTS silently produced no audio on the first real call

First real phone test: LLM generated the greeting text and the conversation
observer logged the assistant turn — but the caller heard only silence. No
TTS audio ever reached the Daily room. Metrics confirmed: `tts_ttfb_ms: null`,
`bot_speaking_duration_ms: null`, `AvgTTSLatency: 0.0`, and zero
`bot_started_speaking` events across the entire deploy history.

**Root cause:** pipecat-ai **0.0.102** has broken ElevenLabs audio context
initialization. Pipecat PR #3729 (merged Feb 2026, shipped in 0.0.103+) fixed
two specific bugs: "Added `create_context_id()` override to reuse context IDs
across multiple `run_tts()` invocations" and "Fixed initialization logic to
emit `TTSStartedFrame` only once per turn instead of per sentence". A local
repro on 0.0.102 confirmed the symptoms — service initialized with
`sample_rate=0` and `run_tts()` raised `TypeError: missing context_id
positional arg`.

**Fix (committed in `1703e8c`):** bumped pinned version to
`pipecat-ai[...]>=0.0.106,<0.1.0`. Current deploy runs 0.0.108 which also
includes PR #4293 (ElevenLabs boolean param handling fixes).

**Verification:** real inbound phone call to +1 (209) 807-5018 with full
two-way conversation confirmed working. 5-turn exchange, 37.78s duration,
`CompletionStatus: completed`, agent response latency 1.0–1.2s. Transcript:

```
[bot]:  Hey there! Welcome, thanks for calling. How can I help you today?
[user]: Hello?
[bot]:  Hi! I'm here to help. What can I do for you?
[user]: What can you do?
[bot]:  Great question! I can help you with things like answering questions,
        giving you information, checking the time or date, or just having a
        conversation. Is there something specific I can help you with today?
```

### Issue 4 — Deploy's post-deploy integration tests reported "some failed"

Only the first SSM parameter check passed before the script aborted. Almost
certainly an assertion that expected non-placeholder values in the API keys
secret, before `init-secrets.sh` had populated it. **Not investigated further**
— once secrets were seeded + ECS forced to redeploy, the webhook smoke test
(end-to-end with Daily) succeeded, which is a stronger signal than the
infra-only integration test.

**Follow-up (CLEANUP.md item):** figure out what that integration test was
asserting on and either fix its ordering assumption or drop it.

---

## What's live right now

- ECS service `voice-agent-poc-service`: 1/1 running, task def rev 2, HEALTHY.
- BotRunner Lambda: 2 successful invocations observed during gate testing.
- Daily webhook: HMAC configured, pinless dial-in points at
  `https://1v6foyd02m.execute-api.us-east-1.amazonaws.com/poc/start`.
- CloudWatch logs:
  - `/ecs/voice-agent-poc` (14-day retention)
  - `/aws/lambda/VoiceAgentBotRunner-WebhookApiBotRunnerFunction315-USSIiCLg9tNa`
  - `/aws/lambda/voice-agent-poc-session-counter`
  - `/aws/ecs/containerinsights/voice-agent-poc-cluster/performance`
- CloudWatch dashboard: `https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=voice-agent-poc-monitoring`
- Configured pipeline: **Deepgram STT + Bedrock Haiku-4.5 + ElevenLabs TTS** (eleven_flash_v2_5).

---

## What was NOT tested in this pass

- SageMaker path for STT/TTS. Fork is in "cloud APIs mode" (`cloud-api-mode-stt-not-deployed` / `cloud-api-mode-tts-not-deployed`) and that's intentional for the baseline.
- HMAC verification on the webhook Lambda — the stored HMAC secret is retained in the local `.env` for future hardening but nothing currently validates Daily's signed requests against it. Anyone who finds the API Gateway URL can POST to /start. OK for dev, MUST tighten before prod.
- A2A / capability registry routes. Feature stripped during the baseline fork; no regression test.
- Tool registration scenarios other than `get_current_time` + `hangup_call`. `transfer_to_agent` was skipped by the filter because no transfer destination is configured.

---

## Cost exposure (monthly, dev)

Rough estimate of always-on infrastructure:
- NAT Gateway × 3 AZs: ~$100/mo (biggest line item)
- Fargate: 1 task idle @ 0.5 vCPU / 1 GB ≈ $15/mo
- NLB: ~$18/mo
- VPC endpoints (interface): 5 × ~$7/mo = $35/mo
- Daily phone number: ~$1/mo
- API Gateway + Lambda: negligible at dev volume
- CloudWatch logs/metrics: < $5/mo at dev volume

Ballpark **~$175/mo sitting idle.** Worth tearing down between work sessions
if we aren't actively testing — `./deploy.sh destroy` from
`infrastructure/` tears down all 5 stacks cleanly.
