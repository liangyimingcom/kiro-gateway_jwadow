/**
 * ObservabilityStack — CDK 合规与快照测试（CDK compliance + snapshot tests）。
 *
 * 任务 15.5：为可观测性 Stack 编写 CDK 断言与快照测试，覆盖：
 *   - 存在 CloudWatch 仪表板（需求 8.6）。
 *   - 存在 5xx 错误率告警（阈值 5、约 5 分钟周期，需求 8.4）。
 *   - 存在无健康目标告警（需求 8.5）。
 *   - 存在 SNS 告警主题。
 *   - 模板快照 `toMatchSnapshot()`。
 *
 * ObservabilityStack 消费 ComputeStack 的 ALB / 目标组 / Service / Cluster 引用，
 * 故测试先按 network → data → compute 顺序装配上游 Stack（mirror infra/bin/app.ts），
 * 再实例化 ObservabilityStack。容器镜像构建上下文使用仅含固定 Dockerfile 的临时目录，
 * 保证资产指纹确定（synth 阶段不触发 docker build）。
 *
 * _Requirements: 8.4, 8.5, 8.6_
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
import { ObservabilityStack } from '../lib/observability-stack';

const TEST_ENV: Environment = { account: '123456789012', region: 'us-east-1' };

const TEST_CONFIG: GatewayConfig = {
  stackName: 'kiro-gateway',
  region: 'us-east-1',
  account: '123456789012',
  minInstances: 2,
  maxInstances: 6,
  instanceSize: { cpu: 512, memoryMiB: 1024 },
};

const toArray = <T>(value: T | T[] | undefined): T[] => {
  if (value === undefined) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
};

/**
 * 提取告警引用的全部度量名称，兼容两种渲染形式：
 *   - 旧式：顶层 `MetricName`。
 *   - 新式：`Metrics[].MetricStat.Metric.MetricName`（当度量带 label / 为数学表达式时）。
 */
const alarmMetricNames = (alarm: any): string[] => {
  const props = alarm?.Properties ?? {};
  if (typeof props.MetricName === 'string') {
    return [props.MetricName];
  }
  return toArray<any>(props.Metrics)
    .map((metric) => metric?.MetricStat?.Metric?.MetricName)
    .filter((name): name is string => typeof name === 'string');
};

/** 装配 network → data → compute → observability，返回 observability 模板。 */
function synthObservabilityStack(imageContextPath: string): Template {
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
    vpc: network.vpc,
    publicSubnets: network.publicSubnets,
    privateSubnets: network.privateSubnets,
    table: data.table,
    encryptionKey: data.encryptionKey,
    proxyApiKeySecret: data.proxyApiKeySecret,
    credentialsJsonSecret: data.credentialsJsonSecret,
    accountSecretPrefix: data.accountSecretPrefix,
    configParameterPrefix: data.configParameterPrefix,
    configBucket: data.configBucket,
    debugLogsBucket: data.debugLogsBucket,
    imageContextPath,
  });

  const observability = new ObservabilityStack(app, 'kiro-gateway-observability', {
    config: TEST_CONFIG,
    env: TEST_ENV,
    loadBalancer: compute.loadBalancer,
    targetGroup: compute.targetGroup,
    service: compute.service,
    cluster: compute.cluster,
  });

  return Template.fromStack(observability);
}

describe('ObservabilityStack 合规测试', () => {
  let imageContextPath: string;
  let template: Template;

  beforeAll(() => {
    imageContextPath = fs.mkdtempSync(path.join(os.tmpdir(), 'kiro-gw-obs-img-'));
    fs.writeFileSync(
      path.join(imageContextPath, 'Dockerfile'),
      'FROM public.ecr.aws/docker/library/python:3.11-slim\n',
    );
    template = synthObservabilityStack(imageContextPath);
  });

  afterAll(() => {
    fs.rmSync(imageContextPath, { recursive: true, force: true });
  });

  test('存在 CloudWatch 仪表板（需求 8.6）', () => {
    template.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
  });

  test('存在 SNS 告警主题', () => {
    template.resourceCountIs('AWS::SNS::Topic', 1);
    template.hasResourceProperties(
      'AWS::SNS::Topic',
      Match.objectLike({ TopicName: `${TEST_CONFIG.stackName}-gateway-alarms` }),
    );
  });

  test('存在 5xx 错误率告警（阈值 5、约 5 分钟）（需求 8.4）', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const errorRateAlarm = Object.values(alarms).find(
      (alarm: any) =>
        alarm.Properties?.AlarmName === `${TEST_CONFIG.stackName}-gateway-5xx-error-rate`,
    ) as any;

    expect(errorRateAlarm).toBeDefined();
    expect(errorRateAlarm.Properties.Threshold).toBe(5);
    expect(errorRateAlarm.Properties.ComparisonOperator).toBe('GreaterThanThreshold');

    // 错误率为度量数学表达式：各子度量统计周期约 5 分钟（300 秒）。
    const metrics = toArray<any>(errorRateAlarm.Properties.Metrics);
    const periods = metrics
      .filter((metric) => metric.MetricStat)
      .map((metric) => metric.MetricStat.Period);
    expect(periods.length).toBeGreaterThan(0);
    for (const period of periods) {
      expect(period).toBe(300);
    }
  });

  test('存在无健康目标告警（需求 8.5）', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const noHealthyHostsAlarm = Object.values(alarms).find(
      (alarm: any) =>
        alarm.Properties?.AlarmName === `${TEST_CONFIG.stackName}-gateway-no-healthy-hosts`,
    ) as any;

    expect(noHealthyHostsAlarm).toBeDefined();
    expect(noHealthyHostsAlarm.Properties.Threshold).toBe(1);
    expect(noHealthyHostsAlarm.Properties.ComparisonOperator).toBe('LessThanThreshold');
    // 健康目标数度量带 label，CDK 以 Metrics 数组（MetricDataQuery）渲染。
    expect(alarmMetricNames(noHealthyHostsAlarm)).toContain('HealthyHostCount');
  });

  test('告警均关联 SNS 告警动作', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const alarmList = Object.values(alarms) as any[];
    expect(alarmList.length).toBeGreaterThanOrEqual(2);
    for (const alarm of alarmList) {
      expect(toArray<unknown>(alarm.Properties?.AlarmActions).length).toBeGreaterThan(0);
    }
  });

  test('合成模板快照（toMatchSnapshot）', () => {
    expect(template.toJSON()).toMatchSnapshot();
  });
});
