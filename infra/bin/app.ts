#!/usr/bin/env node
/**
 * CDK 应用入口（App entry point）。
 *
 * 装配全部 Stack 并按正确依赖顺序串联：
 *
 *   NetworkStack → DataStack → ComputeStack → ObservabilityStack
 *
 * 该顺序保证 `cdk deploy --all` 先建网络/数据，再建计算，最后建可观测性；
 * `cdk destroy --all` 则按逆序安全拆除（需求 9.2、9.5）。
 *
 * 部署成功后，ComputeStack 会输出 Load_Balancer 访问入口地址（`GatewayEndpoint`，
 * 完整 http(s):// URL）；本入口在 app 层再次以聚合输出 `GatewayEndpoint` 暴露，
 * 便于 `cdk deploy --all` 末尾醒目打印（需求 9.4）。
 *
 * ---------------------------------------------------------------------------
 * 参数（context > 环境变量 > 默认值，见 lib/config.ts 的 DEFAULT_CONFIG）：
 *   region        | AWS_REGION              部署区域（默认 us-east-1）
 *   stackName     | GATEWAY_STACK_NAME      Stack 名称前缀（默认 kiro-gateway）
 *   minInstances  | GATEWAY_MIN_INSTANCES   最小实例数（默认 2）
 *   maxInstances  | GATEWAY_MAX_INSTANCES   最大实例数（默认 6）
 *   cpu           | GATEWAY_CPU             单实例 CPU 单位（默认 512）
 *   memory        | GATEWAY_MEMORY_MIB      单实例内存 MiB（默认 1024）
 *
 * 计算层可选参数（仅 CDK context，缺省安全回退）：
 *   certificateArn          ACM 证书 ARN；提供时启用 HTTPS:443 并将 HTTP:80 重定向（默认无 → HTTP:80）
 *   enableWaf               是否在 ALB 前置 AWS WAF（true/false，默认 false）
 *   requestsPerTarget       每目标请求并发伸缩阈值（正整数，默认不启用该指标）
 *   streamingReadTimeout    流式读取超时秒数，决定 ALB 空闲超时下限（默认 300）
 *   gracefulShutdownTimeout 优雅停机超时秒数（默认 120，上限 120）
 *   logRetentionDays        CloudWatch 日志保留天数（透传可观测性，若 Stack 支持）
 *
 * ---------------------------------------------------------------------------
 * 用法示例：
 *   # 合成模板
 *   npm run synth
 *
 *   # 一键部署全部 Stack（构建镜像 → 推送 ECR → 创建资源）
 *   npm run deploy            # 等价于 `cdk deploy --all`
 *   cdk deploy --all -c region=ap-southeast-1 -c minInstances=2 -c maxInstances=10 \
 *                    -c cpu=1024 -c memory=2048
 *
 *   # 启用 HTTPS + WAF + 请求并发伸缩
 *   cdk deploy --all -c certificateArn=arn:aws:acm:us-east-1:123456789012:certificate/abc \
 *                    -c enableWaf=true -c requestsPerTarget=200
 *
 *   # 通过环境变量传参
 *   GATEWAY_MAX_INSTANCES=12 AWS_REGION=eu-west-1 npm run deploy
 *
 *   # 一键销毁全部 Stack
 *   npm run destroy           # 等价于 `cdk destroy --all`
 *
 * _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
 */
import { App, CfnOutput, Environment } from 'aws-cdk-lib';
import { resolveConfig } from '../lib/config';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { ObservabilityStack } from '../lib/observability-stack';

const app = new App();

// 解析共享参数对象（context > 环境变量 > 默认值）。
const config = resolveConfig(app);

// CDK 环境：region 来自参数；account 可选（缺省时由 CDK 从凭证环境推断）。
const env: Environment = {
  account: config.account ?? process.env.CDK_DEFAULT_ACCOUNT,
  region: config.region,
};

// ---------------------------------------------------------------------------
// 计算层可选参数：仅在 context 提供时生效，否则交由 ComputeStack 内部安全默认值。
// ---------------------------------------------------------------------------
function ctxString(key: string): string | undefined {
  const raw = app.node.tryGetContext(key);
  if (raw === undefined || raw === null || `${raw}` === '') {
    return undefined;
  }
  return `${raw}`;
}

