import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { VoiceAgentConfig } from '../config';
import { SSM_PARAMS } from '../ssm-parameters';
import { SageMakerEndpointConstruct } from '../constructs';

/**
 * Props for SageMakerStack
 */
export interface SageMakerStackProps extends cdk.StackProps {
  readonly config: VoiceAgentConfig;
}

/**
 * SageMaker infrastructure stack.
 * Thin wrapper that delegates to SageMakerEndpointConstruct.
 *
 * Creates Deepgram STT and TTS model endpoints with:
 * - IAM execution role
 * - VPC-isolated endpoints
 * - CloudWatch alarms for monitoring
 *
 * Requires:
 * - Network stack deployed (VPC, security groups)
 * - Deepgram model package ARNs from AWS Marketplace subscription
 */
export class SageMakerStack extends cdk.Stack {
  /** SageMaker construct containing endpoint resources */
  public readonly sagemakerConstruct: SageMakerEndpointConstruct;

  constructor(scope: Construct, id: string, props: SageMakerStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Read network dependencies from SSM Parameters
    const vpcId = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.VPC_ID);
    const sagemakerSgId = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.SAGEMAKER_SG_ID);

    // Import VPC and security group
    const vpc = ec2.Vpc.fromLookup(this, 'ImportedVpc', { vpcId });
    const sagemakerSg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'ImportedSageMakerSG',
      sagemakerSgId
    );

    // Get Deepgram model package ARNs from context or environment
    // These are obtained after subscribing to Deepgram on AWS Marketplace:
    //   https://aws.amazon.com/marketplace/seller-profile?id=deepgram
    // After subscribing, find the model package ARN in the SageMaker console
    // or in the Marketplace subscription confirmation.
    const sttModelPackageArn =
      this.node.tryGetContext('deepgram:sttModelPackageArn') ||
      process.env.DEEPGRAM_STT_MODEL_PACKAGE_ARN ||
      'PLACEHOLDER-subscribe-to-deepgram-stt-on-marketplace';

    const ttsModelPackageArn =
      this.node.tryGetContext('deepgram:ttsModelPackageArn') ||
      process.env.DEEPGRAM_TTS_MODEL_PACKAGE_ARN ||
      'PLACEHOLDER-subscribe-to-deepgram-tts-on-marketplace';

    // Validate model ARNs are not placeholders in production
    if (config.environment === 'prod') {
      if (sttModelPackageArn.includes('PLACEHOLDER')) {
        throw new Error(
          'Production deployment requires valid Deepgram STT model package ARN. ' +
            'Set deepgram:sttModelPackageArn in context or DEEPGRAM_STT_MODEL_PACKAGE_ARN env var.'
        );
      }
      if (ttsModelPackageArn.includes('PLACEHOLDER')) {
        throw new Error(
          'Production deployment requires valid Deepgram TTS model package ARN. ' +
            'Set deepgram:ttsModelPackageArn in context or DEEPGRAM_TTS_MODEL_PACKAGE_ARN env var.'
        );
      }
    }

    // Delegate to SageMakerEndpointConstruct
    this.sagemakerConstruct = new SageMakerEndpointConstruct(this, 'Endpoints', {
      environment: config.environment,
      projectName: config.projectName,
      vpc,
      securityGroup: sagemakerSg,
      sttModelPackageArn,
      ttsModelPackageArn,
    });

    // SSM Parameters - created at stack level (not inside the construct)
    // to preserve the same logical IDs as the stub stack, avoiding
    // CloudFormation replacement conflicts during stub-to-real migration.
    new ssm.StringParameter(this, 'SttEndpointNameParam', {
      parameterName: SSM_PARAMS.STT_ENDPOINT_NAME,
      stringValue: this.sagemakerConstruct.sttEndpointName,
      description: 'Voice Agent STT Endpoint Name',
    });

    new ssm.StringParameter(this, 'TtsEndpointNameParam', {
      parameterName: SSM_PARAMS.TTS_ENDPOINT_NAME,
      stringValue: this.sagemakerConstruct.ttsEndpointName,
      description: 'Voice Agent TTS Endpoint Name',
    });

    // CloudFormation outputs (for console visibility)
    new cdk.CfnOutput(this, 'SttEndpointName', {
      value: this.sagemakerConstruct.sttEndpointName,
      description: 'Deepgram STT Endpoint Name',
    });

    new cdk.CfnOutput(this, 'TtsEndpointName', {
      value: this.sagemakerConstruct.ttsEndpointName,
      description: 'Deepgram TTS Endpoint Name',
    });

    new cdk.CfnOutput(this, 'SageMakerExecutionRoleArn', {
      value: this.sagemakerConstruct.executionRole.roleArn,
      description: 'SageMaker Execution Role ARN',
    });

    // =====================
    // Optional: Deepgram Flux STT Endpoint
    // =====================
    // Flux provides native turn detection and multilingual support.
    // Only deployed when a valid model package ARN is provided.
    const fluxSttModelPackageArn = config.fluxSttModelPackageArn;

    if (fluxSttModelPackageArn && !fluxSttModelPackageArn.includes('PLACEHOLDER')) {
      const resourcePrefix = `${config.projectName}-${config.environment}`;
      const fluxSttModelName = `${resourcePrefix}-flux-stt-model`;
      const fluxSttEndpointName = `${resourcePrefix}-flux-stt-endpoint`;

      const privateSubnets = vpc.selectSubnets({
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      });

      const fluxSttModel = new cdk.aws_sagemaker.CfnModel(this, 'FluxSttModel', {
        modelName: fluxSttModelName,
        executionRoleArn: this.sagemakerConstruct.executionRole.roleArn,
        enableNetworkIsolation: true,
        primaryContainer: {
          modelPackageName: fluxSttModelPackageArn,
        },
        vpcConfig: {
          securityGroupIds: [sagemakerSg.securityGroupId],
          subnets: privateSubnets.subnetIds,
        },
      });

      const fluxSttEndpointConfig = new cdk.aws_sagemaker.CfnEndpointConfig(
        this,
        'FluxSttEndpointConfig',
        {
          endpointConfigName: `${resourcePrefix}-flux-stt-config`,
          productionVariants: [
            {
              variantName: 'AllTraffic',
              modelName: fluxSttModelName,
              initialInstanceCount: config.environment === 'prod' ? 2 : 1,
              instanceType: 'ml.g6.2xlarge',
              initialVariantWeight: 1,
              modelDataDownloadTimeoutInSeconds: 3600,
            },
          ],
        }
      );
      fluxSttEndpointConfig.addDependency(fluxSttModel);

      const fluxSttEndpoint = new cdk.aws_sagemaker.CfnEndpoint(
        this,
        'FluxSttEndpoint',
        {
          endpointName: fluxSttEndpointName,
          endpointConfigName: fluxSttEndpointConfig.endpointConfigName!,
        }
      );
      fluxSttEndpoint.addDependency(fluxSttEndpointConfig);

      new ssm.StringParameter(this, 'FluxSttEndpointNameParam', {
        parameterName: SSM_PARAMS.FLUX_STT_ENDPOINT_NAME,
        stringValue: fluxSttEndpointName,
        description: 'Voice Agent Flux STT Endpoint Name (Deepgram Flux with native turn detection)',
      });

      new cdk.CfnOutput(this, 'FluxSttEndpointName', {
        value: fluxSttEndpointName,
        description: 'Deepgram Flux STT Endpoint Name',
      });
    }
  }
}
