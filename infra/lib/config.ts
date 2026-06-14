/**
 * 共享参数 / 配置接口（Typed parameters for all stacks）
 *
 * 本模块集中定义部署参数对象 `GatewayConfig`，并提供从 CDK context（cdk.json / `-c` 标志）
 * 与环境变量解析参数的逻辑，确保 network / data / compute / observability 各 Stack 共享
 * 同一份参数对象。
 *
 * 参数优先级（满足 Requirements 9.3 的参数化输入）：
 *   CDK context（`-c key=value` 或 cdk.json 的 "context"） > 环境变量 > 代码默认值
 *
 * _Requirements: 9.1, 9.3_
 */
import { App } from 'aws-cdk-lib';

/**
 * 单个 Gateway 实例（Fargate Task）的规格。
 * 取值需符合 Fargate 合法的 CPU / 内存组合（由 compute-stack 在任务 15 中校验）。
 */
export interface InstanceSize {
  /** 任务 CPU 单位（1024 = 1 vCPU）。 */
  readonly cpu: number;
  /** 任务内存（MiB）。 */
  readonly memoryMiB: number;
}

/**
 * 全部 Stack 共享的部署参数对象。
 */
export interface GatewayConfig {
  /** CloudFormation Stack 名称前缀（如 `kiro-gateway`）。 */
  readonly stackName: string;
  /** 部署目标 AWS 区域（如 `us-east-1`）。 */
  readonly region: string;
  /** 部署目标 AWS 账号 ID（可选；缺省时由 CDK 从环境推断）。 */
  readonly account?: string;
  /** Auto Scaling 的最小实例数（需求 4.3）。 */
  readonly minInstances: number;
  /** Auto Scaling 的最大实例数（需求 4.3）。 */
  readonly maxInstances: number;
  /** 单实例规格（CPU / 内存，需求 1.5）。 */
  readonly instanceSize: InstanceSize;
}

/**
 * 代码默认值（在 context 与环境变量均缺省时使用）。
 *
 * 默认 2 个实例满足需求 1.2 / 3.1（≥2 实例、跨 ≥2 AZ）。
 * 默认 0.5 vCPU / 1024 MiB 为合法的 Fargate 组合，适合轻量代理负载。
 */
export const DEFAULT_CONFIG: GatewayConfig = {
  stackName: 'kiro-gateway',
  region: 'us-east-1',
  account: undefined,
  minInstances: 2,
  maxInstances: 6,
  instanceSize: {
    cpu: 512,
    memoryMiB: 1024,
  },
};

/**
 * 按优先级解析单个标量参数：CDK context > 环境变量 > 默认值。
 */
function resolveValue(
  app: App,
  contextKey: string,
  envKey: string,
  defaultValue: string,
): string {
  const fromContext = app.node.tryGetContext(contextKey);
  if (fromContext !== undefined && fromContext !== null && `${fromContext}` !== '') {
    return `${fromContext}`;
  }
  const fromEnv = process.env[envKey];
  if (fromEnv !== undefined && fromEnv !== '') {
    return fromEnv;
  }
  return defaultValue;
}

/**
 * 将字符串解析为正整数；非法或非正时回退到默认值。
 */
function toPositiveInt(raw: string, defaultValue: number): number {
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed) || parsed <= 0) {
    return defaultValue;
  }
  return parsed;
}

/**
 * 从 CDK App 的 context、进程环境变量与默认值解析出 `GatewayConfig`。
 *
 * 支持的 context 键 / 环境变量（context 优先）：
 *   stackName     | GATEWAY_STACK_NAME
 *   region        | AWS_REGION / CDK_DEFAULT_REGION
 *   account       | CDK_DEFAULT_ACCOUNT
 *   minInstances  | GATEWAY_MIN_INSTANCES
 *   maxInstances  | GATEWAY_MAX_INSTANCES
 *   cpu           | GATEWAY_CPU
 *   memory        | GATEWAY_MEMORY_MIB
 *
 * 示例：
 *   cdk synth -c stackName=my-gw -c region=ap-southeast-1 \
 *             -c minInstances=2 -c maxInstances=10 -c cpu=1024 -c memory=2048
 */
export function resolveConfig(app: App): GatewayConfig {
  const stackName = resolveValue(app, 'stackName', 'GATEWAY_STACK_NAME', DEFAULT_CONFIG.stackName);

  const region = resolveValue(
    app,
    'region',
    'AWS_REGION',
    process.env.CDK_DEFAULT_REGION ?? DEFAULT_CONFIG.region,
  );

  const accountRaw = resolveValue(app, 'account', 'CDK_DEFAULT_ACCOUNT', '');
  const account = accountRaw === '' ? undefined : accountRaw;

  const minInstances = toPositiveInt(
    resolveValue(app, 'minInstances', 'GATEWAY_MIN_INSTANCES', `${DEFAULT_CONFIG.minInstances}`),
    DEFAULT_CONFIG.minInstances,
  );

  const maxInstances = toPositiveInt(
    resolveValue(app, 'maxInstances', 'GATEWAY_MAX_INSTANCES', `${DEFAULT_CONFIG.maxInstances}`),
    DEFAULT_CONFIG.maxInstances,
  );

  const cpu = toPositiveInt(
    resolveValue(app, 'cpu', 'GATEWAY_CPU', `${DEFAULT_CONFIG.instanceSize.cpu}`),
    DEFAULT_CONFIG.instanceSize.cpu,
  );

  const memoryMiB = toPositiveInt(
    resolveValue(app, 'memory', 'GATEWAY_MEMORY_MIB', `${DEFAULT_CONFIG.instanceSize.memoryMiB}`),
    DEFAULT_CONFIG.instanceSize.memoryMiB,
  );

  // 保证 max >= min，避免非法的伸缩配置。
  const normalizedMax = Math.max(minInstances, maxInstances);

  return {
    stackName,
    region,
    account,
    minInstances,
    maxInstances: normalizedMax,
    instanceSize: { cpu, memoryMiB },
  };
}