function ctxBool(key: string): boolean | undefined {
  const raw = ctxString(key);
  if (raw === undefined) {
    return undefined;
  }
  return raw.toLowerCase() === 'true' || raw === '1';
}

function ctxPositiveInt(key: string): number | undefined {
  const raw = ctxString(key);
  if (raw === undefined) {
    return undefined;
  }
  const parsed = Number.parseInt(raw, 10);
  return Number.isNaN(parsed) || parsed <= 0 ? undefined : parsed;
}

const certificateArn = ctxString('certificateArn');
const enableWaf = ctxBool('enableWaf');
const requestsPerTarget = ctxPositiveInt('requestsPerTarget');
const streamingReadTimeoutSeconds = ctxPositiveInt('streamingReadTimeout');
const gracefulShutdownTimeoutSeconds = ctxPositiveInt('gracefulShutdownTimeout');

// ---------------------------------------------------------------------------
// Stack 装配（各 Stack 共享同一份 config 参数对象，确保参数一致）。
// ---------------------------------------------------------------------------

// 1) 网络：VPC、公有/私有子网（跨 ≥2 AZ）。
const network = new NetworkStack(app, `${config.stackName}-network`, { config, env });

// 2) 数据：DynamoDB 表、KMS、Secrets、SSM 前缀、S3 桶。
const data = new DataStack(app, `${config.stackName}-data`, { config, env });

// 3) 计算：ECS Cluster / Fargate Service / ALB / 目标组 /（可选）ACM·WAF·请求并发伸缩。
const compute = new ComputeStack(app, `${config.stackName}-compute`, {
  config,
  env,
  // NetworkStack 引用
  vpc: network.vpc,
  publicSubnets: network.publicSubnets,
  privateSubnets: network.privateSubnets,
  // DataStack 引用
  table: data.table,
  encryptionKey: data.encryptionKey,
  proxyApiKeySecret: data.proxyApiKeySecret,
  credentialsJsonSecret: data.credentialsJsonSecret,
  accountSecretPrefix: data.accountSecretPrefix,
  configParameterPrefix: data.configParameterPrefix,
  configBucket: data.configBucket,
  debugLogsBucket: data.debugLogsBucket,
  // 可选参数（仅在 context 提供时生效）
  certificateArn,
  enableWaf,
  requestsPerTarget,
  streamingReadTimeoutSeconds,
  gracefulShutdownTimeoutSeconds,
});

// 4) 可观测性：CloudWatch 指标 / 告警 / 仪表板（消费计算层的 ALB、目标组、服务、集群）。
const observability = new ObservabilityStack(app, `${config.stackName}-observability`, {
  config,
  env,
  loadBalancer: compute.loadBalancer,
  targetGroup: compute.targetGroup,
  service: compute.service,
  cluster: compute.cluster,
});

// ---------------------------------------------------------------------------
// 显式依赖排序（需求 9.2、9.5）：
//   network → data → compute → observability
// 确保 `cdk deploy --all` 正序创建、`cdk destroy --all` 逆序销毁。
// 计算层同时依赖网络与数据；可观测性依赖计算层。
// ---------------------------------------------------------------------------
data.addDependency(network);
compute.addDependency(network);
compute.addDependency(data);
observability.addDependency(compute);

// ---------------------------------------------------------------------------
// 应用级访问入口聚合输出（需求 9.4）：
// 计算层已输出 `GatewayEndpoint`，此处在 compute Stack 模板中再以 app 级别命名
// 暴露同一入口，便于部署末尾醒目打印 Load_Balancer 访问地址。
// ---------------------------------------------------------------------------
new CfnOutput(compute, 'AppGatewayEndpoint', {
  value: `http${certificateArn ? 's' : ''}://${compute.loadBalancer.loadBalancerDnsName}`,
  description: 'Load_Balancer 访问入口（部署成功后访问网关的完整 URL）。',
});

app.synth();
