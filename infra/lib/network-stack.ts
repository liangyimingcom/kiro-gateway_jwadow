/**
 * NetworkStack — VPC / 子网 / NAT Gateway / VPC Endpoints。
 *
 * 实现网络基础设施（对应 design.md「架构 → 整体 AWS 架构」与「IaC 与交付物 → CDK 项目结构」）：
 *
 *   - 单一 VPC，跨 ≥2 个可用区（Availability Zone）部署，满足高可用（需求 3.1、3.2）。
 *   - 公有子网（PUBLIC）：放置 Application Load Balancer 与 NAT Gateway。
 *   - 私有子网（PRIVATE_WITH_EGRESS）：放置 ECS Fargate 任务，经 NAT Gateway 出站。
 *   - NAT Gateway：为私有子网提供出站能力，访问上游 `runtime.{region}.kiro.dev`
 *     与 `oidc.{region}.amazonaws.com`（设计文档运行时请求流）。
 *   - VPC Endpoints（可选，默认开启）：S3 / DynamoDB 使用免费的 Gateway Endpoint；
 *     Secrets Manager / SSM / ECR / CloudWatch Logs 使用 Interface Endpoint，
 *     使任务对 AWS 托管服务的调用走 VPC 内网而非 NAT，降低 NAT 数据处理费用并提升安全性。
 *
 * 本 Stack 通过公有 `readonly` 属性向下游 Stack（ComputeStack，任务 15.1）暴露
 * VPC 与子网选择，使其可据此放置 ALB（公有子网）与 Fargate 服务（私有子网）。
 *
 * _Requirements: 3.1, 3.2_
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';
import { GatewayConfig } from './config';

export interface NetworkStackProps extends StackProps {
  readonly config: GatewayConfig;
  /**
   * VPC 横跨的可用区数量（需 ≥2 以满足高可用需求 3.1/3.2）。默认 2。
   */
  readonly maxAzs?: number;
  /**
   * NAT Gateway 数量。
   *   - 缺省时等于 `maxAzs`：每个 AZ 一个 NAT Gateway，实现真正的多 AZ 高可用出站
   *     （单 AZ 故障不影响其余 AZ 私有子网的出站）。
   *   - 为降低成本，可显式设为 1：所有私有子网共享单个 NAT Gateway（牺牲跨 AZ 出站冗余）。
   */
  readonly natGateways?: number;
  /**
   * 是否创建 Interface VPC Endpoints（Secrets Manager / SSM / ECR / CloudWatch Logs）。
   * 默认 true：使任务对这些 AWS 服务的调用走 VPC 内网，减少 NAT 数据处理成本（需求 12 成本可控）。
   * Gateway Endpoint（S3 / DynamoDB）始终创建，因其免费且无副作用。
   */
  readonly enableInterfaceEndpoints?: boolean;
}

export class NetworkStack extends Stack {
  /** 网关所在 VPC，供 ComputeStack 放置 ALB 与 Fargate 服务。 */
  public readonly vpc: ec2.IVpc;

  /** 公有子网选择（放置 ALB）。 */
  public readonly publicSubnets: ec2.SubnetSelection;

  /** 私有（带出站）子网选择（放置 ECS Fargate 任务）。 */
  public readonly privateSubnets: ec2.SubnetSelection;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    // 高可用要求至少跨 2 个可用区（需求 3.1/3.2）。
    const maxAzs = Math.max(2, props.maxAzs ?? 2);
    // 默认每个 AZ 一个 NAT Gateway，实现跨 AZ 出站冗余；可显式降为 1 以节约成本。
    const natGateways = Math.max(1, props.natGateways ?? maxAzs);
    const enableInterfaceEndpoints = props.enableInterfaceEndpoints ?? true;

    // 定义 VPC：公有子网（ALB + NAT）与私有子网（Fargate 任务）跨各 AZ 对称分布。
    const vpc = new ec2.Vpc(this, 'GatewayVpc', {
      maxAzs,
      natGateways,
      ipAddresses: ec2.IpAddresses.cidr('10.0.0.0/16'),
      restrictDefaultSecurityGroup: true,
      subnetConfiguration: [
        {
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'private',
          // PRIVATE_WITH_EGRESS：无公网入站，经 NAT Gateway 出站访问上游 Kiro / OIDC 端点。
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
      ],
    });

    this.vpc = vpc;
    this.publicSubnets = { subnetType: ec2.SubnetType.PUBLIC };
    this.privateSubnets = { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS };

    // Gateway Endpoints（免费）：S3 与 DynamoDB 经 VPC 内网访问，避免占用 NAT 带宽。
    vpc.addGatewayEndpoint('S3GatewayEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
    });
    vpc.addGatewayEndpoint('DynamoDbGatewayEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
      subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
    });

    // Interface Endpoints（可选，默认开启）：使任务对 AWS 托管服务的调用走 VPC 内网，
    // 降低 NAT 数据处理费用（需求 12 成本可控）并提升安全性。
    if (enableInterfaceEndpoints) {
      const interfaceServices: Record<string, ec2.InterfaceVpcEndpointAwsService> = {
        SecretsManagerEndpoint: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        SsmEndpoint: ec2.InterfaceVpcEndpointAwsService.SSM,
        EcrApiEndpoint: ec2.InterfaceVpcEndpointAwsService.ECR,
        EcrDockerEndpoint: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
        CloudWatchLogsEndpoint: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      };

      for (const [endpointId, service] of Object.entries(interfaceServices)) {
        vpc.addInterfaceEndpoint(endpointId, {
          service,
          subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
          privateDnsEnabled: true,
        });
      }
    }
  }
}
