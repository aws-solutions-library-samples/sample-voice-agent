import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ecs from 'aws-cdk-lib/aws-ecs';

/**
 * Props for VoiceAgentMonitoringConstruct
 */
export interface VoiceAgentMonitoringProps {
  /** Deployment environment (poc, dev, staging, prod) */
  environment: string;
  /** Project name for resource naming */
  projectName: string;
  /** ECS cluster for container metrics */
  cluster: ecs.ICluster;
  /** ECS service name for container metrics */
  serviceName: string;
  /** Whether to create SNS topic for notifications */
  enableNotifications?: boolean;
  /** Email addresses for notifications (if enableNotifications is true) */
  notificationEmails?: string[];
  /** Target sessions per task for scaling (used for alarm thresholds and dashboard annotations) */
  targetSessionsPerTask?: number;
  /** Per-task session capacity: /ready returns 503 at this limit (used for dashboard annotations) */
  sessionCapacityPerTask?: number;
  /** NLB target group full name (for HealthyHostCount metrics) */
  targetGroupFullName?: string;
  /** NLB load balancer full name (for HealthyHostCount metrics) */
  loadBalancerFullName?: string;
  /** Log group for task protection failure metric filter */
  taskLogGroup?: logs.ILogGroup;
}

/**
 * Alarm thresholds configuration
 */
interface AlarmThresholds {
  /** E2E latency P95 threshold in milliseconds */
  e2eLatencyP95Ms: number;
  /** Error rate threshold in percent */
  errorRatePercent: number;
  /** CPU utilization threshold in percent */
  cpuUtilizationPercent: number;
  /** Memory utilization threshold in percent */
  memoryUtilizationPercent: number;
  /** Container restarts per hour threshold */
  containerRestartsPerHour: number;
}

const DEFAULT_THRESHOLDS: AlarmThresholds = {
  e2eLatencyP95Ms: 2000,
  errorRatePercent: 5,
  cpuUtilizationPercent: 80,
  memoryUtilizationPercent: 85,
  containerRestartsPerHour: 2,
};

/**
 * CloudWatch monitoring construct for voice agent pipeline.
 *
 * Creates:
 * - CloudWatch alarms for latency, errors, and resource usage
 * - Operational dashboard with key metrics
 * - Optional SNS topic for alarm notifications
 *
 * Metrics are sourced from:
 * - VoiceAgent/Pipeline namespace (EMF from voice agent container)
 * - ECS/ContainerInsights namespace (ECS service metrics)
 */
export class VoiceAgentMonitoringConstruct extends Construct {
  /** CloudWatch alarms */
  public readonly alarms: cloudwatch.Alarm[];
  /** Operational dashboard */
  public readonly dashboard: cloudwatch.Dashboard;
  /** SNS notification topic (if enabled) */
  public readonly notificationTopic?: sns.Topic;
  /** Dashboard name */
  public readonly dashboardName: string;

  constructor(scope: Construct, id: string, props: VoiceAgentMonitoringProps) {
    super(scope, id);

    const resourcePrefix = `${props.projectName}-${props.environment}`;
    this.dashboardName = `${resourcePrefix}-monitoring`;

    // Create notification topic if enabled
    let alarmAction: cloudwatch.IAlarmAction | undefined;
    if (props.enableNotifications) {
      this.notificationTopic = this.createNotificationTopic(
        resourcePrefix,
        props.notificationEmails ?? []
      );
      alarmAction = new cloudwatch_actions.SnsAction(this.notificationTopic);
    }

    // Create alarms
    this.alarms = this.createAlarms(props, resourcePrefix, DEFAULT_THRESHOLDS, alarmAction);

    // Create dashboard
    this.dashboard = this.createDashboard(props, resourcePrefix, DEFAULT_THRESHOLDS);

    // Create saved queries
    this.createSavedQueries(props, resourcePrefix);
  }

  /**
   * Creates SNS topic for alarm notifications.
   */
  private createNotificationTopic(resourcePrefix: string, emails: string[]): sns.Topic {
    const topic = new sns.Topic(this, 'AlarmNotificationTopic', {
      topicName: `${resourcePrefix}-alarms`,
      displayName: 'Voice Agent Alarm Notifications',
    });

    // Add email subscriptions
    emails.forEach((email, index) => {
      new sns.Subscription(this, `EmailSubscription${index}`, {
        topic,
        protocol: sns.SubscriptionProtocol.EMAIL,
        endpoint: email,
      });
    });

    return topic;
  }

