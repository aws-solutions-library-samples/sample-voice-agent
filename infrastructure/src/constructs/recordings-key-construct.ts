import { Construct } from 'constructs';
import { RemovalPolicy, Stack } from 'aws-cdk-lib';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { SSM_PARAMS } from '../ssm-parameters';

/**
 * Props for RecordingsKeyConstruct.
 */
export interface RecordingsKeyConstructProps {
  /** Deployment environment (e.g. `poc`, `prod`) — used for the alias suffix. */
  environment: string;
  /**
   * Name of the IAM role that Daily.co assumes to write recordings to S3.
   * Manually managed (not in CDK) — exists in account today as
   * `daily-recordings-uploader`. We attach a CDK-managed policy to it so
   * the IAM-side allow for `kms:GenerateDataKey` is version-controlled.
   *
   * Defaults to `daily-recordings-uploader` if not provided.
   */
  dailyUploaderRoleName?: string;
  /**
   * Name of the IAM role used by `medcloud-voice-api` Lambda. Needs
   * `kms:Decrypt` on the recordings CMK so a future presigned-URL flow can
   * stream audio out of S3. Manually managed (not in CDK) — exists today
   * as `medcloud-lambda-role`.
   *
   * Defaults to `medcloud-lambda-role` if not provided.
   */
  voiceApiRoleName?: string;
}

/**
 * Customer-managed KMS key dedicated to S3 server-side encryption of voice
 * recordings (`s3://medcloud-voice-us-prod-825/voice-recordings/*`).
 *
 * **Why a dedicated key (not the secrets CMK):** Daily.co's writer role
 * lives outside our trust boundary (cross-account assume-role from Daily's
 * AWS account). Giving that role `kms:GenerateDataKey` on the secrets CMK
 * would also expand its reach over our Secrets Manager–stored API keys
 * (DAILY_API_KEY / DEEPGRAM_API_KEY / ELEVENLABS_API_KEY). Separate keys =
 * smaller blast radius. See
 * `docs/guides/recordings-sse-kms-runbook.md` for the full audit
 * rationale.
 *
 * **What this construct does NOT do:** flip the bucket-default
 * encryption. The recordings bucket (`medcloud-voice-us-prod-825`) is a
 * pre-existing manually-provisioned bucket and is not managed by CDK.
 * Setting its `BucketEncryption` is a one-shot ops change captured in the
 * runbook. This construct produces the CMK + IAM grants that make that
 * flip safe.
 *
 * **Order of operations (must hold):**
 * 1. CDK deploys this construct → CMK + IAM grants live.
 * 2. Daily console "Test upload" → confirms Daily's role can
 *    `GenerateDataKey` against the new CMK.
 * 3. Manual `aws s3api put-bucket-encryption` → flips bucket default to
 *    `aws:kms` pointing at the new CMK ARN, with `BucketKeyEnabled: true`.
 * 4. Real test call → recording lands; verify SSEKMSKeyId + CloudTrail.
 *
 * Skipping step 2 risks a silent recording outage at step 3.
 */
export class RecordingsKeyConstruct extends Construct {
  /** The CMK encrypting recordings. */
  public readonly key: kms.IKey;
  /** Managed policy granting Daily's writer role kms:GenerateDataKey + Decrypt. */
  public readonly dailyWritePolicy: iam.IManagedPolicy;
  /** Managed policy granting voice-api Lambda kms:Decrypt for future presign flow. */
  public readonly voiceApiReadPolicy: iam.IManagedPolicy;

