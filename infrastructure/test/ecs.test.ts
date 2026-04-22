import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../src/stacks/ecs-stack';
import { SSM_PARAMS } from '../src/ssm-parameters';
import { TEST_CONFIG, TEST_ENV, ECS_SSM_CONTEXT } from './helpers';

/**
 * EcsStack tests -- one shared template for all ECS assertions.
 * EcsStack synthesis is expensive (~25s) so we do it once.
 */
let template: Template;

beforeAll(() => {
  const app = new cdk.App({ context: ECS_SSM_CONTEXT });
  const stack = new EcsStack(app, 'TestEcsStack', {
    env: TEST_ENV,
    config: TEST_CONFIG,
  });
  template = Template.fromStack(stack);
});

describe('EcsStack Core Resources', () => {
  it('should create an ECS cluster', () => {
    template.resourceCountIs('AWS::ECS::Cluster', 1);
  });

  it('should create ECS cluster with enhanced container insights', () => {
    template.hasResourceProperties('AWS::ECS::Cluster', {
      ClusterSettings: Match.arrayWith([
        Match.objectLike({
          Name: 'containerInsights',
          Value: 'enhanced',
        }),
      ]),
    });
  });

  it('should create a Fargate task definition', () => {
    template.resourceCountIs('AWS::ECS::TaskDefinition', 1);
  });

  it('should create task definition with correct CPU and memory', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      Cpu: '4096',
      Memory: '8192',
      RequiresCompatibilities: ['FARGATE'],
    });
  });

  it('should create task execution role', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Effect: 'Allow',
            Principal: {
              Service: 'ecs-tasks.amazonaws.com',
            },
          }),
        ]),
      },
    });
  });

  it('should create security group for ECS service', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: Match.stringLikeRegexp('Security group for Voice Agent ECS Service'),
    });
  });

  it('should create CloudWatch log group', () => {
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      RetentionInDays: 14,
    });
  });

  it('should output cluster name', () => {
    template.hasOutput('ClusterName', {});
  });

  it('should output task definition family', () => {
    template.hasOutput('TaskDefinitionFamily', {});
  });

  it('should output container image URI', () => {
    template.hasOutput('ContainerImageUri', {});
  });
});

describe('EcsStack SSM Parameters', () => {
  it('should create SSM parameter for cluster ARN', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.ECS_CLUSTER_ARN,
    });
  });

  it('should create SSM parameter for task definition ARN', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.ECS_TASK_DEFINITION_ARN,
    });
  });

  it('should create SSM parameter for task security group ID', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.ECS_TASK_SG_ID,
    });
  });
});

describe('EcsStack Container Environment Variables', () => {
  it('should set SERVICE_MODE to true', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: 'SERVICE_MODE', Value: 'true' })]),
        }),
      ]),
    });
  });

  it('should set SERVICE_PORT to 8080', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: 'SERVICE_PORT', Value: '8080' })]),
        }),
      ]),
    });
  });

  it('should set STT_PROVIDER to sagemaker', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: 'STT_PROVIDER', Value: 'sagemaker' }),
          ]),
        }),
      ]),
    });
  });

  it('should set TTS_PROVIDER to sagemaker', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: 'TTS_PROVIDER', Value: 'sagemaker' }),
          ]),
        }),
      ]),
    });
  });

  it('should set STT_ENDPOINT_NAME', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: 'STT_ENDPOINT_NAME' })]),
        }),
      ]),
    });
  });

  it('should set TTS_ENDPOINT_NAME', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: 'TTS_ENDPOINT_NAME' })]),
        }),
      ]),
    });
  });

  it('should set API_KEY_SECRET_ARN', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: 'API_KEY_SECRET_ARN' })]),
        }),
      ]),
    });
  });

  it('should set ENVIRONMENT', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: 'ENVIRONMENT', Value: 'poc' })]),
        }),
      ]),
    });
  });

  it('should not inject KB_KNOWLEDGE_BASE_ID as container env var (read from SSM at runtime)', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.not(
            Match.arrayWith([Match.objectLike({ Name: 'KB_KNOWLEDGE_BASE_ID' })])
          ),
        }),
      ]),
    });
  });
});

