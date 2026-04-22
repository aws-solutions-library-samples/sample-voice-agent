# AWS permissions required — cosentus-voice-engine migration

**Status**: pending grant from AWS admin (Lakshay).
**Owner**: Alex Kashkarian.
**Audit date**: 2026-04-22.
**Target principal**: IAM user `medcloud-voice-alex`
(`arn:aws:iam::825269749545:user/medcloud-voice-alex`).

## TL;DR

We're migrating the voice engine to AWS Fargate via CDK. The current
`MedCloudVoiceOnly` policy was scoped for the EC2 + Lambda setup and
is missing roughly 10 services required for Fargate deploys, plus has
three explicit `Deny` statements that actively block CDK bootstrap.

**Two clean options**, in order of preference:

1. **Attach `AdministratorAccess` (managed)** to `medcloud-voice-alex`
   for the duration of the migration. Fastest unblock. Remove after
   migration stabilizes.
2. **Update `MedCloudVoiceOnly` to v8** with the additional allows
   listed in "Full required permission set" below, and narrow the
   three blocking `Deny` statements so CDK can create its own roles.
   Same outcome, more audit-friendly, more ops work.

Either option also needs the same three `Deny` statements relaxed; see
"Explicit denies that must be removed or narrowed" below.

## 1. Scope — what we're deploying, and when we need the permissions

| Phase | What we deploy / touch | When |
|---|---|---|
| **Baseline deploy** (this task) | 5 CDK stacks: Network, Storage (KMS + Secrets), SageMaker stub, ECS Fargate, BotRunner (Lambda + API Gateway) | Now |
| **Ongoing deploys** | Re-deploy any of the 5 stacks; push new container images; rotate secrets | Continuous, per-PR |
| **Phase 7 customization** | Aurora/RDS if we migrate DB here; additional Secrets; Route53 custom domain; S3 recording bucket; ECR image pushes from CI | Next few weeks |
| **Bedrock runtime** | Claude Haiku 4.5 + Sonnet 4.5 invoke (cross-region inference profiles) | Runtime, every call |
| **Teardown** | `cdk destroy` — same permission surface as create | On dev cleanup + on production replace |

## 2. Current state — what `MedCloudVoiceOnly` v7 has today

Last fetched from IAM at audit time. 18 statements total.

**Allows (relevant):**

| Service | Actions (summarized) | Resource scope |
|---|---|---|
| `lambda` | Get, Update*, Invoke | `function:medcloud-voice-api` only |
| `s3` | Get/Put/List | `medcloud-voice-us-prod-825` and `lambda-deploys/voice-*` only |
| `sqs` | Create, Send, Receive, Delete, etc. | `medcloud-voice-*` pattern |
| `ec2` | Describe*, Start/Stop/Reboot, AllocateAddress, AuthorizeSecurityGroupIngress | Various |
| `bedrock` | Invoke*, ListFoundationModels, GetFoundationModel | `*` |
| `cloudwatch` | PutMetric*, DescribeAlarms, Get*, List* | `*` |
| `logs` | Create+Put on log groups with name containing `voice` | `log-group:*voice*` only |
| `sns` | Create/Subscribe/Publish | `medcloud-voice-*` pattern |
| `sts`, `iam` (read-only on self) | GetCallerIdentity, GetUser, GetPolicy, etc. | Self/own |

**Explicit denies that must be removed or narrowed for the migration:**

| Statement | Actions denied | Why this blocks us |
|---|---|---|
| `ProtectIAMAndCognito` | `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:CreatePolicy`, `iam:DeleteRole`, `iam:CreateUser`, etc. | **Hard-blocks CDK bootstrap + every CDK stack that creates a role** (all 5 stacks create task/exec/lambda roles). |
| `ProtectIAMAndCognito` | `apigateway:*`, `apigatewayv2:*` | **Blocks BotRunner stack** (API Gateway webhook for Daily). |
| `ProtectIAMAndCognito` | `rds:*`, `cognito-idp:*` | **Blocks Phase 7** if we migrate DB or auth here. |
| `ProtectCoreLambdas` | Lambda update/delete on 10 production functions | Narrow resource list; does NOT block us unless we try to modify those specific prod functions. Not a migration blocker. |
| `ProtectMainS3Bucket` | Write/delete on `medcloud-documents-us-prod-v2` | Narrow resource list; not a blocker. |
| `ProtectRDSSecurityGroup` | SG-modify on `sg-0fa0c00f028a5a13a` | Narrow resource; not a blocker. |
| `ProtectNonVoiceLogs` | `logs:Get/Filter` on specific non-voice log groups | Narrow; not a blocker. |

