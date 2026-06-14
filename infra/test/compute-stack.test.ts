/**
 * ComputeStack — CDK 合规与快照测试（CDK compliance + snapshot tests）。
 *
 * 任务 15.5：为计算 Stack 编写 CDK 断言与快照测试，覆盖：
 *   - ALB 公网入口 + 监听器存在（需求 2.1、2.2）。
 *   - ALB 空闲超时 ≥ 流式读取超时（需求 2.6）。
 *   - 目标组 `GET /health` 健康检查 + deregistration delay（draining）（需求 2.4、4.5）。
 *   - ECS Fargate Service `desiredCount ≥ 2`、TaskDefinition CPU/内存、容器端口 8000、
 *     注入 `STORAGE_BACKEND=aws`（需求 1.1、1.2、1.5）。
 *   - Auto Scaling：ScalableTarget min/max 等于配置、CPU 目标跟踪 70%（需求 4.1–4.4）。
 *   - IAM 最小权限：任务角色不含 `Action:* Resource:*` 管理员语句，且
 *     dynamodb/secrets/ssm/s3 语句均收敛到具体资源 ARN（需求 6.4、9.6）。
 *   - 无证书时监听器为 HTTP:80（测试自控配置）。
 *   - 模板快照 `toMatchSnapshot()`。
 *
 * 为使快照与资产哈希稳定且与仓库内容无关，测试通过 `imageContextPath` 传入一个仅含
 * 固定内容 Dockerfile 的临时构建上下文目录（容器镜像仅在 `cdk deploy` 时构建，synth 阶段
 * 只做指纹计算，不触发 docker build）。
 *
 * _Requirements: 2.6, 6.4, 8.4, 8.5, 9.6_
 */
import * as os from 'os';
import * as fs from 'fs';
import * as path from 'path';
import { App, Environment } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { GatewayConfig } from '../lib/config';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';

// 确定性环境（account/region 固定），保证跨 Stack 引用与快照稳定。
const TEST_ENV: Environment = { account: '123456789012', region: 'us-east-1' };

// 样例 GatewayConfig（对齐 lib/config.ts 的默认合法 Fargate 组合）。
const TEST_CONFIG: GatewayConfig = {
  stackName: 'kiro-gateway',
  region: 'us-east-1',
  account: '123456789012',
  minInstances: 2,
  maxInstances: 6,
  instanceSize: { cpu: 512, memoryMiB: 1024 },
};

// 流式读取超时（秒）：决定 ALB 空闲超时下限（需求 2.6）。
const STREAMING_READ_TIMEOUT = 300;

const toArray = <T>(value: T | T[] | undefined): T[] => {
  if (value === undefined) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
};

/**
 * 装配 network → data → compute（镜像 stub）并返回 compute 的合成模板。
 * 镜像构建上下文使用临时目录中的固定 Dockerfile，保证资产哈希确定且与仓库内容解耦。
 */
function synthComputeStack(imageContextPath: string): Template {
  const app = new App();

  const network = new NetworkStack(app, 'kiro-gateway-network', {
    config: TEST_CONFIG,
    env: TEST_ENV,
  });
  const data = new DataStack(app, 'kiro-gateway-data', {
    config: TEST_CONFIG,
    env: TEST_ENV,
  });

  const compute = new ComputeStack(app, 'kiro-gateway-compute', {
    config: TEST_CONFIG,
    env: TEST_ENV,
    // NetworkStack 引用（mirror infra/bin/app.ts）。
    vpc: network.vpc,
    publicSubnets: network.publicSubnets,
    privateSubnets: network.privateSubnets,
    // DataStack 引用。
    table: data.table,
    encryptionKey: data.encryptionKey,
    proxyApiKeySecret: data.proxyApiKeySecret,
    credentialsJsonSecret: data.credentialsJsonSecret,
    accountSecretPrefix: data.accountSecretPrefix,
    configParameterPrefix: data.configParameterPrefix,
    configBucket: data.configBucket,
    debugLogsBucket: data.debugLogsBucket,
    // 确定性资产上下文（仅含固定 Dockerfile）。
    imageContextPath,
    // 显式设置流式读取超时；未提供 certificateArn → 监听器回退 HTTP:80。
    streamingReadTimeoutSeconds: STREAMING_READ_TIMEOUT,
  });

  return Template.fromStack(compute);
}

