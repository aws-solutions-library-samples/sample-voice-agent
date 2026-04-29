# Recordings SSE-KMS — operational runbook

**Status:** authoritative for the bucket-encryption flip on
`s3://medcloud-voice-us-prod-825`.
**Pairs with:** `infrastructure/src/constructs/recordings-key-construct.ts`
(CDK PR `feat/phase-8-recordings-sse-kms`).
**Audience:** anyone deploying, re-deploying, or recreating the recordings
encryption setup. Including future-you after the bucket gets accidentally
rebuilt.

## Context (read this first or you will break something)

The recordings bucket `medcloud-voice-us-prod-825` is a **manually
provisioned account bucket**. It is **not** managed by CDK. Setting its
default encryption to SSE-KMS therefore happens in two parts:

1. **CDK** (this PR) creates the dedicated CMK, the IAM grants on it for
   Daily's writer role and the voice-api Lambda role, and an SSM parameter
   exposing the CMK ARN.
2. **An out-of-band `aws s3api put-bucket-encryption` call** flips the
   bucket-level default to `aws:kms` pointing at the new CMK. This step
   is captured below verbatim so anyone re-creating the bucket can replay
   it.

If you only do step 1, nothing changes for new uploads (bucket default is
still SSE-S3). If you only do step 2 without step 1, every Daily upload
fails with `KMS.AccessDeniedException` because Daily's role has zero KMS
permissions today and the bucket would now require them.

This runbook is therefore strictly ordered. Do not skip steps.

## What "the new CMK" gives us

- **HIPAA posture**: CloudTrail records every `kms:Decrypt` against
  recordings. SSE-S3 has no equivalent audit signal.
- **Revocable access**: removing a principal from the key policy
  immediately blocks them from new decrypts. Doesn't require touching the
  bucket itself.
- **Blast-radius isolation**: the secrets CMK (used for
  `DAILY_API_KEY` / `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY`) and the
  recordings CMK are deliberately separate. Daily's cross-account writer
  role gets reach over recordings only — never our API keys.

## Pre-flight checklist

Run these before step A. None of them mutate anything.

```bash
# 1. Confirm we're in the right account (825... is voice prod).
aws sts get-caller-identity --query Account --output text

# 2. Confirm bucket still uses SSE-S3 (otherwise this runbook is moot).
aws s3api get-bucket-encryption --bucket medcloud-voice-us-prod-825 \
  --query "ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm" \
  --output text
# Expect: AES256

# 3. Confirm Daily's writer role still exists and is the correct one.
aws iam get-role --role-name daily-recordings-uploader \
  --query "Role.AssumeRolePolicyDocument.Statement[0]" --output json
# Expect: trust policy allowing arn:aws:iam::291871421005:root with
# ExternalId "cosentus".

# 4. Confirm voice-api Lambda role still exists.
aws iam get-role --role-name medcloud-lambda-role \
  --query "Role.RoleName" --output text
# Expect: medcloud-lambda-role
```

If any of these come back unexpected, **stop and investigate**. The CDK PR
will fail in a confusing way if Daily's role has been renamed.

## Step A — deploy the CDK PR

Live state required afterwards:
- New KMS CMK with alias `alias/voice-recordings-poc` (or `-prod` per env).
- Two managed IAM policies attached:
  - `voice-recordings-kms-write-{env}` → on `daily-recordings-uploader`
  - `voice-recordings-kms-read-{env}` → on `medcloud-lambda-role`
- SSM parameter `/voice-agent/storage/recordings-key-arn` containing the
  new CMK's ARN.

Deploy:

```bash
cd infrastructure
npx cdk deploy VoiceAgentStorage --require-approval never
```

The diff should show **only** new resources (KMS key, alias, two managed
policies, one SSM parameter, one CFN output). If you see modifications to
the existing secrets CMK / Secrets Manager secret, halt — something else
drifted and that needs separate review.

Verify the SSM parameter landed:

```bash
aws ssm get-parameter \
  --name /voice-agent/storage/recordings-key-arn \
  --query "Parameter.Value" --output text
```

Capture that ARN — you'll paste it into step C.

## Step B — Daily test upload (the safety gate)

**This is the gate that prevents a silent recording outage in step C.** It
confirms Daily's role can `GenerateDataKey` against the new CMK *before*
you flip the bucket default.