## 3. Permission gaps, per AWS service

Symbols:
- ✅ allowed today
- ❌ denied today (or simply absent)
- ⚠️ partially allowed — works for some actions, not others

### 3.1 Services wholly missing from the policy

These services have no `Allow` statement covering them today. They're
required for the Fargate migration and need to be added.

| Service | Required actions (summary) | Used by |
|---|---|---|
| `cloudformation` | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStack*`, `ExecuteChangeSet`, `GetTemplate`, `ListStacks`, `ValidateTemplate`, `CreateChangeSet`, `DeleteChangeSet` | CDK itself (all stacks) |
| `ecs` | `CreateCluster`, `CreateService`, `UpdateService`, `DeleteService`, `RegisterTaskDefinition`, `DeregisterTaskDefinition`, `Describe*`, `List*`, `TagResource`, `UntagResource` | VoiceAgentEcs stack |
| `ecr` | `CreateRepository`, `PutImage`, `Initiate/Upload/CompleteLayerUpload`, `BatchCheckLayerAvailability`, `GetAuthorizationToken`, `DescribeRepositories`, `DescribeImages`, `DeleteRepository`, `PutLifecyclePolicy` | CDK Docker image asset push + every rebuild |
| `secretsmanager` | `CreateSecret`, `PutSecretValue`, `GetSecretValue`, `DescribeSecret`, `UpdateSecret`, `TagResource`, `DeleteSecret` | VoiceAgentStorage stack + `init-secrets.sh` + runtime |
| `kms` | `CreateKey`, `CreateAlias`, `DescribeKey`, `ListKeys`, `ListAliases`, `EnableKey`, `ScheduleKeyDeletion`, `TagResource`, `GetKeyPolicy`, `PutKeyPolicy`, `Encrypt`, `Decrypt`, `GenerateDataKey` | VoiceAgentStorage KMS key, Secrets encryption |
| `servicediscovery` | `CreatePrivateDnsNamespace`, `DeleteNamespace`, `CreateService`, `DeleteService`, `List*`, `TagResource` | VoiceAgentEcs Cloud Map for ECS service discovery |
| `route53` | `CreateHostedZone`, `ChangeResourceRecordSets`, `ListHostedZones`, `GetHostedZone` | Cloud Map creates Route53 records under the hood; also Phase 7 custom domain |
| `dynamodb` | `CreateTable`, `UpdateTable`, `DeleteTable`, `DescribeTable`, `TagResource`, `ListTables`, `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query` | Session table (runtime + CDK creation) |
| `ssm` | `PutParameter`, `GetParameter`, `GetParameters`, `GetParametersByPath`, `DescribeParameters`, `DeleteParameter`, `AddTagsToResource` | Cross-stack SSM references, runtime config (KB/provider/A2A removed; still has provider + feature flags + session table name + secret ARN) |
| `events` | `PutRule`, `PutTargets`, `DeleteRule`, `RemoveTargets`, `DescribeRule`, `ListRules` | Session-counter Lambda + any scheduled tasks |
| `elasticloadbalancing` | V2 create/modify/describe/delete (LoadBalancer, TargetGroup, Listener, Listener rule) | ECS-to-ALB wiring (if the stack uses an ALB — low risk even if not) |
| `application-autoscaling` | `RegisterScalableTarget`, `PutScalingPolicy`, `Describe*`, `DeregisterScalableTarget`, `DeleteScalingPolicy` | Fargate auto-scaling rules |
| `scheduler` | `CreateSchedule`, `DeleteSchedule` (if EventBridge Scheduler is used) | Optional, check at deploy time |

### 3.2 Services partially covered — add actions

| Service | Already allowed | Additionally required | Notes |
|---|---|---|---|
| `iam` | `GetUser`, `GetPolicy`, `ListAttachedUserPolicies`, `GetPolicyVersion` (read-only on self) | `CreateRole`, `DeleteRole`, `GetRole`, `TagRole`, `UntagRole`, `PutRolePolicy`, `DeleteRolePolicy`, `AttachRolePolicy`, `DetachRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `PassRole`, `CreateInstanceProfile`, `DeleteInstanceProfile`, `AddRoleToInstanceProfile`, `RemoveRoleFromInstanceProfile`, `CreatePolicy`, `CreatePolicyVersion`, `DeletePolicy`, `DeletePolicyVersion`, `ListPolicies` | **Also need the `ProtectIAMAndCognito` deny narrowed** to not block CDK-path roles. Scope by path `/cdk-*` or prefix `voice-agent-*` if a narrow grant is required. |
| `ec2` | Describes, Start/Stop/Reboot, AllocateAddress, AuthorizeSecurityGroupIngress | `CreateVpc`, `DeleteVpc`, `CreateSubnet`, `DeleteSubnet`, `CreateSecurityGroup`, `DeleteSecurityGroup`, `RevokeSecurityGroupIngress`, `CreateInternetGateway`, `AttachInternetGateway`, `DetachInternetGateway`, `DeleteInternetGateway`, `CreateNatGateway`, `DeleteNatGateway`, `CreateRouteTable`, `DeleteRouteTable`, `CreateRoute`, `DeleteRoute`, `AssociateRouteTable`, `DisassociateRouteTable`, `CreateVpcEndpoint`, `DeleteVpcEndpoint`, `ModifyVpcEndpoint`, `CreateTags`, `DeleteTags`, `DescribeAvailabilityZones`, `DescribeRouteTables`, `DescribeInternetGateways`, `DescribeNatGateways`, `DescribeVpcEndpoints` | All required for VoiceAgentNetwork stack. Confirmed `DescribeAvailabilityZones` is denied today (seen during fork PR#1 synth). |
| `lambda` | Get/Update/Invoke on `medcloud-voice-api` only | `CreateFunction`, `DeleteFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `AddPermission`, `RemovePermission`, `TagResource`, `UntagResource`, `ListFunctions`, `GetPolicy` — scoped to `voice-agent-*` function names | VoiceAgentBotRunner creates a new Lambda. |
| `s3` | Get/Put/List on two specific buckets | `CreateBucket`, `DeleteBucket`, `PutBucketPolicy`, `GetBucketPolicy`, `PutBucketVersioning`, `PutEncryptionConfiguration`, `PutBucketPublicAccessBlock`, `PutBucketLifecycleConfiguration`, `PutBucketTagging`, `ListAllMyBuckets` — scoped to `cdk-*` and `voice-agent-*` bucket name patterns | CDK creates its own assets bucket during bootstrap + may create additional buckets in Phase 7 for recordings. |
| `bedrock` | Invoke*, ListFoundationModels, GetFoundationModel | `ListInferenceProfiles`, `GetInferenceProfile`, `GetFoundationModelAvailability` | Nice-to-have for diagnostics. Runtime invocation already works. |
| `cloudwatch` | Metrics + alarms | `PutDashboard`, `GetDashboard`, `ListDashboards`, `DeleteDashboards` | VoiceAgentEcs creates a dashboard |
| `logs` | Read/write on `*voice*` log groups | Read/write on `/aws/lambda/voice-agent-*` + `/ecs/voice-agent*` — expand the wildcard or add a second statement for the ECS-path log groups. Also add `logs:StartQuery`, `logs:GetQueryResults`, `logs:StopQuery`, `logs:DescribeQueries` for CloudWatch Logs Insights | |
| `sns` | Create/Subscribe/Publish on `medcloud-voice-*` | Also add `voice-agent-*` prefix if the new CDK-named topics don't match the `medcloud-` prefix | Should double-check what the CDK stacks name their topics at deploy time |
| `sqs` | Create/Send/etc. on `medcloud-voice-*` | Expand the prefix to include `voice-agent-*` for any new CDK-named queues | |

### 3.3 Services currently `Deny`ed that must be unblocked

These three `Deny` statements inside `ProtectIAMAndCognito` block deploys:

| Deny'd action | Unblock strategy |
|---|---|
| `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, etc. | Narrow the Deny to exclude roles with path prefix `/cdk-*` and `/voice-agent-*`, OR remove the Deny and rely on Allow absence to protect. CDK bootstrap requires unrestricted CreateRole on its own path. |
| `apigateway:*` + `apigatewayv2:*` | Narrow by resource ARN (e.g. only Deny on specific existing prod API Gateways), OR remove and add an `Allow` for new voice-agent API Gateway ARNs. |
| `rds:*` | Phase 7 only; if RDS migration is out of scope for now, leave the Deny and revisit when Phase 7 plan lands. Flag for future review. |

`ProtectRDSSecurityGroup`, `ProtectCoreLambdas`, `ProtectMainS3Bucket`,
`ProtectNonVoiceLogs` are narrow-resource denies that won't block the
migration — leave as-is.

## 4. Bedrock model access — already good

Current `BedrockLLM` allow + account-level model access verified via
live `InvokeModel` calls:

| Model | Inference profile ID | Status |
|---|---|---|
| Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | ✅ invocation works today |
| Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | ✅ invocation works today |

No Bedrock model-access request needed from Lakshay. Runtime code
already uses `us.anthropic.claude-haiku-4-5-20251001-v1:0` as the
default LLM model ID.

## 5. Recommended approach

### Option 1 — attach `AdministratorAccess` (recommended)

- **One action**: `aws iam attach-user-policy --user-name medcloud-voice-alex --policy-arn arn:aws:iam::aws:policy/AdministratorAccess`
- **Trade-off**: broad, but expected for an engineer doing
  infrastructure development. Removed at the end of the migration
  stabilization period.
- **Still need**: the three `Deny` statements in `MedCloudVoiceOnly`
  are evaluated ahead of `Allow` in IAM. `AdministratorAccess` does
  NOT override an explicit `Deny`. So Lakshay also needs to narrow
  or remove the `ProtectIAMAndCognito` denies for `iam:CreateRole`,
  `apigateway:*`, `apigatewayv2:*` (leave `rds:*` if Phase 7 is
  pending).

### Option 2 — update `MedCloudVoiceOnly` to v8 with the delta

- Add statements per section 3.1 and 3.2 above.
- Narrow the three denies per section 3.3.
- More audit-friendly. ~30 minutes of policy authoring vs 1 action.

### Option 3 — separate `voice-agent-deploy-role`

- Create a new IAM role with `AdministratorAccess`, assumable by
  `medcloud-voice-alex` via `sts:AssumeRole` with MFA or time-bound
  tokens.
- Most audit-defensible but requires tooling changes on our side
  (set `AWS_PROFILE` or `credential_source` per deploy session).
- Overkill for a dev deploy but worth considering for production
  deploys after the migration stabilizes.

## 6. Cleanup expectations

- Dev deploy created by this migration will be torn down with
  `cdk destroy` when we're done validating.
- If Option 1 is chosen, `AdministratorAccess` is removed at that
  point; `MedCloudVoiceOnly` remains as-is.
- If Option 2 is chosen, `MedCloudVoiceOnly` v8 stays — we'll need it
  for Phase 7+ work anyway (ongoing deploys, ECR pushes, DB migration,
  recording bucket creation).

## 7. Lakshay-pasteable request

Everything below the divider is meant to be copy-pasted into Slack or
an IAM change ticket. It's self-contained.

---

**To:** Lakshay
**From:** Alex
**Subject:** IAM expansion for voice-engine migration to AWS Fargate

Hey — we're migrating the voice engine from the current EC2 + Twilio
setup to a Fargate-based architecture in the same AWS account
(`825269749545`). Going to do it in our `cosentus-voice-engine`
repository, which is a fork of an AWS sample. This'll be CDK-based.

The existing IAM policy `MedCloudVoiceOnly` on my user `medcloud-voice-alex`
was scoped for the EC2+Lambda setup and is missing about 10 services
that CDK needs to deploy the Fargate stacks, plus has three `Deny`
statements that block CDK bootstrap.

**Simplest ask (my preference):**

Attach `AdministratorAccess` to `medcloud-voice-alex` temporarily for
the duration of the migration. **AND** narrow three existing `Deny`
statements in `MedCloudVoiceOnly` so that `AdministratorAccess` isn't
overridden:

1. `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`,
   `iam:PassRole`, `iam:CreateInstanceProfile`,
   `iam:AddRoleToInstanceProfile`, etc. — needed for CDK bootstrap +
   every CDK stack that creates a task/exec/lambda role. Suggest
   scoping the carve-out to role path prefix `/voice-agent-*` and
   `/cdk-*`.
2. `apigateway:*` + `apigatewayv2:*` — the CDK BotRunner stack
   creates a new API Gateway for the Daily.co webhook. Suggest
   allowing these on any ARN matching `voice-agent-*`.
3. `rds:*` — eventually needed if we migrate the database to Aurora
   here. Fine to defer this one until Phase 7.

**Outcome I'll produce:** five CDK stacks running a Fargate voice
engine that handles phone calls via Daily.co, using Deepgram +
ElevenLabs + Bedrock Claude. We'll `cdk destroy` the dev deploy when
we're done validating. I can send you the cdk.out/template output if
you want to see the exact resources being created.

**Cleanup commitment:** once the migration stabilizes (est. 4 weeks),
I'll ping you to remove `AdministratorAccess` and either go back to
`MedCloudVoiceOnly` with a new v8 that has only the long-term runtime
perms (no `CreateStack`, etc.) or move to a scoped `voice-agent-deploy-role`
pattern.

**Also flagging:** Bedrock model access for Claude Haiku 4.5 and
Sonnet 4.5 is already enabled at the account level — I verified with
live invoke calls. Nothing needed there.

Let me know if you'd rather go with Option 2 (update
`MedCloudVoiceOnly` to v8 with the specific deltas) instead of
Option 1 (`AdministratorAccess` + deny narrowing). Happy to write the
v8 policy document if that's your preference — ~30 statements, I've
got them mapped.

