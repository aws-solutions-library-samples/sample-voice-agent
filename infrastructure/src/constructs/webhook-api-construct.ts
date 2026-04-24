import { Construct } from 'constructs';
import { Duration } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { SSM_PARAMS } from '../ssm-parameters';

/**
 * Props for WebhookApiConstruct
 */
export interface WebhookApiConstructProps {
  /** Deployment environment */
  environment: string;
  /** Project name for resource naming */
  projectName: string;
  /** VPC for Lambda deployment */
  vpc: ec2.IVpc;
  /** Security group for Lambda */
  lambdaSecurityGroup: ec2.ISecurityGroup;
  /** API Key Secret ARN */
  apiKeySecretArn: string;
  /** KMS Encryption Key ARN for decrypting secrets */
  encryptionKeyArn: string;
  /** ECS Cluster ARN */
  ecsClusterArn: string;
  /** ECS Task Definition ARN */
  ecsTaskDefinitionArn: string;
  /** ECS Task Security Group ID */
  ecsTaskSecurityGroupId: string;
  /** ECS Service HTTP Endpoint */
  ecsServiceEndpoint: string;
  /** Private subnet IDs for ECS tasks */
  privateSubnetIds: string;
}

/**
 * Webhook API construct.
 * Creates Lambda function and API Gateway for Daily webhooks.
 *
 * Outputs are stored in SSM Parameters for cross-stack reference.
 */
export class WebhookApiConstruct extends Construct {
  /** API Gateway endpoint URL */
  public readonly apiEndpoint: string;
  /** Bot Runner Lambda function */
  public readonly botRunnerFunction: lambda.IFunction;
  /** API Gateway REST API */
  public readonly api: apigateway.RestApi;

  constructor(scope: Construct, id: string, props: WebhookApiConstructProps) {
    super(scope, id);

    // Validate required props
    if (!props.vpc) {
      throw new Error(`${id}: vpc is required in props`);
    }
    if (!props.lambdaSecurityGroup) {
      throw new Error(`${id}: lambdaSecurityGroup is required in props`);
    }

    // Lambda function for Daily webhooks
    // Uses Python handler from src/functions/bot-runner/
    this.botRunnerFunction = new lambda.Function(this, 'BotRunnerFunction', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'handler.start_session',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'functions', 'bot-runner')),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSecurityGroup],
      timeout: Duration.seconds(30),
      memorySize: 256,
      description: `Handles Daily dial-in webhooks and calls ECS service - ${props.environment}`,
      environment: {
        ECS_SERVICE_ENDPOINT: props.ecsServiceEndpoint,
        DAILY_API_KEY_SECRET_ARN: props.apiKeySecretArn,
        // HMAC verification on. The bot-runner reads DAILY_HMAC_SECRET
        // from the shared API key secret at runtime and verifies every
        // webhook's X-Pinless-Signature header against the raw body.
        // Override to 'false' only for emergency rotation scenarios —
        // the /start endpoint is internet-exposed.
        //
        // Re-enabled 2026-04-24 after re-running pinless_dialin
        // configuration against Daily and persisting the returned HMAC
        // to Secrets Manager. See docs/lambda-patches if this breaks
        // and you need to rotate again.
        DAILY_HMAC_VERIFY: 'true',
        // Phase 7B: on every inbound webhook the bot-runner invokes the
        // voice-api Lambda to resolve the dialed number →
        // voice_phone_numbers.inbound_agent_id. Same alias the ECS task
        // uses (see ecs-stack.ts) so prod + dev stay in lockstep.
        VOICE_API_LAMBDA_NAME: 'medcloud-voice-api:live',
        // Agent to route to when the dialed number has no row in
        // voice_phone_numbers (or the lookup Lambda fails open).
        // Keeps inbound calls working during initial provisioning / if
        // Aurora is unreachable. Empty = 503 back to Daily.
        DEFAULT_INBOUND_AGENT: 'chris-claim-status',
        LOG_LEVEL: 'INFO',
      },
    });

    // Grant permissions to read secrets
    this.botRunnerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [props.apiKeySecretArn],
      })
    );

    // Grant permissions to decrypt secrets with KMS
    this.botRunnerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['kms:Decrypt'],
        resources: [props.encryptionKeyArn],
      })
    );

    // Grant permissions to read SSM parameters (for ECS endpoint discovery)
    this.botRunnerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter'],
        resources: [`arn:aws:ssm:*:*:parameter/voice-agent/ecs/*`],
      })
    );

    // Phase 7B: bot-runner needs to invoke the voice-api Lambda to look up
    // which agent a dialed number is assigned to. Same pattern as the ECS
    // task role (see ecs-stack.ts ~L392). Alias-qualified ARN covers
    // :live + any future aliases without a redeploy.
    this.botRunnerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [
          `arn:aws:lambda:*:*:function:medcloud-voice-api`,
          `arn:aws:lambda:*:*:function:medcloud-voice-api:*`,
        ],
      })
    );

    // Note: Lambda calls the ECS service via HTTP, so no ECS task permissions needed

    // API Gateway for webhook endpoint
    this.api = new apigateway.RestApi(this, 'WebhookApi', {
      description: `Daily webhook endpoint for Voice Agent - ${props.environment}`,
      deployOptions: {
        stageName: props.environment,
      },
    });

    // POST /start endpoint
    const startResource = this.api.root.addResource('start');
    startResource.addMethod('POST', new apigateway.LambdaIntegration(this.botRunnerFunction));

    this.apiEndpoint = `${this.api.url}start`;

    // Store outputs in SSM Parameters
    new ssm.StringParameter(this, 'WebhookUrlParam', {
      parameterName: SSM_PARAMS.WEBHOOK_URL,
      stringValue: this.apiEndpoint,
      description: 'Voice Agent Daily Webhook URL',
    });
  }
}
