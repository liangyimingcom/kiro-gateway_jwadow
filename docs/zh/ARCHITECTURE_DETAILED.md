# Kiro Gateway 全面技术解析

> 版本：v2.4.dev.13 | 最后更新：2026-05-18 | 作者：[@Jwadow](https://github.com/jwadow)

---

## 目录

1. [项目定位与需求分析](#1-项目定位与需求分析)
2. [整体架构设计](#2-整体架构设计)
3. [请求生命周期](#3-请求生命周期)
4. [核心模块详解](#4-核心模块详解)
5. [关键流程深度解析](#5-关键流程深度解析)
6. [配置与部署](#6-配置与部署)
7. [测试体系](#7-测试体系)
8. [版本演进历程](#8-版本演进历程)

---

## 1. 项目定位与需求分析

### 1.1 项目是什么

**Kiro Gateway** 是一个 Python FastAPI 反向代理网关，将 Kiro API（Amazon Q Developer / AWS CodeWhisperer）的私有协议封装为业界标准的 **OpenAI** 和 **Anthropic** 兼容 API。

### 1.2 解决什么问题

| 痛点 | 解决方案 |
|------|----------|
| Kiro API 使用 AWS 私有协议，无法被主流工具直接调用 | 网关提供标准 OpenAI/Anthropic 端点 |
| Token 认证复杂（多种来源、自动刷新） | 统一认证管理器自动处理 |
| 单账号配额/限流 | 多账号自动切换（Circuit Breaker） |
| 流式响应格式不兼容 | AWS SSE → 标准 SSE 转换 |
| 模型名称不统一 | 智能模型名解析管道 |

### 1.3 目标用户

使用 **Cursor、Cline、Claude Code、Continue、LangChain、OpenAI SDK** 等工具的开发者，希望通过 Kiro 免费/付费额度使用 Claude 模型。

### 1.4 支持的 API 协议

| 协议 | 端点 | 认证方式 |
|------|------|----------|
| OpenAI | `/v1/models`, `/v1/chat/completions` | `Authorization: Bearer {key}` |
| Anthropic | `/v1/messages`, `/v1/messages/count_tokens` | `x-api-key: {key}` |



---

## 2. 整体架构设计

### 2.1 分层架构总览

```mermaid
graph TB
    subgraph 客户端层
        C1[OpenAI SDK / Cursor / Cline]
        C2[Anthropic SDK / Claude Code]
    end

    subgraph API路由层
        R1[routes_openai.py]
        R2[routes_anthropic.py]
    end

    subgraph 转换层
        CV1[converters_openai.py]
        CV2[converters_anthropic.py]
        CVC[converters_core.py]
    end

    subgraph 核心服务层
        AUTH[auth.py - 认证管理]
        AM[account_manager.py - 多账号]
        HC[http_client.py - HTTP重试]
        MR[model_resolver.py - 模型解析]
        CACHE[cache.py - 模型缓存]
    end

    subgraph 流式输出层
        SC[streaming_core.py - Kiro流解析]
        SO[streaming_openai.py - OpenAI SSE]
        SA[streaming_anthropic.py - Anthropic SSE]
    end

    subgraph 解析与工具层
        P[parsers.py - AWS事件流]
        TP[thinking_parser.py - 思考链FSM]
        MCP[mcp_tools.py - Web搜索]
        TK[tokenizer.py - Token计数]
    end

    subgraph 错误与恢复层
        NE[network_errors.py]
        KE[kiro_errors.py]
        AE[account_errors.py]
        TR[truncation_recovery.py]
        TS[truncation_state.py]
    end

    subgraph 辅助层
        DL[debug_logger.py]
        DM[debug_middleware.py]
        PG[payload_guards.py]
        UT[utils.py]
        CFG[config.py]
    end

    C1 --> R1
    C2 --> R2
    R1 --> CV1
    R2 --> CV2
    CV1 --> CVC
    CV2 --> CVC
    CVC --> HC
    HC --> AUTH
    HC --> AM
    AM --> AUTH
    MR --> CACHE
    R1 --> MR
    R2 --> MR
    HC -->|AWS SSE| SC
    SC --> SO
    SC --> SA
    SO --> C1
    SA --> C2
    SC --> P
    SC --> TP
```

### 2.2 设计原则

| 原则 | 说明 |
|------|------|
| **适配器模式** | 通过薄适配器层将 OpenAI/Anthropic 协议适配到统一的 Kiro 载荷 |
| **共享核心** | `converters_core.py` + `streaming_core.py` 包含所有共用逻辑，避免重复 |
| **最小干预** | 只修复 API 怪癖，不改变用户原始意图 |
| **系统优于补丁** | 遇到问题建系统（如 truncation_recovery），不打临时补丁 |



---

## 3. 请求生命周期

### 3.1 OpenAI 完整请求流程

```mermaid
sequenceDiagram
    participant Client as OpenAI客户端
    participant Route as routes_openai
    participant Conv as converters_openai
    participant Core as converters_core
    participant PG as payload_guards
    participant AM as AccountManager
    participant Auth as KiroAuthManager
    participant HTTP as KiroHttpClient
    participant Kiro as Kiro API
    participant SC as streaming_core
    participant SO as streaming_openai
    participant TP as thinking_parser

    Client->>Route: POST /v1/chat/completions
    Route->>Route: verify_api_key (Bearer token)
    Route->>Conv: build_kiro_payload(request_data)
    Conv->>Core: 统一消息格式 + 构建Kiro载荷
    Core->>Core: merge_adjacent_messages()
    Core->>Core: process_tools_with_long_descriptions()
    Core->>Core: build_kiro_history()
    Core-->>PG: check_payload_size / trim_payload_to_limit
    Route->>AM: get_next_account(model)
    AM->>Auth: get_access_token()
    Auth->>Auth: is_token_expiring_soon? → refresh
    Route->>HTTP: POST /generateAssistantResponse
    HTTP->>Kiro: 发送请求 (含重试逻辑)
    Kiro-->>SC: AWS SSE 事件流
    SC->>SC: parse_kiro_stream() → KiroEvent[]
    SC->>TP: 提取thinking块 (如启用)
    SC->>SO: format为OpenAI chunk
    SO-->>Client: data: {...}\ndata: [DONE]
```

### 3.2 Anthropic 请求流程

```mermaid
sequenceDiagram
    participant Client as Anthropic客户端
    participant Route as routes_anthropic
    participant Conv as converters_anthropic
    participant Core as converters_core
    participant AM as AccountManager
    participant Auth as KiroAuthManager
    participant HTTP as KiroHttpClient
    participant Kiro as Kiro API
    participant SC as streaming_core
    participant SA as streaming_anthropic

    Client->>Route: POST /v1/messages
    Route->>Route: verify_anthropic_api_key (x-api-key)
    Route->>Conv: 转换 Anthropic 请求
    Conv->>Core: build_kiro_payload()
    Route->>AM: get_next_account(model)
    AM->>Auth: get_access_token()
    Route->>HTTP: POST /generateAssistantResponse
    Kiro-->>SC: AWS SSE 事件流
    SC->>SA: format为Anthropic事件
    SA-->>Client: event: content_block_delta\ndata: {...}
```

### 3.3 非流式（Non-Streaming）模式

非流式请求仍然内部使用流式连接 Kiro API，但在网关层通过 `collect_stream_response()` / `collect_anthropic_response()` 将所有 chunk 收集后组装为完整 JSON 一次性返回。



---

## 4. 核心模块详解

### 4.1 入口与生命周期 — `main.py`

| 职责 | 说明 |
|------|------|
| 日志配置 | Loguru 彩色输出 + 拦截 uvicorn/FastAPI 标准日志 |
| 配置验证 | `validate_configuration()` 检查凭证存在性 |
| VPN/代理 | 启动前设置 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量 |
| Lifespan | 创建共享 httpx 连接池、AccountManager、初始化账号 |
| 中间件 | CORS → DebugLoggerMiddleware |
| 路由注册 | `openai_router` + `anthropic_router` |
| CLI 入口 | argparse 解析 `--host`/`--port`，优先级：CLI > ENV > 默认 |

### 4.2 配置中心 — `kiro/config.py`

集中管理所有常量与环境变量，关键参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_API_KEY` | `changeme_proxy_secret` | 代理鉴权密钥 |
| `REGION` | `us-east-1` | AWS 区域 |
| `TOKEN_REFRESH_THRESHOLD` | 600s | 提前刷新Token阈值 |
| `MAX_RETRIES` | 3 | HTTP重试次数 |
| `MODEL_CACHE_TTL` | 3600s | 模型缓存过期 |
| `STREAMING_READ_TIMEOUT` | 300s | 流式读超时 |
| `DEBUG_MODE` | `off` | 调试日志模式 |
| `APP_VERSION` | `2.4.dev.13` | 当前版本号 |
| `HIDDEN_MODELS` | [...] | 隐藏/未文档化模型列表 |
| `MODEL_ALIASES` | {...} | 模型别名映射 |
| `ACCOUNT_SYSTEM` | `false` | 是否启用多账号系统 |

动态URL生成函数：
- `get_kiro_refresh_url(region)` → Token刷新端点
- `get_kiro_api_host(region)` → 主API `codewhisperer.{region}.amazonaws.com`
- `get_kiro_q_host(region)` → Q API `q.{region}.amazonaws.com`

### 4.3 认证管理 — `kiro/auth.py`

```mermaid
stateDiagram-v2
    [*] --> 检测认证类型
    检测认证类型 --> KiroDesktop: 无clientId/clientSecret
    检测认证类型 --> AWS_SSO_OIDC: 有clientId/clientSecret
    
    KiroDesktop --> Token有效: refreshToken
    AWS_SSO_OIDC --> Token有效: OIDC刷新
    
    Token有效 --> 即将过期: expiry < 10min
    即将过期 --> 刷新中: asyncio.Lock保护
    刷新中 --> Token有效: 刷新成功
    刷新中 --> 强制刷新: 403触发force_refresh
    强制刷新 --> Token有效
```

**类：`KiroAuthManager`**

| 方法 | 说明 |
|------|------|
| `get_access_token()` | 返回有效token，必要时自动刷新 |
| `force_refresh()` | 强制刷新（被403触发） |
| `is_token_expiring_soon()` | 检查是否即将过期 |

**支持的凭证来源（4种）：**
1. JSON 文件（Kiro IDE）
2. 环境变量（REFRESH_TOKEN）
3. SQLite 数据库（kiro-cli）
4. AWS SSO OIDC（Builder ID / Enterprise）

### 4.4 多账号管理 — `kiro/account_manager.py`

```mermaid
flowchart TD
    REQ[收到请求] --> GET[get_next_account]
    GET --> STICKY{当前账号健康?}
    STICKY -->|是| USE[使用当前账号]
    STICKY -->|否| NEXT[尝试下一个账号]
    NEXT --> INIT{已初始化?}
    INIT -->|否| LAZY[懒初始化 _initialize_account]
    INIT -->|是| CHECK{在冷却期?}
    CHECK -->|是| SKIP[跳过,下一个]
    CHECK -->|否| USE
    
    USE --> RESULT{请求结果}
    RESULT -->|成功| REPORT_OK[report_success]
    RESULT -->|失败| CLASSIFY[classify_error]
    CLASSIFY -->|RECOVERABLE| REPORT_FAIL[report_failure + 尝试下一个]
    CLASSIFY -->|FATAL| RETURN_ERR[直接返回错误给客户端]
    
    REPORT_FAIL --> COOLDOWN[进入冷却期]
```

**核心类与函数：**

| 类/函数 | 说明 |
|---------|------|
| `AccountManager` | 管理多账号生命周期、切换、状态持久化 |
| `Account` | 单个账号封装（auth_manager + stats） |
| `AccountStats` | 成功/失败计数、冷却时间 |
| `get_next_account(model, exclude)` | 获取下一个可用账号 |
| `report_success/report_failure` | 上报结果更新统计 |
| `_initialize_account` | 懒初始化（加载凭证+获取模型列表） |
| `save_state_periodically` | 后台定时保存状态到 state.json |

### 4.5 错误分类 — `kiro/account_errors.py`

```mermaid
flowchart LR
    ERR[Kiro API 错误] --> CODE{HTTP状态码}
    CODE -->|402| REC[RECOVERABLE - 配额耗尽]
    CODE -->|403| REC2[RECOVERABLE - Token失效]
    CODE -->|429| REC3[RECOVERABLE - 限流]
    CODE -->|400+INVALID_MODEL| REC4[RECOVERABLE - 订阅不足]
    CODE -->|400+CONTENT_LENGTH| FAT[FATAL - 上下文溢出]
    CODE -->|400+其他| FAT2[FATAL - 请求格式错误]
    CODE -->|5xx| FAT3[FATAL - 服务器错误]
```

- **RECOVERABLE**：尝试下一个账号
- **FATAL**：立即返回错误给客户端（所有账号都会失败）



### 4.6 HTTP 客户端 — `kiro/http_client.py`

**类：`KiroHttpClient`** — 带指数退避的自动重试 HTTP 客户端。

| 错误码 | 处理策略 |
|--------|----------|
| 403 | 调用 `force_refresh()` 刷新 Token 后重试 |
| 429 | 指数退避：1s → 2s → 4s |
| 5xx | 指数退避，最多 MAX_RETRIES 次 |
| 超时 | 指数退避 |

关键设计：**流式请求使用 per-request 客户端**（防止 CLOSE_WAIT 泄漏），非流式请求共享连接池。

### 4.7 模型解析 — `kiro/model_resolver.py`

```mermaid
flowchart TD
    INPUT[客户端模型名] --> NORM[normalize_model_name]
    NORM -->|"claude-haiku-4-5-20251001 → claude-haiku-4.5"| ALIAS{检查别名}
    ALIAS -->|命中| RESOLVE[返回别名目标]
    ALIAS -->|未命中| CACHE{动态缓存查找}
    CACHE -->|命中| RESOLVE2[返回缓存模型]
    CACHE -->|未命中| HIDDEN{隐藏模型列表}
    HIDDEN -->|命中| RESOLVE3[返回隐藏模型ID]
    HIDDEN -->|未命中| PASS[透传给Kiro API决定]
```

**4层解析管道：**
1. **名称标准化**：`claude-haiku-4-5` → `claude-haiku-4.5`（破折号转点号、去日期后缀）
2. **别名检查**：`auto` → `claude-sonnet-4.5`
3. **动态缓存**：从 `/ListAvailableModels` API 获取的模型列表
4. **透传**：未知模型直接发给 Kiro（网关是网关不是门卫）

### 4.8 转换器 — `kiro/converters_*.py`

#### 4.8.1 共享核心 `converters_core.py`

统一数据结构：

| 类 | 说明 |
|----|------|
| `UnifiedMessage` | 统一消息格式（role, content, tool_calls, tool_results, images） |
| `UnifiedTool` | 统一工具格式（name, description, parameters） |
| `ThinkingConfig` | 扩展思考配置 |
| `KiroPayloadResult` | 构建结果（payload + metadata） |

核心函数流水线：

```mermaid
flowchart TD
    MSG[原始消息列表] --> NORM_ROLE[normalize_message_roles]
    NORM_ROLE --> ENSURE_USER[ensure_first_message_is_user]
    ENSURE_USER --> MERGE[merge_adjacent_messages]
    MERGE --> ALT[ensure_alternating_roles]
    ALT --> STRIP[strip_all_tool_content - 如模型不支持工具]
    STRIP --> HISTORY[build_kiro_history]
    
    TOOLS[工具定义] --> PROC_TOOLS[process_tools_with_long_descriptions]
    PROC_TOOLS --> SANITIZE[sanitize_json_schema]
    PROC_TOOLS --> VALIDATE[validate_tool_names]
    PROC_TOOLS --> CONVERT_T[convert_tools_to_kiro_format]
    
    HISTORY --> PAYLOAD[build_kiro_payload]
    CONVERT_T --> PAYLOAD
    PAYLOAD --> GUARDS[payload_guards - 大小检查/裁剪]
```

#### 4.8.2 OpenAI 适配器 `converters_openai.py`

- 从 `messages` 中提取 `system` 角色消息作为系统提示词
- 将 OpenAI 格式的 `tool_calls` / `tool_call_id` 转为 `UnifiedMessage`
- 处理 `content` 为字符串或数组两种形式

#### 4.8.3 Anthropic 适配器 `converters_anthropic.py`

- `system` 字段已独立于 `messages` 之外（Anthropic 标准）
- 将 `content` 块数组（text/image/tool_use/tool_result）转为统一格式

### 4.9 流式处理 — `kiro/streaming_*.py`

#### 4.9.1 核心解析 `streaming_core.py`

| 类/函数 | 说明 |
|---------|------|
| `KiroEvent` | Kiro 事件（type: content/tool_start/tool_input/tool_stop/usage） |
| `StreamResult` | 完整流结果（text, tool_calls, usage, thinking） |
| `parse_kiro_stream()` | 异步生成器，逐 chunk 解析 AWS SSE |
| `collect_stream_to_result()` | 收集全部事件为 StreamResult |
| `stream_with_first_token_retry()` | 首Token超时重试 |
| `calculate_tokens_from_context_usage()` | 从百分比计算token数 |

#### 4.9.2 OpenAI 格式化 `streaming_openai.py`

- `stream_kiro_to_openai_internal()` → 逐 chunk 生成 `data: {...}\n\n`
- `stream_kiro_to_openai()` → 带重试的外层包装
- `collect_stream_response()` → 非流式：收集后返回完整 JSON

#### 4.9.3 Anthropic 格式化 `streaming_anthropic.py`

- `stream_kiro_to_anthropic()` → 生成 `event: type\ndata: {...}\n\n`
- `collect_anthropic_response()` → 非流式收集
- 支持 `thinking` 块输出（Anthropic `content_block` 格式）



### 4.10 AWS 事件流解析 — `kiro/parsers.py`

**类：`AwsEventStreamParser`**

Kiro API 返回的 SSE 格式是 AWS 私有事件流，该解析器处理：
- **大括号计数**：正确解析嵌套 JSON
- **内容去重**：过滤重复事件
- **工具调用**：结构化 + 方括号格式 `[Called func with args: {...}]`
- **转义序列**：解码 `\n`、`\"` 等

辅助函数：
- `find_matching_brace()` — 找到配对右花括号
- `parse_bracket_tool_calls()` — 解析旧格式工具调用
- `deduplicate_tool_calls()` — 去重

### 4.11 思考链解析 — `kiro/thinking_parser.py`

```mermaid
stateDiagram-v2
    [*] --> NORMAL
    NORMAL --> IN_OPENING_TAG: 检测到 "<thinking"
    IN_OPENING_TAG --> IN_THINKING: 检测到 ">"
    IN_THINKING --> IN_CLOSING_TAG: 检测到 "</thinking"
    IN_CLOSING_TAG --> NORMAL: 检测到 ">"
    IN_THINKING --> IN_THINKING: 累积思考内容
```

基于有限状态机（FSM）从流式文本中实时提取 `<thinking>...</thinking>` 块，将模型"推理过程"与最终输出分离。

**类：`ThinkingParser`** | **状态枚举：`ParserState`** | **结果：`ThinkingParseResult`**

### 4.12 Payload 守卫 — `kiro/payload_guards.py`

防止请求因过大被 Kiro API 拒绝：

| 函数 | 说明 |
|------|------|
| `check_payload_size()` | 计算 payload JSON 字节数 |
| `trim_payload_to_limit()` | 从历史开头逐条删除直到小于限制 |
| `_strip_empty_tool_uses()` | 清理空的工具调用块 |
| `_align_to_user_message()` | 裁剪后确保历史以 user 消息开头 |
| `_repair_orphaned_tool_results()` | 修复孤立的工具结果 |

### 4.13 截断恢复 — `kiro/truncation_recovery.py` + `truncation_state.py`

当 payload 过大被裁剪时，通过系统提示词告知模型"上下文被截断"，并注入恢复提示。

`truncation_state.py` 维护截断缓存（工具调用截断 / 内容截断），供后续请求查询恢复信息。

### 4.14 MCP 工具桥接 — `kiro/mcp_tools.py`

将 Kiro 原生的 Web Search 能力以标准 tool_use 形式暴露：

| 函数 | 说明 |
|------|------|
| `call_kiro_mcp_api()` | 调用 Kiro MCP 后端执行搜索 |
| `generate_search_summary()` | 格式化搜索结果为可读摘要 |
| `generate_openai_web_search_sse()` | 生成 OpenAI 格式搜索SSE |
| `generate_anthropic_web_search_sse()` | 生成 Anthropic 格式搜索SSE |
| `handle_native_web_search()` | 处理原生搜索工具调用 |
| `extract_query_from_messages()` | 从消息中提取搜索查询 |

### 4.15 Token 计数 — `kiro/tokenizer.py`

基于 `tiktoken`（OpenAI Rust 库）+ Claude 修正系数（1.15）进行 token 估算：

```
total_tokens = context_usage_percentage × max_input_tokens  (Kiro API返回)
completion_tokens = tiktoken(response_text)                  (本地计算)
prompt_tokens = total_tokens - completion_tokens             (差值)
```

精度约 97-99.7%。

### 4.16 模型缓存 — `kiro/cache.py`

**类：`ModelInfoCache`** — 线程安全的模型配置存储。

- 懒加载：首次请求时从 `/ListAvailableModels` 获取
- TTL：1小时自动过期
- 回退：缓存失效时使用静态 FALLBACK_MODELS

### 4.17 网络错误分类 — `kiro/network_errors.py`

将底层网络异常翻译为用户友好消息：

| 异常 | 分类 | 用户消息 |
|------|------|----------|
| `ConnectTimeout` | TIMEOUT | "连接超时" |
| `ReadTimeout` | TIMEOUT | "服务器响应超时" |
| DNS 错误 | DNS | "DNS解析失败" |
| SSL 错误 | SSL | "SSL证书问题" |
| 代理错误 | PROXY | "代理连接失败" |

### 4.18 Kiro 错误增强 — `kiro/kiro_errors.py`

`enhance_kiro_error()` 将 Kiro API 返回的模糊错误（如 "Improperly formed request"）增强为带建议的结构化信息。

### 4.19 调试系统 — `kiro/debug_logger.py` + `debug_middleware.py`

三种模式：`off` / `errors` / `all`

DebugMiddleware 在请求进入时初始化日志上下文，DebugLogger 单例在各阶段记录：
- `request_body.json` — 客户端原始请求
- `kiro_request_body.json` — 发给 Kiro 的请求
- `response_stream_raw.txt` — Kiro 原始流
- `response_stream_modified.txt` — 转换后的流

### 4.20 工具函数 — `kiro/utils.py`

| 函数 | 说明 |
|------|------|
| `get_machine_fingerprint()` | SHA256(hostname-username-kiro-gateway) |
| `get_kiro_headers()` | 构建 Kiro API 请求头 |
| `generate_completion_id()` | `chatcmpl-{uuid}` |
| `generate_conversation_id()` | 基于消息内容的确定性 UUID |
| `generate_tool_call_id()` | `call_{uuid[:8]}` |



---

## 5. 关键流程深度解析

### 5.1 多账号故障切换（Circuit Breaker）

```mermaid
flowchart TD
    START[请求到达] --> GET_ACC[AccountManager.get_next_account]
    GET_ACC --> STICKY{Sticky账号可用?}
    STICKY -->|健康+支持该模型| USE[使用Sticky账号]
    STICKY -->|不可用| ROUND[轮询其他账号]
    
    ROUND --> LAZY{已初始化?}
    LAZY -->|否| INIT[懒初始化: 加载凭证+获取模型列表]
    LAZY -->|是| COOL{冷却期内?}
    COOL -->|是| NEXT[跳过, 尝试下一个]
    COOL -->|否| USE2[使用该账号]
    
    USE --> SEND[发送请求]
    USE2 --> SEND
    SEND --> RESULT{结果}
    
    RESULT -->|成功| SUCCESS[report_success: 重置失败计数, 设为Sticky]
    RESULT -->|失败| CLASSIFY[account_errors.classify_error]
    CLASSIFY -->|FATAL| CLIENT_ERR[返回错误给客户端]
    CLASSIFY -->|RECOVERABLE| FAIL[report_failure: 失败计数+1]
    FAIL --> EXCEED{连续失败超限?}
    EXCEED -->|是| COOLDOWN[进入冷却期 - 指数退避]
    EXCEED -->|否| RETRY[排除当前账号, 重新get_next_account]
```

**状态持久化**：`state.json` 记录当前账号索引 + 各账号统计，后台每10s保存一次。

### 5.2 Token 刷新与认证流程

```mermaid
flowchart TD
    REQ[需要Token] --> CHECK{Token存在且未过期?}
    CHECK -->|是| RETURN[返回currentToken]
    CHECK -->|否/即将过期| LOCK[获取asyncio.Lock]
    LOCK --> DOUBLE_CHECK{再次检查}
    DOUBLE_CHECK -->|已被其他协程刷新| RETURN
    DOUBLE_CHECK -->|需要刷新| DETECT{认证类型}
    DETECT -->|KiroDesktop| KIRO[POST /refreshToken]
    DETECT -->|AWS_SSO_OIDC| OIDC[POST oidc.{region}.amazonaws.com/token]
    KIRO --> SAVE[保存新Token + 更新expiresAt]
    OIDC --> SAVE
    SAVE --> PERSIST[写回JSON文件/SQLite]
    PERSIST --> RETURN
```

### 5.3 流式解析与思考链提取

```mermaid
flowchart LR
    STREAM[AWS SSE 原始流] --> PARSER[AwsEventStreamParser]
    PARSER --> EVENTS[KiroEvent序列]
    EVENTS --> THINKING{包含thinking标签?}
    THINKING -->|是| FSM[ThinkingParser FSM提取]
    FSM --> THINK_BLOCK[thinking内容]
    FSM --> CONTENT[文本内容]
    THINKING -->|否| CONTENT2[直接输出文本]
    
    CONTENT --> FORMAT{输出格式}
    THINK_BLOCK --> FORMAT
    FORMAT -->|OpenAI| OAI["data: {delta: {content: ...}}"]
    FORMAT -->|Anthropic| ANT["event: content_block_delta"]
```

### 5.4 Payload 大小管理

```mermaid
flowchart TD
    BUILD[构建完成的Kiro Payload] --> CHECK_SIZE[check_payload_size]
    CHECK_SIZE --> OVER{超过限制?}
    OVER -->|否| SEND[直接发送]
    OVER -->|是| TRIM[trim_payload_to_limit]
    TRIM --> STRIP[从历史开头逐条移除]
    STRIP --> FIX1[_align_to_user_message]
    FIX1 --> FIX2[_repair_orphaned_tool_results]
    FIX2 --> FIX3[_strip_empty_tool_uses]
    FIX3 --> INJECT[inject 截断恢复提示到system prompt]
    INJECT --> SEND
```

### 5.5 工具调用长描述处理

```mermaid
flowchart TD
    TOOLS[工具定义列表] --> LOOP[遍历每个工具]
    LOOP --> CHECK{description > 10000字符?}
    CHECK -->|否| KEEP[保持原样]
    CHECK -->|是| REF[description替换为引用标记]
    REF --> DOC[完整文档移到system prompt]
    KEEP --> FINAL[最终工具列表]
    DOC --> FINAL
```



---

## 6. 配置与部署

### 6.1 环境变量一览（`.env.example`）

```bash
# === 必须 ===
PROXY_API_KEY="my-super-secret-password-123"

# === 认证（四选一）===
KIRO_CREDS_FILE="~/.aws/sso/cache/kiro-auth-token.json"  # JSON文件
REFRESH_TOKEN="your_refresh_token"                         # 直接Token
KIRO_CLI_DB_FILE="~/.local/share/kiro-cli/data.sqlite3"  # SQLite
# 或 AWS SSO（自动检测）

# === 可选 ===
PROFILE_ARN="arn:aws:codewhisperer:us-east-1:..."
KIRO_REGION="us-east-1"
KIRO_API_REGION="us-east-1"
SERVER_HOST="0.0.0.0"
SERVER_PORT="8000"
VPN_PROXY_URL="http://127.0.0.1:7890"
DEBUG_MODE="off"
ACCOUNT_SYSTEM="false"
```

### 6.2 多账号配置（`credentials.json`）

```json
[
  {"type": "json", "path": "~/.aws/sso/cache/kiro-auth-token.json"},
  {"type": "sqlite", "path": "~/.local/share/kiro-cli/data.sqlite3"},
  {"type": "refresh_token", "refresh_token": "eyJ...", "profile_arn": "arn:..."}
]
```

### 6.3 Docker 部署

```mermaid
flowchart LR
    DEV[开发者] -->|docker-compose up -d| COMPOSE[docker-compose.yml]
    COMPOSE --> BUILD[Dockerfile: 单阶段构建]
    BUILD --> IMAGE[非root用户kiro运行]
    IMAGE --> HEALTH[/health 健康检查]
    IMAGE --> VOLUMES[挂载凭证+日志]
```

**Dockerfile 特点：**
- 单阶段优化构建
- 非 root 用户 `kiro` 运行
- 健康检查端点 `/health`
- 支持所有4种认证方式

**CI/CD：** `.github/workflows/docker.yml`
- 自动测试 → Docker构建 → 健康检查 → 推送 ghcr.io

### 6.4 启动流程

```mermaid
flowchart TD
    START[python main.py] --> PARSE[parse_cli_args]
    PARSE --> VALIDATE[validate_configuration]
    VALIDATE --> WARN[_warn_timeout_configuration]
    WARN --> RESOLVE[resolve_server_config: CLI>ENV>Default]
    RESOLVE --> BANNER[print_startup_banner]
    BANNER --> UVICORN[uvicorn.run]
    UVICORN --> LIFESPAN[lifespan context manager]
    LIFESPAN --> HTTP_POOL[创建httpx连接池]
    HTTP_POOL --> MIGRATE[.env → credentials.json 迁移]
    MIGRATE --> ACCOUNT_MGR[创建AccountManager]
    ACCOUNT_MGR --> INIT_ACC[初始化首个可用账号]
    INIT_ACC --> BG_SAVE[启动后台state保存任务]
    BG_SAVE --> READY[服务就绪 ✓]
```

---

## 7. 测试体系

### 7.1 测试结构

```
tests/
├── conftest.py                        # 全局fixture: 网络隔离 + Mock
├── unit/
│   ├── test_account_errors.py         # 错误分类
│   ├── test_account_manager.py        # 多账号管理
│   ├── test_auth_manager.py           # 认证管理
│   ├── test_cache.py                  # 模型缓存
│   ├── test_config.py                 # 配置加载
│   ├── test_converters_anthropic.py   # Anthropic转换
│   ├── test_converters_core.py        # 核心转换
│   ├── test_converters_openai.py      # OpenAI转换
│   ├── test_debug_logger.py           # 调试日志
│   ├── test_debug_middleware.py       # 调试中间件
│   ├── test_exceptions.py            # 异常处理
│   ├── test_http_client.py            # HTTP客户端
│   ├── test_kiro_errors.py            # 错误增强
│   ├── test_main_cli.py              # CLI入口
│   ├── test_main_lifespan.py         # 生命周期
│   ├── test_mcp_tools.py             # MCP工具
│   ├── test_model_resolver.py        # 模型解析
│   └── ... (更多)
├── integration/
│   ├── test_account_system_flow.py   # 账号系统集成
│   └── test_full_flow.py             # 完整请求流程集成
```

### 7.2 测试哲学

- **完全网络隔离**：`conftest.py` 中 `block_all_network_calls` fixture 阻止所有真实请求
- **Arrange-Act-Assert** 模式
- **覆盖边界**：不只测快乐路径，重点测试错误/边界/畸形输入
- **双API对称**：OpenAI + Anthropic、流式 + 非流式

### 7.3 运行测试

```bash
pytest                          # 全部测试
pytest tests/unit/ -v           # 仅单元测试
pytest tests/integration/ -v    # 仅集成测试
pytest --cov=kiro --cov-report=html  # 覆盖率报告
```



---

## 8. 版本演进历程

> 基于 commit 历史与代码结构推断的主要里程碑。当前版本 **v2.4.dev.13**。

### 8.1 版本时间线

```mermaid
timeline
    title Kiro Gateway 版本演进
    section v1.x 基础功能
        OpenAI 兼容 API : 基本的 /v1/chat/completions 端点
        Token 认证 : KiroAuthManager + refreshToken
        流式响应 : AWS SSE → OpenAI SSE 转换
        模型映射 : 静态模型名转换
    section v2.0-2.1 架构升级
        Anthropic API : /v1/messages 完整支持
        双协议对称 : 共享核心 converters_core + streaming_core
        多认证方式 : JSON + ENV + SQLite + AWS SSO OIDC
        思考链 : thinking_parser FSM 提取
    section v2.2-2.3 可靠性
        多账号系统 : AccountManager + Circuit Breaker
        错误分类体系 : account_errors + network_errors + kiro_errors
        截断恢复 : truncation_recovery + truncation_state
        Payload 守卫 : payload_guards 大小裁剪
    section v2.4 功能丰富
        MCP 工具 : web_search 桥接
        VPN/代理 : HTTP/SOCKS5 支持
        调试系统 : debug_logger + debug_middleware
        Docker CI/CD : GitHub Actions + ghcr.io
        动态模型解析 : ModelResolver 4层管道
        Token 计数 : tiktoken + Claude修正系数
```

### 8.2 关键功能引入顺序

| 阶段 | 功能模块 | 核心文件 |
|------|----------|----------|
| **阶段1** | 基本代理功能 | `main.py`, `auth.py`, `config.py`, `routes_openai.py` |
| **阶段2** | 流式处理 | `streaming_openai.py`, `parsers.py` |
| **阶段3** | Anthropic 支持 | `routes_anthropic.py`, `converters_anthropic.py`, `streaming_anthropic.py` |
| **阶段4** | 共享核心重构 | `converters_core.py`, `streaming_core.py` |
| **阶段5** | 多账号 + 错误体系 | `account_manager.py`, `account_errors.py`, `network_errors.py` |
| **阶段6** | 高级功能 | `thinking_parser.py`, `truncation_recovery.py`, `mcp_tools.py` |
| **阶段7** | 运维 + DX | `debug_logger.py`, `debug_middleware.py`, `tokenizer.py`, Docker |

### 8.3 最新更新

| 日期 | Commit | 说明 |
|------|--------|------|
| 2026-05-18 | `a5292ca` | refactor(errors): 优化最后一个账号错误消息措辞 |

> 当前版本号 `2.4.dev.13` 表明处于 v2.4 的开发迭代中，已经过 13 次开发版本构建。

---

## 附录：模块依赖关系图

```mermaid
graph LR
    main --> config
    main --> auth
    main --> cache
    main --> account_manager
    main --> routes_openai
    main --> routes_anthropic
    main --> exceptions
    main --> debug_middleware
    
    routes_openai --> converters_openai
    routes_openai --> streaming_openai
    routes_openai --> model_resolver
    routes_openai --> http_client
    routes_openai --> mcp_tools
    
    routes_anthropic --> converters_anthropic
    routes_anthropic --> streaming_anthropic
    routes_anthropic --> model_resolver
    routes_anthropic --> http_client
    routes_anthropic --> mcp_tools
    
    converters_openai --> converters_core
    converters_anthropic --> converters_core
    converters_core --> payload_guards
    converters_core --> truncation_recovery
    
    streaming_openai --> streaming_core
    streaming_anthropic --> streaming_core
    streaming_core --> parsers
    streaming_core --> thinking_parser
    streaming_core --> tokenizer
    
    account_manager --> auth
    account_manager --> account_errors
    account_manager --> cache
    
    http_client --> auth
    http_client --> network_errors
    
    model_resolver --> cache
    
    debug_middleware --> debug_logger
```

---

> **文档生成时间**：2026-05-28  
> **源码版本**：v2.4.dev.13 (commit a5292ca)  
> **许可证**：AGPL-3.0
