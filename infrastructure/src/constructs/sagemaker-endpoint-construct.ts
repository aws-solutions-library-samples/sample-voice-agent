import { Construct } from 'constructs';
import { Duration } from 'aws-cdk-lib';
import * as sagemaker from 'aws-cdk-lib/aws-sagemaker';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';


/**
 * Props for SageMakerEndpointConstruct
 */
export interface SageMakerEndpointConstructProps {
  /** Deployment environment */
  environment: string;
  /** Project name for resource naming */
  projectName: string;
  /** VPC for endpoint placement */
  vpc: ec2.IVpc;
  /** Security group for SageMaker endpoints */
  securityGroup: ec2.ISecurityGroup;
  /** Deepgram STT model package ARN from AWS Marketplace */
  sttModelPackageArn: string;
  /** Deepgram TTS model package ARN from AWS Marketplace */
  ttsModelPackageArn: string;
}

/**
 * SageMaker endpoints construct for Deepgram STT and TTS models.
 *
 * Creates:
 * - IAM execution role with SageMaker and CloudWatch permissions
 * - STT model and endpoint (Deepgram Nova-3 for speech-to-text)
 * - TTS model and endpoint (Deepgram Aura for text-to-speech)
 * - CloudWatch alarms for latency and error monitoring
 *
 * Endpoints are deployed in private subnets for network isolation.
 * Outputs are stored in SSM Parameters for cross-stack reference.
 */
export class SageMakerEndpointConstruct extends Construct {
  /** STT endpoint name */
  public readonly sttEndpointName: string;
  /** TTS endpoint name */
  public readonly ttsEndpointName: string;
  /** SageMaker execution role */
  public readonly executionRole: iam.Role;
  /** STT endpoint */
  public readonly sttEndpoint: sagemaker.CfnEndpoint;
  /** TTS endpoint */
  public readonly ttsEndpoint: sagemaker.CfnEndpoint;