  constructor(scope: Construct, id: string, props: RecordingsKeyConstructProps) {
    super(scope, id);

    const isProd = props.environment === 'prod';
    const dailyRoleName = props.dailyUploaderRoleName ?? 'daily-recordings-uploader';
    const voiceApiRoleName = props.voiceApiRoleName ?? 'medcloud-lambda-role';

    // --- 1. The CMK ---------------------------------------------------------
    // Rotation is mandatory for HIPAA posture. RemovalPolicy=RETAIN in prod
    // so a stack destroy never orphans encrypted recordings (KMS schedules
    // 7-30 day deletion; RETAIN avoids the timer entirely).
    const key = new kms.Key(this, 'Key', {
      description: `Voice recordings SSE-KMS CMK (${props.environment})`,
      alias: `voice-recordings-${props.environment}`,
      enableKeyRotation: true,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });
    this.key = key;

    // --- 2. Key policy: explicit grants for the two consumers --------------
    // The default key policy CDK emits has the "Enable IAM User Permissions"
    // root statement, which means IAM-side allows are sufficient for
    // same-account principals. We add explicit allow statements anyway
    // because:
    //   (a) it makes the key policy self-documenting (anyone reading the
    //       policy sees who is supposed to use the key),
    //   (b) it survives accidental over-pruning of the consumer's IAM
    //       policy,
    //   (c) cross-account assumed-role identities sometimes evaluate
    //       differently against IAM-only allows; explicit key policy
    //       grants are the authoritative path.
    const accountId = Stack.of(this).account;

    key.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowDailyRecordingsUploader',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ArnPrincipal(`arn:aws:iam::${accountId}:role/${dailyRoleName}`)],
        actions: ['kms:GenerateDataKey', 'kms:Decrypt', 'kms:DescribeKey'],
        resources: ['*'],
      })
    );

    key.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowVoiceApiLambdaDecrypt',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ArnPrincipal(`arn:aws:iam::${accountId}:role/${voiceApiRoleName}`)],
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: ['*'],
      })
    );

    // --- 3. IAM-side managed policies, attached to imported roles ----------
    // The two consumer roles are NOT managed by this CDK app. Rather than
    // mutating their inline policies (`fromRoleArn(..., { mutable: true })`),
    // we create dedicated CDK-managed policies and attach them by role
    // name. This keeps the CDK-owned KMS allow scope clearly separated
    // from the existing manual policies on those roles, and makes
    // rollback a single-resource delete.

    const dailyRole = iam.Role.fromRoleArn(
      this,
      'ImportedDailyUploaderRole',
      `arn:aws:iam::${accountId}:role/${dailyRoleName}`,
      { mutable: false }
    );

    const voiceApiRole = iam.Role.fromRoleArn(
      this,
      'ImportedVoiceApiRole',
      `arn:aws:iam::${accountId}:role/${voiceApiRoleName}`,
      { mutable: false }
    );

    this.dailyWritePolicy = new iam.ManagedPolicy(this, 'DailyRecordingsKmsWrite', {
      managedPolicyName: `voice-recordings-kms-write-${props.environment}`,
      description:
        'Grants the Daily.co recordings writer role kms:GenerateDataKey on the recordings CMK so SSE-KMS PutObject calls succeed.',
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['kms:GenerateDataKey', 'kms:Decrypt', 'kms:DescribeKey'],
          resources: [key.keyArn],
        }),
      ],
      roles: [dailyRole],
    });

    this.voiceApiReadPolicy = new iam.ManagedPolicy(this, 'VoiceApiRecordingsKmsRead', {
      managedPolicyName: `voice-recordings-kms-read-${props.environment}`,
      description:
        'Grants medcloud-voice-api Lambda kms:Decrypt on the recordings CMK for the future presigned-URL playback flow.',
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['kms:Decrypt', 'kms:DescribeKey'],
          resources: [key.keyArn],
        }),
      ],
      roles: [voiceApiRole],
    });

    // --- 4. SSM parameter --------------------------------------------------
    // Future consumers (presigned-URL flow in voice-api Lambda, plus the
    // manual `put-bucket-encryption` command in the runbook) read the CMK
    // ARN from here so we don't hard-code it anywhere.
    new ssm.StringParameter(this, 'RecordingsKeyArnParam', {
      parameterName: SSM_PARAMS.RECORDINGS_KEY_ARN,
      stringValue: key.keyArn,
      description: 'Voice recordings SSE-KMS CMK ARN',
    });
  }
}
