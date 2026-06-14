# 需求文档

## 引言（Introduction）

本需求文档描述了将 **kiro-gateway**（一个基于 Python FastAPI 的单机代理网关，兼容 OpenAI / Anthropic API，反向代理 Kiro API / Amazon Q Developer）改造为 **AWS 云原生池化服务** 的目标与验收标准。

改造借鉴 AWS 官方方案《Guidance for Multi-Provider Generative AI Gateway on AWS》的最佳实践，将单机系统升级为：容器化计算池（ECS Fargate）+ 应用负载均衡（ALB）+ 自动伸缩（Auto Scaling）+ 多可用区高可用部署，并将本地文件（`.env`、`credentials.json`、`state.json`、本地凭证文件、`debug_logs/`）统一改造为外部托管服务（S3 / Secrets Manager / SSM Parameter Store / DynamoDB / CloudWatch）。改造后的产出物在整体格式上对齐 AWS guidance 项目，包括中文说明文档、架构图（Mermaid）、一键部署模板（IaC）与部署指南，便于用户查阅、使用与在 AWS 上部署。

本次改造的核心约束是 **向后兼容**：改造后的网关必须保持与现有 OpenAI / Anthropic 客户端的 API 契约一致（含 SSE 流式响应），使现有客户端无需修改即可接入。

### 改造前现状关键事实

- 技术栈：Python 3.10+ / FastAPI / uvicorn / httpx / loguru，单机运行（`python main.py` 或 `docker-compose`）。
- 已有 Docker 镜像（`Dockerfile` + `docker-compose.yml`），推送至 `ghcr.io`。
- 配置来自本地文件：`.env`、`credentials.json`（多账号配置）、`state.json`（运行时状态），以及本地凭证文件（`~/.aws/sso/cache/*.json`、kiro-cli 的 `data.sqlite3`）。
- 状态持久化：`state.json` 写本地磁盘（账号失败计数、熔断器状态、模型映射、统计），由后台任务每 10 秒定期写盘。
- 多账号系统（`AccountManager`）：内存中的熔断器（Circuit Breaker）+ sticky 粘性行为 + 故障转移，状态写本地 `state.json`——单机内存状态，多实例无法共享。
- 认证 token 刷新：`KiroAuthManager` 在内存中管理 token，刷新后会写回本地 JSON / SQLite 文件。
- 调试日志写本地 `debug_logs/` 目录。
- 健康检查端点：`GET /health`。
- 对外 API：`/v1/models`、`/v1/chat/completions`（OpenAI）、`/v1/messages`（Anthropic），支持 SSE 流式。

## 术语表（Glossary）

- **Gateway（网关）**：改造后部署在 AWS 上的 kiro-gateway 应用实例，提供 OpenAI / Anthropic 兼容 API。
- **Gateway_Service（网关服务）**：由多个 Gateway 容器实例组成的池化计算服务（ECS Fargate Service），由编排器统一管理。
- **Load_Balancer（负载均衡器）**：Application Load Balancer（ALB），负责将客户端请求分发到健康的 Gateway 实例。
- **Auto_Scaler（自动伸缩控制器）**：ECS Service Auto Scaling 与其伸缩策略，负责根据指标增减 Gateway 实例数量。
- **Config_Store（配置存储）**：用于集中存放非敏感配置（如模型映射、超时参数）的 AWS 服务，指 SSM Parameter Store 与 S3。
- **Secret_Store（密钥存储）**：用于集中存放敏感凭证（Kiro refresh token、`credentials.json`、`PROXY_API_KEY`）的 AWS Secrets Manager。
- **State_Store（共享状态存储）**：用于多实例共享运行时状态（账号失败计数、熔断器状态、模型映射、统计）的托管服务，指 DynamoDB。
- **Object_Store（对象存储）**：用于集中存放配置文件与调试日志归档的 Amazon S3。
- **Observability_Service（可观测性服务）**：Amazon CloudWatch，包含 Logs、Metrics、Alarms 与 Dashboard。
- **Deployment_Template（部署模板）**：实现一键部署的基础设施即代码（IaC）模板，指 AWS CDK 或 CloudFormation。
- **Account_Manager（账号管理器）**：管理多个 Kiro 账号、执行熔断与故障转移的组件，改造后从 State_Store 读写共享状态。
- **Circuit_Breaker（熔断器）**：基于账号连续失败次数与指数退避冷却时间，判定账号是否暂时跳过的机制。
- **Health_Check（健康检查）**：`GET /health` 端点，供 Load_Balancer 与编排器判定实例健康状态。
- **OpenAI_API**：兼容 OpenAI 的端点集合：`/v1/models`、`/v1/chat/completions`。
- **Anthropic_API**：兼容 Anthropic 的端点：`/v1/messages`。
- **SSE_Stream（流式响应）**：基于 Server-Sent Events 的流式输出，用于 `stream=true` 的请求。
- **Availability_Zone（可用区，AZ）**：AWS 区域内相互隔离的数据中心分区。
- **Operator（运维人员）**：负责部署、配置与运维 Gateway_Service 的用户。
- **API_Client（API 客户端）**：调用 OpenAI_API 或 Anthropic_API 的下游应用或工具。

