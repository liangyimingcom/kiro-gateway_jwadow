# 部署指南（Deployment Guide）

本指南介绍如何使用仓库内的 AWS CDK（TypeScript）模板，将 **kiro-gateway** 一键部署为 AWS 云原生池化服务（ECS Fargate + ALB + Auto Scaling + 多可用区高可用，状态/配置/密钥外置至 DynamoDB / SSM / Secrets Manager / S3）。

本文档对应 AWS guidance 项目《Guidance for Multi-Provider Generative AI Gateway on AWS》的「Deployment」章节格式，命令与参数均与仓库内 `infra/` 下的实际 CDK 实现保持一致。

> 相关文档：方案概述见 [`CLOUD_NATIVE_README.md`](./CLOUD_NATIVE_README.md)，架构图见 [`ARCHITECTURE_AWS.md`](./ARCHITECTURE_AWS.md)，本地迁移见 [`MIGRATION_GUIDE.md`](./MIGRATION_GUIDE.md)，成本预估见 [`COST_ESTIMATION.md`](./COST_ESTIMATION.md)。

---

## 1. 前置条件（Prerequisites）

在开始部署前，请确保已具备以下条件：

| 条件 | 说明 |
| --- | --- |
| **AWS 账号与凭证** | 一个具备创建 VPC、ECS、ALB、DynamoDB、KMS、Secrets Manager、SSM、S3、CloudWatch、IAM、WAF 等资源权限的 AWS 账号。本地需通过 `aws configure`（或环境变量 / SSO）配置好可用凭证。 |
| **AWS CLI** | 已安装并可用，用于配置凭证、部署后写入密钥与配置、以及问题排查。校验：`aws sts get-caller-identity`。 |
| **Node.js** | 需要 **Node.js 18 或更高版本**（建议 20 LTS）。校验：`node -v`。 |
| **AWS CDK** | 仓库 `infra/` 已将 `aws-cdk` 作为开发依赖，安装后可用 `npx cdk` 调用。若希望全局命令 `cdk`，可执行 `npm i -g aws-cdk`。 |
| **Docker** | **必需**。容器镜像通过 `ContainerImage.fromAsset` 由仓库根目录的 `Dockerfile` 在本地构建并推送至 ECR，部署机器上需运行 Docker daemon。校验：`docker info`。 |

### 1.1 一次性 CDK Bootstrap

每个「账号 + 区域」组合在首次部署前需执行一次 CDK 引导（创建用于资产上传的 S3 桶、ECR 仓库与角色）：

```bash
# 将 <account> 替换为 12 位 AWS 账号 ID，<region> 替换为目标区域（如 us-east-1）
cdk bootstrap aws://<account>/<region>

# 或使用 npx（无需全局安装 CDK）
npx cdk bootstrap aws://<account>/<region>
```

> 若跳过该步骤，部署时会因缺少引导资源而失败（见「故障排查」）。

---

## 2. 安装步骤（Install）

进入 IaC 目录并安装依赖：

```bash
cd infra
npm ci
```

`npm ci` 会按 `package-lock.json` 精确安装 CDK 及其依赖。安装完成后即可使用 `infra/package.json` 中定义的脚本。

可选：先合成（synth）CloudFormation 模板做一次本地校验，不会创建任何 AWS 资源：

```bash
npm run synth        # 等价于 cdk synth
```

---

## 3. 一键部署（Deploy）

### 3.1 部署命令

```bash
# 使用 package.json 脚本（等价于 cdk deploy --all）
npm run deploy

# 或直接调用 CDK，并跳过逐项审批提示（适合 CI / 无人值守）
npx cdk deploy --all --require-approval never
```

部署会按依赖顺序串联创建四个 Stack：

```
kiro-gateway-network  →  kiro-gateway-data  →  kiro-gateway-compute  →  kiro-gateway-observability
```

（销毁时按逆序安全拆除。Stack 名前缀由参数 `stackName` 决定，默认 `kiro-gateway`。）

`cdk deploy` 会自动：构建容器镜像（读取仓库根目录 `Dockerfile`）→ 推送 ECR → 创建/更新全部资源 → 在末尾打印访问入口输出。

### 3.2 参数说明（Context 参数）

参数优先级为：**CDK context（`-c key=value`） > 环境变量 > 代码默认值**。使用 `-c` 传入，例如 `-c region=ap-southeast-1`。

#### 核心参数（可用 `-c` 或环境变量传入）

