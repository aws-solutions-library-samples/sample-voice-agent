import { Construct } from 'constructs';
import { RemovalPolicy } from 'aws-cdk-lib';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { SSM_PARAMS } from '../ssm-parameters';

/**
 * Props for SecretsConstruct
 */
export interface SecretsConstructProps {
  /** Deployment environment */
  environment: string;
  /** Project name for resource naming */
  projectName: string;
}

/**
 * Secrets infrastructure construct.
 * Creates Secrets Manager secrets for API keys with KMS encryption.
 *
 * Outputs are stored in SSM Parameters for cross-stack reference.
 */
export class SecretsConstruct extends Construct {
  /** Secret containing API keys */
  public readonly apiKeySecret: secretsmanager.ISecret;
  /** KMS key for secret encryption */
  public readonly encryptionKey: kms.IKey;

  constructor(scope: Construct, id: string, props: SecretsConstructProps) {
    super(scope, id);

    const isProd = props.environment === 'prod';

    // KMS key for encrypting secrets
    this.encryptionKey = new kms.Key(this, 'SecretsKey', {
      description: `KMS key for Voice Agent secrets encryption - ${props.environment}`,
      enableKeyRotation: true,
      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    // Secret for API keys (Daily, Deepgram, ElevenLabs)
    // Actual values populated manually after deployment
    this.apiKeySecret = new secretsmanager.Secret(this, 'ApiKeySecret', {
      description: `API keys for external services (Daily, Deepgram, ElevenLabs) - ${props.environment}`,
      encryptionKey: this.encryptionKey,
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          DAILY_API_KEY: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
          DEEPGRAM_API_KEY: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
          ELEVENLABS_API_KEY: 'PLACEHOLDER_REPLACE_AFTER_DEPLOY',
        }),
        generateStringKey: 'generated_field', // Required but unused
      },
    });

    // Store outputs in SSM Parameters for cross-stack reference
    new ssm.StringParameter(this, 'ApiKeySecretArnParam', {
      parameterName: SSM_PARAMS.API_KEY_SECRET_ARN,
      stringValue: this.apiKeySecret.secretArn,
      description: 'Voice Agent API Keys Secret ARN',
    });

    new ssm.StringParameter(this, 'EncryptionKeyArnParam', {
      parameterName: SSM_PARAMS.ENCRYPTION_KEY_ARN,
      stringValue: this.encryptionKey.keyArn,
      description: 'Voice Agent KMS Encryption Key ARN',
    });
  }
}