## 需求（Requirements）

### 需求 1：容器化计算池（云服务池化）

**用户故事：** 作为运维人员，我希望网关以容器化计算池的形式运行在 AWS 托管编排服务上，以便摆脱单机部署限制并支持多实例并行处理。

#### 验收标准（Acceptance Criteria）

1. THE Gateway_Service SHALL 以容器镜像形式在 AWS ECS Fargate 上运行 Gateway 实例。
2. THE Gateway_Service SHALL 支持同时运行不少于 2 个 Gateway 实例。
3. WHEN Operator 部署 Gateway_Service 时，THE Deployment_Template SHALL 创建无需管理底层服务器的托管计算资源。
4. THE Gateway SHALL 以无状态方式运行，将所有运行时共享状态外置到 State_Store。
5. WHERE Operator 指定单个实例的 CPU 与内存规格，THE Gateway_Service SHALL 按指定规格创建 Gateway 实例。
6. THE Gateway SHALL 复用现有的 OpenAI_API 与 Anthropic_API 路由实现，不改变对外 API 契约。

### 需求 2：负载均衡

**用户故事：** 作为 API 客户端，我希望通过统一入口访问网关，由负载均衡器把请求分发到健康实例，以便获得稳定的服务地址与请求分发。

#### 验收标准

1. THE Load_Balancer SHALL 对外提供单一稳定的访问入口地址。
2. WHEN API_Client 发送请求至 Load_Balancer 时，THE Load_Balancer SHALL 将请求转发至一个通过 Health_Check 的 Gateway 实例。
3. WHILE 某个 Gateway 实例未通过 Health_Check，THE Load_Balancer SHALL 停止向该实例转发新请求。
4. THE Load_Balancer SHALL 通过 `GET /health` 端点对每个 Gateway 实例执行周期性 Health_Check。
5. WHEN API_Client 发起 SSE_Stream 请求时，THE Load_Balancer SHALL 在整个流式响应期间保持连接转发而不中断该响应。
6. THE Load_Balancer SHALL 将每个请求的空闲超时配置为不小于 STREAMING_READ_TIMEOUT 所定义的流式读取超时时长。

### 需求 3：高可用（多可用区部署）

**用户故事：** 作为运维人员，我希望网关跨多个可用区部署，以便在单个可用区故障时服务仍可继续提供。

#### 验收标准

1. THE Gateway_Service SHALL 将 Gateway 实例分布在不少于 2 个 Availability_Zone 中。
2. THE Load_Balancer SHALL 部署在不少于 2 个 Availability_Zone 中。
3. IF 一个 Availability_Zone 不可用，THEN THE Gateway_Service SHALL 继续通过其余 Availability_Zone 中的健康实例处理请求。
4. IF 一个 Gateway 实例终止或不健康，THEN THE Gateway_Service SHALL 自动创建新的 Gateway 实例以恢复期望的实例数量。
5. THE State_Store SHALL 提供跨多个 Availability_Zone 的数据冗余。

### 需求 4：自动伸缩

**用户故事：** 作为运维人员，我希望网关根据负载自动增减实例数量，以便在高峰期保证性能并在低谷期节约成本。

#### 验收标准

1. WHEN Gateway_Service 的平均 CPU 利用率在持续 3 分钟内超过 70%，THE Auto_Scaler SHALL 增加 Gateway 实例数量。
2. WHEN Gateway_Service 的平均 CPU 利用率在持续 10 分钟内低于 30%，THE Auto_Scaler SHALL 减少 Gateway 实例数量。
3. THE Auto_Scaler SHALL 将 Gateway 实例数量维持在 Operator 配置的最小值与最大值之间。
4. WHILE Gateway 实例数量已达到配置的最大值，THE Auto_Scaler SHALL 停止继续增加实例。
5. WHEN Auto_Scaler 缩减实例时，THE Gateway_Service SHALL 在终止实例前完成该实例正在处理的请求（优雅停机）。
6. WHERE Operator 配置基于请求并发数的伸缩策略，THE Auto_Scaler SHALL 依据每实例请求并发指标执行伸缩。

### 需求 5：配置外置到 S3 与 SSM Parameter Store

**用户故事：** 作为运维人员，我希望网关从外部 S3 与参数存储统一获取配置，而非读取本地文件，以便多个实例使用一致的配置。

