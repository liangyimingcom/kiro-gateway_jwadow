# 实现计划（Implementation Plan）：AWS 云原生网关（aws-cloud-native-gateway）

## 概述（Overview）

本实现计划将 design.md 中描述的 AWS 云原生再架构拆解为一系列增量式、测试驱动的编码任务。整体顺序为：**存储后端抽象层 → 本地文件后端（行为等价于现状）→ 重构现有模块依赖抽象 → AWS 后端实现 → 本地 TTL 缓存与优雅停机 → 属性化/单元/集成测试 → AWS CDK 基础设施（IaC）→ CI/CD → 中文文档交付物**。

每个任务都建立在前序任务之上，确保没有悬空或未集成的代码。路由层、转换层（converters）、流式层（streaming）、解析层保持不变，仅替换“存储 / 状态 / 凭证 / 配置 / 日志”这几处 I/O 边界。

标注约定：
- 带 `*` 的子任务为可选测试任务（单元测试、属性测试、集成测试），可为加速 MVP 而跳过。
- 属性化测试任务显式引用 design.md “正确性属性”章节中的属性编号，并使用注释标注：`# Feature: aws-cloud-native-gateway, Property {number}: {property_text}`。
- 每个任务通过 `_Requirements: X.Y_` 引用其满足的具体验收标准。

---

## 任务（Tasks）

- [x] 1. 搭建存储后端抽象层骨架与后端选择机制
  - 在 `kiro/backends/` 下创建包结构，定义抽象接口模块 `kiro/backends/interfaces.py`
  - 使用 `typing.Protocol` 定义五个核心接口：`ConfigProvider`、`SecretProvider`、`StateStore`、`TokenRefreshCoordinator`、`DebugLogSink`（签名对齐 design.md “组件与接口”章节）
  - 定义共享数据类型：`AccountState`、`TokenBundle`，以及自定义异常 `MissingConfigError`
  - 创建后端工厂 `kiro/backends/factory.py`，依据 `STORAGE_BACKEND`（`local` | `aws`）环境变量返回对应实现集合
  - _Requirements: 1.4, 5.1, 5.5_

