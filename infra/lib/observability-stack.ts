/**
 * ObservabilityStack — CloudWatch 仪表板 / 指标 / 告警 + SNS 告警通知。
 *
 * 在 ComputeStack 创建 ALB / 目标组 / ECS Service 之后，本 Stack 装配集中可观测性（需求 8）：
 *
 *   - **仪表板（Dashboard）**：集中展示请求量、错误率、延迟与运行实例数（需求 8.6）。
 *   - **告警（Alarms）**：
 *       · 5xx 错误率 > 5% 持续 5 分钟（基于 ALB / 目标组的 5xx 与总请求数计算，需求 8.4）。
 *       · 无健康目标（健康主机数 < 1）告警（需求 8.5）。
 *   - **SNS 主题**：作为告警动作目标，便于 Operator 订阅邮件 / 其他通知。
 *
 * 应用进程将结构化日志输出到 stdout，由 ECS awslogs 驱动采集至 CloudWatch Logs（需求 8.1/8.3）；
 * 本 Stack 聚焦指标、告警与仪表板。
 *
 * _Requirements: 8.3, 8.4, 8.5, 8.6_
 */
import {
  Duration,
  Stack,
  StackProps,
  aws_cloudwatch as cloudwatch,
  aws_cloudwatch_actions as cwActions,
  aws_ecs as ecs,
  aws_elasticloadbalancingv2 as elbv2,
  aws_sns as sns,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { GatewayConfig } from './config';

export interface ObservabilityStackProps extends StackProps {
  readonly config: GatewayConfig;
  /** 公网 ALB（提供 ALB 级 5xx 等指标）。 */
  readonly loadBalancer: elbv2.IApplicationLoadBalancer;
  /** ALB 目标组（提供请求量 / 目标 5xx / 延迟 / 健康主机数指标）。 */
  readonly targetGroup: elbv2.IApplicationTargetGroup;
  /** ECS Fargate Service（用于运行实例数指标维度）。 */
  readonly service: ecs.IBaseService;
  /** ECS Cluster（用于运行实例数指标维度）。 */
  readonly cluster: ecs.ICluster;
  /**
   * 可选：5xx 错误率告警阈值（百分比）。默认 5（需求 8.4）。
   */
  readonly errorRateThresholdPercent?: number;
}

export class ObservabilityStack extends Stack {
  /** 告警动作的 SNS 主题（Operator 可订阅）。 */
  public readonly alarmTopic: sns.Topic;

  /** 集中监控仪表板。 */
  public readonly dashboard: cloudwatch.Dashboard;

  /** 5xx 错误率告警。 */
  public readonly errorRateAlarm: cloudwatch.Alarm;

  /** 无健康目标告警。 */
  public readonly noHealthyHostsAlarm: cloudwatch.Alarm;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const { config, loadBalancer, targetGroup, service, cluster } = props;
    const prefix = config.stackName;
    const period = Duration.minutes(5);
    const errorRateThreshold = props.errorRateThresholdPercent ?? 5;

    // SNS 告警主题：作为各告警的动作目标。
    this.alarmTopic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `${prefix}-gateway-alarms`,
      displayName: `${prefix} Gateway 告警通知`,
    });
    const alarmAction = new cwActions.SnsAction(this.alarmTopic);

    // -------------------------------------------------------------------
    // 核心指标。
    // -------------------------------------------------------------------
    const requestCount = targetGroup.metrics.requestCount({
      period,
      label: '请求量',
    });
    const target5xx = targetGroup.metrics.httpCodeTarget(
      elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
      { period, label: '目标 5xx' },
    );
    const elb5xx = loadBalancer.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, {
      period,
      label: 'ALB 5xx',
    });
    const latencyP95 = targetGroup.metrics.targetResponseTime({
      period,
      statistic: 'p95',
      label: '延迟 P95',
    });
    const healthyHosts = targetGroup.metrics.healthyHostCount({
      period,
      statistic: 'Minimum',
      label: '健康目标数',
    });

    // 运行实例数（依赖 ECS Container Insights，由 ComputeStack 在 Cluster 上启用）。
    const runningTaskCount = new cloudwatch.Metric({
      namespace: 'ECS/ContainerInsights',
      metricName: 'RunningTaskCount',
      dimensionsMap: {
        ClusterName: cluster.clusterName,
        ServiceName: service.serviceName,
      },
      statistic: 'Average',
      period,
      label: '运行实例数',
    });

    // 5xx 错误率（%）：(目标 5xx + ALB 5xx) / 请求量 * 100。
    const errorRate = new cloudwatch.MathExpression({
      expression: '100 * (m5xxTarget + m5xxElb) / FILL(mRequests, 0)',
      usingMetrics: {
        m5xxTarget: target5xx,
        m5xxElb: elb5xx,
        mRequests: requestCount,
      },
      period,
      label: '5xx 错误率(%)',
    });

    // -------------------------------------------------------------------
    // 告警。
    // -------------------------------------------------------------------
    // 5xx 错误率 > 5% 持续 5 分钟（需求 8.4）。
    this.errorRateAlarm = new cloudwatch.Alarm(this, 'Error5xxRateAlarm', {
      alarmName: `${prefix}-gateway-5xx-error-rate`,
      alarmDescription: '5xx 错误率在持续 5 分钟内超过阈值。',
      metric: errorRate,
      threshold: errorRateThreshold,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    this.errorRateAlarm.addAlarmAction(alarmAction);
    this.errorRateAlarm.addOkAction(alarmAction);

    // 无健康目标：健康主机数 < 1 持续 5 分钟（需求 8.5）。
    this.noHealthyHostsAlarm = new cloudwatch.Alarm(this, 'NoHealthyHostsAlarm', {
      alarmName: `${prefix}-gateway-no-healthy-hosts`,
      alarmDescription: '没有任何 Gateway 实例通过健康检查。',
      metric: healthyHosts,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      // 指标缺失（如目标全部注销）按告警处理，避免漏报。
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    this.noHealthyHostsAlarm.addAlarmAction(alarmAction);
    this.noHealthyHostsAlarm.addOkAction(alarmAction);

    // -------------------------------------------------------------------
    // 仪表板（需求 8.6）。
    // -------------------------------------------------------------------
    this.dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `${prefix}-gateway`,
    });
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: '请求量',
        left: [requestCount],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: '错误率与 5xx',
        left: [errorRate],
        right: [target5xx, elb5xx],
        width: 12,
      }),
    );
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: '延迟 (P95)',
        left: [latencyP95],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: '运行实例数与健康目标',
        left: [runningTaskCount],
        right: [healthyHosts],
        width: 12,
      }),
    );
    this.dashboard.addWidgets(
      new cloudwatch.AlarmWidget({
        title: '5xx 错误率告警',
        alarm: this.errorRateAlarm,
        width: 12,
      }),
      new cloudwatch.AlarmWidget({
        title: '无健康目标告警',
        alarm: this.noHealthyHostsAlarm,
        width: 12,
      }),
    );
  }
}
