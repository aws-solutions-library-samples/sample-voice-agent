import * as dotenv from 'dotenv';
import * as path from 'path';
import { App } from 'aws-cdk-lib';

// Load environment variables from .env file
dotenv.config({ path: path.join(__dirname, '..', '.env') });

/**
 * Valid deployment environments
 */
export type Environment = 'poc' | 'dev' | 'staging' | 'prod';

/**
 * Configuration for the Voice Agent infrastructure.
 * All values are validated at synth time.
 */
export interface VoiceAgentConfig {
  /** Deployment environment */
  environment: Environment;
  /** AWS region for deployment */
  region: string;
  /** Project name prefix for resources */
  projectName: string;
  /** VPC CIDR block */
  vpcCidr: string;
  /** Maximum availability zones */
  maxAzs: number;
  /** Number of NAT gateways */
  natGateways: number;
  /** Auto-scaling: minimum ECS task count */
  minCapacity: number;
  /** Auto-scaling: maximum ECS task count */
  maxCapacity: number;
  /** Auto-scaling: target sessions per task for target tracking */
  targetSessionsPerTask: number;
  /** Per-task session capacity: /ready returns 503 at this limit, NLB stops routing new calls */
  sessionCapacityPerTask: number;
  /** Optional SIP URI for call transfers (e.g., sip:user@pbx:5060). When set, the transfer tool is enabled. */
  transferDestination?: string;
}

/**
 * Validates an environment string.
 */
function validateEnvironment(env: string): Environment {
  const validEnvironments: Environment[] = ['poc', 'dev', 'staging', 'prod'];
  if (!validEnvironments.includes(env as Environment)) {
    throw new Error(
      `Invalid environment '${env}'. Must be one of: ${validEnvironments.join(', ')}`
    );
  }
  return env as Environment;
}

/**
 * Validates AWS region format.
 */
function validateRegion(region: string): string {
  const regionRegex = /^[a-z]{2}-[a-z]+-\d+$/;
  if (!regionRegex.test(region)) {
    throw new Error(`Invalid region format '${region}'. Expected format like 'us-east-1'`);
  }
  return region;
}

/**
 * Validates CIDR block format.
 */
function validateCidr(cidr: string): string {
  const cidrRegex = /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/;
  if (!cidrRegex.test(cidr)) {
    throw new Error(`Invalid CIDR format '${cidr}'. Expected format like '10.0.0.0/16'`);
  }
  return cidr;
}

/**
 * Loads configuration from CDK context and environment variables.
 * Context values override environment variables.
 *
 * @param app CDK App instance
 * @returns Validated configuration
 */
export function loadConfig(app: App): VoiceAgentConfig {
  // Context prefix for voice agent configuration
  const prefix = 'voice-agent';

  // Get values from context or environment, with defaults
  const environment = validateEnvironment(
    app.node.tryGetContext(`${prefix}:environment`) || process.env.ENVIRONMENT || 'poc'
  );

  const region = validateRegion(
    app.node.tryGetContext(`${prefix}:region`) || process.env.AWS_REGION || 'us-east-1'
  );

  const projectName =
    app.node.tryGetContext(`${prefix}:projectName`) || process.env.PROJECT_NAME || 'voice-agent';

  const vpcCidr = validateCidr(
    app.node.tryGetContext(`${prefix}:vpcCidr`) || process.env.VPC_CIDR || '10.0.0.0/16'
  );

  const maxAzs = parseInt(
    app.node.tryGetContext(`${prefix}:maxAzs`) || process.env.MAX_AZS || '3',
    10
  );

  const natGateways = parseInt(
    app.node.tryGetContext(`${prefix}:natGateways`) || process.env.NAT_GATEWAYS || '2',
    10
  );

  const minCapacity = parseInt(
    app.node.tryGetContext(`${prefix}:minCapacity`) || process.env.MIN_CAPACITY || '1',
    10
  );

  const maxCapacity = parseInt(
    app.node.tryGetContext(`${prefix}:maxCapacity`) || process.env.MAX_CAPACITY || '12',
    10
  );

  const targetSessionsPerTask = parseInt(
    app.node.tryGetContext(`${prefix}:targetSessionsPerTask`) ||
      process.env.TARGET_SESSIONS_PER_TASK ||
      '3',
    10
  );

  const sessionCapacityPerTask = parseInt(
    app.node.tryGetContext(`${prefix}:sessionCapacityPerTask`) ||
      process.env.SESSION_CAPACITY_PER_TASK ||
      '10',
    10
  );

  // Validate numeric values
  if (maxAzs < 1 || maxAzs > 6) {
    throw new Error(`maxAzs must be between 1 and 6, got ${maxAzs}`);
  }

  if (natGateways < 0 || natGateways > maxAzs) {
    throw new Error(`natGateways must be between 0 and maxAzs (${maxAzs}), got ${natGateways}`);
  }

  if (minCapacity < 1 || minCapacity > 100) {
    throw new Error(`minCapacity must be between 1 and 100, got ${minCapacity}`);
  }
  if (maxCapacity < minCapacity || maxCapacity > 100) {
    throw new Error(
      `maxCapacity must be between minCapacity (${minCapacity}) and 100, got ${maxCapacity}`
    );
  }
  if (targetSessionsPerTask < 1 || targetSessionsPerTask > 10) {
    throw new Error(`targetSessionsPerTask must be between 1 and 10, got ${targetSessionsPerTask}`);
  }
  if (sessionCapacityPerTask < 1 || sessionCapacityPerTask > 50) {
    throw new Error(
      `sessionCapacityPerTask must be between 1 and 50, got ${sessionCapacityPerTask}`
    );
  }
  if (sessionCapacityPerTask < targetSessionsPerTask) {
    throw new Error(
      `sessionCapacityPerTask (${sessionCapacityPerTask}) must be >= targetSessionsPerTask (${targetSessionsPerTask})`
    );
  }

  // Optional: SIP transfer destination
  // Can be set via CDK context, env var, or left unset to disable transfers
  const transferDestination =
    app.node.tryGetContext(`${prefix}:transferDestination`) ||
    process.env.TRANSFER_DESTINATION ||
    undefined;

  return {
    environment,
    region,
    projectName,
    vpcCidr,
    maxAzs,
    natGateways,
    minCapacity,
    maxCapacity,
    targetSessionsPerTask,
    sessionCapacityPerTask,
    transferDestination,
  };
}