#### 验收标准

1. WHEN Gateway 实例启动时，THE Gateway SHALL 从 Config_Store 或 Object_Store 加载运行配置，而非读取本地 `.env` 文件。
2. THE Gateway SHALL 从 Object_Store 读取 `credentials.json` 中定义的多账号配置。
3. WHERE 同一配置项同时存在于 Config_Store 与默认值，THE Gateway SHALL 优先使用 Config_Store 中的取值。
4. IF 必需配置项在 Config_Store 与 Object_Store 中均缺失，THEN THE Gateway SHALL 记录明确的错误信息并以非零状态退出启动流程。
5. THE Gateway SHALL 在不修改 OpenAI_API 与 Anthropic_API 行为的前提下完成配置来源从本地文件到外部存储的替换。
6. WHEN Operator 更新 Config_Store 中的配置项，THE Gateway_Service SHALL 在实例重启或重新部署后采用更新后的配置。

### 需求 6：凭证与密钥安全管理（Secrets Manager）

**用户故事：** 作为运维人员，我希望敏感凭证集中存放于密钥管理服务并加密保护，而非明文存放在本地文件，以便满足安全合规要求。

#### 验收标准

1. THE Gateway SHALL 从 Secret_Store 读取 Kiro 凭证（refresh token、JSON 凭证内容、SQLite 凭证内容）。
2. THE Gateway SHALL 从 Secret_Store 读取客户端鉴权密钥 PROXY_API_KEY。
3. THE Secret_Store SHALL 对存储的凭证进行静态加密。
4. THE Gateway SHALL 通过 IAM 角色访问 Secret_Store，而非在配置或镜像中嵌入访问密钥。
5. THE Gateway SHALL 在日志输出中对凭证与密钥值进行脱敏处理。
6. WHEN API_Client 在 `Authorization` 头中提供的密钥与 Secret_Store 中的 PROXY_API_KEY 不匹配，THE Gateway SHALL 拒绝该请求并返回鉴权失败状态码。
7. WHERE Kiro 的 access token 在运行期间被刷新，THE Gateway SHALL 将刷新后的 token 写入 State_Store 或 Secret_Store，而非写回本地文件。

### 需求 7：共享运行时状态外置（DynamoDB）

**用户故事：** 作为运维人员，我希望账号熔断状态与统计信息在所有实例间共享，而非各实例独立的内存与本地 `state.json`，以便多实例对账号故障转移做出一致决策。

#### 验收标准

1. THE Account_Manager SHALL 将账号失败计数、最近失败时间、模型缓存时间与使用统计持久化到 State_Store，而非本地 `state.json`。
2. THE Account_Manager SHALL 将账号到模型的映射关系持久化到 State_Store。
3. WHEN 一个 Gateway 实例将某账号标记为失败，THE State_Store SHALL 使该失败状态对其余 Gateway 实例可见。
4. WHILE 某账号的 Circuit_Breaker 处于冷却期，所有 Gateway 实例 SHALL 依据 State_Store 中的共享状态对该账号执行相同的跳过判定。
5. WHEN Gateway 实例需要选择处理账号时，THE Account_Manager SHALL 从 State_Store 读取最新的共享状态以做出选择。
6. THE Account_Manager SHALL 对 State_Store 中并发更新的同一账号计数采用原子更新方式，以避免多实例间的更新覆盖。
7. THE Account_Manager SHALL 在保持现有 Circuit_Breaker 指数退避与故障转移语义不变的前提下完成状态外置。

### 需求 8：可观测性（CloudWatch 日志、指标与告警）

**用户故事：** 作为运维人员，我希望集中查看日志、指标并接收告警，而非登录单机查看本地日志，以便监控服务健康与排查问题。

#### 验收标准

1. THE Gateway SHALL 将应用日志输出到标准输出流，以便被 Observability_Service 采集。
2. THE Gateway SHALL 将调试日志写入 Observability_Service 或 Object_Store，而非本地 `debug_logs/` 目录。
3. THE Gateway_Service SHALL 向 Observability_Service 上报请求量、错误率与请求延迟指标。
4. WHEN Gateway_Service 在持续 5 分钟内的 5xx 错误率超过 5%，THE Observability_Service SHALL 触发告警。
5. WHEN 没有任何 Gateway 实例通过 Health_Check，THE Observability_Service SHALL 触发告警。
6. THE Deployment_Template SHALL 创建一个集中展示请求量、错误率、延迟与实例数量的 Observability_Service 仪表板。
7. THE Gateway SHALL 在日志条目中包含请求标识，以便跨实例关联同一请求的日志。

### 需求 9：一键部署（IaC 模板）

