/**
 * Centralized SSM Parameter names for cross-stack communication.
 * Using SSM Parameters instead of CloudFormation exports to:
 * - Avoid cyclic dependencies
 * - Allow independent stack deployments
 * - Support multi-account/region scenarios
 */
export const SSM_PARAMS = {
  // Network stack outputs
  VPC_ID: '/voice-agent/network/vpc-id',
  PRIVATE_SUBNET_IDS: '/voice-agent/network/private-subnet-ids',
  ISOLATED_SUBNET_IDS: '/voice-agent/network/isolated-subnet-ids',
  SAGEMAKER_SG_ID: '/voice-agent/network/sagemaker-sg-id',
  LAMBDA_SG_ID: '/voice-agent/network/lambda-sg-id',
  VPC_ENDPOINT_SG_ID: '/voice-agent/network/vpc-endpoint-sg-id',

  // Storage stack outputs
  API_KEY_SECRET_ARN: '/voice-agent/storage/api-key-secret-arn',
  ENCRYPTION_KEY_ARN: '/voice-agent/storage/encryption-key-arn',
  RECORDINGS_KEY_ARN: '/voice-agent/storage/recordings-key-arn',

  // SageMaker stack outputs
  STT_ENDPOINT_NAME: '/voice-agent/sagemaker/stt-endpoint-name',
  TTS_ENDPOINT_NAME: '/voice-agent/sagemaker/tts-endpoint-name',

  // ECS stack outputs
  ECS_CLUSTER_ARN: '/voice-agent/ecs/cluster-arn',
  ECS_TASK_DEFINITION_ARN: '/voice-agent/ecs/task-definition-arn',
  ECS_TASK_SG_ID: '/voice-agent/ecs/task-sg-id',
  ECS_SERVICE_ENDPOINT: '/voice-agent/ecs/service-endpoint',

  // BotRunner stack outputs
  WEBHOOK_URL: '/voice-agent/botrunner/webhook-url',

  // Monitoring stack outputs
  MONITORING_DASHBOARD_NAME: '/voice-agent/monitoring/dashboard-name',
  MONITORING_ALARM_TOPIC_ARN: '/voice-agent/monitoring/alarm-topic-arn',

  // Session tracking outputs
  SESSION_TABLE_NAME: '/voice-agent/sessions/table-name',
  SESSION_TABLE_ARN: '/voice-agent/sessions/table-arn',

  // Knowledge Base outputs
  KNOWLEDGE_BASE_ID: '/voice-agent/knowledge-base/id',
  KNOWLEDGE_BASE_ARN: '/voice-agent/knowledge-base/arn',
  KNOWLEDGE_BASE_BUCKET: '/voice-agent/knowledge-base/bucket-name',

  // CRM outputs
  CRM_API_URL: '/voice-agent/crm/api-url',
  CRM_CUSTOMERS_TABLE_NAME: '/voice-agent/crm/customers-table-name',
  CRM_CASES_TABLE_NAME: '/voice-agent/crm/cases-table-name',
  CRM_INTERACTIONS_TABLE_NAME: '/voice-agent/crm/interactions-table-name',

  // Transfer configuration is set directly via CDK context or env var
  // (voice-agent:transferDestination or TRANSFER_DESTINATION)
  // No external SSM parameters are needed for transfers.

  // A2A Capability Registry outputs
  A2A_NAMESPACE_ID: '/voice-agent/a2a/namespace-id',
  A2A_NAMESPACE_NAME: '/voice-agent/a2a/namespace-name',
} as const;

export type SsmParamKey = keyof typeof SSM_PARAMS;
