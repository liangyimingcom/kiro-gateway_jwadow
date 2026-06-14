/**
 * IAM 角色定义 — 最小权限任务角色 + 执行角色（least-privilege ECS roles）。
 *
 * 本模块构建 ECS Fargate 任务所需的两个 IAM 角色，遵循“最小权限”（需求 6.4、9.6）：
 *
 *   - **任务角色（taskRole）**：容器内应用进程的身份，仅授予访问本部署 DataStack 资源所需的
 *     具体动作与具体资源 ARN：
 *       · DynamoDB：对 State_Store 表及其索引的读写。
 *       · Secrets Manager：对 `<stack>/proxy-api-key`、`<stack>/credentials-json` 的读取，
 *         对 credentials-json 的写入，以及对 `<stack>/account/*` 前缀的
 *         GetSecretValue/PutSecretValue/CreateSecret/DescribeSecret（运行时写回刷新后的 token）。
 *       · SSM Parameter Store：对 `/<stack>/config/*` 的 GetParameter(s)/GetParametersByPath。
 *       · S3：对配置桶对象的读取、对调试日志桶对象的写入。
 *       · KMS：对 DataStack 客户管理密钥的 Encrypt/Decrypt/GenerateDataKey（覆盖 Secrets 访问
 *         与 S3 SSE-KMS 读写）。
 *       · CloudWatch：PutMetricData（限定命名空间）与应用日志组写入。
 *
 *   - **执行角色（executionRole）**：ECS 代理用于拉取 ECR 镜像、写入容器 stdout 日志组，
 *     并在任务定义经 `secrets` 注入敏感值时解密对应密钥。
 *
 * 实现说明：权限以 **基于身份（identity-based）** 的内联策略语句授予，并以具体资源 ARN 收敛，
 * 既保证最小权限，又避免修改 DataStack 中资源（KMS 密钥 / 密钥 / 桶）的资源策略——后者会在
 * Compute→Data 引用之外引入 Data→Compute 的反向引用，从而形成跨 Stack 循环依赖。DataStack 的
 * KMS 密钥默认信任账号内 IAM，故基于身份的授权即可生效。
 *
 * _Requirements: 6.4, 9.6_
 */
