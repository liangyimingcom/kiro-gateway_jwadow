/**
 * ComputeStack — ECS Cluster / Fargate Service / ALB / 目标组 / （可选）ACM·WAF。
 *
 * 本 Stack 实现 Gateway_Service 的池化计算与单一入口（需求 1/2/3/11）：
 *
 *   - **ECS Cluster**：部署于 NetworkStack 的 VPC，启用 Container Insights 以采集运行实例数等指标。
 *   - **Fargate TaskDefinition**：按 `config.instanceSize`（CPU/内存）创建无服务器任务（需求 1.1、1.5）；
 *     容器镜像由仓库根目录 Dockerfile 经 `ContainerImage.fromAsset` 构建（`cdk deploy` 自动构建/推送 ECR）；
 *     容器监听 8000，注入 `STORAGE_BACKEND=aws` 等环境变量；使用 iam.ts 的最小权限任务角色（需求 6.4、9.6）。
 *   - **FargateService**：任务运行于私有子网（无公网入站），跨 ≥2 AZ，`desiredCount ≥ 2`（需求 1.2、3.1）；
 *     配置健康检查宽限期与停止超时以配合优雅停机（需求 4.5）。
 *   - **ALB**：部署于公有子网、跨 ≥2 AZ（需求 2.1、3.2）；空闲超时 ≥ `STREAMING_READ_TIMEOUT`
 *     以避免 SSE 长流被提前断开（需求 2.5、2.6）；`GET /health` 健康检查（需求 2.4）；
 *     目标组 deregistration delay（draining）实现安全缩容（需求 4.5）。
 *   - **HTTPS（可选）**：提供 ACM 证书 ARN 时启用 HTTPS:443 并将 HTTP:80 重定向至 HTTPS；
 *     未提供时回退到 HTTP:80（开发/无域名场景；生产建议提供证书）。
 *   - **WAF（可选）**：提供开关时在 ALB 前置 AWS WAF 基础防护。
 *   - **Auto Scaling**：装配 GatewayAutoScaling（CPU 70% 目标跟踪 + 可选请求并发）。
 *
 * 通过 public readonly 暴露 cluster / service / loadBalancer / targetGroup，供 ObservabilityStack
 * （15.3）与 app.ts（15.4）消费。
 *
 * _Requirements: 1.1, 1.2, 1.3, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.3, 3.4_
 */
