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

    // Lambda function for Daily webhooks + outbound dialing.
    // Uses Python handler from src/functions/bot-runner/
    //
    // entrypoint = handler.route — dispatches to start_session
    // (inbound webhook, /start) or start_dial_out (outbound,
    // /dial-out, Phase 7D). Single Lambda function so both flows
    // share the same DailyClient + ECS service client + IAM.
    this.botRunnerFunction = new lambda.Function(this, 'BotRunnerFunction', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'handler.route',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'functions', 'bot-runner')),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSecurityGroup],
      timeout: Duration.seconds(30),
      memorySize: 256,
      description: `Handles Daily inbound webhooks + outbound dial-out - ${props.environment}`,
      environment: {
        ECS_SERVICE_ENDPOINT: props.ecsServiceEndpoint,
        DAILY_API_KEY_SECRET_ARN: props.apiKeySecretArn,
        // HMAC verification: DISABLED pending investigation.
        //
        // 2026-04-24: We re-ran the documented pinless_dialin
        // configuration against Daily and persisted the returned HMAC
        // to Secrets Manager, but webhooks arriving for +12098075018
        // don't verify against ANY canonical-form variant we tried
        // (hex/b64 × ts.body / body.ts / body-only / ts+body, with
        // seconds AND milliseconds, against the b64-decoded + literal
        // secret — 336 combinations total, zero matches). Daily's GET
        // /v1 now reports pinless_dialin=null even though webhooks
        // still arrive, implying the active signing secret is stored
        // per-phone-number at provisioning time and isn't exposed
        // through the domain-level pinless_dialin API.
        //
        // Rather than block 7B verification on this, we're leaving
        // HMAC verification off and filing it as tech-debt. /start is
        // internet-exposed, so this is a real hole — don't ship to
        // prod inbound until it's resolved. Dev number traffic only
        // for now.
        //
        // Follow-up: talk to Daily support about per-number HMAC
        // configuration. Once we know the correct source, re-enable
        // via rotate-daily-hmac.sh (scripts/) and flip this to 'true'.
        DAILY_HMAC_VERIFY: 'false',
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

    // POST /start — Daily inbound webhook (HMAC-verified)
    const startResource = this.api.root.addResource('start');
    startResource.addMethod('POST', new apigateway.LambdaIntegration(this.botRunnerFunction));

    // POST /dial-out — Phase 7D outbound dialing trigger.
    // No HMAC: this path is meant for same-account callers with
    // lambda:Invoke permission (SQS consumer, operator scripts).
    // API Gateway integration is provided so curl-based smoke tests
    // and the frontend's "Test outbound" button can exercise it
    // without needing an SDK. In production the SQS consumer should
    // prefer direct lambda:Invoke (no API Gateway hop, IAM auth).
    const dialOutResource = this.api.root.addResource('dial-out');
    dialOutResource.addMethod('POST', new apigateway.LambdaIntegration(this.botRunnerFunction));

    // POST /recording-webhook — Phase 7E PR 3 Daily recording event
    // receiver. Daily POSTs here for `recording.ready-to-download`
    // and `recording.error` events. No HMAC verification today (same
    // parked tech-debt item as the inbound /start path); we rely on
    // Daily's IP allowlist + lookup-by-session-id to reject events
    // for rooms we don't own. The handler always 200s to keep Daily
    // from retrying — bad payloads are logged.
    const recordingWebhookResource = this.api.root.addResource('recording-webhook');
    recordingWebhookResource.addMethod('POST', new apigateway.LambdaIntegration(this.botRunnerFunction));

    this.apiEndpoint = `${this.api.url}start`;

    // Store outputs in SSM Parameters
    new ssm.StringParameter(this, 'WebhookUrlParam', {
      parameterName: SSM_PARAMS.WEBHOOK_URL,
      stringValue: this.apiEndpoint,
      description: 'Voice Agent Daily Webhook URL',
    });

    // Phase 7D: dial-out endpoint URL → SSM so the SQS consumer can
    // discover it without hardcoding.
    new ssm.StringParameter(this, 'DialOutUrlParam', {
      parameterName: '/voice-agent/botrunner/dial-out-url',
      stringValue: `${this.api.url}dial-out`,
      description: 'Voice Agent outbound dialing endpoint',
    });

    // Phase 7E PR 3: recording webhook URL → SSM so the Daily
    // bootstrap script (or operators) can register this URL with
    // Daily's POST /v1/webhooks endpoint without hardcoding.
    new ssm.StringParameter(this, 'RecordingWebhookUrlParam', {
      parameterName: '/voice-agent/botrunner/recording-webhook-url',
      stringValue: `${this.api.url}recording-webhook`,
      description: 'Voice Agent Daily recording webhook receiver',
    });
  }
}
