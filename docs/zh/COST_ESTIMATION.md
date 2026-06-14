<div align="center">

# 成本预估（Cost Estimation）

**Kiro Gateway · AWS 云原生部署的成本说明与主要成本因素**

[🇨🇳 中文](./COST_ESTIMATION.md) · [方案概述](./CLOUD_NATIVE_README.md) · [架构](./ARCHITECTURE_AWS.md) · [部署指南](./DEPLOYMENT_GUIDE.md) · [迁移指南](./MIGRATION_GUIDE.md)

</div>

> 本文档对应 AWS 官方方案《[Guidance for Multi-Provider Generative AI Gateway on AWS](https://github.com/aws-solutions-library-samples/guidance-for-multi-provider-generative-ai-gateway-on-aws)》的「Cost / Cost Estimate」章节格式，并结合本仓库 `infra/` 下 CDK 模板实际创建的资源给出说明。
>
> 内容已根据该 AWS 官方 guidance 仓库的公开结构进行改写与归纳，以符合内容许可与署名要求（Content was rephrased for compliance with licensing restrictions）。

---

## 📑 目录

1. [概述](#1-概述overview)
2. [主要成本因素](#2-主要成本因素cost-drivers)
3. [示意性月度成本估算](#3-示意性月度成本估算)
4. [成本控制建议](#4-成本控制建议cost-optimization)
5. [说明：上游 Kiro / Amazon Q 用量单独计费](#5-说明上游-kiro--amazon-q-用量单独计费)
6. [相关文档](#6-相关文档)

---

## 1. 概述（Overview）

> ⚠️ **重要免责声明**
>
> - 本文档中的所有金额均为**示意性估算（illustrative estimates）**，**不构成报价或费用承诺**。
> - 实际费用**高度依赖用量**（请求量、流式时长、数据传输量、调试日志量、自动伸缩实例数）与**所选区域**，并随时间变化。
> - 文中参考价格统一采用 **`us-east-1`（美国东部 - 弗吉尼亚北部）区域的公开按需（on-demand）价格**作为基准；其他区域价格通常更高。
> - **价格可能随时调整。** 请以 AWS 官方为准，并在部署前用官方工具复核：
>   - [AWS Pricing Calculator（价格计算器）](https://calculator.aws/)
>   - 各服务定价页：[Fargate](https://aws.amazon.com/fargate/pricing/) · [ALB（弹性负载均衡）](https://aws.amazon.com/elasticloadbalancing/pricing/) · [VPC / NAT Gateway](https://aws.amazon.com/vpc/pricing/) · [DynamoDB](https://aws.amazon.com/dynamodb/pricing/on-demand/) · [S3](https://aws.amazon.com/s3/pricing/) · [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/) · [Systems Manager（参数存储）](https://aws.amazon.com/systems-manager/pricing/) · [KMS](https://aws.amazon.com/kms/pricing/) · [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) · [ECR](https://aws.amazon.com/ecr/pricing/)

本方案以 **ECS Fargate + ALB + 多可用区（AZ）+ NAT Gateway + DynamoDB / Secrets Manager / SSM / S3 / CloudWatch / KMS** 等托管服务组合部署。其成本可分为两类：

- **固定成本（按小时计，与是否有流量无关）**：NAT Gateway、ALB、Fargate 常驻任务、VPC Interface Endpoints、KMS 密钥。这些是**小型部署中的主要开销**。
- **用量成本（按量计费）**：DynamoDB 读写、S3 存储/请求、Secrets Manager / SSM API 调用、CloudWatch 日志与指标、数据传输。在**低负载**下这些通常**金额较小**。

下文的「小型默认部署」指 CDK 默认参数：**2 个 Fargate 任务（每个 0.5 vCPU / 1 GB）、跨 2 个 AZ、默认每个 AZ 一个 NAT Gateway（共 2 个）、1 个 ALB、DynamoDB 按需轻量用量、S3 小容量**。

---

## 2. 主要成本因素（Cost Drivers）

下表按资源列出**成本驱动因素**与**小型默认部署下的月度量级**（`us-east-1` 按需价、示意值，仅用于建立直觉，**非精确报价**）：

| 资源 | 成本由什么驱动 | 小型默认部署月度量级（示意） | 备注 |
|---|---|---|---|
| **NAT Gateway** | 每个 NAT 按小时固定计费 **+** 处理的数据量（每 GB） | **≈ $33 / 个**（默认 2 个 ⇒ **≈ $65**）+ 数据处理 | 🔴 **主要固定成本之一**。默认每 AZ 一个（高可用）。可降为 1 个或改用 VPC Endpoints 减少数据处理 |
| **Application Load Balancer (ALB)** | 按小时固定计费 **+** LCU（连接/带宽/规则用量） | **≈ $16–25** | 🔴 **主要固定成本之一**。单一稳定入口，长连接 SSE 流式会增加 LCU |
| **ECS Fargate（计算池）** | 按 vCPU·小时 + GB·小时，乘以常驻任务数与运行时长 | **≈ $18 / 任务**（默认 2 个 ⇒ **≈ $36**） | 🔴 **主要固定成本之一**。随自动伸缩实例数线性变化；0.5 vCPU/1 GB 为默认轻量规格 |
| **VPC Interface Endpoints** | 每个 Endpoint 按 AZ·小时固定计费 **+** 数据处理（每 GB） | **≈ $7 / Endpoint·AZ**（默认 5 服务 × 2 AZ ⇒ **可达 ≈ $70**） | ⚠️ 默认开启（Secrets Manager / SSM / ECR×2 / CloudWatch Logs）。**减少 NAT 数据处理但本身有固定费**，低流量时需权衡（见优化建议） |
| **DynamoDB（State_Store）** | 按需模式：按读写请求数计费 + 存储 + PITR | **通常 < $5**（轻量用量） | 🟢 低量级。`PAY_PER_REQUEST` 避免低负载固定容量费；已开启时间点恢复（PITR）有少量存储费 |
| **Amazon S3（配置桶 + 调试日志桶）** | 存储量（GB·月）+ 请求数 + 数据传输 | **通常 < $2** | 🟢 低量级。调试日志桶有生命周期规则按保留期（默认 30 天）自动清除 |
| **Secrets Manager（Secret_Store）** | 每个密钥 **≈ $0.40 / 月** + API 调用（每 1 万次） | **通常 $1–3** | 🟢 低量级。本方案含 proxy-api-key、credentials-json 及各账号 token；应用侧 TTL 缓存大幅减少计费调用 |
| **SSM Parameter Store（Config_Store）** | 标准层（Standard）参数不计存储/吞吐费 | **≈ $0（标准层）** | 🟢 本方案使用标准层参数，基本免费；TTL 缓存进一步减少 API 调用 |
| **KMS（静态加密密钥）** | 每个客户管理密钥（CMK）**≈ $1 / 月** + 请求（每 1 万次） | **通常 $1–3** | 🟢 低量级。1 个 CMK 统一加密 DynamoDB / Secrets / S3，已开启自动轮换 |
| **CloudWatch（可观测性）** | 日志摄取（每 GB）+ 存储 + 自定义指标 + 告警 + 仪表板 | **通常 $3–15** | 🟢 取决于日志量与指标数；告警与仪表板单价低 |
| **ECR（镜像存储）** | 镜像存储量（GB·月）+ 数据传输 | **通常 < $1** | 🟢 容器镜像体积小，量级极低 |
| **数据传输（Data Transfer）** | 出向公网流量（每 GB，分层计费） | **随用量变化** | 取决于响应体大小与请求量；区域内/VPC 内流量更便宜 |

> 📌 **结论：** 在小型部署中，**NAT Gateway + ALB + Fargate（再加上默认开启的 VPC Interface Endpoints）构成主要的固定成本**；而 **DynamoDB / S3 / Secrets Manager / SSM / KMS / ECR 在低用量下通常只占很小比例**。

---

## 3. 示意性月度成本估算

将上表的固定成本相加，小型默认部署（2 任务 / 2 NAT / 2 AZ / Interface Endpoints 开启）的**基线月度成本量级约在 $150–$350 / 月**之间，外加随用量浮动的数据传输与日志费用。

> ⚠️ **该区间为粗略示意，请勿直接用于预算决策。**
> - 关闭 VPC Interface Endpoints、改用 **1 个 NAT Gateway**、并采用 **Fargate Spot**，可将基线显著下压（量级可降至约 **$100–$150 / 月**，以牺牲部分冗余/容错为代价）。
> - 高流量、长流式会话、大量调试日志或更高的自动伸缩上限会**显著抬高**实际费用。
> - **务必使用 [AWS Pricing Calculator](https://calculator.aws/) 按你的目标区域与预期用量重新计算。**

| 场景 | 配置要点 | 月度量级（示意） |
|---|---|---|
| 默认小型部署 | 2 任务 + 2 NAT + Interface Endpoints 开启 | **≈ $150–$350** |
| 成本优化部署 | 2 任务（Spot）+ 1 NAT + 关闭 Interface Endpoints | **≈ $100–$150** |
| 高负载 / 高用量 | 自动伸缩至更多实例 + 大量日志/传输 | **显著高于上述（按量上升）** |

---

## 4. 成本控制建议（Cost Optimization）

以下建议均可结合本仓库 `infra/` 的 CDK 参数或运行时配置落地（对应需求 12「成本可控」）：

1. **低谷期自动缩容到最小值。** 自动伸缩（Auto Scaling）在 CPU 利用率持续偏低时会把 Fargate 实例数缩减到 `minInstances`（默认 2）。如对可用性要求不极致，可评估更低的最小实例数以降低常驻计算成本。

2. **减少 NAT Gateway 数量。** 默认每个 AZ 一个 NAT Gateway（高可用出站）。通过 CDK 参数 `natGateways=1` 改为**单个 NAT Gateway**，可将该项固定成本大致减半（代价是失去跨 AZ 出站冗余）。对成本极敏感的非生产环境，亦可考虑 NAT 实例（NAT Instance）替代托管 NAT Gateway。

3. **善用 VPC Endpoints 与其权衡。** 本方案默认创建 **S3 / DynamoDB 的 Gateway Endpoint（免费）**，以及 Secrets Manager / SSM / ECR / CloudWatch Logs 的 **Interface Endpoint**，使这些调用走 VPC 内网、**减少 NAT 数据处理费**并提升安全性。但 Interface Endpoint 本身按 Endpoint·AZ·小时收费——**在极低流量下，其固定费可能超过它所节省的 NAT 数据费**。可通过参数 `enableInterfaceEndpoints=false` 关闭 Interface Endpoint，改由 NAT 承载这些调用，按你的实际流量二选一更省。

4. **DynamoDB 按需 vs 预置。** 本方案默认 `PAY_PER_REQUEST`（按需），避免低负载时的固定容量开销。仅当流量**高且稳定可预测**时，切换到预置容量（Provisioned + Auto Scaling）才更划算。

5. **TTL 缓存减少按调用计费的访问。** 应用对 Secret_Store / Config_Store / State_Store 的读取结果做带 TTL 的本地缓存，显著减少 **Secrets Manager / SSM 的按调用计费**与 DynamoDB 读请求。保持缓存开启即可获得该收益。

6. **控制调试日志保留与生命周期。** 调试日志桶已配置生命周期规则按保留期（默认 30 天）自动过期清除，并清理未完成分段上传。通过 CDK 参数 `debugLogRetentionDays` 调小保留天数、或在生产将调试模式关闭，可降低 S3 与 CloudWatch 成本。

7. **对非关键负载使用 Fargate Spot。** 对可容忍中断的环境，使用 Fargate Spot 容量可在相同规格下显著降低计算单价。

8. **合理设定（right-sizing）CPU / 内存。** 通过参数 `cpu` / `memory` 按实际负载调整单实例规格，避免为轻量代理负载过度配置；网关为 I/O 密集型，通常无需高 CPU。

9. **拆除时清理「保留（RETAIN）」资源。** 为防误删数据，DynamoDB 表、KMS 密钥与配置桶采用 `RemovalPolicy.RETAIN`——**执行 `cdk destroy` 后它们仍会保留并继续产生费用**。彻底下线时，请手动删除这些保留资源、各账号 token 密钥（运行时创建、不在 CDK 管理范围）以及残留的 ECR 镜像与 CloudWatch 日志组，以停止计费。详见 [部署指南](./DEPLOYMENT_GUIDE.md) 与 [方案概述](./CLOUD_NATIVE_README.md) 的卸载章节。

---

## 5. 说明：上游 Kiro / Amazon Q 用量单独计费

> 📌 本文档估算的**仅是部署本网关所需的 AWS 基础设施成本**。

网关向上游 **Kiro API / Amazon Q Developer** 转发请求所产生的**模型用量本身，与上述 AWS 基础设施费用相互独立**：它取决于你的 Kiro / Amazon Q 账户套餐（免费 / 付费）与其各自的配额、计费与服务条款，**不包含在本文档的 AWS 成本估算之内**。请分别参考 Kiro / Amazon Q 的官方计费与配额说明。

---

## 6. 相关文档

| 文档 | 内容 |
|---|---|
| [CLOUD_NATIVE_README.md](./CLOUD_NATIVE_README.md) | AWS 云原生部署方案概述、架构、前置条件、部署与卸载 |
| [ARCHITECTURE_AWS.md](./ARCHITECTURE_AWS.md) | 完整 AWS 架构与组件交互（含 Mermaid 架构图、请求流） |
| [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md) | 一键部署命令与参数说明、销毁命令 |
| [MIGRATION_GUIDE.md](./MIGRATION_GUIDE.md) | 从本地文件配置迁移至 SSM / Secrets Manager / S3 / DynamoDB 的步骤 |
| [README.md](./README.md) | kiro-gateway 中文总览（单机使用） |

---

_本文档对应需求：13.6（提供预估成本说明与影响成本的主要因素）。文中金额为基于 `us-east-1` 按需公开价格的示意性估算，价格可能变动，请以 [AWS Pricing Calculator](https://calculator.aws/) 与各服务官方定价页为准。_