import * as path from 'path';
import {
  CfnOutput,
  Duration,
  Stack,
  StackProps,
  aws_dynamodb as dynamodb,
  aws_ec2 as ec2,
  aws_ecs as ecs,
  aws_elasticloadbalancingv2 as elbv2,
  aws_kms as kms,
  aws_logs as logs,
  aws_s3 as s3,
  aws_secretsmanager as secretsmanager,
  aws_wafv2 as wafv2,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { GatewayConfig } from './config';
import { GatewayRoles } from './iam';
import { GatewayAutoScaling } from './autoscaling-construct';

export interface ComputeStackProps extends StackProps {
  readonly config: GatewayConfig;

  // --- NetworkStack 引用 ---
  /** 部署 VPC。 */
  readonly vpc: ec2.IVpc;
  /** 公有子网选择（放置 ALB）。 */
  readonly publicSubnets: ec2.SubnetSelection;
  /** 私有子网选择（放置 Fargate 任务）。 */
  readonly privateSubnets: ec2.SubnetSelection;

  // --- DataStack 引用 ---
  /** State_Store DynamoDB 表。 */
  readonly table: dynamodb.ITable;
  /** 静态加密 KMS 密钥。 */
  readonly encryptionKey: kms.IKey;
  /** `<stack>/proxy-api-key` 密钥。 */
  readonly proxyApiKeySecret: secretsmanager.ISecret;
  /** `<stack>/credentials-json` 密钥。 */
  readonly credentialsJsonSecret: secretsmanager.ISecret;
  /** 各账号 token 密钥名称前缀。 */
  readonly accountSecretPrefix: string;
  /** SSM Config_Store 参数前缀。 */
  readonly configParameterPrefix: string;
  /** 配置桶。 */
  readonly configBucket: s3.IBucket;
  /** 调试日志桶。 */
  readonly debugLogsBucket: s3.IBucket;

  // --- 可选参数 ---
  /** 镜像构建上下文目录（含 Dockerfile）。默认仓库根目录（相对 infra/lib 上两级）。 */
  readonly imageContextPath?: string;
  /**
   * 流式读取超时（秒）。决定 ALB 空闲超时下限（需求 2.6）。默认 300。
   */
  readonly streamingReadTimeoutSeconds?: number;
  /** 优雅停机超时（秒）。容器 stopTimeout 与健康检查宽限期据此设置。默认 120。 */
  readonly gracefulShutdownTimeoutSeconds?: number;
  /** 可选：ACM 证书 ARN。提供时启用 HTTPS 监听并将 HTTP 重定向至 HTTPS。 */
  readonly certificateArn?: string;
  /** 可选：是否在 ALB 前置 AWS WAF（默认 false）。 */
  readonly enableWaf?: boolean;
  /** 可选：每目标请求数伸缩阈值（传递给 GatewayAutoScaling）。 */
  readonly requestsPerTarget?: number;
}

const CONTAINER_NAME = 'gateway';
const CONTAINER_PORT = 8000;
// Fargate 容器 stopTimeout 上限为 120 秒。
const MAX_FARGATE_STOP_TIMEOUT_SECONDS = 120;

export class ComputeStack extends Stack {
  /** ECS Cluster。 */
  public readonly cluster: ecs.Cluster;
  /** Gateway Fargate Service。 */
  public readonly service: ecs.FargateService;
  /** 公网 Application Load Balancer。 */
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  /** ALB 目标组（供伸缩与可观测性消费）。 */
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  /** 最小权限任务角色。 */
  public readonly taskRole: GatewayRoles;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const { config } = props;
    const prefix = config.stackName;
    const streamingReadTimeout = props.streamingReadTimeoutSeconds ?? 300;
    const gracefulShutdownTimeout = Math.min(
      props.gracefulShutdownTimeoutSeconds ?? 120,
      MAX_FARGATE_STOP_TIMEOUT_SECONDS,
    );
    // 至少 2 个实例以满足高可用（需求 1.2、3.1）。
    const desiredCount = Math.max(2, config.minInstances);

    // -------------------------------------------------------------------
    // ECS Cluster（启用 Container Insights）。
    // -------------------------------------------------------------------
    this.cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: `${prefix}-gateway`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // -------------------------------------------------------------------
    // 最小权限角色（任务角色 + 执行角色）。
    // -------------------------------------------------------------------
    this.taskRole = new GatewayRoles(this, 'Roles', {
      config,
      table: props.table,
      encryptionKey: props.encryptionKey,
      proxyApiKeySecret: props.proxyApiKeySecret,
      credentialsJsonSecret: props.credentialsJsonSecret,
      accountSecretPrefix: props.accountSecretPrefix,
      configParameterPrefix: props.configParameterPrefix,
      configBucket: props.configBucket,
      debugLogsBucket: props.debugLogsBucket,
    });

    // -------------------------------------------------------------------
    // Fargate 任务定义 + 容器。
    // -------------------------------------------------------------------
    const taskDefinition = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: config.instanceSize.cpu,
      memoryLimitMiB: config.instanceSize.memoryMiB,
      taskRole: this.taskRole.taskRole,
      executionRole: this.taskRole.executionRole,
      family: `${prefix}-gateway`,
    });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${prefix}-gateway`,
      retention: logs.RetentionDays.ONE_MONTH,
    });

    // 镜像由仓库根目录 Dockerfile 构建（cdk deploy 自动构建并推送 ECR）。
    const imageContext = props.imageContextPath ?? path.resolve(__dirname, '..', '..');

    const container = taskDefinition.addContainer(CONTAINER_NAME, {
      image: ecs.ContainerImage.fromAsset(imageContext, {
        // 排除 IaC 自身与版本控制目录，避免将 infra/cdk.out 递归拷入镜像构建上下文。
        exclude: ['infra', 'cdk.out', '.git'],
      }),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'gateway', logGroup }),
      // 优雅停机：收到 SIGTERM 后给在途请求（含 SSE）完成的时间（需求 4.5）。
      stopTimeout: Duration.seconds(gracefulShutdownTimeout),
      environment: {
        // 选择 AWS 存储后端（需求 5.1）。
        STORAGE_BACKEND: 'aws',
        STACK_NAME: prefix,
        AWS_REGION: this.region,
        // State_Store / Object_Store 资源名（应用亦可由 STACK_NAME 推导 `<stack>-gateway-state`）。
        GATEWAY_STATE_TABLE: props.table.tableName,
        DEBUG_S3_BUCKET: props.debugLogsBucket.bucketName,
        CONFIG_S3_BUCKET: props.configBucket.bucketName,
        // Config_Store / Secret_Store 前缀与固定密钥名。
        CONFIG_PARAMETER_PREFIX: props.configParameterPrefix,
        ACCOUNT_SECRET_PREFIX: props.accountSecretPrefix,
        PROXY_API_KEY_SECRET: props.proxyApiKeySecret.secretName,
        CREDENTIALS_JSON_SECRET: props.credentialsJsonSecret.secretName,
        // 超时参数（应用启动默认值；运行时仍以 SSM 为准）。
        STREAMING_READ_TIMEOUT: `${streamingReadTimeout}`,
        GRACEFUL_SHUTDOWN_TIMEOUT: `${gracefulShutdownTimeout}`,
      },
    });
    container.addPortMappings({
      containerPort: CONTAINER_PORT,
      protocol: ecs.Protocol.TCP,
    });

    // -------------------------------------------------------------------
    // Fargate Service（私有子网，跨 ≥2 AZ）。
    // -------------------------------------------------------------------
    this.service = new ecs.FargateService(this, 'Service', {
      cluster: this.cluster,
      serviceName: `${prefix}-gateway`,
      taskDefinition,
      desiredCount,
      assignPublicIp: false,
      vpcSubnets: props.privateSubnets,
      // 健康检查宽限期：给容器启动留出时间，避免启动期被误判不健康（需求 3.4）。
      healthCheckGracePeriod: Duration.seconds(Math.max(60, gracefulShutdownTimeout)),
      // 部署期保持容量，结合滚动更新维持可用性。
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      // 部署失败自动回滚。
      circuitBreaker: { rollback: true },
      enableExecuteCommand: false,
    });

    // -------------------------------------------------------------------
    // ALB（公有子网，跨 ≥2 AZ）+ 监听器 + 目标组。
    // -------------------------------------------------------------------
    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      loadBalancerName: `${prefix}-gateway`,
      vpc: props.vpc,
      internetFacing: true,
      vpcSubnets: props.publicSubnets,
      // 空闲超时 ≥ 流式读取超时，避免 SSE 长流被 ALB 提前断开（需求 2.6）。
      idleTimeout: Duration.seconds(streamingReadTimeout),
    });

    const listener = this.createListener(props.certificateArn);

    this.targetGroup = listener.addTargets('GatewayTargets', {
      port: CONTAINER_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [
        this.service.loadBalancerTarget({
          containerName: CONTAINER_NAME,
          containerPort: CONTAINER_PORT,
        }),
      ],
      // 缩容/部署时排空在途连接（draining），配合优雅停机（需求 4.5）。
      deregistrationDelay: Duration.seconds(Math.max(60, gracefulShutdownTimeout)),
      healthCheck: {
        path: '/health',
        port: 'traffic-port',
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        interval: Duration.seconds(30),
        timeout: Duration.seconds(10),
        healthyHttpCodes: '200',
      },
    });

    // -------------------------------------------------------------------
    // Auto Scaling（CPU 70% 目标跟踪 + 可选请求并发）。
    // -------------------------------------------------------------------
    new GatewayAutoScaling(this, 'Scaling', {
      service: this.service,
      config,
      targetGroup: this.targetGroup,
      requestsPerTarget: props.requestsPerTarget,
    });

    // -------------------------------------------------------------------
    // 可选 WAF：在 ALB 前置 AWS 托管基础规则集。
    // -------------------------------------------------------------------
    if (props.enableWaf) {
      this.attachWaf(prefix);
    }

    // -------------------------------------------------------------------
    // ALB 访问入口输出（需求 9.4）：部署成功后打印 Load_Balancer 访问地址。
    //   - LoadBalancerDns：ALB DNS 名称（裸主机名）。
    //   - GatewayEndpoint：完整 http(s):// URL —— 提供证书时为 https://，否则 http://。
    // -------------------------------------------------------------------
    const scheme = props.certificateArn ? 'https' : 'http';
    const endpointUrl = `${scheme}://${this.loadBalancer.loadBalancerDnsName}`;

    new CfnOutput(this, 'LoadBalancerDns', {
      value: this.loadBalancer.loadBalancerDnsName,
      description: 'ALB 访问入口 DNS 名称。',
    });

    // 醒目命名的访问入口：部署成功后据此直接访问网关（需求 9.4）。
    new CfnOutput(this, 'GatewayEndpoint', {
      value: endpointUrl,
      description: 'Load_Balancer 访问入口完整 URL（部署成功后访问网关的入口地址）。',
      exportName: `${prefix}-gateway-endpoint`,
    });
  }

  /**
   * 创建监听器：
   *   - 提供 ACM 证书 ARN → HTTPS:443（绑定证书），并将 HTTP:80 永久重定向至 HTTPS。
   *   - 未提供 → HTTP:80（无证书回退；生产建议提供证书以启用 TLS）。
   */
  private createListener(certificateArn?: string): elbv2.ApplicationListener {
    if (certificateArn) {
      const httpsListener = this.loadBalancer.addListener('HttpsListener', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [elbv2.ListenerCertificate.fromArn(certificateArn)],
        sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS,
      });
      // HTTP:80 → HTTPS:443 重定向。
      this.loadBalancer.addListener('HttpRedirectListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS',
          port: '443',
          permanent: true,
        }),
      });
      return httpsListener;
    }

    return this.loadBalancer.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
    });
  }

  /** 在 ALB 前置 AWS WAF（REGIONAL 作用域 + AWS 托管通用规则集）。 */
  private attachWaf(prefix: string): void {
    const webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      name: `${prefix}-gateway`,
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `${prefix}-gateway-waf`,
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: 'AWSManagedCommonRuleSet',
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: `${prefix}-gateway-common`,
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    new wafv2.CfnWebACLAssociation(this, 'WebAclAssociation', {
      resourceArn: this.loadBalancer.loadBalancerArn,
      webAclArn: webAcl.attrArn,
    });
  }
}