  /**
   * Creates CloudWatch alarms for voice agent monitoring.
   */
  private createAlarms(
    props: VoiceAgentMonitoringProps,
    resourcePrefix: string,
    thresholds: AlarmThresholds,
    alarmAction?: cloudwatch.IAlarmAction
  ): cloudwatch.Alarm[] {
    const alarms: cloudwatch.Alarm[] = [];

    // =================================================================
    // Agent Response Latency Alarm (Average > 2000ms for 3 consecutive 1-minute periods)
    // =================================================================
    const e2eLatencyAlarm = new cloudwatch.Alarm(this, 'E2ELatencyAlarm', {
      alarmName: `${resourcePrefix}-e2e-latency-high`,
      alarmDescription:
        'Agent response latency average exceeds 2000ms - voice response feels sluggish',
      metric: new cloudwatch.Metric({
        namespace: 'VoiceAgent/Pipeline',
        metricName: 'AgentResponseLatency',
        dimensionsMap: {
          Environment: props.environment,
        },
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: thresholds.e2eLatencyP95Ms,
      evaluationPeriods: 3,
      datapointsToAlarm: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.push(e2eLatencyAlarm);

    // =================================================================
    // Error Rate Alarm (> 5% failure rate over 5-minute window)
    // Uses call completion status to calculate error rate
    // =================================================================
    const errorCountMetric = new cloudwatch.Metric({
      namespace: 'VoiceAgent/Pipeline',
      metricName: 'CallDuration',
      dimensionsMap: {
        Environment: props.environment,
        CompletionStatus: 'error',
      },
      statistic: 'SampleCount',
      period: cdk.Duration.minutes(5),
    });

    const totalCallsMetric = new cloudwatch.Metric({
      namespace: 'VoiceAgent/Pipeline',
      metricName: 'CallDuration',
      dimensionsMap: {
        Environment: props.environment,
      },
      statistic: 'SampleCount',
      period: cdk.Duration.minutes(5),
    });

    const errorRateExpression = new cloudwatch.MathExpression({
      expression: 'IF(total > 0, (errors / total) * 100, 0)',
      usingMetrics: {
        errors: errorCountMetric,
        total: totalCallsMetric,
      },
      period: cdk.Duration.minutes(5),
      label: 'Error Rate %',
    });

    const errorRateAlarm = new cloudwatch.Alarm(this, 'ErrorRateAlarm', {
      alarmName: `${resourcePrefix}-error-rate-high`,
      alarmDescription: 'Call error rate exceeds 5% - service degradation detected',
      metric: errorRateExpression,
      threshold: thresholds.errorRatePercent,
      evaluationPeriods: 2,
      datapointsToAlarm: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.push(errorRateAlarm);

    // =================================================================
    // CPU Utilization Alarm (> 80% sustained)
    // =================================================================
    const cpuAlarm = new cloudwatch.Alarm(this, 'CpuUtilizationAlarm', {
      alarmName: `${resourcePrefix}-cpu-high`,
      alarmDescription: 'CPU utilization exceeds 80% - consider scaling',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/ECS',
        metricName: 'CPUUtilization',
        dimensionsMap: {
          ClusterName: props.cluster.clusterName,
          ServiceName: props.serviceName,
        },
        statistic: 'Average',
        period: cdk.Duration.minutes(5),
      }),
      threshold: thresholds.cpuUtilizationPercent,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.push(cpuAlarm);

    // =================================================================
    // Memory Utilization Alarm (> 85% sustained)
    // =================================================================
    const memoryAlarm = new cloudwatch.Alarm(this, 'MemoryUtilizationAlarm', {
      alarmName: `${resourcePrefix}-memory-high`,
      alarmDescription: 'Memory utilization exceeds 85% - consider scaling',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/ECS',
        metricName: 'MemoryUtilization',
        dimensionsMap: {
          ClusterName: props.cluster.clusterName,
          ServiceName: props.serviceName,
        },
        statistic: 'Average',
        period: cdk.Duration.minutes(5),
      }),
      threshold: thresholds.memoryUtilizationPercent,
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.push(memoryAlarm);

    // =================================================================
    // Container Restart Alarm (> 2 restarts/hour)
    // Uses RunningTaskCount drops to detect container restarts
    // =================================================================
    // Track when running tasks drop below 1 (indicates restart in progress)
    // Using a math expression to count restart events over an hour
    const runningTaskMetric = new cloudwatch.Metric({
      namespace: 'ECS/ContainerInsights',
      metricName: 'RunningTaskCount',
      dimensionsMap: {
        ClusterName: props.cluster.clusterName,
        ServiceName: props.serviceName,
      },
      statistic: 'Minimum',
      period: cdk.Duration.minutes(5),
    });

    // Count periods where running tasks dropped to 0 (container restarted)
    // IF running < 1, count as 1 restart; otherwise 0
    // SUM over 1 hour (12 periods of 5 minutes)
    const restartExpression = new cloudwatch.MathExpression({
      expression: 'IF(running < 1, 1, 0)',
      usingMetrics: {
        running: runningTaskMetric,
      },
      period: cdk.Duration.minutes(5),
      label: 'Container Restarts',
    });

    const containerRestartAlarm = new cloudwatch.Alarm(this, 'ContainerRestartAlarm', {
      alarmName: `${resourcePrefix}-container-restarts`,
      alarmDescription: 'Container restarting frequently - investigate crash cause',
      metric: restartExpression,
      threshold: 1, // Alert on any restart detected
      evaluationPeriods: 12, // 1 hour window (12 x 5-min periods)
      datapointsToAlarm: thresholds.containerRestartsPerHour,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.push(containerRestartAlarm);

    // =================================================================
    // Sessions Per Task High (approaching per-container capacity)
    // =================================================================
    const alarmTargetSessions = props.targetSessionsPerTask ?? 3;
    const alarmSessionCapacity = props.sessionCapacityPerTask ?? 10;
    const sessionsAlarmThreshold = alarmSessionCapacity - 0.5;
    const sessionsPerTaskAlarm = new cloudwatch.Alarm(this, 'SessionsPerTaskHighAlarm', {
      alarmName: `${resourcePrefix}-sessions-per-task-high`,
      alarmDescription: `MaxSessionsPerTask exceeds ${sessionsAlarmThreshold} (capacity=${alarmSessionCapacity}, target=${alarmTargetSessions}) -- approaching per-container capacity, verify scaling is responding`,
      metric: new cloudwatch.Metric({
        namespace: 'VoiceAgent/Sessions',
        metricName: 'MaxSessionsPerTask',
        dimensionsMap: {
          Environment: props.environment,
        },
        statistic: 'Average',
        period: cdk.Duration.minutes(1),
      }),
      threshold: sessionsAlarmThreshold,
      evaluationPeriods: 2,
      datapointsToAlarm: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.push(sessionsPerTaskAlarm);

    // =================================================================
    // Task Protection Failure (log-based metric filter)
    // Fires when the ECS Task Scale-in Protection API call fails after
    // all retries, meaning active calls may not be protected from scale-in.
    // =================================================================
    const taskLogGroup =
      props.taskLogGroup ??
      logs.LogGroup.fromLogGroupName(this, 'ProtectionFailureLogGroup', `/ecs/${resourcePrefix}`);
    const protectionFailureFilter = new logs.MetricFilter(this, 'ProtectionFailureMetricFilter', {
      logGroup: taskLogGroup,
      filterPattern: logs.FilterPattern.literal('"task_protection_all_retries_exhausted"'),
      metricNamespace: 'VoiceAgent/Sessions',
      metricName: 'TaskProtectionFailures',
      metricValue: '1',
      defaultValue: 0,
    });

    const protectionFailureAlarm = new cloudwatch.Alarm(this, 'ProtectionFailureAlarm', {
      alarmName: `${resourcePrefix}-task-protection-failure`,
      alarmDescription:
        'Task scale-in protection API failed after all retries -- active calls may be terminated during scale-in',
      metric: protectionFailureFilter.metric({
        statistic: 'Sum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    alarms.push(protectionFailureAlarm);

    // =================================================================
    // Metric Staleness (SessionsPerTask INSUFFICIENT_DATA > 5 min)
    // Detects when the session counter Lambda stops emitting metrics,
    // which would blind the auto-scaling policies.
    // =================================================================
    const metricStalenessAlarm = new cloudwatch.Alarm(this, 'MetricStalenessAlarm', {
      alarmName: `${resourcePrefix}-metric-staleness`,
      alarmDescription:
        'SessionsPerTask metric missing for >5 minutes -- session counter Lambda may have failed, auto-scaling is blind',
      metric: new cloudwatch.Metric({
        namespace: 'VoiceAgent/Sessions',
        metricName: 'SessionsPerTask',
        dimensionsMap: {
          Environment: props.environment,
        },
        statistic: 'SampleCount',
        period: cdk.Duration.minutes(1),
      }),
      threshold: 1,
      evaluationPeriods: 5,
      datapointsToAlarm: 1, // Need at least 1 datapoint in 5 periods
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    alarms.push(metricStalenessAlarm);

    // Add alarm actions if notifications enabled
    if (alarmAction) {
      alarms.forEach((alarm) => {
        alarm.addAlarmAction(alarmAction);
        alarm.addOkAction(alarmAction);
      });
    }

    return alarms;
  }

  /**
   * Creates operational dashboard for voice agent monitoring.
   */
  private createDashboard(
    props: VoiceAgentMonitoringProps,
    _resourcePrefix: string,
    thresholds: AlarmThresholds
  ): cloudwatch.Dashboard {
    const dashboard = new cloudwatch.Dashboard(this, 'OperationalDashboard', {
      dashboardName: this.dashboardName,
    });

    // =================================================================
    // Row 1: Service Health Summary
    // =================================================================
    dashboard.addWidgets(
      // Alarm Status
      new cloudwatch.AlarmStatusWidget({
        title: 'Service Health',
        alarms: this.alarms,
        width: 8,
        height: 4,
      }),
      // Completed Calls (adapts to dashboard time range)
      new cloudwatch.GraphWidget({
        title: 'Completed Calls',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'CallDuration',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'SampleCount',
            period: cdk.Duration.minutes(5),
            label: 'Calls',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Count',
        },
        width: 8,
        height: 4,
      }),
      // Average Call Duration (adapts to dashboard time range)
      new cloudwatch.GraphWidget({
        title: 'Avg Call Duration',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'CallDuration',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(5),
            label: 'Duration (s)',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Seconds',
        },
        width: 8,
        height: 4,
      })
    );

    // =================================================================
    // Row 2: Agent Response Latency (formerly E2E Latency)
    // =================================================================
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Agent Response Latency',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'AgentResponseLatency',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'p50',
            period: cdk.Duration.minutes(1),
            label: 'P50',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'AgentResponseLatency',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'p95',
            period: cdk.Duration.minutes(1),
            label: 'P95',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'AgentResponseLatency',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'p99',
            period: cdk.Duration.minutes(1),
            label: 'P99',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Milliseconds',
        },
        leftAnnotations: [
          {
            value: thresholds.e2eLatencyP95Ms,
            color: '#ff0000',
            label: `Alarm Threshold (${thresholds.e2eLatencyP95Ms}ms)`,
          },
          {
            value: 1000,
            color: '#ff9900',
            label: 'Target (1000ms)',
          },
        ],
        width: 12,
        height: 6,
      }),
      // Component Latencies
      new cloudwatch.GraphWidget({
        title: 'Component Latencies (Avg)',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'STTLatency',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'STT',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'LLMTimeToFirstByte',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'LLM TTFB',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'TTSTimeToFirstByte',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'TTS TTFB',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Milliseconds',
        },
        width: 12,
        height: 6,
      })
    );

    // =================================================================
    // Row 3: Error Rates and Completion Status
    // =================================================================
    dashboard.addWidgets(
      // Error Rate Over Time
      new cloudwatch.GraphWidget({
        title: 'Error Rate (%)',
        left: [
          new cloudwatch.MathExpression({
            expression: 'IF(total > 0, (errors / total) * 100, 0)',
            usingMetrics: {
              errors: new cloudwatch.Metric({
                namespace: 'VoiceAgent/Pipeline',
                metricName: 'CallDuration',
                dimensionsMap: {
                  Environment: props.environment,
                  CompletionStatus: 'error',
                },
                statistic: 'SampleCount',
                period: cdk.Duration.minutes(5),
              }),
              total: new cloudwatch.Metric({
                namespace: 'VoiceAgent/Pipeline',
                metricName: 'CallDuration',
                dimensionsMap: {
                  Environment: props.environment,
                },
                statistic: 'SampleCount',
                period: cdk.Duration.minutes(5),
              }),
            },
            label: 'Error Rate',
            period: cdk.Duration.minutes(5),
          }),
        ],
        leftYAxis: {
          min: 0,
          max: 100,
          label: 'Percent',
        },
        leftAnnotations: [
          {
            value: thresholds.errorRatePercent,
            color: '#ff0000',
            label: `Alarm Threshold (${thresholds.errorRatePercent}%)`,
          },
        ],
        width: 12,
        height: 6,
      }),
      // Completion Status Distribution
      new cloudwatch.GraphWidget({
        title: 'Calls by Completion Status',
        stacked: true,
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'CallDuration',
            dimensionsMap: {
              Environment: props.environment,
              CompletionStatus: 'completed',
            },
            statistic: 'SampleCount',
            period: cdk.Duration.minutes(5),
            label: 'Completed',
            color: '#2ca02c',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'CallDuration',
            dimensionsMap: {
              Environment: props.environment,
              CompletionStatus: 'cancelled',
            },
            statistic: 'SampleCount',
            period: cdk.Duration.minutes(5),
            label: 'Cancelled',
            color: '#ff7f0e',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'CallDuration',
            dimensionsMap: {
              Environment: props.environment,
              CompletionStatus: 'error',
            },
            statistic: 'SampleCount',
            period: cdk.Duration.minutes(5),
            label: 'Error',
            color: '#d62728',
          }),
        ],
        width: 12,
        height: 6,
      })
    );

    // =================================================================
    // Row 4: Resource Utilization
    // =================================================================
    dashboard.addWidgets(
      // CPU Utilization
      new cloudwatch.GraphWidget({
        title: 'CPU Utilization',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ECS',
            metricName: 'CPUUtilization',
            dimensionsMap: {
              ClusterName: props.cluster.clusterName,
              ServiceName: props.serviceName,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'CPU %',
          }),
        ],
        leftYAxis: {
          min: 0,
          max: 100,
          label: 'Percent',
        },
        leftAnnotations: [
          {
            value: thresholds.cpuUtilizationPercent,
            color: '#ff0000',
            label: `Alarm Threshold (${thresholds.cpuUtilizationPercent}%)`,
          },
        ],
        width: 12,
        height: 6,
      }),
      // Memory Utilization
      new cloudwatch.GraphWidget({
        title: 'Memory Utilization',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/ECS',
            metricName: 'MemoryUtilization',
            dimensionsMap: {
              ClusterName: props.cluster.clusterName,
              ServiceName: props.serviceName,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Memory %',
          }),
        ],
        leftYAxis: {
          min: 0,
          max: 100,
          label: 'Percent',
        },
        leftAnnotations: [
          {
            value: thresholds.memoryUtilizationPercent,
            color: '#ff0000',
            label: `Alarm Threshold (${thresholds.memoryUtilizationPercent}%)`,
          },
        ],
        width: 12,
        height: 6,
      })
    );

    // =================================================================
    // Row 5: Conversation Quality Metrics
    // =================================================================
    dashboard.addWidgets(
      // Turns per Call
      new cloudwatch.GraphWidget({
        title: 'Turns per Call',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'TurnCount',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(5),
            label: 'Avg Turns',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'TurnCount',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Maximum',
            period: cdk.Duration.minutes(5),
            label: 'Max Turns',
          }),
        ],
        width: 6,
        height: 6,
      }),
      // Interruptions (Barge-ins)
      new cloudwatch.GraphWidget({
        title: 'Interruptions (Barge-ins)',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'InterruptionCount',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Sum',
            period: cdk.Duration.minutes(5),
            label: 'Total Interruptions',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'InterruptionCount',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(5),
            label: 'Avg per Call',
          }),
        ],
        width: 6,
        height: 6,
      }),
      // Audio Quality (RMS + Peak Levels)
      new cloudwatch.GraphWidget({
        title: 'Audio Quality (dBFS)',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'AudioRMS',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Avg RMS',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'AudioPeak',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Avg Peak',
            color: '#1f77b4',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'AudioPeak',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Maximum',
            period: cdk.Duration.minutes(1),
            label: 'Max Peak',
            color: '#9467bd',
          }),
        ],
        leftYAxis: {
          min: -60,
          max: 0,
          label: 'dBFS',
        },
        leftAnnotations: [
          {
            value: -55,
            color: '#ff0000',
            label: 'Poor Quality Threshold',
          },
          {
            value: -30,
            color: '#2ca02c',
            label: 'Good Quality',
          },
          {
            value: -3,
            color: '#d62728',
            label: 'Clipping Headroom',
          },
        ],
        width: 6,
        height: 6,
      })
    );

    // =================================================================
    // Row 5b: Tool Execution Metrics
    // Widgets show "No data" when ENABLE_TOOL_CALLING=false (expected)
    // =================================================================
    dashboard.addWidgets(
      // Tool Execution Latency
      new cloudwatch.GraphWidget({
        title: 'Tool Execution Latency',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'ToolExecutionTime',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(5),
            label: 'Avg (ms)',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'ToolExecutionTime',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'p95',
            period: cdk.Duration.minutes(5),
            label: 'P95 (ms)',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'ToolExecutionTime',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Maximum',
            period: cdk.Duration.minutes(5),
            label: 'Max (ms)',
            color: '#d62728',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Milliseconds',
        },
        leftAnnotations: [
          {
            value: 30000,
            color: '#ff0000',
            label: 'A2A Timeout (30s)',
          },
        ],
        width: 12,
        height: 6,
      }),
      // Tool Invocation Count
      new cloudwatch.GraphWidget({
        title: 'Tool Invocations',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'ToolInvocationCount',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Sum',
            period: cdk.Duration.minutes(5),
            label: 'Total Invocations',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Count',
        },
        width: 12,
        height: 6,
      })
    );

    // =================================================================
    // Row 6: Comprehensive Observability Metrics (NEW)
    // =================================================================
    dashboard.addWidgets(
      // STT Confidence Scores
      new cloudwatch.GraphWidget({
        title: 'STT Confidence Scores',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'STTConfidenceAvg',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Avg Confidence',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'STTConfidenceMin',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Min Confidence',
          }),
        ],
        leftYAxis: {
          min: 0,
          max: 1,
          label: 'Score (0-1)',
        },
        leftAnnotations: [
          {
            value: 0.7,
            color: '#ff0000',
            label: 'Low Confidence Threshold',
          },
          {
            value: 0.9,
            color: '#2ca02c',
            label: 'High Confidence',
          },
        ],
        width: 8,
        height: 6,
      }),
      // LLM Token Generation Speed
      new cloudwatch.GraphWidget({
        title: 'LLM Token Generation Speed',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'LLMTokensPerSecond',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Tokens/sec (Avg)',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'LLMTokensPerSecond',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'p95',
            period: cdk.Duration.minutes(1),
            label: 'Tokens/sec (P95)',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Tokens/Second',
        },
        width: 8,
        height: 6,
      }),
      // LLM Output Tokens per Response
      new cloudwatch.GraphWidget({
        title: 'LLM Output Tokens per Response',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'LLMOutputTokens',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Avg Tokens',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Token Count',
        },
        width: 8,
        height: 6,
      })
    );

    // =================================================================
    // Row 7: Conversation Flow & Quality Score (NEW)
    // =================================================================
    dashboard.addWidgets(
      // Conversation Flow Timing
      new cloudwatch.GraphWidget({
        title: 'Conversation Flow Timing',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'TurnGap',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Turn Gap (Avg)',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'ResponseDelay',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Response Delay (Avg)',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Milliseconds',
        },
        width: 12,
        height: 6,
      }),
      // Composite Quality Score
      new cloudwatch.GraphWidget({
        title: 'Composite Quality Score',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Pipeline',
            metricName: 'QualityScore',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Quality Score (Avg)',
          }),
        ],
        leftYAxis: {
          min: 0,
          max: 1,
          label: 'Score (0-1)',
        },
        leftAnnotations: [
          {
            value: 0.6,
            color: '#ff0000',
            label: 'Poor Quality Threshold',
          },
          {
            value: 0.8,
            color: '#ff9900',
            label: 'Good Quality',
          },
          {
            value: 0.9,
            color: '#2ca02c',
            label: 'Excellent Quality',
          },
        ],
        width: 12,
        height: 6,
      })
    );

    // =================================================================
    // Row 8: Auto-Scaling & Task Protection
    // =================================================================
    const targetSessions = props.targetSessionsPerTask ?? 3;
    const sessionCapacity = props.sessionCapacityPerTask ?? 10;

    dashboard.addWidgets(
      // Running Task Count over time
      new cloudwatch.GraphWidget({
        title: 'Task Count (Desired / Running / Ready)',
        left: [
          new cloudwatch.Metric({
            namespace: 'ECS/ContainerInsights',
            metricName: 'DesiredTaskCount',
            dimensionsMap: {
              ClusterName: props.cluster.clusterName,
              ServiceName: props.serviceName,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Desired',
            color: '#9467bd',
          }),
          new cloudwatch.Metric({
            namespace: 'ECS/ContainerInsights',
            metricName: 'RunningTaskCount',
            dimensionsMap: {
              ClusterName: props.cluster.clusterName,
              ServiceName: props.serviceName,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Running',
            color: '#1f77b4',
          }),
          ...(props.targetGroupFullName && props.loadBalancerFullName
            ? [
                new cloudwatch.Metric({
                  namespace: 'AWS/NetworkELB',
                  metricName: 'HealthyHostCount',
                  dimensionsMap: {
                    TargetGroup: props.targetGroupFullName,
                    LoadBalancer: props.loadBalancerFullName,
                  },
                  statistic: 'Average',
                  period: cdk.Duration.minutes(1),
                  label: 'Ready (accepting calls)',
                  color: '#2ca02c',
                }),
              ]
            : []),
        ],
        right: [
          ...(props.targetGroupFullName && props.loadBalancerFullName
            ? [
                new cloudwatch.Metric({
                  namespace: 'AWS/NetworkELB',
                  metricName: 'UnHealthyHostCount',
                  dimensionsMap: {
                    TargetGroup: props.targetGroupFullName,
                    LoadBalancer: props.loadBalancerFullName,
                  },
                  statistic: 'Average',
                  period: cdk.Duration.minutes(1),
                  label: 'Unhealthy (draining/starting)',
                  color: '#d62728',
                }),
              ]
            : []),
        ],
        leftYAxis: {
          min: 0,
          label: 'Tasks',
        },
        rightYAxis: {
          min: 0,
          label: 'Unhealthy',
        },
        width: 8,
        height: 6,
      }),
      // Sessions Per Task trend (Avg and Max)
      new cloudwatch.GraphWidget({
        title: 'Sessions Per Task',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Sessions',
            metricName: 'MaxSessionsPerTask',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Max (hottest task)',
            color: '#d62728',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Sessions',
            metricName: 'SessionsPerTask',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Avg (fleet-wide)',
            color: '#1f77b4',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Sessions per Task',
        },
        leftAnnotations: [
          {
            value: targetSessions,
            color: '#ff9900',
            label: `Scale-out Target (${targetSessions})`,
          },
          {
            value: sessionCapacity,
            color: '#ff0000',
            label: `Container Capacity (${sessionCapacity})`,
          },
        ],
        width: 8,
        height: 6,
      }),
      // Active Sessions & Task Protection Failures
      new cloudwatch.GraphWidget({
        title: 'Active Sessions & Protection Failures',
        left: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Sessions',
            metricName: 'ActiveCount',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Sum',
            period: cdk.Duration.minutes(1),
            label: 'Total Active Sessions',
          }),
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Sessions',
            metricName: 'HealthyTaskCount',
            dimensionsMap: {
              Environment: props.environment,
            },
            statistic: 'Average',
            period: cdk.Duration.minutes(1),
            label: 'Healthy Tasks',
          }),
        ],
        right: [
          new cloudwatch.Metric({
            namespace: 'VoiceAgent/Sessions',
            metricName: 'TaskProtectionFailures',
            statistic: 'Sum',
            period: cdk.Duration.minutes(1),
            label: 'Protection Failures',
            color: '#ff0000',
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Count',
        },
        rightYAxis: {
          min: 0,
          label: 'Failures',
        },
        width: 8,
        height: 6,
      })
    );

    // =================================================================
    // Row 9: Per-Task Session Distribution
    // Auto-discovers all TaskId dimensions via SEARCH expression.
    // Each task gets its own series so you can see load imbalance.
    // =================================================================
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Sessions Per Task (Per-Task Breakdown)',
        left: [
          new cloudwatch.MathExpression({
            expression: `SEARCH('{VoiceAgent/Sessions,Environment,TaskId} MetricName="SessionsPerTask" Environment="${props.environment}"', 'Maximum', 60)`,
            label: '',
            period: cdk.Duration.minutes(1),
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Sessions',
        },
        leftAnnotations: [
          {
            value: targetSessions,
            color: '#ff9900',
            label: `Scale-out Target (${targetSessions})`,
          },
          {
            value: sessionCapacity,
            color: '#ff0000',
            label: `Container Capacity (${sessionCapacity})`,
          },
        ],
        width: 24,
        height: 6,
      })
    );

    // =================================================================
    // Row 10: Capability Agent Health
    // Auto-discovers all non-voice-agent ECS services in the cluster
    // via SEARCH expressions on ECS/ContainerInsights metrics.
    // =================================================================
    const clusterName = props.cluster.clusterName;
    // Exclude the voice agent service -- show only capability agents
    const voiceServiceName = props.serviceName;

    dashboard.addWidgets(
      // Capability Agent Task Count
      new cloudwatch.GraphWidget({
        title: 'Capability Agent Tasks (Running)',
        left: [
          new cloudwatch.MathExpression({
            // SEARCH for RunningTaskCount across all services in this cluster,
            // then REMOVE the voice agent service to isolate capability agents.
            expression: `REMOVE_EMPTY(SEARCH('{ECS/ContainerInsights,ClusterName,ServiceName} MetricName="RunningTaskCount" ClusterName="${clusterName}" NOT ServiceName="${voiceServiceName}"', 'Average', 60))`,
            label: '',
            period: cdk.Duration.minutes(1),
          }),
        ],
        leftYAxis: {
          min: 0,
          label: 'Tasks',
        },
        width: 8,
        height: 6,
      }),
      // Capability Agent CPU Utilization
      new cloudwatch.GraphWidget({
        title: 'Capability Agent CPU (%)',
        left: [
          new cloudwatch.MathExpression({
            expression: `REMOVE_EMPTY(SEARCH('{AWS/ECS,ClusterName,ServiceName} MetricName="CPUUtilization" ClusterName="${clusterName}" NOT ServiceName="${voiceServiceName}"', 'Average', 60))`,
            label: '',
            period: cdk.Duration.minutes(1),
          }),
        ],
        leftYAxis: {
          min: 0,
          max: 100,
          label: 'Percent',
        },
        width: 8,
        height: 6,
      }),
      // Capability Agent Memory Utilization
      new cloudwatch.GraphWidget({
        title: 'Capability Agent Memory (%)',
        left: [
          new cloudwatch.MathExpression({
            expression: `REMOVE_EMPTY(SEARCH('{AWS/ECS,ClusterName,ServiceName} MetricName="MemoryUtilization" ClusterName="${clusterName}" NOT ServiceName="${voiceServiceName}"', 'Average', 60))`,
            label: '',
            period: cdk.Duration.minutes(1),
          }),
        ],
        leftYAxis: {
          min: 0,
          max: 100,
          label: 'Percent',
        },
        width: 8,
        height: 6,
      })
    );

    return dashboard;
  }

  /**
   * Creates CloudWatch Logs Insights saved queries for common debugging scenarios.
   */
  private createSavedQueries(_props: VoiceAgentMonitoringProps, resourcePrefix: string): void {
    const logGroupName = `/ecs/${resourcePrefix}-voice-agent`;

    // Query: Recent calls with errors
    new logs.QueryDefinition(this, 'RecentErrorsQuery', {
      queryDefinitionName: `${resourcePrefix}/recent-errors`,
      queryString: new logs.QueryString({
        fields: ['@timestamp', 'call_id', 'completion_status', 'error_category', 'session_id'],
        filterStatements: ['event = "call_summary"', 'completion_status = "error"'],
        sort: '@timestamp desc',
        limit: 20,
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup', logGroupName)],
    });

    // Query: High latency calls
    new logs.QueryDefinition(this, 'HighLatencyQuery', {
      queryDefinitionName: `${resourcePrefix}/high-latency-calls`,
      queryString: new logs.QueryString({
        fields: ['@timestamp', 'call_id', 'AvgAgentResponseLatency', 'TurnCount', 'session_id'],
        filterStatements: ['event = "call_summary"', 'AvgAgentResponseLatency > 2000'],
        sort: 'AvgAgentResponseLatency desc',
        limit: 20,
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup2', logGroupName)],
    });

    // Query: Tool usage statistics
    new logs.QueryDefinition(this, 'ToolUsageQuery', {
      queryDefinitionName: `${resourcePrefix}/tool-usage`,
      queryString: new logs.QueryString({
        fields: ['@timestamp', 'call_id', 'tool_name', 'ToolStatus', 'ToolExecutionTime'],
        filterStatements: ['event = "tool_execution"'],
        statsStatements: [
          'count() as invocations, avg(ToolExecutionTime) as avg_duration by tool_name, ToolStatus',
        ],
        sort: 'invocations desc',
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup3', logGroupName)],
    });

    // Query: Conversation flow for a specific call
    new logs.QueryDefinition(this, 'ConversationFlowQuery', {
      queryDefinitionName: `${resourcePrefix}/conversation-flow`,
      queryString: new logs.QueryString({
        fields: ['@timestamp', 'turn_number', 'speaker', 'content'],
        filterStatements: ['call_id = "CALL_ID_PLACEHOLDER"', 'event = "conversation_turn"'],
        sort: '@timestamp asc',
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup4', logGroupName)],
    });

    // Query: Audio quality issues
    new logs.QueryDefinition(this, 'AudioQualityQuery', {
      queryDefinitionName: `${resourcePrefix}/audio-quality-issues`,
      queryString: new logs.QueryString({
        fields: ['@timestamp', 'call_id', 'avg_rms_db', 'turn_number'],
        filterStatements: ['event = "turn_completed"', 'audio_rms_db < -55'],
        sort: '@timestamp desc',
        limit: 50,
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup5', logGroupName)],
    });

    // Query: Interruption analysis
    new logs.QueryDefinition(this, 'InterruptionsQuery', {
      queryDefinitionName: `${resourcePrefix}/interruption-analysis`,
      queryString: new logs.QueryString({
        fields: ['@timestamp', 'call_id', 'InterruptionCount', 'TurnCount', 'session_id'],
        filterStatements: ['event = "call_summary"', 'InterruptionCount > 0'],
        sort: 'InterruptionCount desc',
        limit: 20,
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup6', logGroupName)],
    });

    // Query: Call summary overview
    new logs.QueryDefinition(this, 'CallSummaryQuery', {
      queryDefinitionName: `${resourcePrefix}/call-summary`,
      queryString: new logs.QueryString({
        fields: [
          '@timestamp',
          'call_id',
          'session_id',
          'duration_seconds',
          'TurnCount',
          'completion_status',
          'error_category',
          'AvgE2ELatency',
        ],
        filterStatements: ['event = "call_summary"'],
        sort: '@timestamp desc',
        limit: 100,
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup7', logGroupName)],
    });

    // Query: Trace specific call
    new logs.QueryDefinition(this, 'TraceCallQuery', {
      queryDefinitionName: `${resourcePrefix}/trace-call`,
      queryString: new logs.QueryString({
        fields: [
          '@timestamp',
          'event',
          'turn_number',
          'speaker',
          'content',
          'error_category',
          'AvgE2ELatency',
        ],
        filterStatements: ['call_id = "CALL_ID_PLACEHOLDER"'],
        sort: '@timestamp asc',
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup8', logGroupName)],
    });

    // Query: Task protection and scaling events
    new logs.QueryDefinition(this, 'ScalingEventsQuery', {
      queryDefinitionName: `${resourcePrefix}/scaling-events`,
      queryString: new logs.QueryString({
        fields: [
          '@timestamp',
          'event',
          'protection_enabled',
          'active_sessions',
          'signal',
          'elapsed_seconds',
        ],
        filterStatements: [
          'event in ["task_protection_updated", "task_protection_all_retries_exhausted", "drain_started", "drain_waiting", "drain_complete"]',
        ],
        sort: '@timestamp desc',
        limit: 50,
      }),
      logGroups: [logs.LogGroup.fromLogGroupName(this, 'LogGroup9', logGroupName)],
    });
  }
}