There are two equivalent ways to do this. Pick one:

### Option B.1 — Daily console "Test upload" button

1. Daily Dashboard → Domain Settings → Recording → S3 storage section.
2. Click "Test upload". Daily writes (or overwrites)
   `daily-co-test-upload.txt` at the bucket root.
3. Verify with:
   ```bash
   aws s3api head-object --bucket medcloud-voice-us-prod-825 \
     --key daily-co-test-upload.txt \
     --query "{enc:ServerSideEncryption,modified:LastModified}"
   ```
4. Expected: response is non-empty and `LastModified` is within the past
   minute. Encryption will still show `AES256` at this point — the bucket
   default has not been flipped yet. We're only verifying that Daily can
   PUT under the *current* encryption settings *plus the new IAM
   shape*. (Their IAM scope didn't change in step A; the CMK grant we
   added is unused until step C. But running the test here confirms no
   intermediate breakage.)

### Option B.2 — local impersonation of Daily's role

```bash
CREDS=$(aws sts assume-role \
  --role-arn arn:aws:iam::825269749545:role/daily-recordings-uploader \
  --role-session-name kms-readiness-check \
  --external-id cosentus \
  --query Credentials \
  --output json)
export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey)
export AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .SessionToken)

# Confirm GenerateDataKey works against the new CMK now that the
# managed policy is attached.
KEY_ARN=$(aws ssm get-parameter \
  --name /voice-agent/storage/recordings-key-arn \
  --query "Parameter.Value" --output text)
aws kms generate-data-key --key-id "$KEY_ARN" --key-spec AES_256 \
  --query "KeyId" --output text

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
```

Expected: prints the key ARN. Any error here (`AccessDenied`,
`InvalidGrantToken`, etc.) means the IAM grant from step A didn't take
effect or the role rename pre-flight check missed something.

### Halt condition — DO NOT SKIP

> **If step B fails, do not proceed to step C.**
>
> A bucket flipped to SSE-KMS without a working `kms:GenerateDataKey` for
> Daily's role results in **silent recording loss**: every PutObject
> fails with `AccessDenied`, but Daily's webhook still fires
> `recording.error` events that we don't currently surface to the
> frontend. Users will think calls completed normally; recordings will
> just silently never appear.
>
> Roll back step A if necessary (`cdk deploy VoiceAgentStorage` from a
> reverted commit, or destroy and recreate the construct in a follow-up
> PR), debug the IAM/KMS plumbing, then re-run from step A.

## Step C — flip the bucket default to SSE-KMS

After step B passes, run:

```bash
KEY_ARN=$(aws ssm get-parameter \
  --name /voice-agent/storage/recordings-key-arn \
  --query "Parameter.Value" --output text)

aws s3api put-bucket-encryption \
  --bucket medcloud-voice-us-prod-825 \
  --server-side-encryption-configuration "$(cat <<EOF
{
  "Rules": [
    {
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": "$KEY_ARN"
      },
      "BucketKeyEnabled": true
    }
  ]
}
EOF
)"
```

`BucketKeyEnabled: true` is required, not optional. Without it every
object PUT/GET makes a separate KMS API call; with it, KMS calls drop by
~99% and so does the bill. AWS-recommended for any KMS-encrypted bucket
with non-trivial volume.

Verify the flip landed:

```bash
aws s3api get-bucket-encryption --bucket medcloud-voice-us-prod-825 \
  --query "ServerSideEncryptionConfiguration.Rules[0]" --output json
```

Expected:
```json
{
  "ApplyServerSideEncryptionByDefault": {
    "SSEAlgorithm": "aws:kms",
    "KMSMasterKeyID": "arn:aws:kms:us-east-1:825269749545:key/..."
  },
  "BucketKeyEnabled": true
}
```

## Step D — real test call + verification

Place an inbound test call (the same dial-in PSTN flow Phase 7 used for
the VAD hotfix verification). After the call ends, Daily uploads the
m4a recording.

### D.1 — confirm the new object is SSE-KMS

