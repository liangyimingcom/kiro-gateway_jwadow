<div align="center">

# 迁移指南：从单机本地部署到 AWS 云原生部署

**Kiro Gateway · 本地文件后端（`STORAGE_BACKEND=local`）→ AWS 云原生后端（`STORAGE_BACKEND=aws`）**

[🇨🇳 中文](./MIGRATION_GUIDE.md) · [方案概述](./CLOUD_NATIVE_README.md) · [架构](./ARCHITECTURE_AWS.md) · [部署指南](./DEPLOYMENT_GUIDE.md) · [成本预估](./COST_ESTIMATION.md)

</div>

---

## 📑 目录

- [1. 总览](#1-总览)
- [2. 核心约束：对外 API 契约不变](#2-核心约束对外-api-契约不变)
- [3. 资源映射表（本地文件 → AWS）](#3-资源映射表本地文件--aws)
- [4. 前置条件](#4-前置条件)
- [5. 迁移步骤（Step-by-Step）](#5-迁移步骤step-by-step)
  - [步骤 0：约定 `<stack>` 与区域](#步骤-0约定-stack-与区域)
  - [步骤 1：部署基础设施（cdk deploy）](#步骤-1部署基础设施cdk-deploy)
  - [步骤 2：将 `.env` 非敏感配置写入 SSM Parameter Store](#步骤-2将-env-非敏感配置写入-ssm-parameter-store)
  - [步骤 3：设置 `PROXY_API_KEY` 密钥](#步骤-3设置-proxy_api_key-密钥)
  - [步骤 4：上传 `credentials.json` 骨架到 S3 并写入 Secrets Manager](#步骤-4上传-credentialsjson-骨架到-s3-并写入-secrets-manager)
  - [步骤 5：导入本地 token 到 Secrets Manager](#步骤-5导入本地-token-到-secrets-manager)
  - [步骤 6：滚动重启并验证](#步骤-6滚动重启并验证)
- [6. 回滚与双跑（本地后端仍可用）](#6-回滚与双跑本地后端仍可用)
- [7. 注意事项](#7-注意事项)
- [8. 常见问题（FAQ）](#8-常见问题faq)

---

## 1. 总览

改造前的 Kiro Gateway 是**单机部署**：单个进程（`python main.py` 或 `docker-compose`）从本地文件读取全部配置、凭证与运行时状态。改造后引入了**存储后端抽象层**，由环境变量 `STORAGE_BACKEND` 选择后端实现：

| 后端 | `STORAGE_BACKEND` | 数据来源 | 适用形态 |
|---|---|---|---|
| 本地文件后端（现状） | `local`（默认） | `.env`、`credentials.json`、`state.json`、本地凭证文件、`debug_logs/` | 开发态 / 单机 `docker-compose` |
| AWS 云原生后端 | `aws` | SSM Parameter Store / Secrets Manager / DynamoDB / S3 / CloudWatch | 生产态 / ECS Fargate 多实例池 |

> **关键点：** 同一份应用镜像在两种后端下行为等价。迁移**不是改代码**，而是把数据从本地文件搬到对应的 AWS 托管服务，再把 `STORAGE_BACKEND` 从 `local` 切换为 `aws`。

两套后端的组件对应关系（由 `kiro/backends/factory.py` 装配）：

| 抽象接口 | 本地实现（`local`） | AWS 实现（`aws`） |
|---|---|---|
| `ConfigProvider` | `LocalConfigProvider`（读 `.env`） | `SsmConfigProvider`（SSM）+ `S3ConfigProvider`（`credentials.json` 骨架） |
| `SecretProvider` | `LocalSecretProvider`（读 `.env` / 本地凭证文件） | `SecretsManagerProvider`（Secrets Manager） |
| `StateStore` | `LocalStateStore`（读写 `state.json`） | `DynamoStateStore` + `CachingStateStore`（DynamoDB + TTL 缓存） |
| `TokenRefreshCoordinator` | `NoopCoordinator`（单实例 `asyncio.Lock`） | `DynamoRefreshCoordinator`（DynamoDB 租约锁单飞刷新） |
| `DebugLogSink` | `LocalDebugLogSink`（写 `debug_logs/`） | `S3DebugLogSink`（S3 归档） |

---

## 2. 核心约束：对外 API 契约不变

迁移**完全保留**对外 API 契约，现有 OpenAI / Anthropic 客户端**无需任何修改**即可接入云原生部署：

- 端点保留：`GET /`、`GET /health`、`GET /v1/models`、`POST /v1/chat/completions`（OpenAI）、`POST /v1/messages`（Anthropic）。
- **SSE 流式**：`stream=true` 的请求继续以 Server-Sent Events 返回，首个数据块即转发、以 `[DONE]` 收尾，行为与单机一致。
- **鉴权方式不变**：客户端继续在 `Authorization: Bearer <PROXY_API_KEY>`（OpenAI）或 `x-api-key: <PROXY_API_KEY>`（Anthropic）头中携带密钥；区别仅在于服务端的 `PROXY_API_KEY` 来源从 `.env` 变为 Secrets Manager。
- 模型名 / 别名解析结果不变（如 `claude-sonnet-4-5`、`claude-sonnet-4.5`、版本化名称、`auto-kiro` 等）。

客户端唯一可能需要改动的是 **base_url**——把指向单机 `http://localhost:8000` 改为 ALB 的访问入口地址（部署成功后由 CDK 以 `GatewayEndpoint` / `AppGatewayEndpoint` 输出）。

---

## 3. 资源映射表（本地文件 → AWS）

下表中的 `<stack>` 为部署栈名前缀，默认 `kiro-gateway`（见 `infra/lib/config.ts` 的 `DEFAULT_CONFIG.stackName`）。资源名称与 `infra/lib/data-stack.ts` 中的实际声明保持一致。

| 改造前（本地文件） | 内容 | 改造后（AWS） | 资源标识 |
|---|---|---|---|
| `.env`（非敏感项） | 端口、超时、熔断退避、伸缩参数等 | **SSM Parameter Store** | `/<stack>/config/*` |
| `.env` 的 `PROXY_API_KEY` | 客户端鉴权密钥 | **Secrets Manager** | `<stack>/proxy-api-key` |
| `credentials.json`（骨架/非敏感结构） | 多账号配置列表 | **S3 配置桶** | `s3://<stack>-gateway-config/config/credentials.json` |
| `credentials.json` 的敏感字段 | refresh token、profile_arn 等 | **Secrets Manager** | `<stack>/credentials-json` |
| 本地凭证文件中的 token | `~/.aws/sso/cache/*.json`、kiro-cli `data.sqlite3` 中的 access/refresh token | **Secrets Manager**（按账号） | `<stack>/account/<account_id>/token` |
| `state.json`（运行时状态） | 失败计数、熔断状态、模型映射、sticky 索引、统计 | **DynamoDB** | `<stack>-gateway-state`（首次启动从空状态重建） |
| `debug_logs/`（调试日志目录） | 每请求调试载荷 | **S3 调试桶**（带保留期生命周期） | `s3://<stack>-gateway-debug/debug/<yyyy>/<mm>/<dd>/<request_id>.json` |
| 进程 stdout 日志 | 应用主日志 | **CloudWatch Logs** | 由 ECS 任务日志驱动采集 |

补充说明：

- **DynamoDB 单表设计**：表 `<stack>-gateway-state` 用 `PK`/`SK` 承载账号状态（`ACCT#<id>` / `STATE`）、全局 sticky 索引（`GLOBAL` / `STICKY`）、模型→账号映射（`MODEL#<model>` / `ACCOUNTS`）、令牌元数据（`TOKEN#<id>` / `META`）；按需计费（PAY_PER_REQUEST），TTL 属性 `expires_at`，KMS 静态加密。
- **各账号 token 密钥（`<stack>/account/<id>/token`）由应用在运行时按需创建/写回**（`put_secret`）。CDK 仅预创建 `<stack>/proxy-api-key` 与 `<stack>/credentials-json`，并以 `<stack>/account` 前缀授予最小权限。
- **`credentials.json` 拆分存放**：体积较大、属于配置而非单值密钥，因此骨架放 S3，敏感字段（如 refresh token）放 Secrets Manager，加载时合并。
- **S3 调试桶**带生命周期规则，按 `debugLogRetentionDays`（默认 30 天）自动过期清除调试日志。

---

## 4. 前置条件

- 已安装并配置 **AWS CLI v2**，且具备目标账号/区域的部署权限（`aws sts get-caller-identity` 可正常返回）。
- 已安装 **Node.js + AWS CDK**（`infra/` 为 CDK TypeScript 项目）。
- 一份可用的改造前单机配置：`.env`、`credentials.json`（若启用多账号系统），以及本地凭证文件（`~/.aws/sso/cache/*.json` 或 kiro-cli `data.sqlite3`）。
- 容器镜像可被 ECS 拉取（复用现有 `Dockerfile`，由 CI 构建并推送到 ECR）。

> 一键部署的完整命令与参数详见 [部署指南](./DEPLOYMENT_GUIDE.md)；本指南聚焦于**数据迁移**部分。

---

## 5. 迁移步骤（Step-by-Step）

下文命令以 `bash` 为例，`<stack>`、`<region>` 等占位符请按实际替换。所有写入操作均**幂等**，可安全重跑。

### 步骤 0：约定 `<stack>` 与区域

```bash
# 与 cdk deploy 时使用的 stackName / region 保持一致
export STACK=kiro-gateway
export AWS_REGION=us-east-1
```

`<stack>` 决定了全部资源名称（`<stack>-gateway-state`、`<stack>/proxy-api-key`、`/<stack>/config/*` 等）。运行时 ECS 任务通过环境变量 `STACK_NAME`（或 `GATEWAY_STACK_NAME`）感知同一前缀。

### 步骤 1：部署基础设施（cdk deploy）

先创建全部 AWS 资源（DynamoDB 表、Secrets、SSM 参数、S3 桶、ECS/ALB 等）。CDK 会为 SSM 写入合理默认值、为 `proxy-api-key` 生成随机初值、为 `credentials-json` 写入占位骨架 `{"accounts": []}`。

```bash
cd infra
npm install

# 一键部署全部 Stack（network → data → compute → observability）
cdk deploy --all \
  -c stackName=$STACK \
  -c region=$AWS_REGION \
  -c minInstances=2 -c maxInstances=6 \
  -c cpu=512 -c memory=1024
```

部署成功后记录输出的 `GatewayEndpoint`（ALB 访问入口）。此时服务已就绪，但密钥/凭证仍是占位值，需在后续步骤写入真实数据。

### 步骤 2：将 `.env` 非敏感配置写入 SSM Parameter Store

将改造前 `.env` 中的**非敏感**可调项写入 `/<stack>/config/*`。这些参数的运行时优先级为 **SSM 显式值 > 环境变量 > 代码默认值**（由 `SsmConfigProvider` 实现）。

CDK 已创建以下参数的默认值，按需覆盖即可：

```bash
# 流式与超时
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/STREAMING_READ_TIMEOUT" --value "300"
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/FIRST_TOKEN_TIMEOUT" --value "15"

# 熔断器（与 .env 中 ACCOUNT_* 同名）
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/ACCOUNT_RECOVERY_TIMEOUT" --value "60"
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/ACCOUNT_MAX_BACKOFF_MULTIPLIER" --value "1440"
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/ACCOUNT_PROBABILISTIC_RETRY_CHANCE" --value "0.1"

# 伸缩参数
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/scaling/min" --value "2"
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/scaling/max" --value "6"
aws ssm put-parameter --overwrite --type String \
  --name "/$STACK/config/scaling/cpu-target" --value "70"
```

> **不要把敏感值写进 SSM。** `PROXY_API_KEY`、refresh token 等一律走 Secrets Manager（步骤 3–5）。
>
> **不要迁移单机专属项。** 如 `SERVER_HOST` / `SERVER_PORT`（由容器/ALB 接管）、`ACCOUNTS_STATE_FILE` / `DEBUG_DIR`（已被 DynamoDB / S3 取代）。

校验：

```bash
aws ssm get-parameters-by-path --path "/$STACK/config" --recursive \
  --query "Parameters[].{Name:Name,Value:Value}" --output table
```

### 步骤 3：设置 `PROXY_API_KEY` 密钥

把改造前 `.env` 里的 `PROXY_API_KEY` 写入 `<stack>/proxy-api-key`（覆盖 CDK 生成的随机初值），使现有客户端的密钥继续可用：

```bash
aws secretsmanager put-secret-value \
  --secret-id "$STACK/proxy-api-key" \
  --secret-string "my-super-secret-password-123"
```

> 也可以保留 CDK 生成的随机密钥，但那样需要同步更新所有客户端。为保证客户端零改动，建议沿用原 `.env` 的值。

校验（注意：生产环境请勿在共享终端回显明文）：

```bash
aws secretsmanager get-secret-value --secret-id "$STACK/proxy-api-key" \
  --query SecretString --output text
```

### 步骤 4：上传 `credentials.json` 骨架到 S3 并写入 Secrets Manager

`credentials.json` 采用**拆分存放**：

1. **骨架 → S3**（`S3ConfigProvider` 从 `s3://<stack>-gateway-config/config/credentials.json` 读取）。骨架是一个 JSON 数组，结构与单机的 `credentials.json` 一致（见 [`credentials.json.example`](../../credentials.json.example)），但其中的本地文件路径（`path` 指向 `~/.aws/sso/cache/...` 等）在云端不可用，应改为 `refresh_token` 类型条目，或仅保留每账号的非敏感元数据（`profile_arn`、`region`、`api_region`、`enabled` 等）。

```bash
# 假设 credentials.cloud.json 是为云端整理后的骨架（数组）
aws s3 cp credentials.cloud.json \
  "s3://$STACK-gateway-config/config/credentials.json"
```

2. **完整/敏感内容 → Secrets Manager**（`<stack>/credentials-json`）。把含 refresh token 等敏感字段的多账号配置写入该密钥：

```bash
aws secretsmanager put-secret-value \
  --secret-id "$STACK/credentials-json" \
  --secret-string file://credentials.cloud.json
```

> 启动时网关会从 S3 读取骨架、并与 Secrets Manager 中的敏感字段合并，得到与单机等价的多账号配置。S3 骨架至少需包含一个账号条目，否则启动会以明确错误退出（`MissingConfigError`）。

### 步骤 5：导入本地 token 到 Secrets Manager

单机模式下，access/refresh token 来自本地凭证文件（`~/.aws/sso/cache/*.json`、kiro-cli `data.sqlite3`），刷新后写回本地。云原生模式下，token **集中存放**于 Secrets Manager，按账号一个密钥：`<stack>/account/<account_id>/token`。

把每个账号的 token 包导入对应密钥（`<account_id>` 与 `credentials.json` 中账号的标识一致）：

```bash
# 示例：token 包 JSON（字段名沿用单机凭证文件）
cat > account-token.json <<'JSON'
{
  "accessToken": "eyJ...",
  "refreshToken": "eyJ...",
  "expiresAt": "2025-01-12T23:00:00.000Z",
  "profileArn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/abc123",
  "region": "us-east-1"
}
JSON

# 该账号密钥若尚不存在，put-secret-value 在应用侧首次写回时会自动创建；
# 这里手动预置可让首次请求立即可用。
aws secretsmanager put-secret-value \
  --secret-id "$STACK/account/<account_id>/token" \
  --secret-string file://account-token.json \
  || aws secretsmanager create-secret \
       --name "$STACK/account/<account_id>/token" \
       --secret-string file://account-token.json
```

> **token 刷新改为集中存储**：运行期间某账号 token 到期时，由 `DynamoRefreshCoordinator` 以 DynamoDB 租约锁选出**单一实例**执行刷新（单飞），刷新结果写回 Secrets Manager 的同一密钥，其余实例复用——不会重复刷新，也不会写任何本地文件。
>
> 若你只用 `refresh_token` 类型账号且不预置 token，应用会在首次使用该账号时自动刷新并创建对应密钥；预置仅为加速首个请求。

### 步骤 6：滚动重启并验证

确保 ECS 任务定义将后端切换为 AWS，并感知到栈前缀/区域。关键环境变量：

| 环境变量 | 作用 | 默认/推导 |
|---|---|---|
| `STORAGE_BACKEND` | 选择后端 | 需设为 `aws` |
| `STACK_NAME` / `GATEWAY_STACK_NAME` | 资源名前缀 `<stack>` | `kiro-gateway` |
| `AWS_REGION` | AWS 区域 | 由 boto3/环境推导 |
| `GATEWAY_STATE_TABLE` | 覆盖 DynamoDB 表名 | `<stack>-gateway-state` |
| `DEBUG_S3_BUCKET` | 覆盖 S3 调试桶 | `<stack>-gateway-debug` |
| `SECRET_CACHE_TTL_SECONDS` | 密钥读缓存 TTL | `300` |

> 这些变量由 CDK 的 ComputeStack 注入到 Fargate 任务定义；通常无需手工设置。重新部署或滚动重启后，新配置即生效（`SsmConfigProvider.reload` 在启动时加载）。

验证服务健康与契约：

```bash
ENDPOINT="<cdk 输出的 GatewayEndpoint>"

# 1) 健康检查（ALB 也用它做目标健康判定）
curl -s "$ENDPOINT/health"

# 2) 模型列表（需带 PROXY_API_KEY）
curl -s "$ENDPOINT/v1/models" \
  -H "Authorization: Bearer my-super-secret-password-123"

# 3) 一次最小的流式补全（验证 SSE 透传）
curl -N "$ENDPOINT/v1/chat/completions" \
  -H "Authorization: Bearer my-super-secret-password-123" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

若 `/v1/models` 返回模型列表、流式响应正常以 `[DONE]` 收尾，即表示 AWS 后端已成功接管。

---

## 6. 回滚与双跑（本地后端仍可用）

存储后端抽象层使**两套后端始终可并存**，迁移期间可平滑回滚或并行验证：

- **回滚到本地**：把 `STORAGE_BACKEND` 设回 `local`（或不设，默认即 `local`），用 `docker-compose up -d` 在单机继续运行——读取原有 `.env` / `credentials.json` / `state.json` / 本地凭证 / `debug_logs/`，与改造前完全一致。AWS 资源可保留不动（DynamoDB 表与 S3 桶在 `cdk destroy` 时按 `RETAIN` 策略保留，避免误删）。
- **双跑验证等价性**：对同一批请求，分别打到 `local`（docker-compose）与 `aws`（ALB 入口）两套部署，比较响应是否等价（端点、SSE 帧序列、模型解析、鉴权行为）。这是验证向后兼容的推荐做法，也对应设计中的“双后端等价”集成测试。
- **灰度切流**：可先用少量客户端切到 ALB 入口，确认无异常后再整体切换 base_url。

---

## 7. 注意事项

- **`state.json` 不直接迁移**：运行时状态（失败计数、熔断器状态、sticky 索引、统计）**从零重建**。首次启动时 DynamoDB 为空状态，`DynamoStateStore.get_account_state` 对无记录账号返回空 `AccountState`，语义无损——失败计数本就应从 0 开始，熔断器会随真实请求自然演化。无需、也不应把单机的 `state.json` 内容塞进 DynamoDB。
- **token 刷新改为集中存储**：云端不再写回本地凭证文件；刷新经分布式单飞协调后写入 Secrets Manager。请勿在云端继续依赖本地文件回写逻辑（`SQLITE_READONLY` 等单机选项在 AWS 后端无意义）。
- **幂等性**：本指南所有写入（`ssm put-parameter --overwrite`、`secretsmanager put-secret-value`、`s3 cp`）均可安全重跑；DynamoDB 侧的失败计数用原子 `ADD`、模型映射用 String Set 去重，重复/并发更新不会丢失或重复。
- **不要把敏感值写进 SSM 或 S3 骨架**：refresh token、access token、`PROXY_API_KEY` 一律放 Secrets Manager；S3 骨架只放非敏感结构。
- **保持 `<stack>` 一致**：迁移命令里的 `<stack>` 必须与 `cdk deploy` 时的 `stackName` 完全一致，否则应用会绑定到错误（或不存在）的资源名。
- **最终一致性**：账号共享状态经进程内 TTL 缓存（约 1–2s）读取，跨实例为秒级最终一致；熔断/故障转移对短暂滞后不敏感，属预期行为。
- **日志脱敏**：所有日志经 `redact` 脱敏后才输出，迁移过程中也不会在 CloudWatch 中泄露密钥明文；但请仍避免在共享终端用 `get-secret-value` 回显敏感值。

---

## 8. 常见问题（FAQ）

**Q：迁移后客户端要改什么？**
A：通常只改 `base_url`（指向 ALB 入口）。`PROXY_API_KEY`、模型名、请求/响应体、SSE 行为均不变。

**Q：必须先把 `state.json` 导进去吗？**
A：不需要。运行时状态从零重建即可，语义无损。

**Q：能否先只迁移一部分，逐步切换？**
A：可以。本地与 AWS 两套后端可长期并存，支持灰度切流与回滚（见第 6 节）。

**Q：各账号 token 密钥要手动建吗？**
A：不强制。`refresh_token` 类型账号在首次使用时会自动刷新并创建 `<stack>/account/<id>/token`；预置只是为了让首个请求立即可用。

**Q：调试日志去哪了？**
A：写入 S3 调试桶 `s3://<stack>-gateway-debug/debug/<yyyy>/<mm>/<dd>/<request_id>.json`，并按保留期（默认 30 天）自动过期；应用主日志走 stdout → CloudWatch Logs。

---

<div align="center">

**下一步：** [部署指南](./DEPLOYMENT_GUIDE.md) · [架构说明](./ARCHITECTURE_AWS.md) · [成本预估](./COST_ESTIMATION.md)

</div>
