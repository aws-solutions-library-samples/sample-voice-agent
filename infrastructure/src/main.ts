#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { loadConfig } from './config';
import { NetworkStack, StorageStack, SageMakerStubStack, EcsStack, BotRunnerStack } from './stacks';

const app = new cdk.App();

// Load and validate configuration
const config = loadConfig(app);

// Environment configuration for all stacks
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT || process.env.AWS_ACCOUNT_ID,
  region: config.region,
};

/**
 * Stack instantiation with dependencies via SSM Parameters.
 *
 * SSM Parameters are used for cross-stack communication to:
 * - Avoid cyclic dependencies
 * - Allow independent stack deployments
 * - Support multi-account/region scenarios
 *
 * Deployment order (enforced by addDependency):
 *
 * 1. Network Stack (no dependencies)
 *    └── Writes: VPC ID, Subnet IDs, Security Group IDs
 *
 * 2. Storage Stack (depends on Network for VPC endpoints)
 *    └── Writes: Secret ARN, KMS Key ARN
 *
 * 3. SageMaker Stub Stack — cloud-API mode only
 *    └── Writes stub SSM params for STT/TTS endpoint names
 *    └── Downstream stacks read these params but the values are
 *        placeholders; the Pipecat runtime uses cloud APIs
 *        (Deepgram STT + ElevenLabs TTS) configured via Secrets
 *        Manager, not the stub values.
 *    └── This fork removed the real SageMakerStack (self-hosted
 *        model endpoints). If you ever need self-hosted mode,
 *        restore SageMakerStack from upstream and branch on
 *        `config.sageMakerEnabled` again.
 *
 * 4. ECS Stack (depends on Network, Storage, SageMaker stub)
 *    └── Reads: VPC ID, Subnet IDs, Secret ARN, STT/TTS endpoint
 *         name params (stub values in cloud-API mode)
 *    └── Writes: Cluster ARN, Task Definition ARN, Task SG ID
 *    └── Runs pipecat with asyncio.run() - the pattern pipecat expects
 *
 * 5. BotRunner Stack (depends on Network, Storage, SageMaker, ECS)
 *    └── Reads: VPC ID, Subnet IDs, Lambda SG ID, Secret ARN, ECS ARNs
 *    └── Writes: Webhook URL
 */

// Phase 2: Network Stack (no dependencies)
const networkStack = new NetworkStack(app, 'VoiceAgentNetwork', {
  env,
  config,
  description: 'Voice Agent POC - Network infrastructure (VPC, subnets, endpoints)',
  tags: {
    Project: config.projectName,
    Environment: config.environment,
    Phase: '2',
  },
});

// Phase 3: Storage Stack
const storageStack = new StorageStack(app, 'VoiceAgentStorage', {
  env,
  config,
  description: 'Voice Agent POC - Storage infrastructure (Secrets Manager)',
  tags: {
    Project: config.projectName,
    Environment: config.environment,
    Phase: '3',
  },
});
storageStack.addDependency(networkStack);

// Phase 4: SageMaker Stub Stack (cloud-API mode only in this fork)
const sagemakerStack = new SageMakerStubStack(app, 'VoiceAgentSageMaker', {
  env,
  config,
  description: 'Voice Agent POC - Cloud API mode (SageMaker skipped)',
  tags: {
    Project: config.projectName,
    Environment: config.environment,
    Phase: '4',
    Mode: 'cloud-api',
  },
});
sagemakerStack.addDependency(networkStack);

// Phase 6: ECS Stack
// ECS Fargate properly supports pipecat's async patterns
const ecsStack = new EcsStack(app, 'VoiceAgentEcs', {
  env,
  config,
  description: 'Voice Agent POC - ECS Fargate for Pipecat',
  tags: {
    Project: config.projectName,
    Environment: config.environment,
    Phase: '6',
  },
});
ecsStack.addDependency(networkStack);
ecsStack.addDependency(storageStack);

// Phase 7: Bot Runner Stack
const botrunnerStack = new BotRunnerStack(app, 'VoiceAgentBotRunner', {
  env,
  config,
  description: 'Voice Agent POC - Bot Runner Lambda and API Gateway',
  tags: {
    Project: config.projectName,
    Environment: config.environment,
    Phase: '7',
  },
});
botrunnerStack.addDependency(networkStack);
botrunnerStack.addDependency(storageStack);
botrunnerStack.addDependency(sagemakerStack);
botrunnerStack.addDependency(ecsStack);

app.synth();
