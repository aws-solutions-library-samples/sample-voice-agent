import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { Construct } from 'constructs';

/**
 * Props for SessionCounterLambdaConstruct
 */
export interface SessionCounterLambdaConstructProps {
  /**
   * Environment name (e.g., 'dev', 'staging', 'prod')
   */
  readonly environment: string;

  /**
   * Project name prefix for resources
   */
  readonly projectName: string;

  /**
   * DynamoDB table for session tracking
   */
  readonly sessionTable: dynamodb.Table;

  /**
   * Schedule rate for counting sessions (default: 1 minute)
   */
  readonly scheduleRate?: cdk.Duration;
}

/**
 * Lambda function that periodically counts active sessions.
 *
 * Features:
 * - Queries DynamoDB GSI1 for active session count
 * - Queries task heartbeats for healthy task count
 * - Emits CloudWatch custom metrics for scaling decisions
 * - Runs on CloudWatch Events schedule (default: every minute)
 */
export class SessionCounterLambdaConstruct extends Construct {
  /** The Lambda function */
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: SessionCounterLambdaConstructProps) {
    super(scope, id);

    const { environment, projectName, sessionTable } = props;
    const scheduleRate = props.scheduleRate ?? cdk.Duration.minutes(1);
    const resourcePrefix = `${projectName}-${environment}`;

    // =====================
    // CloudWatch Log Group
    // =====================
    // Using explicit logGroup instead of deprecated logRetention
    // Log group name must match /aws/lambda/{functionName} pattern
    const logGroup = new logs.LogGroup(this, 'SessionCounterLogGroup', {
      logGroupName: `/aws/lambda/${resourcePrefix}-session-counter`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // =====================
    // Lambda Function
    // =====================
    this.function = new lambda.Function(this, 'SessionCounterFunction', {
      functionName: `${resourcePrefix}-session-counter`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'functions', 'session-counter')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        SESSION_TABLE_NAME: sessionTable.tableName,
        ENVIRONMENT: environment,
      },
      logGroup: logGroup,
    });

    // =====================
    // IAM Permissions
    // =====================

    // DynamoDB read permissions (Query on GSI1, Scan for heartbeats)
    // and UpdateItem for orphaned session cleanup
    this.function.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'DynamoDBReadAccess',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Query', 'dynamodb:Scan', 'dynamodb:UpdateItem'],
        resources: [sessionTable.tableArn, `${sessionTable.tableArn}/index/*`],
      })
    );

    // CloudWatch metrics write permission
    this.function.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'CloudWatchMetricsWrite',
        effect: iam.Effect.ALLOW,
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'cloudwatch:namespace': 'VoiceAgent/Sessions',
          },
        },
      })
    );

    // =====================
    // CloudWatch Events Schedule
    // =====================
    const rule = new events.Rule(this, 'SessionCounterSchedule', {
      ruleName: `${resourcePrefix}-session-counter-schedule`,
      description: 'Trigger session counter Lambda every minute',
      schedule: events.Schedule.rate(scheduleRate),
    });

    rule.addTarget(new targets.LambdaFunction(this.function));

    // =====================
    // Outputs
    // =====================
    new cdk.CfnOutput(this, 'SessionCounterFunctionArn', {
      value: this.function.functionArn,
      description: 'Session counter Lambda function ARN',
    });
  }
}
