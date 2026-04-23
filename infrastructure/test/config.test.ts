import * as cdk from 'aws-cdk-lib';
import { loadConfig } from '../src/config';

describe('Configuration', () => {
  describe('loadConfig', () => {
    it('should load valid configuration from context', () => {
      const app = new cdk.App({
        context: {
          'voice-agent:environment': 'poc',
          'voice-agent:region': 'us-east-1',
          'voice-agent:projectName': 'test-voice-agent',
          'voice-agent:vpcCidr': '10.0.0.0/16',
          'voice-agent:maxAzs': '3',
          'voice-agent:natGateways': '2',
        },
      });

      const config = loadConfig(app);

      expect(config.environment).toBe('poc');
      expect(config.region).toBe('us-east-1');
      expect(config.projectName).toBe('test-voice-agent');
      expect(config.vpcCidr).toBe('10.0.0.0/16');
      expect(config.maxAzs).toBe(3);
      expect(config.natGateways).toBe(2);
    });

    it('should throw error for invalid environment', () => {
      const app = new cdk.App({
        context: {
          'voice-agent:environment': 'invalid',
          'voice-agent:region': 'us-east-1',
          'voice-agent:projectName': 'test',
          'voice-agent:vpcCidr': '10.0.0.0/16',
          'voice-agent:maxAzs': '3',
          'voice-agent:natGateways': '2',
        },
      });

      expect(() => loadConfig(app)).toThrow(/Invalid environment/);
    });

    it('should throw error for invalid region format', () => {
      const app = new cdk.App({
        context: {
          'voice-agent:environment': 'poc',
          'voice-agent:region': 'invalid-region',
          'voice-agent:projectName': 'test',
          'voice-agent:vpcCidr': '10.0.0.0/16',
          'voice-agent:maxAzs': '3',
          'voice-agent:natGateways': '2',
        },
      });

      expect(() => loadConfig(app)).toThrow(/Invalid region format/);
    });

    it('should throw error for invalid VPC CIDR', () => {
      const app = new cdk.App({
        context: {
          'voice-agent:environment': 'poc',
          'voice-agent:region': 'us-east-1',
          'voice-agent:projectName': 'test',
          'voice-agent:vpcCidr': 'invalid-cidr',
          'voice-agent:maxAzs': '3',
          'voice-agent:natGateways': '2',
        },
      });

      expect(() => loadConfig(app)).toThrow(/Invalid CIDR format/);
    });

    it('should throw error for invalid maxAzs', () => {
      const app = new cdk.App({
        context: {
          'voice-agent:environment': 'poc',
          'voice-agent:region': 'us-east-1',
          'voice-agent:projectName': 'test',
          'voice-agent:vpcCidr': '10.0.0.0/16',
          'voice-agent:maxAzs': '10', // Too high
          'voice-agent:natGateways': '2',
        },
      });

      expect(() => loadConfig(app)).toThrow(/maxAzs must be between/);
    });

    it('should throw error for natGateways greater than maxAzs', () => {
      const app = new cdk.App({
        context: {
          'voice-agent:environment': 'poc',
          'voice-agent:region': 'us-east-1',
          'voice-agent:projectName': 'test',
          'voice-agent:vpcCidr': '10.0.0.0/16',
          'voice-agent:maxAzs': '2',
          'voice-agent:natGateways': '5', // Greater than maxAzs
        },
      });

      expect(() => loadConfig(app)).toThrow(/natGateways must be between/);
    });

    it('defaults natGateways by environment when context+env unset', () => {
      // Clear any test-env NAT_GATEWAYS override that could mask defaults.
      const originalEnv = process.env.NAT_GATEWAYS;
      delete process.env.NAT_GATEWAYS;
      try {
        const expectations: Record<string, number> = {
          poc: 1,
          dev: 1,
          staging: 2,
          prod: 3,
        };
        for (const [env, expected] of Object.entries(expectations)) {
          const app = new cdk.App({
            context: {
              'voice-agent:environment': env,
              'voice-agent:region': 'us-east-1',
              'voice-agent:projectName': 'test',
              'voice-agent:vpcCidr': '10.0.0.0/16',
              'voice-agent:maxAzs': '3',
              // natGateways context intentionally omitted — testing defaults
            },
          });

          const config = loadConfig(app);
          expect(config.natGateways).toBe(expected);
        }
      } finally {
        if (originalEnv !== undefined) {
          process.env.NAT_GATEWAYS = originalEnv;
        }
      }
    });

    it('explicit natGateways context still overrides env-based default', () => {
      const app = new cdk.App({
        context: {
          'voice-agent:environment': 'poc', // would default to 1
          'voice-agent:region': 'us-east-1',
          'voice-agent:projectName': 'test',
          'voice-agent:vpcCidr': '10.0.0.0/16',
          'voice-agent:maxAzs': '3',
          'voice-agent:natGateways': '3', // explicit override
        },
      });

      const config = loadConfig(app);
      expect(config.natGateways).toBe(3);
    });

    it('should accept all valid environments', () => {
      const validEnvironments = ['poc', 'dev', 'staging', 'prod'];

      for (const env of validEnvironments) {
        const app = new cdk.App({
          context: {
            'voice-agent:environment': env,
            'voice-agent:region': 'us-east-1',
            'voice-agent:projectName': 'test',
            'voice-agent:vpcCidr': '10.0.0.0/16',
            'voice-agent:maxAzs': '3',
            'voice-agent:natGateways': '2',
          },
        });

        const config = loadConfig(app);
        expect(config.environment).toBe(env);
      }
    });
  });
});
