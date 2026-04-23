import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as appscaling from 'aws-cdk-lib/aws-applicationautoscaling';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as servicediscovery from 'aws-cdk-lib/aws-servicediscovery';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import { VoiceAgentConfig } from '../config';
import { SSM_PARAMS } from '../ssm-parameters';
import {
  VoiceAgentMonitoringConstruct,
  SessionTableConstruct,
  SessionCounterLambdaConstruct,
} from '../constructs';

/**
 * Props for EcsStack
 */
export interface EcsStackProps extends cdk.StackProps {
  readonly config: VoiceAgentConfig;
}

/**
 * ECS infrastructure stack for Pipecat voice pipeline.
 *
 * Creates:
 * - ECS Fargate Service (always-on) for running voice agent containers
 * - Network Load Balancer for routing call requests
 * - Task definition with proper permissions
 * - ECR image asset (built during cdk deploy)
 * - IAM execution and task roles
 *
 * Architecture:
 * - ECS Service keeps 1+ containers running at all times (no cold starts)
 * - Containers run HTTP server that accepts call configurations
 * - NLB routes incoming call requests to available containers
 * - Tasks connect to Daily rooms via WebRTC
 * - Tasks use Bedrock for LLM, ElevenLabs/Deepgram for TTS/STT
 */
export class EcsStack extends cdk.Stack {
  /** ECS cluster for voice agent tasks */
  public readonly cluster: ecs.Cluster;
  /** Task definition for voice agent containers */
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  /** ECS Service (always-on) */
  public readonly service: ecs.FargateService;
  /** Network Load Balancer for routing requests */
  public readonly loadBalancer: elbv2.NetworkLoadBalancer;
  /** Container image URI */
  public readonly imageUri: string;
  /** Cluster ARN */
  public readonly clusterArn: string;
  /** Task definition ARN */
  public readonly taskDefinitionArn: string;
  /** Security group for ECS tasks */
  public readonly taskSecurityGroup: ec2.SecurityGroup;
  /** Service endpoint URL */
  public readonly serviceEndpoint: string;
  /** CloudMap HTTP namespace for A2A capability discovery */
  public readonly capabilityNamespace: servicediscovery.HttpNamespace;

