import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { SageMakerStack } from '../src/stacks/sagemaker-stack';
import { BotRunnerStack } from '../src/stacks/bot-runner-stack';
import { SSM_PARAMS } from '../src/ssm-parameters';
import { TEST_CONFIG, TEST_ENV, BOTRUNNER_SSM_CONTEXT } from './helpers';

describe('SageMakerStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new SageMakerStack(app, 'TestSageMakerStack', {
      env: TEST_ENV,
      config: TEST_CONFIG,
    });
    template = Template.fromStack(stack);
  });

  it('should create SageMaker execution role', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Effect: 'Allow',
            Principal: {
              Service: 'sagemaker.amazonaws.com',
            },
          }),
        ]),
      },
    });
  });

  it('should create STT model', () => {
    template.hasResourceProperties('AWS::SageMaker::Model', {
      ModelName: Match.stringLikeRegexp('stt-model'),
    });
  });

  it('should create TTS model', () => {
    template.hasResourceProperties('AWS::SageMaker::Model', {
      ModelName: Match.stringLikeRegexp('tts-model'),
    });
  });

  it('should create STT endpoint config with instance pools, g6.2xlarge as top priority', () => {
    template.hasResourceProperties('AWS::SageMaker::EndpointConfig', {
      EndpointConfigName: Match.stringLikeRegexp('stt-config'),
      ProductionVariants: Match.arrayWith([
        Match.objectLike({
          VariantName: 'AllTraffic',
          InstancePools: [
            Match.objectLike({ InstanceType: 'ml.g6.2xlarge', Priority: 1 }),
            Match.objectLike({ InstanceType: 'ml.g6e.2xlarge', Priority: 2 }),
            Match.objectLike({ InstanceType: 'ml.g5.2xlarge', Priority: 3 }),
            Match.objectLike({ InstanceType: 'ml.g4dn.2xlarge', Priority: 4 }),
          ],
        }),
      ]),
    });
  });

  it('should create TTS endpoint config with instance pools, g6.12xlarge as top priority', () => {
    template.hasResourceProperties('AWS::SageMaker::EndpointConfig', {
      EndpointConfigName: Match.stringLikeRegexp('tts-config'),
      ProductionVariants: Match.arrayWith([
        Match.objectLike({
          VariantName: 'AllTraffic',
          InstancePools: [
            Match.objectLike({ InstanceType: 'ml.g6.12xlarge', Priority: 1 }),
            Match.objectLike({ InstanceType: 'ml.g6e.12xlarge', Priority: 2 }),
            Match.objectLike({ InstanceType: 'ml.g5.12xlarge', Priority: 3 }),
            Match.objectLike({ InstanceType: 'ml.g4dn.12xlarge', Priority: 4 }),
          ],
        }),
      ]),
    });
  });

  it('should not use a single fixed InstanceType for STT/TTS variants (would reintroduce ICE risk)', () => {
    template.hasResourceProperties('AWS::SageMaker::EndpointConfig', {
      ProductionVariants: Match.arrayWith([
        Match.objectLike({
          VariantName: 'AllTraffic',
          InstanceType: Match.absent(),
        }),
      ]),
    });
  });

  it('should create STT endpoint', () => {
    template.hasResourceProperties('AWS::SageMaker::Endpoint', {
      EndpointName: Match.stringLikeRegexp('stt-endpoint'),
    });
  });

  it('should create TTS endpoint', () => {
    template.hasResourceProperties('AWS::SageMaker::Endpoint', {
      EndpointName: Match.stringLikeRegexp('tts-endpoint'),
    });
  });

  it('should create CloudWatch alarms for STT latency', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: Match.stringLikeRegexp('stt-latency'),
      MetricName: 'ModelLatency',
      Namespace: 'AWS/SageMaker',
    });
  });

  it('should create CloudWatch alarms for TTS latency', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: Match.stringLikeRegexp('tts-latency'),
      MetricName: 'ModelLatency',
      Namespace: 'AWS/SageMaker',
    });
  });

  it('should create SSM parameter for STT endpoint name', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.STT_ENDPOINT_NAME,
    });
  });

  it('should create SSM parameter for TTS endpoint name', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.TTS_ENDPOINT_NAME,
    });
  });

  it('should output STT endpoint name', () => {
    template.hasOutput('SttEndpointName', {});
  });

  it('should output TTS endpoint name', () => {
    template.hasOutput('TtsEndpointName', {});
  });

  it('should output SageMaker execution role ARN', () => {
    template.hasOutput('SageMakerExecutionRoleArn', {});
  });
});

describe('BotRunnerStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App({ context: BOTRUNNER_SSM_CONTEXT });
    const stack = new BotRunnerStack(app, 'TestBotRunnerStack', {
      env: TEST_ENV,
      config: TEST_CONFIG,
    });
    template = Template.fromStack(stack);
  });

  it('should create a Lambda function', () => {
    template.resourceCountIs('AWS::Lambda::Function', 1);
  });

  it('should create Lambda with Python 3.11 runtime', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Runtime: 'python3.11',
    });
  });

  it('should create API Gateway REST API', () => {
    template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
  });

  it('should create API Gateway with POST method', () => {
    template.resourceCountIs('AWS::ApiGateway::Method', 1);
    template.hasResourceProperties('AWS::ApiGateway::Method', {
      HttpMethod: 'POST',
    });
  });

  it('should create SSM parameter for webhook URL', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: SSM_PARAMS.WEBHOOK_URL,
    });
  });

  it('should output webhook endpoint', () => {
    template.hasOutput('WebhookEndpoint', {});
  });

  it('should grant Lambda permission to read secrets', () => {
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

  it('should configure Lambda with ECS service endpoint', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({
          ECS_SERVICE_ENDPOINT: Match.anyValue(),
        }),
      },
    });
  });
});