describe('EcsStack IAM Permissions', () => {
  it('should grant Bedrock InvokeModel permissions to task role', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'bedrock:InvokeModel',
              'bedrock:InvokeModelWithResponseStream',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant Secrets Manager read permissions to task role', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'secretsmanager:GetSecretValue',
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant KMS decrypt permissions to task role', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'kms:Decrypt',
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant Bedrock KB retrieval permissions to task role', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant SageMaker InvokeEndpoint permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['sagemaker:InvokeEndpoint']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant SageMaker InvokeEndpointWithBidirectionalStream for BiDi streaming', () => {
    // Critical: BiDi streaming is required for real-time Deepgram STT/TTS.
    // Without this permission, voice pipeline silently fails at runtime.
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['sagemaker:InvokeEndpointWithBidirectionalStream']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant SageMaker InvokeEndpointWithResponseStream', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['sagemaker:InvokeEndpointWithResponseStream']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant DynamoDB PutItem for session tracking', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['dynamodb:PutItem']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant DynamoDB Query for session lookups', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['dynamodb:Query']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant DynamoDB access to table and GSI indexes', () => {
    // Must include /index/* for GSI queries (active session counting)
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['dynamodb:Query']),
            Effect: 'Allow',
            Resource: Match.arrayWith([
              Match.objectLike({
                'Fn::Join': Match.arrayWith([
                  Match.arrayWith([Match.stringLikeRegexp('/index/\\*')]),
                ]),
              }),
            ]),
          }),
        ]),
      },
    });
  });

  it('should grant ssm:GetParameter for runtime config', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['ssm:GetParameter']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should grant ssm:GetParameters for batch config reads', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['ssm:GetParameters']),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  it('should scope SSM permissions to /voice-agent/* path', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['ssm:GetParameter']),
            Effect: 'Allow',
            Resource: Match.stringLikeRegexp('parameter/voice-agent/\\*'),
          }),
        ]),
      },
    });
  });

  it('should grant ServiceDiscovery permissions to task role', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'servicediscovery:DiscoverInstances',
              'servicediscovery:ListServices',
              'servicediscovery:ListNamespaces',
            ]),
            Effect: 'Allow',
            Resource: '*',
          }),
        ]),
      },
    });
  });
});

describe('EcsStack Security Group Ingress', () => {
  it('should allow inbound on port 8080 from NLB', () => {
    // NLB uses TCP passthrough, so the task SG must allow 8080 from anywhere
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({
          IpProtocol: 'tcp',
          FromPort: 8080,
          ToPort: 8080,
          CidrIp: '0.0.0.0/0',
        }),
      ]),
    });
  });

  it('should add port 8443 ingress on SageMaker SG for BiDi streaming', () => {
    // Port 8443 is used for SageMaker HTTP/2 BiDi streaming.
    // Without this rule, STT/TTS streaming fails silently.
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      IpProtocol: 'tcp',
      FromPort: 8443,
      ToPort: 8443,
    });
  });

  it('should add port 443 ingress on VPC endpoint SG for HTTPS', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      IpProtocol: 'tcp',
      FromPort: 443,
      ToPort: 443,
    });
  });
});

describe('EcsStack CloudMap Namespace', () => {
  it('should create a CloudMap HTTP namespace', () => {
    template.hasResourceProperties('AWS::ServiceDiscovery::HttpNamespace', {
      Name: `${TEST_CONFIG.projectName}-${TEST_CONFIG.environment}-capabilities`,
    });
  });

  it('should set A2A_NAMESPACE environment variable on container', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([Match.objectLike({ Name: 'A2A_NAMESPACE' })]),
        }),
      ]),
    });
  });

  it('should create SSM parameter for A2A namespace ID', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.A2A_NAMESPACE_ID,
    });
  });

  it('should create SSM parameter for A2A namespace name', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.A2A_NAMESPACE_NAME,
    });
  });

  it('should output capability namespace name', () => {
    template.hasOutput('CapabilityNamespaceName', {});
  });

  it('should output capability namespace ID', () => {
    template.hasOutput('CapabilityNamespaceId', {});
  });
});

describe('Auto-Scaling', () => {
  test('creates scalable target with correct min/max capacity', () => {
    template.hasResourceProperties('AWS::ApplicationAutoScaling::ScalableTarget', {
      MinCapacity: 1,
      MaxCapacity: 12,
      ScalableDimension: 'ecs:service:DesiredCount',
      ServiceNamespace: 'ecs',
    });
  });

  test('creates target tracking scaling policy on SessionsPerTask (fleet avg)', () => {
    template.hasResourceProperties(
      'AWS::ApplicationAutoScaling::ScalingPolicy',
      Match.objectLike({
        PolicyType: 'TargetTrackingScaling',
        TargetTrackingScalingPolicyConfiguration: Match.objectLike({
          TargetValue: 3,
          DisableScaleIn: true,
          CustomizedMetricSpecification: Match.objectLike({
            MetricName: 'SessionsPerTask',
            Namespace: 'VoiceAgent/Sessions',
            Statistic: 'Average',
          }),
        }),
      })
    );
  });

  test('creates step scaling policy for scale-in', () => {
    // The scale-in policy creates a LowerAlarm that triggers removal of idle tasks.
    // Verify at least one step scaling policy has a negative step adjustment.
    template.hasResourceProperties(
      'AWS::ApplicationAutoScaling::ScalingPolicy',
      Match.objectLike({
        PolicyType: 'StepScaling',
        StepScalingPolicyConfiguration: Match.objectLike({
          AdjustmentType: 'ChangeInCapacity',
          Cooldown: 30,
          StepAdjustments: Match.arrayWith([
            Match.objectLike({
              ScalingAdjustment: -3,
            }),
          ]),
        }),
      })
    );
  });

  test('task role has ECS task protection permissions', () => {
    template.hasResourceProperties(
      'AWS::IAM::Policy',
      Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: ['ecs:GetTaskProtection', 'ecs:UpdateTaskProtection'],
              Effect: 'Allow',
              Resource: '*',
            }),
          ]),
        }),
      })
    );
  });
});

describe('Container Configuration', () => {
  test('container has stop timeout of 120 seconds', () => {
    template.hasResourceProperties(
      'AWS::ECS::TaskDefinition',
      Match.objectLike({
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            StopTimeout: 120,
          }),
        ]),
      })
    );
  });

  test('container has MAX_CONCURRENT_CALLS env var', () => {
    template.hasResourceProperties(
      'AWS::ECS::TaskDefinition',
      Match.objectLike({
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Environment: Match.arrayWith([
              Match.objectLike({
                Name: 'MAX_CONCURRENT_CALLS',
                Value: '10',
              }),
            ]),
          }),
        ]),
      })
    );
  });
});

describe('NLB Configuration', () => {
  test('target group uses /ready health check path', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::TargetGroup',
      Match.objectLike({
        HealthCheckPath: '/ready',
      })
    );
  });

  test('target group has 300s deregistration delay', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::TargetGroup',
      Match.objectLike({
        TargetGroupAttributes: Match.arrayWith([
          Match.objectLike({
            Key: 'deregistration_delay.timeout_seconds',
            Value: '300',
          }),
        ]),
      })
    );
  });
});

describe('Service Deployment Configuration', () => {
  test('service uses minCapacity as desired count', () => {
    template.hasResourceProperties(
      'AWS::ECS::Service',
      Match.objectLike({
        DesiredCount: 1,
      })
    );
  });

  test('service has deployment configuration for safe rollouts', () => {
    template.hasResourceProperties(
      'AWS::ECS::Service',
      Match.objectLike({
        DeploymentConfiguration: Match.objectLike({
          MinimumHealthyPercent: 100,
          MaximumPercent: 200,
        }),
      })
    );
  });
});
