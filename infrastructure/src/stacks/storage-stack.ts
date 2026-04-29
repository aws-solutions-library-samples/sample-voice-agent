import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { VoiceAgentConfig } from '../config';
import { RecordingsKeyConstruct, SecretsConstruct } from '../constructs';

/**
 * Props for StorageStack
 */
export interface StorageStackProps extends cdk.StackProps {
  readonly config: VoiceAgentConfig;
}

/**
 * Storage infrastructure stack.
 *
 * Hosts:
 * - SecretsConstruct: Secrets Manager + dedicated CMK for external-API keys
 *   (Daily, Deepgram, ElevenLabs).
 * - RecordingsKeyConstruct: dedicated CMK for SSE-KMS on the recordings
 *   bucket (`medcloud-voice-us-prod-825/voice-recordings/*`). The bucket
 *   itself is unmanaged in CDK; the runbook in
 *   `docs/guides/recordings-sse-kms-runbook.md` covers the manual
 *   bucket-encryption flip that pairs with this CDK change.
 */
export class StorageStack extends cdk.Stack {
  /** Secrets construct containing KMS and Secrets Manager resources */
  public readonly secretsConstruct: SecretsConstruct;
  /** Recordings KMS construct: CMK + IAM grants for SSE-KMS on voice-recordings/* */
  public readonly recordingsKeyConstruct: RecordingsKeyConstruct;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Delegate to SecretsConstruct
    this.secretsConstruct = new SecretsConstruct(this, 'Secrets', {
      environment: config.environment,
      projectName: config.projectName,
    });

    // Recordings CMK (dedicated; not the secrets CMK — see construct doc).
    this.recordingsKeyConstruct = new RecordingsKeyConstruct(this, 'RecordingsKey', {
      environment: config.environment,
    });

    // CloudFormation outputs (for console visibility)
    new cdk.CfnOutput(this, 'ApiKeySecretArn', {
      value: this.secretsConstruct.apiKeySecret.secretArn,
      description: 'API Keys Secret ARN',
    });

    new cdk.CfnOutput(this, 'RecordingsKeyArn', {
      value: this.recordingsKeyConstruct.key.keyArn,
      description: 'Voice recordings SSE-KMS CMK ARN',
    });
  }
}