  constructor(scope: Construct, id: string, props: SageMakerEndpointConstructProps) {
    super(scope, id);

    const resourcePrefix = `${props.projectName}-${props.environment}`;
    const isProd = props.environment === 'prod';

    // Private subnets for endpoint placement
    const privateSubnets = props.vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    });

    // =====================
    // IAM Execution Role
    // =====================

    this.executionRole = new iam.Role(this, 'ExecutionRole', {
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com'),
      description: `SageMaker execution role for Deepgram endpoints - ${resourcePrefix}`,
    });

    // SageMaker base permissions
    this.executionRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSageMakerFullAccess')
    );

    // CloudWatch logging permissions
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchLogging',
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:DescribeLogStreams',
        ],
        resources: ['arn:aws:logs:*:*:log-group:/aws/sagemaker/*'],
      })
    );

    // ECR permissions for model containers
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ECRAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
        ],
        resources: ['*'],
      })
    );

    // VPC permissions for network-isolated endpoints
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'VPCNetworkAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'ec2:CreateNetworkInterface',
          'ec2:CreateNetworkInterfacePermission',
          'ec2:DeleteNetworkInterface',
          'ec2:DeleteNetworkInterfacePermission',
          'ec2:DescribeNetworkInterfaces',
          'ec2:DescribeVpcs',
          'ec2:DescribeDhcpOptions',
          'ec2:DescribeSubnets',
          'ec2:DescribeSecurityGroups',
        ],
        resources: ['*'],
      })
    );

    // =====================
    // STT Endpoint (Deepgram Nova-3)
    // =====================

    const sttModelName = `${resourcePrefix}-stt-model`;
    this.sttEndpointName = `${resourcePrefix}-stt-endpoint`;

    const sttModel = new sagemaker.CfnModel(this, 'SttModel', {
      modelName: sttModelName,
      executionRoleArn: this.executionRole.roleArn,
      enableNetworkIsolation: true,
      primaryContainer: {
        modelPackageName: props.sttModelPackageArn,
      },
      vpcConfig: {
        securityGroupIds: [props.securityGroup.securityGroupId],
        subnets: privateSubnets.subnetIds,
      },
    });

    const sttEndpointConfig = new sagemaker.CfnEndpointConfig(this, 'SttEndpointConfig', {
      endpointConfigName: `${resourcePrefix}-stt-config`,
      productionVariants: [
        {
          variantName: 'AllTraffic',
          modelName: sttModelName,
          initialInstanceCount: isProd ? 2 : 1,
          // Instance pools: SageMaker tries pools in priority order and falls
          // back automatically on InsufficientInstanceCapacity (ICE), instead
          // of failing the whole endpoint (and rolling back the stack).
          // Ordered by inference performance, not just availability - g6/g6e
          // (L4/L40S) and g5 (A10G) are comparable for STT; g4dn (T4) is the
          // last resort since it's the slowest of the supported types.
          instancePools: [
            { instanceType: 'ml.g6.2xlarge', priority: 1 },
            { instanceType: 'ml.g6e.2xlarge', priority: 2 },
            { instanceType: 'ml.g5.2xlarge', priority: 3 },
            { instanceType: 'ml.g4dn.2xlarge', priority: 4 },
          ],
          // Total time SageMaker spends falling back across pools before giving up.
          variantInstanceProvisionTimeoutInSeconds: 1200,
          initialVariantWeight: 1,
          modelDataDownloadTimeoutInSeconds: 3600,
        },
      ],
    });
    sttEndpointConfig.addDependency(sttModel);

    this.sttEndpoint = new sagemaker.CfnEndpoint(this, 'SttEndpoint', {
      endpointName: this.sttEndpointName,
      endpointConfigName: sttEndpointConfig.endpointConfigName!,
    });
    this.sttEndpoint.addDependency(sttEndpointConfig);

    // =====================
    // TTS Endpoint (Deepgram Aura)
    // =====================

    const ttsModelName = `${resourcePrefix}-tts-model`;
    this.ttsEndpointName = `${resourcePrefix}-tts-endpoint`;

    const ttsModel = new sagemaker.CfnModel(this, 'TtsModel', {
      modelName: ttsModelName,
      executionRoleArn: this.executionRole.roleArn,
      enableNetworkIsolation: true,
      primaryContainer: {
        modelPackageName: props.ttsModelPackageArn,
      },
      vpcConfig: {
        securityGroupIds: [props.securityGroup.securityGroupId],
        subnets: privateSubnets.subnetIds,
      },
    });

    const ttsEndpointConfig = new sagemaker.CfnEndpointConfig(this, 'TtsEndpointConfig', {
      endpointConfigName: `${resourcePrefix}-tts-config`,
      productionVariants: [
        {
          variantName: 'AllTraffic',
          modelName: ttsModelName,
          initialInstanceCount: isProd ? 2 : 1,
          // Instance pools: same rationale as the STT variant above. Ordered
          // by measured TTS performance (realtime factor ~0.93 on g6/g5/g6e
          // vs ~1.02 on g4dn), so g4dn is the last-resort fallback rather
          // than the primary or secondary choice.
          instancePools: [
            { instanceType: 'ml.g6.12xlarge', priority: 1 },
            { instanceType: 'ml.g6e.12xlarge', priority: 2 },
            { instanceType: 'ml.g5.12xlarge', priority: 3 },
            { instanceType: 'ml.g4dn.12xlarge', priority: 4 },
          ],
          variantInstanceProvisionTimeoutInSeconds: 1200,
          initialVariantWeight: 1,
          modelDataDownloadTimeoutInSeconds: 3600,
        },
      ],
    });
    ttsEndpointConfig.addDependency(ttsModel);

    this.ttsEndpoint = new sagemaker.CfnEndpoint(this, 'TtsEndpoint', {
      endpointName: this.ttsEndpointName,
      endpointConfigName: ttsEndpointConfig.endpointConfigName!,
    });
    this.ttsEndpoint.addDependency(ttsEndpointConfig);

    // =====================
    // CloudWatch Alarms
    // =====================

    // STT Latency Alarm (P95 > 300ms)
    new cloudwatch.Alarm(this, 'SttLatencyAlarm', {
      alarmName: `${resourcePrefix}-stt-latency`,
      alarmDescription: 'STT endpoint latency exceeds 300ms at P95',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/SageMaker',
        metricName: 'ModelLatency',
        dimensionsMap: {
          EndpointName: this.sttEndpointName,
          VariantName: 'AllTraffic',
        },
        statistic: 'p95',
        period: Duration.minutes(1),
      }),
      threshold: 300000, // 300ms in microseconds
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // STT Error Rate Alarm (> 5%)
    new cloudwatch.Alarm(this, 'SttErrorAlarm', {
      alarmName: `${resourcePrefix}-stt-errors`,
      alarmDescription: 'STT endpoint error rate exceeds 5%',
      metric: new cloudwatch.MathExpression({
        expression: '(errors / invocations) * 100',
        usingMetrics: {
          errors: new cloudwatch.Metric({
            namespace: 'AWS/SageMaker',
            metricName: 'Invocation5XXErrors',
            dimensionsMap: {
              EndpointName: this.sttEndpointName,
              VariantName: 'AllTraffic',
            },
            statistic: 'Sum',
            period: Duration.minutes(5),
          }),
          invocations: new cloudwatch.Metric({
            namespace: 'AWS/SageMaker',
            metricName: 'Invocations',
            dimensionsMap: {
              EndpointName: this.sttEndpointName,
              VariantName: 'AllTraffic',
            },
            statistic: 'Sum',
            period: Duration.minutes(5),
          }),
        },
        period: Duration.minutes(5),
      }),
      threshold: 5,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // TTS Latency Alarm (P95 > 500ms)
    new cloudwatch.Alarm(this, 'TtsLatencyAlarm', {
      alarmName: `${resourcePrefix}-tts-latency`,
      alarmDescription: 'TTS endpoint latency exceeds 500ms at P95',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/SageMaker',
        metricName: 'ModelLatency',
        dimensionsMap: {
          EndpointName: this.ttsEndpointName,
          VariantName: 'AllTraffic',
        },
        statistic: 'p95',
        period: Duration.minutes(1),
      }),
      threshold: 500000, // 500ms in microseconds
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // TTS Error Rate Alarm (> 5%)
    new cloudwatch.Alarm(this, 'TtsErrorAlarm', {
      alarmName: `${resourcePrefix}-tts-errors`,
      alarmDescription: 'TTS endpoint error rate exceeds 5%',
      metric: new cloudwatch.MathExpression({
        expression: '(errors / invocations) * 100',
        usingMetrics: {
          errors: new cloudwatch.Metric({
            namespace: 'AWS/SageMaker',
            metricName: 'Invocation5XXErrors',
            dimensionsMap: {
              EndpointName: this.ttsEndpointName,
              VariantName: 'AllTraffic',
            },
            statistic: 'Sum',
            period: Duration.minutes(5),
          }),
          invocations: new cloudwatch.Metric({
            namespace: 'AWS/SageMaker',
            metricName: 'Invocations',
            dimensionsMap: {
              EndpointName: this.ttsEndpointName,
              VariantName: 'AllTraffic',
            },
            statistic: 'Sum',
            period: Duration.minutes(5),
          }),
        },
        period: Duration.minutes(5),
      }),
      threshold: 5,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // NOTE: SSM parameters are created at the stack level (sagemaker-stack.ts)
    // to maintain the same logical IDs as the stub stack, avoiding CloudFormation
    // resource replacement conflicts during the stub-to-real migration.
  }
}
