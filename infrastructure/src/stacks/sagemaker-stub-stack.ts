import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { VoiceAgentConfig } from '../config';
import { SSM_PARAMS } from '../ssm-parameters';

/**
 * Props for SageMakerStubStack
 */
export interface SageMakerStubStackProps extends cdk.StackProps {
  readonly config: VoiceAgentConfig;
}

/**
 * Stub SageMaker stack for cloud API mode.
 * Creates SSM parameters with placeholder values so downstream stacks can deploy.
 * Use this when STT_PROVIDER and TTS_PROVIDER are set to cloud APIs (deepgram/elevenlabs).
 */
export class SageMakerStubStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: SageMakerStubStackProps) {
    super(scope, id, props);

    // Create placeholder SSM parameters for downstream stacks
    // These won't be used at runtime when cloud APIs are configured
    new ssm.StringParameter(this, 'SttEndpointNameParam', {
      parameterName: SSM_PARAMS.STT_ENDPOINT_NAME,
      stringValue: 'cloud-api-mode-stt-not-deployed',
      description: 'Placeholder - using cloud STT API instead of SageMaker',
    });

    new ssm.StringParameter(this, 'TtsEndpointNameParam', {
      parameterName: SSM_PARAMS.TTS_ENDPOINT_NAME,
      stringValue: 'cloud-api-mode-tts-not-deployed',
      description: 'Placeholder - using cloud TTS API instead of SageMaker',
    });

    // Output for visibility
    new cdk.CfnOutput(this, 'SttEndpointName', {
      value: 'cloud-api-mode-stt-not-deployed',
      description: 'STT: Using Deepgram cloud API (not SageMaker)',
    });

    new cdk.CfnOutput(this, 'TtsEndpointName', {
      value: 'cloud-api-mode-tts-not-deployed',
      description: 'TTS: Using ElevenLabs cloud API (not SageMaker)',
    });

    new cdk.CfnOutput(this, 'Note', {
      value: 'Set STT_PROVIDER=deepgram and TTS_PROVIDER=elevenlabs in Pipecat container',
      description: 'Cloud API mode configuration note',
    });
  }
}