**用户故事：** 作为运维人员，我希望通过一键部署模板创建全部 AWS 资源，而非手工配置，以便快速、可重复地在 AWS 上部署网关。

#### 验收标准

1. THE Deployment_Template SHALL 以基础设施即代码（AWS CDK 或 CloudFormation）形式定义全部所需 AWS 资源。
2. WHEN Operator 执行一次部署命令，THE Deployment_Template SHALL 创建 Gateway_Service、Load_Balancer、Auto_Scaler、State_Store、Secret_Store、Object_Store 与 Observability_Service 资源。
3. THE Deployment_Template SHALL 通过参数化方式允许 Operator 配置实例规格、最小与最大实例数量及 AWS 区域。
4. WHEN 部署成功完成，THE Deployment_Template SHALL 输出 Load_Balancer 的访问入口地址。
5. WHEN Operator 执行销毁命令，THE Deployment_Template SHALL 移除本次部署所创建的资源。
6. THE Deployment_Template SHALL 为 Gateway 实例配置仅授予所需最小权限的 IAM 角色。

### 需求 10：向后兼容（API 契约与流式支持）

**用户故事：** 作为 API 客户端，我希望改造后的网关保持与改造前一致的 OpenAI / Anthropic API 行为与流式输出，以便现有客户端无需修改即可继续使用。

#### 验收标准

1. THE Gateway SHALL 保留 `/v1/models`、`/v1/chat/completions` 与 `/v1/messages` 端点及其请求与响应格式。
2. WHEN API_Client 发送 `stream=true` 的请求，THE Gateway SHALL 以 SSE_Stream 形式返回与改造前一致的流式响应。
3. THE Gateway SHALL 保留 `GET /health` 健康检查端点并返回表示健康的响应。
4. THE Gateway SHALL 保留通过 `Authorization` 头进行客户端鉴权的方式。
5. WHERE API_Client 使用改造前支持的模型名称与别名，THE Gateway SHALL 返回与改造前一致的模型解析结果。

### 需求 11：高性能

**用户故事：** 作为 API 客户端，我希望网关在并发负载下保持低额外延迟与稳定吞吐，以便获得良好的使用体验。

#### 验收标准

1. THE Gateway SHALL 为对上游 Kiro API 的请求复用连接池，而非为每个请求新建连接。
2. WHEN API_Client 发起 SSE_Stream 请求，THE Gateway SHALL 在收到上游首个数据块后即开始向客户端转发，而不等待完整响应。
3. THE Gateway SHALL 将读取共享状态、配置与密钥引入的非流式请求额外延迟控制在 50 毫秒以内（P95）。
4. WHILE Gateway_Service 承受并发请求，THE Gateway_Service SHALL 通过 Auto_Scaler 增加实例以维持请求处理能力。
5. THE Gateway SHALL 对 State_Store、Config_Store 与 Secret_Store 的读取结果进行带 TTL 的本地缓存，以减少重复外部调用。

### 需求 12：成本可控

**用户故事：** 作为运维人员，我希望在满足性能与可用性的前提下控制运行成本，以便高效使用预算。

#### 验收标准

1. WHILE 请求负载处于低谷，THE Auto_Scaler SHALL 将 Gateway 实例数量缩减至 Operator 配置的最小值。
2. THE Gateway SHALL 缓存 Secret_Store 与 Config_Store 的读取结果，以减少按调用次数计费的访问开销。
3. THE State_Store SHALL 采用按请求计费的容量模式，以避免低负载时段的固定容量开销。
4. WHERE Operator 配置调试日志的保留期限，THE Object_Store 或 Observability_Service SHALL 在超过保留期限后清除对应的调试日志。

### 需求 13：完整中文文档与架构图

**用户故事：** 作为用户，我希望获得格式对齐 AWS guidance 项目的完整中文文档与架构图，以便查阅、理解并在 AWS 上部署网关。

#### 验收标准

1. THE 改造产出物 SHALL 提供中文说明文档，覆盖方案概述、架构、前置条件、部署步骤、配置说明与卸载步骤。
2. THE 中文文档 SHALL 包含一张 Mermaid 架构图，展示 Load_Balancer、Gateway_Service、Auto_Scaler、State_Store、Secret_Store、Object_Store 与 Observability_Service 之间的关系。
3. THE 中文文档 SHALL 在整体结构与章节组织上对齐《Guidance for Multi-Provider Generative AI Gateway on AWS》项目格式。
4. THE 中文文档 SHALL 提供一键部署的操作命令与参数说明。
5. THE 中文文档 SHALL 说明从改造前本地文件配置迁移至 Config_Store、Secret_Store 与 State_Store 的步骤。
6. THE 中文文档 SHALL 提供预估成本说明与影响成本的主要因素。