Thanks!

---

## 8. Verification after grant

Once Lakshay has updated perms, I'll re-run this probe to confirm
everything's unblocked before starting the deploy:

```bash
# From repo root
for s in cloudformation ecs ecr secretsmanager kms servicediscovery \
         dynamodb ssm ec2 iam apigateway logs lambda; do
  case $s in
    cloudformation) aws cloudformation list-stacks --region us-east-1 --max-items 1 ;;
    ecs)            aws ecs list-clusters --region us-east-1 ;;
    ecr)            aws ecr describe-repositories --region us-east-1 --max-items 1 ;;
    secretsmanager) aws secretsmanager list-secrets --region us-east-1 --max-items 1 ;;
    kms)            aws kms list-keys --region us-east-1 --limit 1 ;;
    servicediscovery) aws servicediscovery list-namespaces --region us-east-1 --max-items 1 ;;
    dynamodb)       aws dynamodb list-tables --region us-east-1 --max-items 1 ;;
    ssm)            aws ssm describe-parameters --region us-east-1 --max-items 1 ;;
    ec2)            aws ec2 describe-vpcs --region us-east-1 ;;
    iam)            aws iam get-user ;;
    apigateway)     aws apigateway get-rest-apis --region us-east-1 ;;
    logs)           aws logs describe-log-groups --region us-east-1 --limit 1 ;;
    lambda)         aws lambda list-functions --region us-east-1 --max-items 1 ;;
  esac > /dev/null 2>&1 && echo "  $s  OK" || echo "  $s  DENIED"
done
```

Expected: all 13 show `OK`. Once that's green, I'll proceed with the
baseline deploy work described in `docs/baseline-deploy-findings.md`
(to be created at end of deploy).
