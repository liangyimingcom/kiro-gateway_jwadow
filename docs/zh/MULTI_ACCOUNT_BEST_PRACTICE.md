# Kiro Gateway 多账号系统最佳实践

> 版本：v2.4.dev.13 | 最后更新：2026-06-01
> 基于：[liangyimingcom/kiro-gateway_jwadow](https://github.com/liangyimingcom/kiro-gateway_jwadow)

---

## 1. 多账号系统概述

### 1.1 为什么需要多账号

| 痛点 | 解决 |
|------|------|
| 单账号免费额度有限（月度配额 402） | 多个账号轮换，配额耗尽自动切换 |
| 单账号被限流（429 Rate Limit） | 自动切换到下一个可用账号 |
| Token 过期（403） | 刷新失败时切换到其他账号 |
| 模型可用性不同（订阅级别差异） | 高级模型在 A 账号不可用时自动尝试 B 账号 |
| 单点故障 | N 个账号提供 N 倍容错 |

### 1.2 核心架构

```mermaid
flowchart TD
    REQ[客户端请求] --> ROUTE[routes_*.py]
    ROUTE --> AM[AccountManager]
    
    AM --> GET["get_next_account(model, exclude)"]
    GET --> STICKY{"Sticky 账号健康?"}
    STICKY -->|是| USE[使用当前 Sticky 账号]
    STICKY -->|否/冷却中| SCAN["轮询下一个账号"]
    
    SCAN --> CB{"Circuit Breaker<br/>冷却期内?"}
    CB -->|是| SKIP["跳过 (或 10% 概率探测)"]
    CB -->|否| LAZY{"已初始化?"}
    LAZY -->|否| INIT["懒初始化<br/>(加载凭证 + 获取模型列表)"]
    LAZY -->|是| USE2[使用该账号]
    
    USE --> SEND[发送请求到 Kiro API]
    USE2 --> SEND
    
    SEND --> RESULT{结果}
    RESULT -->|成功| SUCCESS["report_success<br/>重置失败计数 / 设为 Sticky"]
    RESULT -->|失败| CLASSIFY["classify_error"]
    CLASSIFY -->|RECOVERABLE| FAIL["report_failure<br/>失败+1 / 进入冷却"]
    CLASSIFY -->|FATAL| CLIENT["直接返回错误给客户端"]
    FAIL --> RETRY["排除当前账号, 重新 get_next_account"]
```

### 1.3 关键设计模式

| 模式 | 说明 |
|------|------|
| **Sticky（粘滞）** | 成功的账号持续使用，直到出错 — 避免不必要的切换 |
| **Circuit Breaker（断路器）** | 连续失败的账号进入冷却期（指数退避） — 不反复尝试已知坏账号 |
| **Lazy Initialization（懒初始化）** | 只初始化第一个工作账号,其余按需加载 — 启动快 |
| **Probabilistic Retry（概率探测）** | 冷却中的账号有 10% 概率被尝试 — 自动恢复 |
| **Dynamic Learning（动态学习）** | 成功调用的模型自动记录到该账号 — 后续优先使用 |

---

## 2. 配置方法

### 2.1 启用多账号系统

```bash
# .env
ACCOUNT_SYSTEM=true
```

首次启动时，会自动将 `.env` 中的单账号配置**一次性迁移**到 `credentials.json`。
之后所有账号管理通过 `credentials.json` 完成。

### 2.2 credentials.json 完整配置

```json
[
  {
    "type": "json",
    "path": "~/.aws/sso/cache/kiro-auth-token-account1.json",
    "comment": "账号1: Kiro IDE 凭证"
  },
  {
    "type": "json",
    "path": "~/.aws/sso/cache/kiro-auth-token-account2.json",
    "comment": "账号2: Kiro IDE 凭证"
  },
  {
    "type": "sqlite",
    "path": "~/.local/share/kiro-cli/data-account3.sqlite3",
    "comment": "账号3: kiro-cli (AWS SSO OIDC)"
  },
  {
    "type": "refresh_token",
    "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "profile_arn": "arn:aws:codewhisperer:us-east-1:123456789:profile/abc",
    "region": "us-east-1",
    "comment": "账号4: 直接 refresh token"
  }
]
```

### 2.3 支持的凭证类型

```mermaid
flowchart LR
    subgraph 凭证类型
        J["type: json<br/>Kiro IDE JSON 文件"]
        S["type: sqlite<br/>kiro-cli SQLite"]
        R["type: refresh_token<br/>环境变量/直接token"]
        A["type: api_key<br/>Session Key(仅认证)"]
    end
    
    J --> AM[AccountManager]
    S --> AM
    R --> AM
    A --> AM
    AM --> POOL["统一账号池<br/>(混合类型共存)"]
```

### 2.4 高级配置：每账号参数覆盖

```json
[
  {
    "type": "json",
    "path": "~/.aws/sso/cache/account-eu.json",
    "enabled": true,
    "profile_arn": "arn:aws:codewhisperer:eu-central-1:123:profile/xyz",
    "region": "eu-west-1",
    "api_region": "eu-central-1",
    "comment": "SSO 区域 eu-west-1, API 区域 eu-central-1"
  },
  {
    "type": "json",
    "path": "/path/to/disabled-account.json",
    "enabled": false,
    "comment": "临时禁用此账号"
  }
]
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `enabled` | 是否启用此账号 | `true` |
| `profile_arn` | AWS CodeWhisperer Profile ARN | 从凭证文件自动读取 |
| `region` | SSO/OIDC 区域(Token 刷新) | `us-east-1` |
| `api_region` | Q API 区域(模型调用端点) | 自动检测 |

### 2.5 批量账号：文件夹扫描

```json
[
  {
    "type": "json",
    "path": "/home/user/kiro-accounts/",
    "comment": "扫描文件夹内所有 JSON 凭证(非递归)"
  },
  {
    "type": "sqlite",
    "path": "/home/user/kiro-dbs/",
    "comment": "扫描文件夹内所有 .sqlite3 文件"
  }
]
```

**大规模部署建议**：把 N 个 Kiro IDE 的凭证 JSON 文件放到同一文件夹,一行配置搞定。

---

## 3. 工作原理详解

### 3.1 启动流程

```mermaid
sequenceDiagram
    participant Main as main.py (lifespan)
    participant AM as AccountManager
    participant KAM as KiroAuthManager
    participant Kiro as Kiro API

    Main->>AM: load_credentials() — 解析 credentials.json
    Note over AM: 每条凭证 → 一个 Account 对象<br/>account_id = 文件路径 或 hash
    Main->>AM: load_state() — 恢复上次的 Sticky 索引 + 统计
    
    loop 逐个尝试初始化 (从 state.json 记录的索引开始)
        Main->>AM: _initialize_account(id)
        AM->>KAM: get_access_token() — 验证凭证
        KAM->>Kiro: POST /refreshToken 或 OIDC
        Kiro-->>KAM: accessToken (或失败)
        AM->>Kiro: GET /ListAvailableModels
        Kiro-->>AM: 模型列表 (或 fallback)
        Note over AM: 第一个成功的 → 设为 Sticky
    end
    
    Main->>AM: save_state_periodically() (后台每10s)
```

### 3.2 请求处理流程（多账号故障切换）

```mermaid
sequenceDiagram
    participant Client as 客户端
    participant Route as routes_openai.py
    participant AM as AccountManager
    participant A as 账号A
    participant B as 账号B
    participant Kiro as Kiro API

    Client->>Route: POST /v1/chat/completions
    Route->>AM: get_next_account("claude-sonnet-4.5", exclude={})
    AM-->>Route: 账号A (Sticky)
    Route->>Kiro: generateAssistantResponse (A 的 token)
    Kiro-->>Route: 429 Rate Limited
    
    Route->>AM: report_failure(A, RECOVERABLE, 429)
    Note over AM: A.failures=1, cooldown=60s
    
    Route->>AM: get_next_account("claude-sonnet-4.5", exclude={A})
    AM-->>Route: 账号B
    Route->>Kiro: generateAssistantResponse (B 的 token)
    Kiro-->>Route: 200 OK (流式响应)
    
    Route->>AM: report_success(B, "claude-sonnet-4.5")
    Note over AM: B 成为新的 Sticky
    Route-->>Client: SSE 流
```

### 3.3 Circuit Breaker 状态机

```mermaid
stateDiagram-v2
    [*] --> Closed: 初始(健康)
    Closed --> Open: 失败 → failures > 0
    Open --> HalfOpen: 冷却期过 (base × 2^(failures-1))
    Open --> Closed: 10% 概率探测成功
    HalfOpen --> Closed: 请求成功 (reset failures)
    HalfOpen --> Open: 请求失败 (failures + 1)
    
    note right of Open
        指数退避冷却:
        1次失败: 60s
        2次: 120s
        3次: 240s
        ...
        12次+: 86400s (1天上限)
    end note
```

### 3.4 错误分类（决定是切换还是返回）

```mermaid
flowchart TD
    ERR[Kiro API 返回错误] --> CODE{HTTP 状态码}
    
    CODE -->|402| REC1["RECOVERABLE<br/>月度配额耗尽 → 切换"]
    CODE -->|403| REC2["RECOVERABLE<br/>Token 失效 → 切换"]
    CODE -->|429| REC3["RECOVERABLE<br/>限流 → 切换"]
    CODE -->|"400 + INVALID_MODEL_ID"| REC4["RECOVERABLE<br/>模型不可用 → 切换"]
    
    CODE -->|"400 + CONTENT_LENGTH..."| FAT1["FATAL<br/>上下文溢出 → 返回客户端"]
    CODE -->|"400 + 其他/null"| FAT2["FATAL<br/>请求格式错误 → 返回客户端"]
    CODE -->|5xx| FAT3["FATAL<br/>服务器错误 → 返回客户端"]
    
    REC1 --> NEXT[尝试下一个账号]
    REC2 --> NEXT
    REC3 --> NEXT
    REC4 --> NEXT
    FAT1 --> CLIENT[返回错误给客户端]
    FAT2 --> CLIENT
    FAT3 --> CLIENT
```

**设计逻辑**：RECOVERABLE 错误是**账号级别**的（换个账号可能没问题），FATAL 是**请求级别**的（所有账号都会失败）。

### 3.5 状态持久化

```mermaid
flowchart LR
    AM["AccountManager<br/>(内存状态)"] -->|每10s 自动| STATE["state.json"]
    AM -->|关闭时 final save| STATE
    
    STATE --> CONTENT["记录:<br/>- current_account_index (Sticky)<br/>- 各账号 failures / last_failure_time<br/>- 各账号 stats"]
    
    RESTART["重启时"] --> LOAD["load_state()"]
    LOAD --> RESTORE["恢复 Sticky + 统计<br/>(不从头轮询)"]
```

---

## 4. 运维最佳实践

### 4.1 大规模部署推荐配置

```bash
# .env — 10+ 账号的生产环境
ACCOUNT_SYSTEM=true
PROXY_API_KEY="your-strong-proxy-password"

# 调优参数
ACCOUNT_RECOVERY_TIMEOUT=60          # 冷却基础时间(秒)
ACCOUNT_MAX_BACKOFF_MULTIPLIER=1440  # 最大退避倍率(60s×1440=24h)
ACCOUNT_PROBABILISTIC_RETRY_CHANCE=0.1  # 冷却中10%概率探测
ACCOUNT_CACHE_TTL=43200              # 模型缓存TTL(12h)
STATE_SAVE_INTERVAL_SECONDS=10       # 状态保存间隔

# 调试(生产环境建议 errors)
DEBUG_MODE=errors
```

### 4.2 账号数量规划

```mermaid
flowchart TD
    LOAD{日请求量?} -->|"< 100"| N1["2-3 个账号足够"]
    LOAD -->|"100-500"| N2["5-10 个账号"]
    LOAD -->|"500-2000"| N3["10-20 个账号"]
    LOAD -->|"> 2000"| N4["20+ 个账号<br/>(考虑多实例部署)"]
    
    N1 --> TIP1["重点: 容错<br/>任一账号失效仍可用"]
    N2 --> TIP2["重点: 配额分散<br/>月度配额 × N"]
    N3 --> TIP3["重点: 限流分散<br/>RPM × N"]
    N4 --> TIP4["重点: 注意文件夹扫描<br/>+ 单实例 state.json 限制"]
```

### 4.3 日常运维检查

| 检查项 | 方法 | 频率 |
|--------|------|------|
| 账号健康 | 查看日志中 `failure #N` / `cooldown` | 日常 |
| 配额使用 | 访问 `app.kiro.dev/account/usage`（每个账号） | 每周 |
| Token 有效性 | 查看日志中 `403` / `force_refresh` | 日常 |
| 模型可用性 | 查看 `INVALID_MODEL_ID` 日志 | 按需 |
| 状态文件 | 查看 `state.json` 的 `current_account_index` | 排错时 |

### 4.4 监控告警建议

```mermaid
flowchart TD
    LOG[应用日志] --> PARSE[日志解析]
    PARSE --> M1["所有账号进入冷却期<br/>→ P0 告警(服务不可用)"]
    PARSE --> M2["某账号 failures > 5<br/>→ P1 告警(该账号可能需检查)"]
    PARSE --> M3["402 MONTHLY_REQUEST_COUNT<br/>→ P2 告警(配额即将耗尽)"]
    PARSE --> M4["连续 503 无可用账号<br/>→ P0 告警(客户端受影响)"]
```

---

## 5. 纠错与故障排除

### 5.1 常见错误与解决方法

#### 错误1：启动时 "Failed to initialize any account"

```
ERROR | Failed to initialize any account. Check your credentials.
```

**原因**：所有账号的凭证都无法通过验证（Token 过期/文件路径错误/网络不通）。

**排查步骤**：

```mermaid
flowchart TD
    E["Failed to initialize any account"] --> C1{"credentials.json 存在?"}
    C1 -->|否| FIX1["创建 credentials.json<br/>或设置 ACCOUNT_SYSTEM=true 让 .env 自动迁移"]
    C1 -->|是| C2{"凭证文件路径正确?"}
    C2 -->|否| FIX2["修正 path 指向正确的凭证文件"]
    C2 -->|是| C3{"Token 未过期?"}
    C3 -->|否| FIX3["重新登录 Kiro IDE / kiro-cli login"]
    C3 -->|是| C4{"网络可达 kiro.dev?"}
    C4 -->|否| FIX4["配置 VPN_PROXY_URL"]
```

```bash
# 排查命令
cat credentials.json                 # 检查配置
ls -la ~/.aws/sso/cache/             # 检查凭证文件是否存在
DEBUG_MODE=all python main.py        # 查看详细日志
```

#### 错误2：频繁出现 "Account xxx failure #N"

```
WARNING | Account /home/user/.aws/sso/cache/token.json failure #3: status=429, reason=None, cooldown=4m
```

**原因**：该账号被限流。

**处理**：
- 这是**正常行为** — 系统会自动切换到其他账号
- 如果**所有**账号都频繁 429 → 增加账号数量
- 冷却结束后该账号自动恢复

#### 错误3："All accounts unavailable" / 503

```
HTTP 503: No available accounts for model claude-sonnet-4.5
```

**原因**：所有账号都在冷却期（Circuit Breaker 全开）。

**处理**：

```mermaid
flowchart TD
    E503["503 所有账号不可用"] --> A{"账号数量?"}
    A -->|少| FIX1["增加更多账号"]
    A -->|已经很多| B{"请求频率?"}
    B -->|太高| FIX2["降低客户端请求频率<br/>或增加更多账号"]
    B -->|正常| C{"冷却参数?"}
    C --> FIX3["降低 ACCOUNT_RECOVERY_TIMEOUT<br/>或提高 ACCOUNT_PROBABILISTIC_RETRY_CHANCE"]
```

```bash
# 临时加速恢复
ACCOUNT_RECOVERY_TIMEOUT=30          # 缩短冷却
ACCOUNT_PROBABILISTIC_RETRY_CHANCE=0.3  # 提高探测概率
```

#### 错误4：Token 刷新失败

```
ERROR | Failed to refresh token: HTTP 401
```

**处理**：
- 重新登录对应账号（Kiro IDE / `kiro-cli login`）
- 如果是 JSON 文件凭证 → 确认 `refreshToken` 字段有效
- 多账号时此错误不影响服务（自动切换到其他账号）

### 5.2 state.json 手动纠错

`state.json` 存储了运行时状态,紧急时可手动编辑：

```json
{
  "current_account_index": 0,
  "accounts": {
    "/path/to/account1.json": {
      "failures": 0,
      "last_failure_time": 0,
      "stats": {"total_requests": 150, "successful_requests": 148, "failed_requests": 2}
    },
    "/path/to/account2.json": {
      "failures": 5,
      "last_failure_time": 1717200000.0,
      "stats": {"total_requests": 80, "successful_requests": 75, "failed_requests": 5}
    }
  }
}
```

**手动修复示例**：

```bash
# 重置所有账号的失败计数（强制全部恢复）
python3 -c "
import json
with open('state.json', 'r+') as f:
    state = json.load(f)
    for acc in state.get('accounts', {}).values():
        acc['failures'] = 0
        acc['last_failure_time'] = 0
    f.seek(0)
    json.dump(state, f, indent=2)
    f.truncate()
print('All accounts reset')
"

# 或直接删除 state.json (重启后从头开始)
rm state.json
```

### 5.3 凭证文件热更新

`credentials.json` 的变更**需要重启**网关才能生效
（当前版本不支持运行时热加载）。

但凭证文件（JSON/SQLite）中的 **Token 刷新是自动的** — 只要 `refreshToken` 有效,
`accessToken` 过期后会自动刷新并写回文件。

```mermaid
flowchart LR
    subgraph 需要重启
        A["增/删/改 credentials.json 条目"]
        B["修改 .env 配置参数"]
    end
    
    subgraph 自动处理(无需重启)
        C["accessToken 过期 → 自动刷新"]
        D["账号进入/退出冷却期"]
        E["Sticky 切换"]
    end
```

---

## 6. 最佳实践总结

### 6.1 配置最佳实践

| 实践 | 说明 |
|------|------|
| 使用**文件夹扫描** | 大量账号时一行配置搞定 |
| 所有账号**同一区域** | 避免跨区域延迟差异 |
| 保持凭证文件**权限 600** | 防止 Token 泄露 |
| **不同订阅级别**的账号混用 | 高级模型不可用时自动 fallback 到支持它的账号 |
| 设置 `enabled: false` 临时禁用 | 而非删除条目（保留统计） |

### 6.2 部署最佳实践

```mermaid
flowchart TD
    subgraph 单实例部署_推荐
        SI["1 个网关实例<br/>N 个 Kiro 账号<br/>state.json 本地文件"]
    end
    
    subgraph 多实例部署_注意
        MI["M 个网关实例<br/>N 个 Kiro 账号<br/>⚠️ state.json 不共享<br/>各实例独立 Sticky"]
    end
    
    SI --> TIP1["✅ 简单可靠<br/>状态一致"]
    MI --> TIP2["⚠️ 各实例可能同时<br/>用同一个账号"]
```

**多实例注意**：`state.json` + 内存中的 `asyncio.Lock` 意味着**每个实例有独立状态**。
如果多实例部署,考虑：
- 将不同的 credentials.json 子集分配给不同实例（账号分片）
- 或接受各实例独立轮换（简单,但可能同时打同一个账号）

### 6.3 调参指南

| 场景 | 调整 |
|------|------|
| 账号恢复太慢 | 降低 `ACCOUNT_RECOVERY_TIMEOUT`（如 30s） |
| 想更快发现账号恢复 | 提高 `ACCOUNT_PROBABILISTIC_RETRY_CHANCE`（如 0.2） |
| 不想等太久就放弃冷却账号 | 降低 `ACCOUNT_MAX_BACKOFF_MULTIPLIER`（如 60，即最大 1h） |
| 模型列表变化频繁 | 降低 `ACCOUNT_CACHE_TTL`（如 3600s = 1h） |
| 状态持久化更频繁 | 降低 `STATE_SAVE_INTERVAL_SECONDS`（如 5s） |

---

## 7. 示例：10 个 Kiro IDE 账号的完整配置

### Step 1: 准备凭证文件

```bash
# 把 10 个 Kiro IDE 账号的凭证 JSON 文件放到一个文件夹
mkdir -p ~/kiro-accounts/
# 每个文件格式:
# {
#   "accessToken": "eyJ...",
#   "refreshToken": "eyJ...",
#   "expiresAt": "2025-07-12T23:00:00.000Z",
#   "profileArn": "arn:aws:codewhisperer:us-east-1:xxx:profile/yyy",
#   "region": "us-east-1"
# }
```

### Step 2: 创建 credentials.json

```json
[
  {
    "type": "json",
    "path": "~/kiro-accounts/",
    "comment": "文件夹扫描: 自动加载全部 JSON 凭证"
  }
]
```

### Step 3: 配置 .env

```bash
ACCOUNT_SYSTEM=true
PROXY_API_KEY="your-strong-proxy-password-here"
KIRO_REGION="us-east-1"
DEBUG_MODE=errors
```

### Step 4: 启动

```bash
python main.py
```

预期日志输出：
```
INFO  | Loaded 10 account(s) from credentials
INFO  | Attempting to initialize account: /home/user/kiro-accounts/account1.json
INFO  | Successfully initialized account: /home/user/kiro-accounts/account1.json
INFO  | Account system initialized successfully
INFO  | Server running at: http://localhost:8000
```

### Step 5: 验证

```bash
# 健康检查
curl http://localhost:8000/health

# 测试请求
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer your-strong-proxy-password-here" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4.5","messages":[{"role":"user","content":"ping"}],"stream":false}'
```

---

## 8. 参数参考

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ACCOUNT_SYSTEM` | `false` | 启用多账号系统 |
| `ACCOUNTS_CONFIG_FILE` | `credentials.json` | 凭证配置文件路径 |
| `ACCOUNTS_STATE_FILE` | `state.json` | 运行时状态文件路径 |
| `ACCOUNT_RECOVERY_TIMEOUT` | `60` | 冷却基础时间(秒) |
| `ACCOUNT_MAX_BACKOFF_MULTIPLIER` | `1440` | 最大退避倍率(60×1440=86400s=1天) |
| `ACCOUNT_PROBABILISTIC_RETRY_CHANCE` | `0.1` | 冷却中的探测概率(10%) |
| `ACCOUNT_CACHE_TTL` | `43200` | 模型缓存过期时间(12小时) |
| `STATE_SAVE_INTERVAL_SECONDS` | `10` | 状态自动保存间隔 |

---

> 关联文档:
> - 架构详解: [`ARCHITECTURE_DETAILED.md`](ARCHITECTURE_DETAILED.md)
> - 风险分析: [`RISK_ANALYSIS.md`](RISK_ANALYSIS.md)
> - API Key 调研: [`API_KEY_AUTH_DESIGN.md`](API_KEY_AUTH_DESIGN.md)
