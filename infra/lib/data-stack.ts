/**
 * DataStack — 状态 / 密钥 / 配置 / 对象存储（State_Store / Secret_Store / Config_Store / Object_Store）。
 *
 * 本 Stack 实现“状态外置 / 配置与密钥外置”所需的全部托管存储资源，供 ComputeStack（任务 15）
 * 以最小权限挂载与授权：
 *
 *   - DynamoDB 单表（single-table design）`<stack>-gateway-state`：
 *       分区键 PK / 排序键 SK，PAY_PER_REQUEST 按需计费（需求 12.3），TTL 属性 `expires_at`，
 *       KMS 静态加密（需求 6.3），开启时间点恢复（PITR）。DynamoDB 天然跨多 AZ 冗余（需求 3.5）。
 *   - Secrets Manager：`<stack>/proxy-api-key`、`<stack>/credentials-json`，KMS 加密（需求 6.3）；
 *       各账号 token（`<stack>/account/<account_id>/token`）由应用在运行时通过 put_secret 创建。
 *   - SSM Parameter Store：`/<stack>/config/*` 下的可调参数（超时 / 熔断退避 / 伸缩等），含合理默认值。
 *   - S3：配置桶（存放 `config/credentials.json` 骨架）与调试日志桶 `<stack>-gateway-debug`，
 *       均启用 KMS 加密（需求 6.3）、阻断公共访问；调试日志桶含按保留期过期的生命周期规则（需求 12.4）。
 *
 * 表 / 密钥 / 参数前缀 / 桶均以 public readonly 属性暴露，便于 ComputeStack/IAM（任务 15）授予
 * 最小权限访问。
 *
 * _Requirements: 3.5, 6.3, 12.3, 12.4_
 */