/**
 * 收集挂载到任务角色（roleName=`kiro-gateway-task`）的全部 IAM 策略语句。
 * 通过角色逻辑 ID 与 IAM::Policy 的 Roles 引用关联，兼容 minimizePolicies 合并后的结构。
 */
function taskRoleStatements(template: Template): any[] {
  const resources = template.toJSON().Resources as Record<string, any>;

  const taskRoleId = Object.keys(resources).find(
    (id) =>
      resources[id].Type === 'AWS::IAM::Role' &&
      resources[id].Properties?.RoleName === `${TEST_CONFIG.stackName}-gateway-task`,
  );
  expect(taskRoleId).toBeDefined();

  const statements: any[] = [];
  for (const id of Object.keys(resources)) {
    const res = resources[id];
    if (res.Type !== 'AWS::IAM::Policy') {
      continue;
    }
    const roles = toArray<any>(res.Properties?.Roles);
    const attachedToTaskRole = roles.some((ref) => ref && ref.Ref === taskRoleId);
    if (!attachedToTaskRole) {
      continue;
    }
    statements.push(...toArray<any>(res.Properties?.PolicyDocument?.Statement));
  }
  return statements;
}

describe('ComputeStack 合规测试', () => {
  let imageContextPath: string;
  let template: Template;

  beforeAll(() => {
    // 创建仅含固定 Dockerfile 的临时构建上下文，保证资产哈希确定。
    imageContextPath = fs.mkdtempSync(path.join(os.tmpdir(), 'kiro-gw-img-'));
    fs.writeFileSync(
      path.join(imageContextPath, 'Dockerfile'),
      'FROM public.ecr.aws/docker/library/python:3.11-slim\n',
    );
    template = synthComputeStack(imageContextPath);
  });

  afterAll(() => {
    fs.rmSync(imageContextPath, { recursive: true, force: true });
  });

  test('ALB 为公网入口（internet-facing）且存在监听器', () => {
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      Match.objectLike({ Scheme: 'internet-facing' }),
    );
    // 监听器存在（无证书时仅 HTTP:80 一个）。
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 1);
  });

  test('ALB 空闲超时 ≥ 流式读取超时（需求 2.6）', () => {
    const albs = template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer');
    const alb = Object.values(albs)[0] as any;
    const attributes = toArray<any>(alb.Properties?.LoadBalancerAttributes);
    const idleTimeout = attributes.find(
      (attr) => attr.Key === 'idle_timeout.timeout_seconds',
    );
    expect(idleTimeout).toBeDefined();
    expect(Number(idleTimeout.Value)).toBeGreaterThanOrEqual(STREAMING_READ_TIMEOUT);
  });

  test('未提供证书时监听器为 HTTP:80', () => {
    template.hasResourceProperties(
      'AWS::ElasticLoadBalancingV2::Listener',
      Match.objectLike({ Protocol: 'HTTP', Port: 80 }),
    );
  });

  test('目标组健康检查路径为 /health 且设置了 deregistration delay（draining）（需求 2.4/4.5）', () => {
    const targetGroups = template.findResources('AWS::ElasticLoadBalancingV2::TargetGroup');
    const targetGroup = Object.values(targetGroups)[0] as any;
    expect(targetGroup.Properties?.HealthCheckPath).toBe('/health');

    const attributes = toArray<any>(targetGroup.Properties?.TargetGroupAttributes);
    const deregistrationDelay = attributes.find(
      (attr) => attr.Key === 'deregistration_delay.timeout_seconds',
    );
    expect(deregistrationDelay).toBeDefined();
    expect(Number(deregistrationDelay.Value)).toBeGreaterThan(0);
  });

  test('ECS Fargate Service desiredCount ≥ 2（需求 1.2、3.1）', () => {
    const services = template.findResources('AWS::ECS::Service');
    const service = Object.values(services)[0] as any;
    expect(service.Properties?.DesiredCount).toBeGreaterThanOrEqual(2);
    expect(service.Properties?.LaunchType).toBe('FARGATE');
  });

  test('TaskDefinition 使用配置的 CPU/内存、容器端口 8000、注入 STORAGE_BACKEND=aws', () => {
    template.hasResourceProperties(
      'AWS::ECS::TaskDefinition',
      Match.objectLike({
        // Fargate 任务定义 CPU/内存在 CloudFormation 中为字符串。
        Cpu: `${TEST_CONFIG.instanceSize.cpu}`,
        Memory: `${TEST_CONFIG.instanceSize.memoryMiB}`,
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            PortMappings: Match.arrayWith([Match.objectLike({ ContainerPort: 8000 })]),
            Environment: Match.arrayWith([
              Match.objectLike({ Name: 'STORAGE_BACKEND', Value: 'aws' }),
            ]),
          }),
        ]),
      }),
    );
  });

  test('Auto Scaling：ScalableTarget min/max 等于配置，CPU 目标跟踪 70%（需求 4.x）', () => {
    template.hasResourceProperties(
      'AWS::ApplicationAutoScaling::ScalableTarget',
      Match.objectLike({
        MinCapacity: TEST_CONFIG.minInstances,
        MaxCapacity: TEST_CONFIG.maxInstances,
      }),
    );

    template.hasResourceProperties(
      'AWS::ApplicationAutoScaling::ScalingPolicy',
      Match.objectLike({
        PolicyType: 'TargetTrackingScaling',
        TargetTrackingScalingPolicyConfiguration: Match.objectLike({
          TargetValue: 70,
          PredefinedMetricSpecification: Match.objectLike({
            PredefinedMetricType: 'ECSServiceAverageCPUUtilization',
          }),
        }),
      }),
    );
  });

  describe('IAM 最小权限（需求 6.4、9.6）', () => {
    test('任务角色不存在 Action:* + Resource:* 的管理员语句', () => {
      const statements = taskRoleStatements(template);
      expect(statements.length).toBeGreaterThan(0);

      for (const statement of statements) {
        const actions = toArray<unknown>(statement.Action);
        const resources = toArray<unknown>(statement.Resource);
        const grantsAllActions = actions.includes('*');
        const grantsAllResources = resources.includes('*');
        // 不允许同时通配动作与资源（管理员级语句）。
        expect(grantsAllActions && grantsAllResources).toBe(false);
      }
    });

    test('dynamodb/secrets/ssm/s3 语句均收敛到具体资源 ARN（非 Resource:*）', () => {
      const statements = taskRoleStatements(template);
      const servicePrefixes = ['dynamodb:', 'secretsmanager:', 'ssm:', 's3:'];

      for (const prefix of servicePrefixes) {
        const matching = statements.filter((statement) =>
          toArray<unknown>(statement.Action).some(
            (action) => typeof action === 'string' && action.startsWith(prefix),
          ),
        );
        // 该服务的权限语句应当存在……
        expect(matching.length).toBeGreaterThan(0);
        // ……且其资源均为具体 ARN（结构化引用），而非通配 '*'。
        for (const statement of matching) {
          for (const resource of toArray<unknown>(statement.Resource)) {
            expect(resource).not.toBe('*');
          }
        }
      }
    });
  });

  test('合成模板快照（toMatchSnapshot）', () => {
    expect(template.toJSON()).toMatchSnapshot();
  });
});