| Context 键 | 环境变量 | 含义 | 默认值 | 示例 |
| --- | --- | --- | --- | --- |
| `region` | `AWS_REGION`（或 `CDK_DEFAULT_REGION`） | 部署目标区域 | `us-east-1` | `-c region=ap-southeast-1` |
| `stackName` | `GATEWAY_STACK_NAME` | Stack 名称前缀（同时影响资源命名，如表名、密钥名、桶名） | `kiro-gateway` | `-c stackName=my-gw` |
| `minInstances` | `GATEWAY_MIN_INSTANCES` | Auto Scaling 最小实例数（实际期望实例数不低于 2 以保证高可用） | `2` | `-c minInstances=2` |
| `maxInstances` | `GATEWAY_MAX_INSTANCES` | Auto Scaling 最大实例数（会自动规整为不小于 min） | `6` | `-c maxInstances=10` |
| `cpu` | `GATEWAY_CPU` | 单实例 CPU 单位（1024 = 1 vCPU），需符合 Fargate 合法组合 | `512` | `-c cpu=1024` |
| `memory` | `GATEWAY_MEMORY_MIB` | 单实例内存（MiB），需符合 Fargate 合法组合 | `1024` | `-c memory=2048` |
| `account` | `CDK_DEFAULT_ACCOUNT` | 目标账号 ID（可选，缺省时由 CDK 从凭证环境推断） | 自动推断 | `-c account=123456789012` |

#### 计算层可选参数（仅 CDK context 生效）

| Context 键 | 含义 | 默认值 | 示例 |
| --- | --- | --- | --- |
| `certificateArn` | ACM 证书 ARN。**提供时**启用 HTTPS:443 监听并将 HTTP:80 永久重定向至 HTTPS；不提供时回退为 HTTP:80（开发/无域名场景）。 | 无（HTTP:80） | `-c certificateArn=arn:aws:acm:us-east-1:123456789012:certificate/abc` |
| `enableWaf` | 是否在 ALB 前置 AWS WAF（启用 AWS 托管通用规则集） | `false` | `-c enableWaf=true` |
| `requestsPerTarget` | 每目标请求数（并发）伸缩阈值（正整数）。不提供时不启用该伸缩指标，仅保留 CPU 70% 目标跟踪。 | 不启用 | `-c requestsPerTarget=200` |
| `streamingReadTimeout` | 流式读取超时（秒）。决定 ALB 空闲超时下限，避免 SSE 长流被提前断开。 | `300` | `-c streamingReadTimeout=600` |
| `gracefulShutdownTimeout` | 优雅停机超时（秒）。设置容器 `stopTimeout` 与目标组排空（draining）时长，上限 120。 | `120`（上限 120） | `-c gracefulShutdownTimeout=90` |
| `debugLogRetentionDays` | 调试日志 S3 桶对象保留天数（生命周期过期规则） | `30` | `-c debugLogRetentionDays=14` |

#### 组合示例

```bash
# 调整区域与实例规格 / 数量
npx cdk deploy --all \
  -c region=ap-southeast-1 \
  -c minInstances=2 -c maxInstances=10 \
  -c cpu=1024 -c memory=2048

# 启用 HTTPS + WAF + 请求并发伸缩
npx cdk deploy --all \
  -c certificateArn=arn:aws:acm:us-east-1:123456789012:certificate/abc \
  -c enableWaf=true \
  -c requestsPerTarget=200

# 通过环境变量传参（等价于核心参数）
GATEWAY_MAX_INSTANCES=12 AWS_REGION=eu-west-1 npm run deploy
```

---

## 4. 部署输出（Outputs）

部署成功后，CDK 会在终端打印各 Stack 的输出。重点关注访问入口：

| 输出键 | 所属 Stack | 含义 |
| --- | --- | --- |
| `GatewayEndpoint` | compute | **访问网关的完整 URL**。提供证书时为 `https://<alb-dns>`，否则为 `http://<alb-dns>`。 |
| `AppGatewayEndpoint` | compute | 应用级聚合的同一访问入口 URL，便于在 `cdk deploy --all` 末尾醒目打印。 |
| `LoadBalancerDns` | compute | ALB 的 DNS 名称（裸主机名，不含协议）。 |

数据层（data）还会输出后续配置所需的资源标识：

- `StateTableName`：DynamoDB 状态表名（`<stack>-gateway-state`）
- `ProxyApiKeySecretArn`：`<stack>/proxy-api-key` 密钥 ARN
- `CredentialsJsonSecretArn`：`<stack>/credentials-json` 密钥 ARN
- `ConfigParameterPrefix`：SSM 配置参数前缀（`/<stack>/config`）
- `AccountSecretPrefix`：各账号 token 密钥名称前缀（`<stack>/account`）
- `ConfigBucketName`：配置桶（`<stack>-gateway-config`）
- `DebugLogsBucketName`：调试日志桶（`<stack>-gateway-debug`）
- `EncryptionKeyArn`：统一静态加密 KMS 密钥 ARN

> 记下 `GatewayEndpoint`（下文记为 `<endpoint>`），用于部署后的验证与客户端接入。

---

## 5. 部署后配置（Post-Deployment）

数据层会创建占位密钥与配置骨架，**正式使用前需替换为真实值**。以下命令将 `<stack>` 替换为实际前缀（默认 `kiro-gateway`），`<region>` 替换为部署区域。

### 5.1 设置真实的客户端鉴权密钥 `PROXY_API_KEY`

`<stack>/proxy-api-key` 在部署时会自动生成随机值，可直接读取使用，或覆盖为自定义值：

```bash
# 读取自动生成的密钥（作为客户端 Authorization 使用）
aws secretsmanager get-secret-value \
  --secret-id "<stack>/proxy-api-key" \
  --region <region> --query SecretString --output text

# 或覆盖为自定义值
aws secretsmanager put-secret-value \
  --secret-id "<stack>/proxy-api-key" \
  --secret-string "sk-my-strong-key" \
  --region <region>
```