import {
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  Tags,
  aws_dynamodb as dynamodb,
  aws_kms as kms,
  aws_s3 as s3,
  aws_secretsmanager as secretsmanager,
  aws_ssm as ssm,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { GatewayConfig } from './config';

export interface DataStackProps extends StackProps {
  readonly config: GatewayConfig;
  /**
   * 调试日志在 Object_Store 中的保留天数（需求 12.4）。
   * 缺省时回退到 CDK context 键 `debugLogRetentionDays`，再回退到 30 天。
   */
  readonly debugLogRetentionDays?: number;
}

/**
 * 各 Config_Store 参数的默认值（在 Operator 未通过 SSM 显式覆盖时使用）。
 * 取值对齐设计文档「Config_Store（SSM Parameter Store）参数布局」与应用现有默认值。
 */
interface ConfigParameterSpec {
  readonly name: string;
  readonly value: string;
  readonly description: string;
}

export class DataStack extends Stack {
  /** State_Store：DynamoDB 单表（账号状态 / 锁 / 统计 / 模型映射 / 令牌元数据）。 */
  public readonly table: dynamodb.Table;

  /** 客户管理的 KMS 密钥，统一用于 DynamoDB / Secrets Manager / S3 的静态加密（需求 6.3）。 */
  public readonly encryptionKey: kms.Key;

  /** `<stack>/proxy-api-key`：客户端鉴权密钥 PROXY_API_KEY。 */
  public readonly proxyApiKeySecret: secretsmanager.Secret;

  /** `<stack>/credentials-json`：多账号 `credentials.json` 的敏感内容。 */
  public readonly credentialsJsonSecret: secretsmanager.Secret;

  /**
   * 各账号 token 密钥的名称前缀（`<stack>/account`）。
   * 运行时应用以 `<stack>/account/<account_id>/token` 创建/写回（put_secret），
   * IAM（任务 15）据此前缀授予 `secretsmanager:*` 的资源级最小权限。
   */
  public readonly accountSecretPrefix: string;

  /** Config_Store 参数前缀（`/<stack>/config`），供 SSM `GetParametersByPath` 与 IAM 授权使用。 */
  public readonly configParameterPrefix: string;

  /** 配置桶：存放非敏感配置骨架（如 `config/credentials.json`）。 */
  public readonly configBucket: s3.Bucket;

  /** 调试日志归档桶 `<stack>-gateway-debug`（含保留期生命周期规则，需求 12.4）。 */
  public readonly debugLogsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    const { config } = props;
    const prefix = config.stackName;

    // 调试日志保留天数：props > context > 默认 30 天。
    const retentionDays = DataStack.resolveRetentionDays(
      props.debugLogRetentionDays,
      this.node.tryGetContext('debugLogRetentionDays'),
    );

    // ---------------------------------------------------------------------
    // KMS：客户管理密钥（CMK），统一加密 State_Store / Secret_Store / Object_Store。
    // 启用自动轮换以满足安全合规（需求 6.3）。
    // ---------------------------------------------------------------------
    this.encryptionKey = new kms.Key(this, 'DataKey', {
      alias: `${prefix}-gateway-data`,
      description: `${prefix} gateway 静态加密密钥（DynamoDB / Secrets Manager / S3）`,
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------------
    // DynamoDB：单表设计（PK/SK），PAY_PER_REQUEST（需求 12.3），TTL=expires_at，
    // KMS 加密（需求 6.3），PITR 开启。DynamoDB 多 AZ 冗余满足需求 3.5。
    // ---------------------------------------------------------------------
    this.table = new dynamodb.Table(this, 'StateTable', {
      tableName: `${prefix}-gateway-state`,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires_at',
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: this.encryptionKey,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      // State_Store 承载运行时共享状态，删除 Stack 时保留以防误删导致状态丢失。
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------------
    // Secrets Manager：KMS 加密（需求 6.3）。
    //   - proxy-api-key：随机生成的客户端鉴权密钥（Operator 可后续覆盖）。
    //   - credentials-json：多账号配置敏感内容，初始为占位 JSON 骨架。
    // 各账号 token（<stack>/account/<id>/token）由应用运行时创建，此处仅暴露前缀供授权。
    // ---------------------------------------------------------------------
    this.proxyApiKeySecret = new secretsmanager.Secret(this, 'ProxyApiKeySecret', {
      secretName: `${prefix}/proxy-api-key`,
      description: `${prefix} 客户端鉴权密钥 PROXY_API_KEY`,
      encryptionKey: this.encryptionKey,
      generateSecretString: {
        passwordLength: 48,
        excludePunctuation: true,
        excludeUppercase: false,
      },
    });

    this.credentialsJsonSecret = new secretsmanager.Secret(this, 'CredentialsJsonSecret', {
      secretName: `${prefix}/credentials-json`,
      description: `${prefix} 多账号 credentials.json 敏感内容`,
      encryptionKey: this.encryptionKey,
      generateSecretString: {
        // 占位骨架：Operator 部署后通过控制台/CLI 覆盖为真实多账号配置。
        secretStringTemplate: JSON.stringify({ accounts: [] }),
        generateStringKey: 'placeholder',
      },
    });

    this.accountSecretPrefix = `${prefix}/account`;

    // ---------------------------------------------------------------------
    // SSM Parameter Store：/<stack>/config/* 可调参数（含合理默认值）。
    // 参数与默认值对齐设计文档与应用现有默认。
    // ---------------------------------------------------------------------
    this.configParameterPrefix = `/${prefix}/config`;
    this.createConfigParameters(this.configParameterPrefix, config);

    // ---------------------------------------------------------------------
    // S3：配置桶 + 调试日志桶。均 KMS 加密（需求 6.3）、阻断公共访问、强制 TLS。
    // ---------------------------------------------------------------------
    this.configBucket = new s3.Bucket(this, 'ConfigBucket', {
      bucketName: `${prefix}-gateway-config`,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      // 配置桶承载部署期配置骨架，保留以防误删。
      removalPolicy: RemovalPolicy.RETAIN,
    });

    this.debugLogsBucket = new s3.Bucket(this, 'DebugLogsBucket', {
      bucketName: `${prefix}-gateway-debug`,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.encryptionKey,
      bucketKeyEnabled: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      // 调试日志按保留期过期清除（需求 12.4）。
      lifecycleRules: [
        {
          id: 'expire-debug-logs',
          enabled: true,
          expiration: Duration.days(retentionDays),
          // 同步清理过期对象的旧版本与未完成分段上传，避免残留成本。
          abortIncompleteMultipartUploadAfter: Duration.days(7),
        },
      ],
      // 调试日志为非关键数据，可随 Stack 自动清理。
      autoDeleteObjects: true,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // 资源标签，便于成本归集与运维识别。
    Tags.of(this).add('app', 'kiro-gateway');
    Tags.of(this).add('stack', prefix);

    this.createOutputs(retentionDays);
  }

  /**
   * 创建 Config_Store 的默认参数（String 类型）。
   * 默认值在 Operator 未显式覆盖时生效；运行时配置优先级为 SSM > 环境变量 > 代码默认。
   */
  private createConfigParameters(parameterPrefix: string, config: GatewayConfig): void {
    const specs: ConfigParameterSpec[] = [
      {
        name: 'STREAMING_READ_TIMEOUT',
        value: '300',
        description: '流式读取超时（秒）。',
      },
      {
        name: 'FIRST_TOKEN_TIMEOUT',
        value: '15',
        description: '首个 token 到达超时（秒）。',
      },
      {
        name: 'ACCOUNT_RECOVERY_TIMEOUT',
        value: '60',
        description: '账号熔断恢复基准超时（秒）。',
      },
      {
        name: 'ACCOUNT_MAX_BACKOFF_MULTIPLIER',
        value: '1440',
        description: '账号熔断指数退避的最大倍数上限。',
      },
      {
        name: 'ACCOUNT_PROBABILISTIC_RETRY_CHANCE',
        value: '0.1',
        description: '熔断窗口内的概率性重试机率（0–1）。',
      },
      {
        name: 'scaling/min',
        value: `${config.minInstances}`,
        description: 'Auto Scaling 最小实例数。',
      },
      {
        name: 'scaling/max',
        value: `${config.maxInstances}`,
        description: 'Auto Scaling 最大实例数。',
      },
      {
        name: 'scaling/cpu-target',
        value: '70',
        description: 'CPU 目标跟踪伸缩的目标利用率（百分比）。',
      },
    ];

    specs.forEach((spec) => {
      // 用名称生成稳定且合法的 construct id（去除路径分隔符等）。
      const constructId = `Param${spec.name
        .split(/[^a-zA-Z0-9]/)
        .filter((part) => part.length > 0)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join('')}`;
      new ssm.StringParameter(this, constructId, {
        parameterName: `${parameterPrefix}/${spec.name}`,
        stringValue: spec.value,
        description: spec.description,
        tier: ssm.ParameterTier.STANDARD,
      });
    });
  }

  /** 暴露关键资源标识，便于跨 Stack 引用与运维查看。 */
  private createOutputs(retentionDays: number): void {
    new CfnOutput(this, 'StateTableName', {
      value: this.table.tableName,
      description: 'DynamoDB State_Store 表名。',
    });
    new CfnOutput(this, 'EncryptionKeyArn', {
      value: this.encryptionKey.keyArn,
      description: '静态加密 KMS 密钥 ARN。',
    });
    new CfnOutput(this, 'ProxyApiKeySecretArn', {
      value: this.proxyApiKeySecret.secretArn,
      description: 'PROXY_API_KEY 密钥 ARN。',
    });
    new CfnOutput(this, 'CredentialsJsonSecretArn', {
      value: this.credentialsJsonSecret.secretArn,
      description: 'credentials.json 密钥 ARN。',
    });
    new CfnOutput(this, 'ConfigParameterPrefix', {
      value: this.configParameterPrefix,
      description: 'SSM Config_Store 参数前缀。',
    });
    new CfnOutput(this, 'AccountSecretPrefix', {
      value: this.accountSecretPrefix,
      description: '各账号 token 密钥名称前缀。',
    });
    new CfnOutput(this, 'ConfigBucketName', {
      value: this.configBucket.bucketName,
      description: 'S3 配置桶名称。',
    });
    new CfnOutput(this, 'DebugLogsBucketName', {
      value: this.debugLogsBucket.bucketName,
      description: `S3 调试日志桶名称（保留 ${retentionDays} 天）。`,
    });
  }

  /** 解析调试日志保留天数：props > context > 默认 30，过滤非法/非正值。 */
  private static resolveRetentionDays(fromProps?: number, fromContext?: unknown): number {
    const candidates: unknown[] = [fromProps, fromContext];
    for (const candidate of candidates) {
      if (candidate === undefined || candidate === null || candidate === '') {
        continue;
      }
      const parsed = Number.parseInt(`${candidate}`, 10);
      if (!Number.isNaN(parsed) && parsed > 0) {
        return parsed;
      }
    }
    return 30;
  }
}
