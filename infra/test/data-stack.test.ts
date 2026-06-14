/**
 * DataStack 的 CDK 合规与快照测试（任务 14.3）。
 *
 * 针对状态 / 密钥 / 配置 / 对象存储的安全与成本合规断言（对应 data-stack.ts 与需求 6.3/12.3/12.4）：
 *   - DynamoDB：PAY_PER_REQUEST 按需计费（需求 12.3）、TTL=`expires_at`、KMS 静态加密（需求 6.3）、PITR。
 *   - Secrets Manager：proxy-api-key 与 credentials-json 均存在且 KMS 加密（需求 6.3）。
 *   - SSM：`/<stack>/config/*` 参数齐备（数量与样例名）。
 *   - S3：两个桶均阻断公共访问并使用 KMS 加密（需求 6.3）；调试日志桶含按保留期过期的生命周期规则（需求 12.4）。
 *   - 合成模板的快照断言（回归保护）。
 *
 * 使用字面量 GatewayConfig 与显式 cdk.Environment（account/region），并显式传入调试日志保留期，
 * 确保 `cdk synth` 输出确定、快照稳定。
 *
 * _Requirements: 6.3, 12.3, 12.4_
 */
import { App, Environment } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { GatewayConfig } from '../lib/config';
import { DataStack } from '../lib/data-stack';

/** 显式环境：固定 account/region 使 synth 结果确定，快照可复现。 */
const TEST_ENV: Environment = { account: '123456789012', region: 'us-east-1' };

/** 字面量部署参数对象，匹配 GatewayConfig 接口（避免依赖 cdk.json context）。 */
const TEST_CONFIG: GatewayConfig = {
  stackName: 'test-gateway',
  region: 'us-east-1',
  account: '123456789012',
  minInstances: 2,
  maxInstances: 6,
  instanceSize: { cpu: 512, memoryMiB: 1024 },
};

/** 测试用调试日志保留天数（显式传入以确定 S3 生命周期断言的期望值）。 */
const RETENTION_DAYS = 30;

/** 资源名前缀（= config.stackName），用于断言密钥名 / 参数前缀 / 桶名。 */
const PREFIX = TEST_CONFIG.stackName;

/** 合成 DataStack 并返回其 CloudFormation 模板断言对象。 */
function synthDataTemplate(): Template {
  const app = new App();
  const stack = new DataStack(app, 'test-gateway-data', {
    config: TEST_CONFIG,
    env: TEST_ENV,
    debugLogRetentionDays: RETENTION_DAYS,
  });
  return Template.fromStack(stack);
}

describe('DataStack — DynamoDB State_Store', () => {
  it('采用 PAY_PER_REQUEST 按需计费（需求 12.3）', () => {
    const template = synthDataTemplate();
    template.hasResourceProperties(
      'AWS::DynamoDB::Table',
      Match.objectLike({ BillingMode: 'PAY_PER_REQUEST' }),
    );
  });

  it('在 `expires_at` 上启用 TTL', () => {
    const template = synthDataTemplate();
    template.hasResourceProperties(
      'AWS::DynamoDB::Table',
      Match.objectLike({
        TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
      }),
    );
  });

  it('启用 KMS 静态加密（需求 6.3）', () => {
    const template = synthDataTemplate();
    template.hasResourceProperties(
      'AWS::DynamoDB::Table',
      Match.objectLike({
        SSESpecification: Match.objectLike({ SSEEnabled: true, SSEType: 'KMS' }),
      }),
    );
  });

  it('启用时间点恢复（PITR）', () => {
    const template = synthDataTemplate();
    template.hasResourceProperties(
      'AWS::DynamoDB::Table',
      Match.objectLike({
        PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
      }),
    );
  });
});

describe('DataStack — Secrets Manager Secret_Store（需求 6.3）', () => {
  it('proxy-api-key 与 credentials-json 两个密钥均存在', () => {
    const template = synthDataTemplate();
    expect(Object.keys(template.findResources('AWS::SecretsManager::Secret')).length).toBe(2);
    template.hasResourceProperties(
      'AWS::SecretsManager::Secret',
      Match.objectLike({ Name: `${PREFIX}/proxy-api-key` }),
    );
    template.hasResourceProperties(
      'AWS::SecretsManager::Secret',
      Match.objectLike({ Name: `${PREFIX}/credentials-json` }),
    );
  });

  it('两个密钥均使用 KMS 加密', () => {
    const template = synthDataTemplate();
    const secrets = template.findResources('AWS::SecretsManager::Secret');
    const secretValues = Object.values(secrets);
    expect(secretValues.length).toBe(2);
    for (const secret of secretValues) {
      // KmsKeyId 指向客户管理的 CMK（具体引用形态由 CDK 生成，断言其存在即可）。
      expect(secret.Properties?.KmsKeyId).toBeDefined();
    }
  });
});

describe('DataStack — SSM Config_Store', () => {
  it('`/<stack>/config/*` 参数齐备且包含样例参数', () => {
    const template = synthDataTemplate();
    // data-stack.ts 定义了 8 个默认配置参数。
    template.resourceCountIs('AWS::SSM::Parameter', 8);
    template.hasResourceProperties(
      'AWS::SSM::Parameter',
      Match.objectLike({ Name: `/${PREFIX}/config/STREAMING_READ_TIMEOUT` }),
    );
    template.hasResourceProperties(
      'AWS::SSM::Parameter',
      Match.objectLike({ Name: `/${PREFIX}/config/scaling/min` }),
    );
  });
});

describe('DataStack — S3 Object_Store（需求 6.3 / 12.4）', () => {
  it('两个桶均阻断公共访问并使用 KMS 加密（需求 6.3）', () => {
    const template = synthDataTemplate();
    const buckets = template.findResources('AWS::S3::Bucket');
    const bucketValues = Object.values(buckets);
    expect(bucketValues.length).toBe(2);

    for (const bucket of bucketValues) {
      // 完全阻断公共访问。
      expect(bucket.Properties?.PublicAccessBlockConfiguration).toEqual({
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      });
      // KMS 静态加密。
      const rules =
        bucket.Properties?.BucketEncryption?.ServerSideEncryptionConfiguration ?? [];
      expect(rules.length).toBeGreaterThanOrEqual(1);
      expect(rules[0].ServerSideEncryptionByDefault.SSEAlgorithm).toBe('aws:kms');
    }
  });

  it('调试日志桶含按保留期过期对象的生命周期规则（需求 12.4）', () => {
    const template = synthDataTemplate();
    template.hasResourceProperties(
      'AWS::S3::Bucket',
      Match.objectLike({
        BucketName: `${PREFIX}-gateway-debug`,
        LifecycleConfiguration: {
          Rules: Match.arrayWith([
            Match.objectLike({
              Id: 'expire-debug-logs',
              Status: 'Enabled',
              ExpirationInDays: RETENTION_DAYS,
            }),
          ]),
        },
      }),
    );
  });
});

describe('DataStack — 快照', () => {
  it('合成模板匹配快照（回归保护）', () => {
    const template = synthDataTemplate();
    expect(template.toJSON()).toMatchSnapshot();
  });
});
