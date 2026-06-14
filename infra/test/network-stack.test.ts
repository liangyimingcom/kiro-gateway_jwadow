/**
 * NetworkStack 的 CDK 断言与快照测试（任务 14.3）。
 *
 * 覆盖网络层的核心资源与高可用约束（对应 network-stack.ts 与设计文档「整体 AWS 架构」）：
 *   - 单一 VPC，跨 ≥2 个可用区（公有 + 私有子网各 ≥2，满足需求 3.1/3.2）。
 *   - 至少一个 NAT Gateway，为私有子网提供出站能力。
 *   - S3 与 DynamoDB 的 Gateway VPC Endpoint（免费，避免占用 NAT 带宽）。
 *   - 合成模板的快照断言（Template.toMatchSnapshot），用于回归保护。
 *
 * 使用字面量 GatewayConfig 与显式 cdk.Environment（account/region），确保 `cdk synth`
 * 输出确定，快照稳定。
 *
 * _Requirements: 6.3, 12.3, 12.4（合规专项见 data-stack.test.ts）；本文件聚焦网络核心资源_
 */
import { App, Environment } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { GatewayConfig } from '../lib/config';
import { NetworkStack } from '../lib/network-stack';

/** 显式环境：固定 account/region 使 synth 结果确定，快照可复现。 */
const TEST_ENV: Environment = { account: '123456789012', region: 'us-east-1' };

/** 字面量部署参数对象，匹配 GatewayConfig 接口（避免依赖 cdk.json context）。 */
const TEST_CONFIG: GatewayConfig = {
  stackName: 'test-gateway',
  region: 'us-east-1',
  account: '123456789012',
  minInstances: 2,
  maxInstances: 6,
  instanceSize: { cpu: 512, memoryMiB: 1024 },
};

/** 合成 NetworkStack 并返回其 CloudFormation 模板断言对象。 */
function synthNetworkTemplate(): Template {
  const app = new App();
  const stack = new NetworkStack(app, 'test-gateway-network', {
    config: TEST_CONFIG,
    env: TEST_ENV,
  });
  return Template.fromStack(stack);
}

/** 从模板中按 `aws-cdk:subnet-type` 标签收集各子网的层级类型（Public / Private）。 */
function subnetTypes(template: Template): string[] {
  const subnets = template.findResources('AWS::EC2::Subnet');
  return Object.values(subnets).map((subnet) => {
    const tags = (subnet.Properties?.Tags ?? []) as Array<{ Key: string; Value: string }>;
    const typeTag = tags.find((tag) => tag.Key === 'aws-cdk:subnet-type');
    return typeTag?.Value ?? 'Unknown';
  });
}

describe('NetworkStack', () => {
  it('创建单一 VPC', () => {
    const template = synthNetworkTemplate();
    template.resourceCountIs('AWS::EC2::VPC', 1);
  });

  it('VPC 跨 ≥2 个可用区，且同时存在公有与私有子网（需求 3.1/3.2）', () => {
    const template = synthNetworkTemplate();
    const types = subnetTypes(template);

    // 公有子网 ≥2（每 AZ 一个），承载 ALB 与 NAT Gateway。
    expect(types.filter((t) => t === 'Public').length).toBeGreaterThanOrEqual(2);
    // 私有（带出站）子网 ≥2（每 AZ 一个），承载 Fargate 任务。
    expect(types.filter((t) => t === 'Private').length).toBeGreaterThanOrEqual(2);
  });

  it('至少创建一个 NAT Gateway 为私有子网提供出站', () => {
    const template = synthNetworkTemplate();
    const natGateways = template.findResources('AWS::EC2::NatGateway');
    expect(Object.keys(natGateways).length).toBeGreaterThanOrEqual(1);
  });

  it('创建 S3 与 DynamoDB 的 Gateway VPC Endpoint', () => {
    const template = synthNetworkTemplate();
    const endpoints = template.findResources('AWS::EC2::VPCEndpoint');

    // 仅保留 Gateway 类型端点，并将其 ServiceName 序列化以匹配服务后缀（.s3 / .dynamodb）。
    const gatewayServiceNames = Object.values(endpoints)
      .filter((e) => e.Properties?.VpcEndpointType === 'Gateway')
      .map((e) => JSON.stringify(e.Properties?.ServiceName));

    expect(gatewayServiceNames.length).toBe(2);
    expect(gatewayServiceNames.some((name) => name.includes('.s3'))).toBe(true);
    expect(gatewayServiceNames.some((name) => name.toLowerCase().includes('dynamodb'))).toBe(true);
  });

  it('合成模板匹配快照（回归保护）', () => {
    const template = synthNetworkTemplate();
    expect(template.toJSON()).toMatchSnapshot();
  });
});