import {
  ArnFormat,
  Stack,
  aws_dynamodb as dynamodb,
  aws_iam as iam,
  aws_kms as kms,
  aws_s3 as s3,
  aws_secretsmanager as secretsmanager,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { GatewayConfig } from './config';

export interface GatewayRolesProps {
  /** 共享部署参数（用于派生命名空间 / 资源前缀）。 */
  readonly config: GatewayConfig;
  /** State_Store DynamoDB 表（授予读写其表与索引）。 */
  readonly table: dynamodb.ITable;
  /** DataStack 客户管理 KMS 密钥（静态加密）。 */
  readonly encryptionKey: kms.IKey;
  /** `<stack>/proxy-api-key` 密钥。 */
  readonly proxyApiKeySecret: secretsmanager.ISecret;
  /** `<stack>/credentials-json` 密钥。 */
  readonly credentialsJsonSecret: secretsmanager.ISecret;
  /** 各账号 token 密钥名称前缀（`<stack>/account`）。 */
  readonly accountSecretPrefix: string;
  /** SSM Config_Store 参数前缀（`/<stack>/config`）。 */
  readonly configParameterPrefix: string;
  /** 配置桶（只读对象）。 */
  readonly configBucket: s3.IBucket;
  /** 调试日志桶（只写对象）。 */
  readonly debugLogsBucket: s3.IBucket;
  /**
   * 自定义 CloudWatch 指标命名空间（PutMetricData 的条件约束）。
   * 默认 `<stack>/gateway`。
   */
  readonly metricNamespace?: string;
}

/**
 * 构建 Gateway 任务所需 IAM 角色（任务角色 + 执行角色）的 Construct。
 */
export class GatewayRoles extends Construct {
  /** 容器内应用进程使用的最小权限任务角色。 */
  public readonly taskRole: iam.Role;

  /** ECS 代理使用的执行角色（拉取镜像 / 写日志 / 注入密钥解密）。 */
  public readonly executionRole: iam.Role;

  /** PutMetricData 受限的 CloudWatch 指标命名空间。 */
  public readonly metricNamespace: string;

  constructor(scope: Construct, id: string, props: GatewayRolesProps) {
    super(scope, id);

    const stack = Stack.of(this);
    const prefix = props.config.stackName;
    this.metricNamespace = props.metricNamespace ?? `${prefix}/gateway`;

    // 复用的资源 ARN。
    const accountSecretArn = stack.formatArn({
      service: 'secretsmanager',
      resource: 'secret',
      // 密钥 ARN 末尾含 6 位随机后缀，`/*` 通配可覆盖前缀下所有账号 token。
      resourceName: `${props.accountSecretPrefix}/*`,
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });
    const configParamArn = stack.formatArn({
      service: 'ssm',
      resource: 'parameter',
      // configParameterPrefix 形如 `/<stack>/config`，去除前导斜杠避免 ARN 出现双斜杠。
      resourceName: `${props.configParameterPrefix.replace(/^\//, '')}/*`,
      arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
    });
    const logGroupArn = stack.formatArn({
      service: 'logs',
      resource: 'log-group',
      resourceName: `/ecs/${prefix}*`,
      arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    });

    // -------------------------------------------------------------------
    // 任务角色（应用身份）。
    // -------------------------------------------------------------------
    this.taskRole = new iam.Role(this, 'TaskRole', {
      roleName: `${prefix}-gateway-task`,
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: `${prefix} Gateway 容器应用的最小权限任务角色`,
    });

    // DynamoDB：State_Store 表与索引读写。
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'StateStoreReadWrite',
        actions: [
          'dynamodb:GetItem',
          'dynamodb:BatchGetItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:ConditionCheckItem',
          'dynamodb:DescribeTable',
        ],
        resources: [props.table.tableArn, `${props.table.tableArn}/index/*`],
      }),
    );

    // Secrets Manager：固定密钥读取 + credentials-json 写入 + 账号 token 前缀。
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'FixedSecretsRead',
        actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
        resources: [
          props.proxyApiKeySecret.secretArn,
          props.credentialsJsonSecret.secretArn,
        ],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CredentialsJsonWrite',
        actions: ['secretsmanager:PutSecretValue'],
        resources: [props.credentialsJsonSecret.secretArn],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AccountTokenSecrets',
        actions: [
          'secretsmanager:GetSecretValue',
          'secretsmanager:PutSecretValue',
          'secretsmanager:CreateSecret',
          'secretsmanager:DescribeSecret',
        ],
        resources: [accountSecretArn],
      }),
    );

    // SSM Parameter Store：/<stack>/config/* 读取（含批量 GetParametersByPath）。
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConfigParameters',
        actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
        resources: [configParamArn],
      }),
    );

    // S3：配置桶对象只读、调试日志桶对象只写。
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ConfigBucketRead',
        actions: ['s3:GetObject', 's3:GetBucketLocation', 's3:ListBucket'],
        resources: [props.configBucket.bucketArn, `${props.configBucket.bucketArn}/*`],
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DebugBucketWrite',
        actions: ['s3:PutObject', 's3:AbortMultipartUpload'],
        resources: [`${props.debugLogsBucket.bucketArn}/*`],
      }),
    );

    // KMS：覆盖 Secrets 访问与 S3 SSE-KMS 读写所需的加解密（限定单一 CMK）。
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DataKeyUse',
        actions: [
          'kms:Decrypt',
          'kms:Encrypt',
          'kms:ReEncrypt*',
          'kms:GenerateDataKey*',
          'kms:DescribeKey',
        ],
        resources: [props.encryptionKey.keyArn],
      }),
    );

    // CloudWatch：自定义指标上报（PutMetricData 不支持资源级授权，按命名空间条件收敛）。
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'PutCustomMetrics',
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'cloudwatch:namespace': this.metricNamespace },
        },
      }),
    );

    // CloudWatch Logs：应用日志组写入（限定 /ecs/<stack>* 前缀）。
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'AppLogs',
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
        resources: [logGroupArn, `${logGroupArn}:*`],
      }),
    );

    // -------------------------------------------------------------------
    // 执行角色（ECS 代理身份）：拉取 ECR 镜像 + 写容器日志 + 注入密钥解密。
    // -------------------------------------------------------------------
    this.executionRole = new iam.Role(this, 'ExecutionRole', {
      roleName: `${prefix}-gateway-exec`,
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: `${prefix} Gateway 任务的 ECS 执行角色`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AmazonECSTaskExecutionRolePolicy',
        ),
      ],
    });

    // 若任务定义经 `secrets` 注入敏感值，执行角色需可读取并解密对应密钥。
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ExecSecretsRead',
        actions: ['secretsmanager:GetSecretValue'],
        resources: [
          props.proxyApiKeySecret.secretArn,
          props.credentialsJsonSecret.secretArn,
        ],
      }),
    );
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'ExecKeyDecrypt',
        actions: ['kms:Decrypt', 'kms:DescribeKey'],
        resources: [props.encryptionKey.keyArn],
      }),
    );
  }
}