### 5.2 上传多账号 `credentials.json` 内容

`<stack>/credentials-json` 初始为占位骨架 `{"accounts": []}`，需替换为真实的多账号凭证内容（格式参考仓库根目录 `credentials.json.example`）：

```bash
# 将本地 credentials.json 的内容写入密钥
aws secretsmanager put-secret-value \
  --secret-id "<stack>/credentials-json" \
  --secret-string file://credentials.json \
  --region <region>
```

可选：也可将非敏感配置骨架放入配置桶（`<stack>-gateway-config`），由应用按需读取：

```bash
aws s3 cp credentials.json \
  "s3://<stack>-gateway-config/config/credentials.json" \
  --region <region>
```

> 各账号 token（`<stack>/account/<account_id>/token`）由网关在运行时按需创建/写回，无需手动创建。

### 5.3 验证部署

```bash
# 健康检查（ALB → /health，应返回 200）
curl -i "<endpoint>/health"

# 样例 /v1/models 调用（带 Authorization 头，使用 5.1 中的密钥）
curl "<endpoint>/v1/models" \
  -H "Authorization: Bearer <PROXY_API_KEY>"
```

若 `/health` 返回 200、`/v1/models` 返回模型列表，即表示部署与配置成功。

---

## 6. 销毁（Destroy）

```bash
# 使用脚本（等价于 cdk destroy --all），按逆序拆除全部 Stack
npm run destroy

# 或直接调用 CDK
npx cdk destroy --all
```

### 6.1 关于 RETAIN（保留）资源

为防止误删导致状态或密钥丢失，部分资源标记为 **RETAIN**，销毁 Stack 后**不会**自动删除，需要时请手动清理：

| 资源 | 删除策略 | 说明 |
| --- | --- | --- |
| DynamoDB 状态表（`<stack>-gateway-state`） | RETAIN | 承载运行时共享状态，保留以防误删。 |
| KMS 密钥（`<stack>-gateway-data` 别名） | RETAIN | 静态加密密钥，保留以保证可解密历史数据。 |
| Secrets（`<stack>/proxy-api-key`、`<stack>/credentials-json`） | 保留（含恢复窗口） | Secrets Manager 删除后默认有恢复窗口。 |
| 配置桶（`<stack>-gateway-config`） | RETAIN | 保留以防误删配置骨架。 |
| 调试日志桶（`<stack>-gateway-debug`） | 自动删除 | 非关键数据，随 Stack 自动清理。 |

手动清理保留资源示例：

```bash
# 删除 DynamoDB 表
aws dynamodb delete-table --table-name "<stack>-gateway-state" --region <region>

# 清空并删除配置桶
aws s3 rb "s3://<stack>-gateway-config" --force --region <region>

# 计划删除密钥（带恢复窗口）
aws secretsmanager delete-secret --secret-id "<stack>/proxy-api-key" --region <region>
aws secretsmanager delete-secret --secret-id "<stack>/credentials-json" --region <region>

# 计划删除 KMS 密钥（按 key id）
aws kms schedule-key-deletion --key-id <key-id> --pending-window-in-days 7 --region <region>
```

---

## 7. 故障排查（Troubleshooting）

| 现象 | 可能原因与处理 |
| --- | --- |
| 部署报缺少引导资源 / `bootstrap` 相关错误 | 未对目标「账号 + 区域」执行 `cdk bootstrap`。执行 `cdk bootstrap aws://<account>/<region>` 后重试。 |
| 镜像构建失败 / 找不到 Docker daemon | 部署机器未运行 Docker，或上下文异常。确认 `docker info` 可用；镜像由仓库根目录 `Dockerfile` 构建，构建上下文已排除 `infra`、`cdk.out`、`.git`，请勿在仓库根目录放置超大文件。 |
| 健康检查持续失败 / 任务反复重启 | 容器监听端口为 8000，健康检查路径 `/health`。检查应用启动日志（CloudWatch 日志组 `/ecs/<stack>-gateway`）、密钥与配置是否已正确写入（见第 5 节）、任务角色权限是否完整。可适当增大 `gracefulShutdownTimeout` 以延长启动宽限期。 |
| 缩容/部署期间偶发 ALB 503 | 实例在排空（draining）窗口内仍在完成在途请求属正常现象。目标组已按 `gracefulShutdownTimeout` 设置 deregistration delay，客户端重试即可；如长流较多可增大 `streamingReadTimeout` 与 `gracefulShutdownTimeout`。 |
| HTTPS 不生效 / 证书报错 | `certificateArn` 必须是**与部署区域相同**区域的有效 ACM 证书 ARN。区域与证书不匹配会导致监听器创建失败；不提供证书时服务以 HTTP:80 暴露。 |
| 资源命名冲突（表名/桶名已存在） | `stackName` 决定资源命名前缀。更换 `-c stackName=<新前缀>` 重新部署，或清理同名遗留资源。 |

---

_Requirements: 13.4_
