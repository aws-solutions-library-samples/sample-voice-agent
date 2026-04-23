# CLEANUP — Known Issues from Baseline Deploy

This doc tracks debt and rough edges discovered during the first AWS dev deploy
(2026-04-22, commit `f0eeec3`). See `docs/baseline-deploy-findings.md` for the
full deploy writeup.

---

## Must-fix before production

### 1. Webhook HMAC verification is disabled

`setup-daily.sh` configures pinless dial-in with HMAC, and the HMAC secret is
saved to `backend/voice-agent/.env` — but **`infrastructure/src/functions/bot-runner/handler.py`
never validates the `X-Signature` header**. The /start endpoint will happily
launch an ECS task (and a Daily room, and burn bot-minutes) for any request.

**Fix sketch:** read `DAILY_HMAC_SECRET` from the shared secret in Secrets
Manager, HMAC-SHA256 the raw request body, constant-time compare with
`X-Signature`, reject 401 on mismatch. Needs `HMAC_SECRET` wired into the
Lambda environment and `init-secrets.sh` extended to persist it.

### 2. `test-webhook` rate has no auth / no IP allowlist

Related to #1. API Gateway stage `/poc/start` is public. At minimum should be
behind a usage plan with an API key, WAF rate limit, or IP allowlist on the
known Daily.co SIP trunk egress IPs (they publish these).

### 3. Lint gate fixup was heavy-handed

The `fix(tts)` commit reformatted ~7 TS files under `infrastructure/src/` and
`infrastructure/test/` to satisfy prettier. These files came from the upstream
reference repo and were not originally in our fork's edit set — they were
collateral damage from running `npm run lint:fix` to unblock the deploy gate.
No functional change, but the diff noise is non-trivial.

**Follow-up:** decide whether to (a) keep enforcing prettier and eat the
reformat as permanent, or (b) exclude upstream untouched files from the lint
gate by scoping the `eslint` config to our fork's actual edits.

---

## Should-fix soon

### 4. Pipecat version pin skew — **RESOLVED**

- `voiceagent` (EC2 prod): `pipecat-ai[...]>=0.0.102`, actual installed 0.0.106.
- `cosentus-voice-engine` (dev Fargate): originally pinned `==0.0.102`, but
  0.0.102 had broken ElevenLabs TTS (audio context IDs never initialized,
  TTSStartedFrame never fired). Bumped to `>=0.0.106,<0.1.0` in commit
  `1703e8c`. Current deploy runs 0.0.108.

**Open question:** should the OG `voiceagent` repo also tighten its pin to
`>=0.0.106` so the two codebases guarantee shared Pipecat behavior? Right now
OG is `>=0.0.102` which would re-admit the broken version if anyone does a
fresh install. Low priority since OG venvs are pinned via lock files, but
worth fixing for consistency.

### 5. `cloud-api-mode-stt-not-deployed` / `-tts-not-deployed` in SSM is confusing

The `VoiceAgentSageMaker` stack writes placeholder endpoint names because we
skipped the real SageMaker deploy. But `ProviderConfig` reads these values
into `stt_endpoint` / `tts_endpoint` at pipeline construction time and logs
them verbose in every `creating_pipeline` event. Makes logs noisy when
grepping for real endpoints.

**Fix:** skip emitting SSM params when value is a placeholder, or drop those
fields from the pipeline log when provider is set to a cloud API.

### 6. Post-deploy integration test suite aborts at first check

`./deploy.sh deploy` runs `scripts/test-integration.sh` after `cdk deploy`
completes. In this run, Test 1 (SSM param check) passed and the script
immediately reported "Some integration tests failed" without running Tests
2+. Almost certainly an early-exit on a missing-secret assertion from before
`init-secrets.sh` ran.

**Fix:** either re-order the script so integration tests run *after* secrets
seeding, or flip failing assertions to warnings on first-deploy conditions.

### 7. `infrastructure/scripts/init-secrets.sh` only writes the 3 API keys

It does NOT persist `DAILY_HMAC_SECRET` or `DAILY_PHONE_NUMBER` into Secrets
Manager, even though `setup-daily.sh` adds them to `backend/voice-agent/.env`.
This means the webhook Lambda (once we wire HMAC verification per issue #1)
will need a separate mechanism to retrieve the secret.

**Fix:** extend `SECRET_VALUE` JSON in `init-secrets.sh` to include
`DAILY_HMAC_SECRET` and `DAILY_PHONE_NUMBER` when present. Update the ECS
task definition and BotRunner Lambda environment to consume both.

---

## Nice-to-have

### 8. Tear-down cost: 3 NAT gateways is overkill for dev

The reference repo provisions `VoiceAgentNetwork` with a NAT in every AZ
(3 total ≈ $100/mo). For dev, one NAT is fine; the multi-AZ high-availability
is a prod concern.

**Fix:** `natGateways: config.environment === 'prod' ? 3 : 1` in
`network-stack.ts`. Saves ~$70/mo when the dev stack is live.

### 9. Docker image rebuild on every `deploy-stack VoiceAgentEcs`

CDK treats any change under `backend/voice-agent/` as a content hash change
and rebuilds the 1.8 GB image from scratch (pip install pipecat-ai + all its
deps). Full rebuild takes ~6-8 min even with a warm layer cache.

**Fix:** pre-build a base image on ECR with the Python deps frozen, have
the project Dockerfile `FROM` that base. Drops per-deploy image build to
seconds (only COPY layers change).

### 10. ECS NLB health check returns 000 from the public internet

`curl http://<nlb-dns>/ready` times out. The NLB is public-facing (default
for Fargate-behind-NLB pattern) but the target security group is scoped to
only accept traffic from the bot-runner Lambda SG. Expected behavior — just
noting that external health probing doesn't work and we should rely on
CloudWatch container insights / ECS service events for health instead.

---

## Tear-down procedure

```bash
cd ~/Desktop/cosentus-voice-engine/infrastructure

# Releases the Daily number first (so we stop paying for it)
curl -X DELETE "https://api.daily.co/v1/phone-number/4559021e-33b2-425c-a599-ad900d414e02" \
  -H "Authorization: Bearer $DAILY_API_KEY"

# Tears down all 5 CDK stacks in reverse dependency order
./deploy.sh destroy

# Leaves behind: ECR images (delete from console if desired), CloudWatch log
# groups (auto-expire at retention), CDKToolkit bootstrap stack (keep — next
# deploy needs it).
```

Expect ~10 min for destroy. Account spend drops to ~$0 for this project.
