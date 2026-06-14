/**
 * GatewayAutoScaling — ECS Service Application Auto Scaling 目标跟踪策略。
 *
 * 为 Gateway_Service（ECS Fargate Service）配置 Application Auto Scaling，实现按指标的
 * 水平伸缩（需求 4）：
 *
 *   - **CPU 目标跟踪（必选）**：目标利用率 ~70%。
 *       · 扩容（scale-out）：CPU 持续 ~3 分钟 >70% 时增加实例（scaleOutCooldown=3min）。
 *       · 缩容（scale-in）：CPU 持续 ~10 分钟回落时减少实例（scaleInCooldown=10min），
 *         缩容更保守以避免抖动（需求 4.1、4.2）。
 *   - **请求并发目标跟踪（可选）**：当提供 ALB 目标组时，按“每目标请求数”
 *     （ALBRequestCountPerTarget）目标跟踪伸缩（需求 4.6）。
 *   - 实例数维持在 [config.minInstances, config.maxInstances] 之间（需求 4.3、4.4）；
 *     已达上限不再扩容、低谷回落至下限以控成本（需求 12.1）。
 *
 * 目标跟踪策略由 Application Auto Scaling 自动创建并维护高/低阈值告警，实例随负载自适应，
 * 配合 ALB 健康检查与优雅停机实现安全伸缩（需求 11.4）。
 *
 * 用法：
 *   new GatewayAutoScaling(this, 'Scaling', { service, config, targetGroup });
 *
 * _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 12.1, 11.4_
 */
import { Duration } from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import { GatewayConfig } from './config';

export interface GatewayAutoScalingProps {
  /** 需要伸缩的 ECS Fargate Service。 */
  readonly service: ecs.FargateService;
  /** 共享部署参数（提供 min/max 实例数与 CPU 目标）。 */
  readonly config: GatewayConfig;
  /**
   * 可选：ALB 目标组。提供时启用基于“每目标请求数”的目标跟踪伸缩（需求 4.6）。
   * 需为具体的 ApplicationTargetGroup（Application Auto Scaling 请求数策略要求）。
   */
  readonly targetGroup?: elbv2.ApplicationTargetGroup;
  /**
   * CPU 目标跟踪的目标利用率（百分比）。默认 70（需求 4.1/4.2）。
   */
  readonly cpuTargetUtilizationPercent?: number;
  /**
   * 可选：每目标请求数阈值（仅在提供 targetGroup 时生效）。默认 1000。
   */
  readonly requestsPerTarget?: number;
  /** 扩容冷却时间。默认 3 分钟（需求 4.1）。 */
  readonly scaleOutCooldown?: Duration;
  /** 缩容冷却时间。默认 10 分钟（需求 4.2）。 */
  readonly scaleInCooldown?: Duration;
}

export class GatewayAutoScaling extends Construct {
  /** 可伸缩的任务数目标，供调用方进一步附加自定义策略。 */
  public readonly scalableTaskCount: ecs.ScalableTaskCount;

  constructor(scope: Construct, id: string, props: GatewayAutoScalingProps) {
    super(scope, id);

    const { service, config } = props;
    const cpuTarget = props.cpuTargetUtilizationPercent ?? 70;
    const scaleOutCooldown = props.scaleOutCooldown ?? Duration.minutes(3);
    const scaleInCooldown = props.scaleInCooldown ?? Duration.minutes(10);

    // 维持实例数在 [min, max] 之间（需求 4.3、4.4）。
    this.scalableTaskCount = service.autoScaleTaskCount({
      minCapacity: config.minInstances,
      maxCapacity: config.maxInstances,
    });

    // CPU 目标跟踪：~70% 目标；扩容快（3min）、缩容稳（10min）。
    this.scalableTaskCount.scaleOnCpuUtilization('CpuTargetTracking', {
      targetUtilizationPercent: cpuTarget,
      scaleOutCooldown,
      scaleInCooldown,
    });

    // 可选：每目标请求数目标跟踪（需求 4.6）。
    if (props.targetGroup) {
      this.scalableTaskCount.scaleOnRequestCount('RequestTargetTracking', {
        requestsPerTarget: props.requestsPerTarget ?? 1000,
        targetGroup: props.targetGroup,
        scaleOutCooldown,
        scaleInCooldown,
      });
    }
  }
}