```bash
LATEST_KEY=$(aws s3api list-objects-v2 \
  --bucket medcloud-voice-us-prod-825 \
  --prefix voice-recordings/ \
  --query "sort_by(Contents,&LastModified)[-1].Key" \
  --output text)

aws s3api head-object --bucket medcloud-voice-us-prod-825 --key "$LATEST_KEY" \
  --query "{enc:ServerSideEncryption,kmsKey:SSEKMSKeyId,bucketKey:BucketKeyEnabled,size:ContentLength}"
```

Expected:
```json
{
  "enc": "aws:kms",
  "kmsKey": "arn:aws:kms:us-east-1:825269749545:key/<recordings-cmk-id>",
  "bucketKey": true,
  "size": <non-zero>
}
```

### D.2 — confirm CloudTrail recorded the GenerateDataKey

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=GenerateDataKey \
  --max-items 5 \
  --query "Events[?contains(Resources[].ResourceName,'<recordings-cmk-id>')].{when:EventTime,by:Username,via:UserAgent}" \
  --output table
```

Expected: at least one event in the past few minutes, sourced from
`daily-recordings-uploader` (after assume-role) or an AWS-internal S3
service principal. **The presence of this event is the actual HIPAA
win** — every future decrypt of recordings is now audited by default.

### D.3 — what we explicitly do NOT verify in this brief

End-to-end audio playback. The `/api/calls/:id/recording` route in
`cosentus-voice-api-lambda/index.mjs` is a TODO — it currently returns
the raw S3 key, not a presigned URL. That belongs in a separate Phase 8
brief. The recordings CMK is already wired with `kms:Decrypt` for
`medcloud-lambda-role` so when that brief lands, no IAM work is needed.

## Rollback

If something goes wrong after step C:

```bash
# Revert bucket encryption to SSE-S3 (objects stay aws:kms; only the
# default for new objects changes back).
aws s3api put-bucket-encryption \
  --bucket medcloud-voice-us-prod-825 \
  --server-side-encryption-configuration '{
    "Rules": [
      {
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}
      }
    ]
  }'
```

Existing SSE-KMS objects remain readable as long as the CMK still exists
and the consumer has `kms:Decrypt`. **Do not delete the CMK** until every
SSE-KMS object has been re-encrypted or deleted, otherwise those objects
become permanently unreadable.

For CDK rollback (revert step A), see standard CDK rollback procedure —
the construct uses `RemovalPolicy.RETAIN` in prod, so a stack destroy
will not orphan-delete the CMK.

## Mixed-mode bucket: explicitly intended

After the flip, the bucket contains:
- 57 OG-era WAVs under `recordings/` from the now-stopped EC2 → SSE-S3.
- All future Daily m4a uploads under `voice-recordings/` → SSE-KMS.

This is **fine and intentional**. The OG WAVs are not surfaced by the new
voice-api Lambda's `voice_calls` table; they're effectively cold archive.
A separate Phase 8 cleanup brief will decide whether to delete them
during the OG EC2 termination soak.

## Frequently asked questions

**Why not just import the bucket into CDK and set encryption from there?**
Importing an unmanaged S3 bucket into CDK is a multi-step process
(`cdk import`) that's brittle on buckets with versioning, lifecycle
rules, and cross-account access setups. We get nothing for the cost
besides moving a one-shot ops change into a CFN-managed change. The
runbook approach is honest about what's actually happening.

**Why not let Daily write under SSE-S3 forever?**
SSE-S3 has no per-object decrypt audit. HIPAA's required-and-addressable
implementation specifications around audit controls (§164.312(b)) are
much easier to defend with a CMK + CloudTrail than with bucket-level
SSE-S3 + S3 access logs only. Recordings touch PHI; we need that audit
trail.

**Why two managed policies instead of one?**
Different blast radius. `voice-recordings-kms-write-{env}` lets Daily
write encrypted objects; `voice-recordings-kms-read-{env}` lets the
voice-api Lambda decrypt them for playback. Splitting them lets us
revoke playback access without breaking writes (or vice versa) without a
key-policy redeploy.

**What if the recordings CMK gets accidentally deleted in dev?**
Dev (`environment: poc`) uses `RemovalPolicy.DESTROY`, so a stack destroy
schedules the CMK for deletion (7-30 day window). Recordings written
during the dev period will become unreadable when the deletion completes.
Acceptable for poc — there's no real PHI in dev recordings. **Prod
explicitly uses `RemovalPolicy.RETAIN`** so this can never happen in
prod.