- [x] 2. 实现本地文件后端（LocalFileBackend，行为等价于现状）
  - [x] 2.1 实现 `LocalConfigProvider`（本地配置提供者）
    - 在 `kiro/backends/local/config_provider.py` 中包装现有 `kiro/config.py` 的 `python-dotenv` 加载逻辑与 Windows 路径处理
    - 实现 `get` / `get_required` / `get_namespace` / `reload`，`get_required` 在缺失时抛 `MissingConfigError`（含键名）
    - 实现配置优先级：环境变量 > 代码默认值
    - _Requirements: 5.1, 5.3, 5.4_

  - [ ]* 2.2 为配置来源优先级编写属性测试
    - **Property 1: 配置来源优先级**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 1: 对任意配置键及其在各层存在性与取值的任意组合，ConfigProvider.get 返回最高优先级层的取值（SSM > 环境变量 > 默认值）`
    - 使用 Hypothesis（`max_examples>=100`）生成三层取值组合
    - **Validates: Requirements 5.3**

  - [ ]* 2.3 为必需配置缺失编写属性测试
    - **Property 2: 必需配置缺失触发明确错误**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 2: 对任意必需配置键集合与其任意缺失子集，对缺失键调用 get_required 抛出含键名的 MissingConfigError，对存在键正常返回`
    - **Validates: Requirements 5.4**

  - [x] 2.4 实现 `LocalSecretProvider`（本地密钥提供者）
    - 在 `kiro/backends/local/secret_provider.py` 中从 `.env` / 本地凭证文件读取密钥
    - 实现 `get_secret` / `get_json_secret` / `put_secret`（开发态写本地）/ `redact`（日志脱敏，输出中不含密钥明文）
    - _Requirements: 5.1, 6.5_

  - [ ]* 2.5 为日志脱敏编写属性测试
    - **Property 3: 日志密钥脱敏**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 3: 对任意文本及任意嵌入其中的密钥值，redact 输出不含任何密钥明文，同时保留非密钥内容`
    - **Validates: Requirements 6.5**

  - [x] 2.6 实现 `LocalStateStore`（本地共享状态存储）
    - 在 `kiro/backends/local/state_store.py` 中基于现有 `state.json`（tmp+rename 原子写、每约 10s 落盘）实现 `StateStore` 接口
    - 实现 `get_account_state` / `increment_failure` / `reset_failure` / `incr_stats` / sticky 索引读写 / 模型映射读写，保持现状语义
    - _Requirements: 1.4, 7.1, 7.2_

  - [x] 2.7 实现 `NoopCoordinator` 与 `LocalDebugLogSink`
    - `kiro/backends/local/coordinator.py`：`NoopCoordinator` 复用现有 `asyncio.Lock` 单飞路径（单实例即单飞）
    - `kiro/backends/local/debug_sink.py`：`LocalDebugLogSink` 写本地 `debug_logs/` 目录（现状）
    - 在工厂中注册 `local` 后端，组装上述全部本地实现
    - _Requirements: 6.7, 8.2_

- [x] 3. 检查点 - 确保本地后端全部测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 4. 重构现有模块以依赖抽象层（保持对外契约不变）
  - [x] 4.1 重构 `kiro/config.py` 依赖 `ConfigProvider`
    - 将 `os.getenv` 调用改为通过注入的 `provider.get`，保留所有常量名、默认值、模型别名/隐藏模型逻辑
    - 通过工厂在启动时注入 provider
    - _Requirements: 5.1, 5.3, 5.5, 10.5_

  - [ ]* 4.2 为模型解析一致性编写属性测试
    - **Property 11: 模型解析一致性**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 11: 对任意模型名称或别名输入（含隐藏模型与 auto-kiro 等别名），改造后 ModelResolver 解析结果与改造前一致`
    - **Validates: Requirements 10.5**

  - [x] 4.3 重构 `kiro/account_manager.py` 依赖 `StateStore`
    - 将 `load_state` / `_save_state` 改为通过 `StateStore` 读写共享状态
    - `failures` / `stats` 改为通过 `increment_failure` / `incr_stats` 原子操作；sticky 索引与模型映射改为经 StateStore 读写
    - 保持熔断器指数退避公式、sticky 选择、概率重试、TTL 刷新、单账号旁路语义不变
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 7.6, 7.7_

  - [ ]* 4.4 为熔断器冷却判定确定性编写属性测试
    - **Property 8: 熔断器冷却判定确定性**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 8: 对任意 (failures, last_failure_time, now)，冷却判定为纯函数 is_in_cooldown = (now - last_failure_time) < ACCOUNT_RECOVERY_TIMEOUT * min(2^(failures-1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)`
    - **Validates: Requirements 7.4**

  - [x] 4.5 重构 `kiro/auth.py` 的 `KiroAuthManager` 依赖 `SecretProvider` + `TokenRefreshCoordinator`
    - 将 `_save_credentials_to_file` / `_save_credentials_to_sqlite` 改为 `SecretProvider.put_secret`
    - 刷新流程改为经 `TokenRefreshCoordinator` 协调（本地态走 Noop 单飞）
    - 保持 token 过期判断、Desktop/SSO OIDC 刷新协议、region 推导不变
    - _Requirements: 6.1, 6.7, 7.7_

  - [x] 4.6 重构 `kiro/debug_logger.py` 依赖 `DebugLogSink`
    - 将本地目录写入改为通过注入的 `DebugLogSink.write`，保持日志内容结构不变
    - _Requirements: 8.2_

  - [ ]* 4.7 为已重构模块编写单元测试与端点存在性测试
    - 复用并补充 `tests/unit/`，断言 `/v1/models`、`/v1/chat/completions`、`/v1/messages`、`/health` 已注册
    - 覆盖空 `credentials.json`、单账号旁路、INVALID_MODEL_ID 不惩罚、token 即将过期判定等边界
    - _Requirements: 10.1, 10.3_

- [x] 5. 检查点 - 确保重构后向后兼容测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 6. 实现 AWS 后端：配置与密钥提供者
  - [x] 6.1 实现 `SsmConfigProvider` 与 `S3ConfigProvider`
    - 在 `kiro/backends/aws/config_provider.py` 中使用 boto3/aioboto3 通过 `GetParametersByPath` 批量读取 SSM（前缀 `/<stack>/config/*`）
    - 实现配置优先级 `SSM 显式值 > 环境变量 > 默认值`
    - `S3ConfigProvider` 从 `s3://<bucket>/config/credentials.json` 读取多账号骨架配置
    - 必需项在 SSM 与 S3 均缺失时记录明确错误并以非零状态退出
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6_

  - [x] 6.2 实现 `SecretsManagerProvider`
    - 在 `kiro/backends/aws/secret_provider.py` 中使用 boto3/aioboto3 读取 `PROXY_API_KEY`、`credentials.json` 敏感字段、各账号 token
    - 实现 `put_secret` 写回刷新后的 token；实现 `redact` 脱敏；通过 IAM 角色访问（不嵌入访问密钥）
    - _Requirements: 6.1, 6.2, 6.4, 6.7_

  - [ ]* 6.3 为刷新令牌持久化往返编写属性测试
    - **Property 5: 刷新令牌持久化往返**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 5: 对任意账号与刷新后 token 包，经 put_secret 写入后再读回应得到等价 token 包，且不写入任何本地凭证文件`
    - 使用 moto 模拟 Secrets Manager（`max_examples>=100`）
    - **Validates: Requirements 6.7**

  - [ ]* 6.4 为客户端鉴权匹配编写属性测试
    - **Property 4: 客户端鉴权匹配**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 4: 对任意 Authorization/x-api-key 提交的密钥，当且仅当其等于 Secret_Store 中 PROXY_API_KEY 时鉴权通过，否则返回鉴权失败状态码`
    - **Validates: Requirements 6.6, 10.4**

- [x] 7. 实现 AWS 后端：DynamoDB 状态存储与刷新协调器
  - [x] 7.1 实现 `DynamoStateStore`（单表设计，原子更新）
    - 在 `kiro/backends/aws/state_store.py` 中使用 boto3/aioboto3 实现单表设计（PK/SK 布局见 design.md 数据模型）
    - 失败计数用 `UpdateItem` + `ADD failures :one`；统计用 `ADD`；重置用 `SET failures = :zero`；模型映射用 `ADD accounts`（String Set 幂等去重）
    - sticky 索引读写；账号状态 `GetItem`；TTL 属性 `expires_at`
    - _Requirements: 7.1, 7.2, 7.3, 7.5, 7.6, 3.5_

  - [ ]* 7.2 为失败计数原子性编写属性测试
    - **Property 9: 失败计数原子性（无丢失更新）**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 9: 对任意初始失败计数与任意 N 次并发 increment_failure 调用，最终 failures 值等于初始值加 N`
    - 使用 moto/LocalStack 模拟 DynamoDB，并发触发（`max_examples>=100`）
    - **Validates: Requirements 7.6**

  - [ ]* 7.3 为共享状态跨实例可见编写属性测试
    - **Property 7: 共享状态跨实例可见**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 7: 对任意账号与任意失败/成功事件序列，一个实例写入后另一实例在缓存过期后读取应观察到一致状态`
    - 使用 moto/LocalStack 模拟 DynamoDB（`max_examples>=100`）
    - **Validates: Requirements 7.3, 7.5**

  - [x] 7.4 实现 `DynamoRefreshCoordinator`（条件写租约锁，单飞刷新）
    - 在 `kiro/backends/aws/coordinator.py` 中以 `PK=LOCK#<account_id>` 条件写（`attribute_not_exists(lock_owner) OR lock_expires_at < :now`）实现短租约锁
    - 实现 `acquire_lock` / `release_lock` / `store_refreshed_token`（写 Secrets Manager + DynamoDB 元数据）/ `wait_for_token`（有界退避轮询），超时后允许降级自刷新
    - _Requirements: 6.7, 7.7_

  - [ ]* 7.5 为令牌刷新单飞编写属性测试
    - **Property 6: 令牌刷新单飞（无重复刷新）**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 6: 对任意账号与任意数量并发刷新同一账号的实例集合，在一次刷新窗口内对上游刷新端点的实际刷新调用次数恰为 1`
    - 使用 moto 模拟 DynamoDB 锁（`max_examples>=100`）
    - **Validates: Requirements 6.7, 7.7**

  - [x] 7.6 实现 `S3DebugLogSink`
    - 在 `kiro/backends/aws/debug_sink.py` 中归档到 `s3://<bucket>/debug/<yyyy>/<mm>/<dd>/<request_id>.json`
    - 应用主日志走 loguru → stdout（由 CloudWatch 采集）
    - 在工厂中注册 `aws` 后端，组装上述全部 AWS 实现
    - _Requirements: 8.1, 8.2, 12.4_

- [x] 8. 检查点 - 确保 AWS 后端单元/属性测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 9. 实现本地 TTL 缓存层（读延迟优化）
  - [x] 9.1 实现进程内 `StateCache` 并接入状态/配置/密钥读路径
    - 在 `kiro/backends/cache_layer.py` 中实现带 TTL（默认 1–2s，可配）的读缓存，覆盖账号状态、sticky 索引、配置与密钥读取
    - 写穿（write-through）更新缓存保证本实例读自洽；缓存命中免外呼以降低计费成本
    - 将非流式请求读取共享状态/配置/密钥的额外延迟控制在 P95 ≤ 50ms
    - _Requirements: 11.3, 11.5, 12.2_

  - [ ]* 9.2 为状态机行为等价编写属性测试
    - **Property 10: 熔断器与故障转移状态机行为等价**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 10: 对任意由成功/可恢复失败/致命失败/INVALID_MODEL_ID 组成的事件序列，以单机内存 AccountManager 为参考模型驱动 DynamoDB 实现后，二者每一步可见状态等价`
    - 使用 moto 模拟 DynamoDB，以内存实现为参考模型（`max_examples>=100`）
    - **Validates: Requirements 7.7**

- [x] 10. 实现优雅停机与请求关联中间件
  - [x] 10.1 实现 SIGTERM 优雅停机（请求 draining）
    - 在 `main.py` / 应用生命周期中注册 SIGTERM 处理：停止接收新请求，等待在途请求（含 SSE）完成或至停止超时
    - _Requirements: 4.5_

  - [x] 10.2 实现 `request_id` 关联中间件与结构化 stdout 日志（含脱敏）
    - 新增入站中间件生成 `request_id` 并贯穿调用链与调试归档
    - 所有日志经 `SecretProvider.redact` 脱敏后输出 stdout
    - _Requirements: 8.1, 8.7, 6.5_

  - [ ]* 10.3 为 SSE 流式透传不变量编写属性测试
    - **Property 12: SSE 流式透传不变量**
    - 注释标注：`# Feature: aws-cloud-native-gateway, Property 12: 对任意上游数据块序列，网关转发的 SSE 帧序列在内容与顺序上与上游一致，收到首块即转发，并以一致的结束标记（如 [DONE]）收尾`
    - 注入式模拟上游流（`max_examples>=100`）
    - **Validates: Requirements 10.2, 11.2**

- [ ] 11. 编写集成测试（双后端等价、优雅停机、性能基准）
  - [ ]* 11.1 编写 LocalStack/moto 端到端集成测试
    - 以 AWS 后端启动应用，验证“配置加载→账号选择→（模拟）上游→SSE 回流→状态写回”全链路
    - _Requirements: 1.4, 5.5, 7.3_

  - [ ]* 11.2 编写双后端等价集成测试
    - 同一批请求分别在 `local` 与 `aws` 后端运行，比较响应等价（向后兼容）
    - _Requirements: 5.5, 10.1, 10.2, 10.5_

  - [ ]* 11.3 编写优雅停机集成测试
    - 发送 SIGTERM 验证在途请求完成后才退出
    - _Requirements: 4.5_

  - [ ]* 11.4 编写性能基准测试
    - 在带 TTL 缓存下测量非流式请求读取共享状态/配置/密钥的 P95 额外延迟 ≤ 50ms
    - _Requirements: 11.3_

- [x] 12. 检查点 - 确保应用层全部测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 13. 搭建 AWS CDK（TypeScript）IaC 项目骨架
  - 在 `infra/` 下创建 CDK 项目：`bin/app.ts`（读取参数：region / min / max / 实例规格）、`cdk.json`、`package.json`、`tsconfig.json`
  - 配置参数化输入（实例 CPU/内存规格、最小/最大实例数、AWS 区域）
  - _Requirements: 9.1, 9.3_

- [x] 14. 实现网络与数据 Stack
  - [x] 14.1 实现 `network-stack.ts`
    - 定义 VPC、跨 2+ 可用区的公有/私有子网、NAT Gateway、可选 VPC Endpoints
    - _Requirements: 3.1, 3.2_

  - [x] 14.2 实现 `data-stack.ts`
    - 定义 DynamoDB 单表（按需计费 PAY_PER_REQUEST、TTL、KMS 加密）、Secrets Manager（KMS）、SSM 参数、S3 桶（KMS + 生命周期规则）
    - _Requirements: 3.5, 6.3, 12.3, 12.4_

  - [ ]* 14.3 为网络与数据 Stack 编写 CDK 快照与合规测试
    - `cdk synth` 快照断言；合规断言 DynamoDB/S3/Secrets 启用 KMS 静态加密、DynamoDB 按需计费、S3 调试日志保留期
    - _Requirements: 6.3, 12.3, 12.4_

- [x] 15. 实现计算、自动伸缩与可观测性 Stack
  - [x] 15.1 实现 `compute-stack.ts`
    - 定义 ECR、ECS Cluster、Fargate Service/TaskDef（≥2 实例、跨 2+ AZ）、ALB（HTTPS/SSE 透传）、目标组、ACM 证书、可选 WAF
    - 配置 ALB 空闲超时 ≥ `STREAMING_READ_TIMEOUT`；目标组 deregistration delay（draining）；`GET /health` 健康检查
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.3, 3.4_

  - [x] 15.2 实现 `autoscaling-construct.ts`
    - 目标跟踪策略：CPU 持续 3 分钟 >70% 扩容、持续 10 分钟 <30% 缩容；维持 min/max 之间；可选请求并发指标
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 12.1, 11.4_

  - [x] 15.3 实现 `observability-stack.ts` 与 `iam.ts`
    - CloudWatch 日志组/指标/告警（5xx>5% 持续 5 分钟、无健康目标）/仪表板（请求量、错误率、延迟、实例数）
    - 最小权限任务角色（特定 DynamoDB 表 / Secrets / SSM 路径 / S3 前缀 / CloudWatch）+ 执行角色
    - _Requirements: 8.3, 8.4, 8.5, 8.6, 6.4, 9.6_

  - [x] 15.4 在 `bin/app.ts` 中装配全部 Stack 并输出 ALB 入口
    - 串联 network/data/compute/observability，部署成功后输出 Load_Balancer 访问入口地址；支持 `cdk deploy --all` 与 `cdk destroy --all`
    - _Requirements: 9.2, 9.4, 9.5_

  - [ ]* 15.5 为计算/伸缩/可观测性 Stack 编写 CDK 快照与合规测试
    - 合规断言：ALB 绑定 ACM TLS、ALB 空闲超时 ≥ 流式读取超时、IAM 最小权限、5xx 与无健康目标告警阈值、最小/最大实例配置
    - _Requirements: 2.6, 6.4, 8.4, 8.5, 9.6_

- [x] 16. 检查点 - 确保 IaC 快照与合规测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 17. 扩展 CI/CD 工作流（构建镜像 → 推送 ECR → cdk deploy）
  - 在现有 `.github/workflows/docker.yml` 基础上扩展：复用现有 `Dockerfile` 构建镜像 → 推送 Amazon ECR → 触发 `cdk deploy`
  - CDK 引用该 ECR 镜像
  - _Requirements: 9.1, 9.2_

- [x] 18. 编写中文文档交付物（对齐 AWS guidance 项目格式）
  - [x] 18.1 编写 `docs/zh/CLOUD_NATIVE_README.md`
    - 方案概述、前置条件、部署步骤、配置说明、卸载步骤
    - _Requirements: 13.1, 13.3, 13.4_

  - [x] 18.2 编写 `docs/zh/ARCHITECTURE_AWS.md`
    - 含 Mermaid 架构图，展示 Load_Balancer、Gateway_Service、Auto_Scaler、State_Store、Secret_Store、Object_Store、Observability_Service 关系
    - _Requirements: 13.2_

  - [x] 18.3 编写 `docs/zh/DEPLOYMENT_GUIDE.md`
    - 一键部署命令与参数说明、销毁命令
    - _Requirements: 13.4_

  - [x] 18.4 编写 `docs/zh/MIGRATION_GUIDE.md`
    - 本地文件 → SSM / Secrets Manager / S3 / DynamoDB 迁移步骤
    - _Requirements: 13.5_

  - [x] 18.5 编写 `docs/zh/COST_ESTIMATION.md`
    - 预估成本与主要成本因素（Fargate / NAT / ALB / DynamoDB 等）
    - _Requirements: 13.6_

- [x] 19. 最终检查点 - 确保全部测试通过
  - 确保所有测试通过，如有疑问请询问用户。

---

## 备注（Notes）

- 标注 `*` 的子任务为可选任务，可为加速 MVP 而跳过。
- 每个任务通过 `_Requirements: X.Y_` 引用具体验收标准以保证可追溯性。
- 检查点用于增量验证。
- 属性化测试验证 design.md 中定义的 12 条普遍正确性属性，使用 Hypothesis（`max_examples>=100`），涉及 AWS 依赖的属性（5、6、7、9、10）使用 moto/LocalStack 模拟；每条属性对应唯一一个属性化测试。
- 单元测试验证具体示例与边界条件；集成测试验证双后端等价、优雅停机与性能基准。
- 基础设施类（IaC、告警、KMS 加密、S3 生命周期）不适用 PBT，改用 CDK 快照测试与合规断言。
- 路由层、转换层、流式层、解析层保持不变，仅替换存储/状态/凭证/配置/日志 I/O 边界。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "2.4", "2.6", "2.7"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.5"] },
    { "id": 3, "tasks": ["4.1", "4.3", "4.5", "4.6"] },
    { "id": 4, "tasks": ["4.2", "4.4", "4.7"] },
    { "id": 5, "tasks": ["6.1", "6.2", "7.1", "7.4", "7.6"] },
    { "id": 6, "tasks": ["6.3", "6.4", "7.2", "7.3", "7.5"] },
    { "id": 7, "tasks": ["9.1", "10.1", "10.2"] },
    { "id": 8, "tasks": ["9.2", "10.3"] },
    { "id": 9, "tasks": ["11.1", "11.2", "11.3", "11.4"] },
    { "id": 10, "tasks": ["13"] },
    { "id": 11, "tasks": ["14.1", "14.2"] },
    { "id": 12, "tasks": ["14.3", "15.1", "15.2", "15.3"] },
    { "id": 13, "tasks": ["15.4"] },
    { "id": 14, "tasks": ["15.5", "17"] },
    { "id": 15, "tasks": ["18.1", "18.2", "18.3", "18.4", "18.5"] }
  ]
}
```