  constructor(scope: Construct, id: string, props: EcsStackProps) {
    super(scope, id, props);

    const { config } = props;
    const resourcePrefix = `${config.projectName}-${config.environment}`;

    // Read dependencies from SSM Parameters
    const vpcId = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.VPC_ID);
    const _privateSubnetIdsStr = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.PRIVATE_SUBNET_IDS
    );
    const apiKeySecretArn = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.API_KEY_SECRET_ARN
    );
    const encryptionKeyArn = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.ENCRYPTION_KEY_ARN
    );
    // CRM and Knowledge Base are optional capability agents discovered via A2A/CloudMap.
    // They deploy independently and register themselves into the CloudMap namespace.
    // The voice agent discovers them automatically -- no SSM lookups needed here.

    // Transfer destination is configured directly via config (CDK context or env var).
    // No SSM lookup is needed -- the voice agent's capability-based tool system
    // automatically enables the transfer tool only when TRANSFER_DESTINATION is set.
    const transferDestination = config.transferDestination;

    // SageMaker endpoint names for Deepgram STT/TTS (optional - only needed when using SageMaker providers)
    const sttEndpointName = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.STT_ENDPOINT_NAME);
    const ttsEndpointName = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.TTS_ENDPOINT_NAME);

    // SageMaker security group for VPC endpoint access
    const sagemakerSgId = ssm.StringParameter.valueFromLookup(this, SSM_PARAMS.SAGEMAKER_SG_ID);
    const vpcEndpointSgId = ssm.StringParameter.valueFromLookup(
      this,
      SSM_PARAMS.VPC_ENDPOINT_SG_ID
    );

    // Import VPC
    const vpc = ec2.Vpc.fromLookup(this, 'ImportedVpc', { vpcId });

    // =====================
    // Security Group
    // =====================
    // ECS tasks need:
    // - Inbound: Port 8080 from NLB for call requests
    // - Outbound: Daily WebRTC, Bedrock, ElevenLabs, Deepgram APIs
    this.taskSecurityGroup = new ec2.SecurityGroup(this, 'TaskSecurityGroup', {
      vpc,
      description: `Security group for Voice Agent ECS Service - ${resourcePrefix}`,
      allowAllOutbound: true, // Required for WebRTC and API calls
    });

    // Allow inbound from anywhere (NLB doesn't have security groups in TCP mode)
    this.taskSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(8080),
      'Allow HTTP from NLB'
    );

    // Allow ECS tasks to reach SageMaker endpoints via VPC endpoint
    // Required for SageMaker BiDi streaming (Deepgram STT/TTS on SageMaker)
    const sagemakerSg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'ImportedSageMakerSG',
      sagemakerSgId
    );
    sagemakerSg.addIngressRule(
      this.taskSecurityGroup,
      ec2.Port.tcp(443),
      'Allow HTTPS from ECS tasks to SageMaker endpoints'
    );
    sagemakerSg.addIngressRule(
      this.taskSecurityGroup,
      ec2.Port.tcp(8443),
      'Allow BiDi streaming (HTTP/2) from ECS tasks to SageMaker endpoints'
    );

    // Allow ECS tasks to reach VPC interface endpoints (SageMaker Runtime, etc.)
    const vpcEndpointSg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'ImportedVpcEndpointSG',
      vpcEndpointSgId
    );
    vpcEndpointSg.addIngressRule(
      this.taskSecurityGroup,
      ec2.Port.tcp(443),
      'Allow HTTPS from ECS tasks to VPC endpoints'
    );
    vpcEndpointSg.addIngressRule(
      this.taskSecurityGroup,
      ec2.Port.tcp(8443),
      'Allow BiDi streaming (HTTP/2) from ECS tasks to VPC endpoints'
    );

    // =====================
    // ECS Cluster
    // =====================
    this.cluster = new ecs.Cluster(this, 'PipecatCluster', {
      clusterName: `${resourcePrefix}-cluster`,
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
      enableFargateCapacityProviders: true,
    });

    this.clusterArn = this.cluster.clusterArn;

    // =====================
    // CloudMap HTTP Namespace (A2A Capability Discovery)
    // =====================
    // HTTP namespace for registering capability agents.
    // Agents register themselves on startup; voice agent discovers them
    // via servicediscovery:DiscoverInstances at runtime.
    this.capabilityNamespace = new servicediscovery.HttpNamespace(this, 'CapabilityNamespace', {
      name: `${resourcePrefix}-capabilities`,
      description: `A2A capability agent discovery namespace - ${resourcePrefix}`,
    });

    // =====================
    // Docker Image Build
    // =====================
    // The main Dockerfile expects BASE_IMAGE to be a pre-baked image with the
    // Python venv already installed (see backend/voice-agent/Dockerfile.base
    // and infrastructure/scripts/build-base-image.sh). This lets us skip the
    // ~8 min pip install on every code-only deploy.
    //
    // Resolution order for BASE_IMAGE:
    //   1. process.env.VOICE_AGENT_BASE_IMAGE  (set by deploy.sh after calling
    //      build-base-image.sh --uri-only)
    //   2. Computed from sha256 of requirements.txt + Dockerfile.base, assuming
    //      the hash-tagged image already exists in ECR at
    //      ${account}.dkr.ecr.${region}.amazonaws.com/voice-agent-base:req-<hash>
    //
    // If neither gives a usable image, `cdk deploy` will fail with a Docker
    // pull error that names the missing tag — surface the instruction to run
    // build-base-image.sh.
    const voiceAgentDir = path.join(__dirname, '..', '..', '..', 'backend', 'voice-agent');
    const baseImageUri = resolveBaseImageUri(voiceAgentDir, cdk.Stack.of(this));

    const containerImage = new ecr_assets.DockerImageAsset(this, 'PipecatImage', {
      directory: voiceAgentDir,
      platform: ecr_assets.Platform.LINUX_AMD64,
      buildArgs: {
        ENVIRONMENT: config.environment,
        BASE_IMAGE: baseImageUri,
      },
    });

    this.imageUri = containerImage.imageUri;

    // =====================
    // IAM Roles
    // =====================

    // Task execution role (used by ECS to pull images, write logs)
    const executionRole = new iam.Role(this, 'TaskExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
      ],
    });

    // Grant ECR pull permissions
    containerImage.repository.grantPull(executionRole);

    // Task role (used by the container application)
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: `Task role for Voice Agent ECS containers - ${resourcePrefix}`,
    });

    // Bedrock permissions - include both foundation models and inference profiles
    // Note: Inference profiles use cross-region inference, so we need to allow
    // multiple US regions (us-east-1, us-east-2, us-west-2) for the foundation models
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockInvokeModel',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: [
          // Foundation models - allow all US regions for cross-region inference
          'arn:aws:bedrock:us-*::foundation-model/anthropic.claude-3-5-haiku-*',
          'arn:aws:bedrock:us-*::foundation-model/anthropic.claude-3-haiku-*',
          'arn:aws:bedrock:us-*::foundation-model/anthropic.claude-haiku-4-5-*',
          // Inference profiles (required for on-demand throughput)
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.claude-*`,
        ],
      })
    );

    // Secrets Manager permissions
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SecretsManagerRead',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [apiKeySecretArn],
      })
    );

    // KMS permissions
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'KMSDecrypt',
        effect: iam.Effect.ALLOW,
        actions: ['kms:Decrypt'],
        resources: [encryptionKeyArn],
      })
    );

    // =====================
    // Session Tracking Table
    // =====================
    const sessionTable = new SessionTableConstruct(this, 'SessionTable', {
      environment: config.environment,
      projectName: config.projectName,
    });

    // Grant DynamoDB permissions to task role
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DynamoDBSessionTracking',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:GetItem', 'dynamodb:Query'],
        resources: [sessionTable.tableArn, `${sessionTable.tableArn}/index/*`],
      })
    );

    // =====================
    // Knowledge Base (RAG) Permissions
    // =====================
    // KB is an optional capability agent deployed independently.
    // Grant broad Bedrock KB permissions so the voice agent can call any KB
    // discovered via A2A -- no SSM lookup on the KB stack needed.
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockKBRetrieve',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate'],
        resources: [`arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/*`],
      })
    );

    // Grant SSM Parameter Store read permissions
    // Container reads all configuration from SSM at startup
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SSMReadConfig',
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter', 'ssm:GetParameters'],
        resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/voice-agent/*`],
      })
    );

    // CloudMap Service Discovery permissions for A2A capability registry
    // Voice agent discovers capability agents at runtime via CloudMap
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ServiceDiscoveryRead',
        effect: iam.Effect.ALLOW,
        actions: [
          'servicediscovery:DiscoverInstances',
          'servicediscovery:ListServices',
          'servicediscovery:ListNamespaces',
        ],
        resources: ['*'], // DiscoverInstances does not support resource-level permissions
      })
    );

    // SageMaker permissions for Deepgram STT/TTS BiDi streaming endpoints
    // Required when STT_PROVIDER=sagemaker or TTS_PROVIDER=sagemaker
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SageMakerInvokeEndpoint',
        effect: iam.Effect.ALLOW,
        actions: [
          'sagemaker:InvokeEndpoint',
          'sagemaker:InvokeEndpointWithResponseStream',
          'sagemaker:InvokeEndpointWithBidirectionalStream',
        ],
        resources: [
          `arn:aws:sagemaker:${this.region}:${this.account}:endpoint/${sttEndpointName}`,
          `arn:aws:sagemaker:${this.region}:${this.account}:endpoint/${ttsEndpointName}`,
        ],
      })
    );

    // ECS Task Scale-in Protection permissions
    // Required for $ECS_AGENT_URI/task-protection/v1/state API
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ECSTaskProtection',
        effect: iam.Effect.ALLOW,
        actions: ['ecs:GetTaskProtection', 'ecs:UpdateTaskProtection'],
        resources: ['*'], // Task protection does not support resource-level permissions
      })
    );

    // =====================
    // CloudWatch Log Group
    // =====================
    const logGroup = new logs.LogGroup(this, 'TaskLogGroup', {
      logGroupName: `/ecs/${resourcePrefix}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // =====================
    // Task Definition
    // =====================
    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'TaskDefinition', {
      family: `${resourcePrefix}-voice-agent`,
      cpu: 4096, // 4 vCPU — headroom for up to 10 concurrent calls
      memoryLimitMiB: 8192, // 8 GB (Fargate minimum for 4 vCPU)
      executionRole,
      taskRole,
      runtimePlatform: {
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
      },
    });

    this.taskDefinitionArn = this.taskDefinition.taskDefinitionArn;

    // Container definition with HTTP port for receiving call requests
    // Configuration is loaded from SSM Parameter Store at startup
    const container = this.taskDefinition.addContainer('PipecatContainer', {
      containerName: 'voice-agent',
      image: ecs.ContainerImage.fromDockerImageAsset(containerImage),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'voice-agent',
        logGroup,
      }),
      essential: true,
      stopTimeout: cdk.Duration.seconds(120),
      environment: {
        // Minimal env vars - everything else loaded from SSM
        AWS_REGION: this.region,
        ENVIRONMENT: config.environment,
        // Secrets Manager ARN for API keys
        API_KEY_SECRET_ARN: apiKeySecretArn,
        // Service mode configuration
        SERVICE_MODE: 'true',
        SERVICE_PORT: '8080',
        // CRM API for customer data
        // Transfer destination for SIP REFER (optional)
        // Only set when configured -- the capability-based tool system
        // automatically registers the transfer tool when this is present
        ...(transferDestination ? { TRANSFER_DESTINATION: transferDestination } : {}),
        // STT/TTS provider: use cloud APIs when SageMaker endpoints are stubs
        STT_PROVIDER: sttEndpointName.includes('cloud-api-mode') ? 'deepgram' : 'sagemaker',
        TTS_PROVIDER: ttsEndpointName.includes('cloud-api-mode') ? 'elevenlabs' : 'sagemaker',
        STT_ENDPOINT_NAME: sttEndpointName,
        TTS_ENDPOINT_NAME: ttsEndpointName,
        // A2A capability discovery namespace
        A2A_NAMESPACE: this.capabilityNamespace.namespaceName,
        // Auto-scaling: max concurrent calls per container
        MAX_CONCURRENT_CALLS: String(config.sessionCapacityPerTask),
      },
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8080/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        // Reduced from 30s: app typically initializes in 3-8s, 10s provides adequate buffer
        startPeriod: cdk.Duration.seconds(10),
      },
    });

    // Expose port 8080 for HTTP requests
    container.addPortMappings({
      containerPort: 8080,
      protocol: ecs.Protocol.TCP,
    });

    // =====================
    // Network Load Balancer
    // =====================
    this.loadBalancer = new elbv2.NetworkLoadBalancer(this, 'ServiceNLB', {
      vpc,
      internetFacing: false, // Internal only - Lambda calls it
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      crossZoneEnabled: true,
    });

    // Target group for ECS Service
    const targetGroup = new elbv2.NetworkTargetGroup(this, 'ServiceTargetGroup', {
      vpc,
      port: 8080,
      protocol: elbv2.Protocol.TCP,
      targetType: elbv2.TargetType.IP,
      deregistrationDelay: cdk.Duration.seconds(300),
      healthCheck: {
        enabled: true,
        protocol: elbv2.Protocol.HTTP,
        path: '/ready',
        // NLB minimum interval is 10s; timeout fixed at 6s for HTTP.
        // 2 healthy checks x 10s = 20s to receive traffic after /ready 200.
        interval: cdk.Duration.seconds(10),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 2,
      },
    });

    // Listener on port 80
    this.loadBalancer.addListener('ServiceListener', {
      port: 80,
      defaultTargetGroups: [targetGroup],
    });

    this.serviceEndpoint = `http://${this.loadBalancer.loadBalancerDnsName}`;

    // =====================
    // ECS Service (Always-On)
    // =====================
    this.service = new ecs.FargateService(this, 'PipecatService', {
      serviceName: `${resourcePrefix}-service`,
      cluster: this.cluster,
      taskDefinition: this.taskDefinition,
      desiredCount: config.minCapacity, // Use minCapacity instead of hardcoded 1
      securityGroups: [this.taskSecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
      enableExecuteCommand: true,
      circuitBreaker: {
        enable: true,
        rollback: true,
      },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    // Register service with target group
    this.service.attachToNetworkTargetGroup(targetGroup);

    // =====================
    // Auto-Scaling
    // =====================
    const scalableTarget = new appscaling.ScalableTarget(this, 'VoiceAgentScaling', {
      serviceNamespace: appscaling.ServiceNamespace.ECS,
      resourceId: `service/${this.cluster.clusterName}/${this.service.serviceName}`,
      scalableDimension: 'ecs:service:DesiredCount',
      minCapacity: config.minCapacity,
      maxCapacity: config.maxCapacity,
    });

    // Target tracking policy: scale out based on average sessions per task.
    // Uses the fleet-wide average (SessionsPerTask) so the metric naturally
    // decreases as new tasks come online with 0 sessions. This prevents
    // overshoot that occurs with MaxSessionsPerTask, which stays high because
    // existing sessions are sticky and don't redistribute to new tasks.
    // disableScaleIn: true -- scale-in is managed by step scaling policy + task protection

    // MaxSessionsPerTask: hottest single task — retained for dashboard/alarms.
    const _maxSessionsPerTaskMetric = new cloudwatch.Metric({
      namespace: 'VoiceAgent/Sessions',
      metricName: 'MaxSessionsPerTask',
      dimensionsMap: { Environment: config.environment },
      statistic: 'Average',
      period: cdk.Duration.minutes(1),
    });

    // AvgSessionsPerTask (SessionsPerTask): fleet-wide average — used for
    // both scale-out (target tracking) and scale-in (step scaling).
    const avgSessionsPerTaskMetric = new cloudwatch.Metric({
      namespace: 'VoiceAgent/Sessions',
      metricName: 'SessionsPerTask',
      dimensionsMap: { Environment: config.environment },
      statistic: 'Average',
      period: cdk.Duration.minutes(1),
    });

    scalableTarget.scaleToTrackMetric('SessionsPerTaskTracking', {
      customMetric: avgSessionsPerTaskMetric,
      targetValue: config.targetSessionsPerTask,
      scaleOutCooldown: cdk.Duration.seconds(60),
      scaleInCooldown: cdk.Duration.seconds(300),
      disableScaleIn: true,
    });

    // Step scaling policy: scale-in when sessions drop
    // Uses AvgSessionsPerTask (fleet average) so we only remove capacity
    // when the whole fleet is underutilized, not just because one task is idle.
    // Safe because ECS Task Scale-in Protection prevents termination of tasks
    // with active calls -- only idle (unprotected) tasks are removed.
    //
    // CDK step scaling for scale-in: the alarm threshold anchors at the
    // "upper" boundary of the highest step. Steps define removal magnitudes
    // at increasing distance below that anchor.
    //
    // Alarm: SessionsPerTask avg < 1.0 for 2 consecutive periods → remove 3 tasks.
    scalableTarget.scaleOnMetric('ScaleIn', {
      metric: avgSessionsPerTaskMetric,
      scalingSteps: [
        { upper: 1, change: 0 }, // Between threshold (1.0) and above: no action
        { upper: 0, change: -3 }, // Below 1.0: remove up to 3 idle tasks
      ],
      adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
      cooldown: cdk.Duration.seconds(30), // Fast cooldown for testing
      evaluationPeriods: 2, // Must be low for 2 consecutive periods before scaling in
    });

    // =====================
    // Monitoring (CloudWatch Alarms & Dashboard)
    // =====================
    const monitoring = new VoiceAgentMonitoringConstruct(this, 'Monitoring', {
      environment: config.environment,
      projectName: config.projectName,
      cluster: this.cluster,
      serviceName: this.service.serviceName,
      enableNotifications: config.environment === 'prod',
      notificationEmails: [], // Add emails in production config
      targetSessionsPerTask: config.targetSessionsPerTask,
      sessionCapacityPerTask: config.sessionCapacityPerTask,
      targetGroupFullName: targetGroup.targetGroupFullName,
      loadBalancerFullName: this.loadBalancer.loadBalancerFullName,
      taskLogGroup: logGroup,
    });

    // =====================
    // Session Counter Lambda
    // =====================
    new SessionCounterLambdaConstruct(this, 'SessionCounter', {
      environment: config.environment,
      projectName: config.projectName,
      sessionTable: sessionTable.table,
    });

    // =====================
    // SSM Parameters
    // =====================
    new ssm.StringParameter(this, 'ClusterArnParam', {
      parameterName: SSM_PARAMS.ECS_CLUSTER_ARN,
      stringValue: this.clusterArn,
      description: 'Voice Agent ECS Cluster ARN',
    });

    new ssm.StringParameter(this, 'TaskDefinitionArnParam', {
      parameterName: SSM_PARAMS.ECS_TASK_DEFINITION_ARN,
      stringValue: this.taskDefinitionArn,
      description: 'Voice Agent ECS Task Definition ARN',
    });

    new ssm.StringParameter(this, 'TaskSecurityGroupIdParam', {
      parameterName: SSM_PARAMS.ECS_TASK_SG_ID,
      stringValue: this.taskSecurityGroup.securityGroupId,
      description: 'Voice Agent ECS Task Security Group ID',
    });

    // New: Service endpoint for Lambda to call
    new ssm.StringParameter(this, 'ServiceEndpointParam', {
      parameterName: SSM_PARAMS.ECS_SERVICE_ENDPOINT,
      stringValue: this.serviceEndpoint,
      description: 'Voice Agent ECS Service HTTP Endpoint',
    });

    // Monitoring dashboard name
    new ssm.StringParameter(this, 'DashboardNameParam', {
      parameterName: SSM_PARAMS.MONITORING_DASHBOARD_NAME,
      stringValue: monitoring.dashboardName,
      description: 'Voice Agent CloudWatch Dashboard Name',
    });

    // A2A Capability Namespace
    new ssm.StringParameter(this, 'A2ANamespaceIdParam', {
      parameterName: SSM_PARAMS.A2A_NAMESPACE_ID,
      stringValue: this.capabilityNamespace.namespaceId,
      description: 'CloudMap HTTP Namespace ID for A2A capability discovery',
    });

    new ssm.StringParameter(this, 'A2ANamespaceNameParam', {
      parameterName: SSM_PARAMS.A2A_NAMESPACE_NAME,
      stringValue: this.capabilityNamespace.namespaceName,
      description: 'CloudMap HTTP Namespace Name for A2A capability discovery',
    });

    // =====================
    // Outputs
    // =====================
    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.cluster.clusterName,
      description: 'ECS Cluster Name',
    });

    new cdk.CfnOutput(this, 'ServiceName', {
      value: this.service.serviceName,
      description: 'ECS Service Name',
    });

    new cdk.CfnOutput(this, 'ServiceEndpoint', {
      value: this.serviceEndpoint,
      description: 'Service HTTP Endpoint (for Lambda)',
    });

    new cdk.CfnOutput(this, 'TaskDefinitionFamily', {
      value: this.taskDefinition.family ?? 'unknown',
      description: 'Task Definition Family',
    });

    new cdk.CfnOutput(this, 'ContainerImageUri', {
      value: this.imageUri,
      description: 'Container Image URI',
    });

    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=${monitoring.dashboardName}`,
      description: 'CloudWatch Dashboard URL',
    });

    new cdk.CfnOutput(this, 'CapabilityNamespaceName', {
      value: this.capabilityNamespace.namespaceName,
      description: 'CloudMap HTTP Namespace for A2A capability agents',
    });

    new cdk.CfnOutput(this, 'CapabilityNamespaceId', {
      value: this.capabilityNamespace.namespaceId,
      description: 'CloudMap HTTP Namespace ID',
    });
  }
}

/**
 * Resolve the BASE_IMAGE build arg for the voice agent Docker build.
 *
 * Must match the tag computation in scripts/build-base-image.sh exactly —
 * sha256 of (requirements.txt concatenated with Dockerfile.base), first
 * 16 hex chars, prefixed with "req-".
 *
 * Falls back to VOICE_AGENT_BASE_IMAGE env var if set (lets ad-hoc builds
 * override, e.g. after a local `docker build -f Dockerfile.base -t foo .`).
 */
function resolveBaseImageUri(voiceAgentDir: string, stack: cdk.Stack): string {
  const envOverride = process.env.VOICE_AGENT_BASE_IMAGE;
  if (envOverride) {
    return envOverride;
  }

  const reqPath = path.join(voiceAgentDir, 'requirements.txt');
  const basePath = path.join(voiceAgentDir, 'Dockerfile.base');
  const hasher = crypto.createHash('sha256');
  hasher.update(fs.readFileSync(reqPath));
  hasher.update(fs.readFileSync(basePath));
  const hash = hasher.digest('hex').slice(0, 16);
  const tag = `req-${hash}`;

  const account = stack.account;
  const region = stack.region;
  const repoName = process.env.BASE_IMAGE_REPO_NAME || 'voice-agent-base';

  return `${account}.dkr.ecr.${region}.amazonaws.com/${repoName}:${tag}`;
}
