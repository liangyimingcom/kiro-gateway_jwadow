# API Key 认证凭证来源 — POC 实施结果

> 任务：按《新增凭证来源调研：Authenticate with an API key》的实施清单完成 POC
> 版本：v2.4.dev.13 | 完成时间：2026-06-01 | 测试：1711 passed

---

## 1. 任务概览

为 Kiro Gateway 新增第 5 种凭证来源 **API Key 认证（`type=api_key`）**，
与现有 4 种方式（JSON / refresh_token / SQLite / AWS SSO OIDC）共存，
并融入多账号系统（每个 API Key = 一个账号）。

```mermaid
flowchart LR
    P0[阶段0 协议验证] --> P1[阶段1 config]
    P1 --> P2[阶段2 auth]
    P2 --> P3[阶段3 account_manager]
    P3 --> P4[阶段4 迁移+示例]
    P4 --> P5[阶段5 脱敏]
    P5 --> P6[阶段6 单元测试]
    P6 --> P7[阶段7 集成+全量]
    P7 --> P8[阶段8 文档+推送]

    style P0 fill:#90EE90
    style P1 fill:#90EE90
    style P2 fill:#90EE90
    style P3 fill:#90EE90
    style P4 fill:#90EE90
    style P5 fill:#90EE90
    style P6 fill:#90EE90
    style P7 fill:#90EE90
    style P8 fill:#90EE90
```


---

## 2. 阶段0：协议验证（关键发现）

由于 kiro.dev 官方文档为 JS 渲染无法直接抓取，POC 阶段对真实 Kiro 端点做了
**实测探测**（使用用户提供的 `ksk_` 测试 Key），以确定 API Key 的传输方式。

### 2.1 探测结果

| # | 测试 | 结果 | 结论 |
|---|------|------|------|
| 1 | `GET runtime.../ListAvailableModels` + Bearer ksk_ | 404 UnknownOperation | runtime 端点不支持该操作（符合现有 `_is_runtime_endpoint`） |
| 2 | `POST runtime.../generateAssistantResponse` 最小体 | 400 REQUEST_BODY_INVALID | 体结构无效（先于鉴权校验） |
| 3 | 合法体 + 无 profileArn | 400 "profileArn is required" | profileArn 必填（先于鉴权校验） |
| 4 | 合法体 + 假 profileArn + 真 ksk_ | **403 "bearer token invalid"** | **ksk_ 不是直传 Bearer token** |
| 5 | 假 profileArn + 假 key | 403 同上 | 对照组一致 |
| 6 | `POST .../token`（OAuth）+ ksk_ 作 clientId | 404 | ksk_ 不是 OAuth clientId |

### 2.2 校验顺序与结论

```mermaid
flowchart TD
    REQ[generateAssistantResponse 请求] --> V1{请求体结构有效?}
    V1 -->|否| E1[400 REQUEST_BODY_INVALID]
    V1 -->|是| V2{profileArn 存在?}
    V2 -->|否| E2[400 profileArn is required]
    V2 -->|是| V3{Bearer token 有效?}
    V3 -->|否| E3[403 bearer token invalid]
    V3 -->|是| OK[200 流式响应]

    style E3 fill:#ff6b6b
```

**决定性结论**：原始 `ksk_` API Key **不能**作为 `runtime.kiro.dev` 的直传 Bearer
token（步骤4 返回 403 token invalid）。因此**必须先做一次服务端 exchange**，
将 API Key 换取短期 access token —— 即设计文档《§2.3》的**可能性 B**得到证实，
排除了可能性 A（直传）。

> ⚠️ 确切的 exchange 操作名称未在可访问的公开文档中给出。因此实现将 exchange
> 端点设计为**可配置**（`KIRO_API_KEY_EXCHANGE_URL`），未确认前会抛出清晰错误，
> 而非静默请求未验证端点。
>
> 注：本结论基于公开端点的黑盒探测，相关外部信息已按授权许可改写。


---

## 3. 实现总览

```mermaid
flowchart TD
    subgraph 客户端配置
        ENV[".env: KIRO_API_KEY=ksk_..."]
        JSON["credentials.json: {type:api_key, api_key:ksk_...}"]
    end

    ENV -->|迁移| JSON
    JSON --> AM[AccountManager.load_credentials]
    AM -->|account_id = api_key_+sha256| ACC[Account 对象]
    ACC --> INIT[_initialize_account]
    INIT --> KAM["KiroAuthManager(api_key=...)"]
    KAM --> DT[_detect_auth_type → API_KEY]

    REQ[请求到达] --> GET[get_access_token]
    GET --> EXP{token 有效?}
    EXP -->|否/首次| EX[_refresh_token_api_key 交换]
    EX --> CONF{exchange 端点已确认?}
    CONF -->|否| ERR[抛清晰错误]
    CONF -->|是| POST[POST exchange url, 解析 accessToken]
    POST --> TOK[缓存 access token]
    EXP -->|是| TOK
    TOK --> HDR["Authorization: Bearer {access token}"]

    style ERR fill:#FFE4B5
```

### 3.1 改动文件清单

| 文件 | 改动 | 风险 |
|------|------|------|
| `kiro/config.py` | +`KIRO_API_KEY`、exchange URL 模板/helper、confirmed 标志 | 🟢 纯新增 |
| `kiro/auth.py` | +`AuthType.API_KEY`、`api_key` 参数、`_redact_secret()`、`_refresh_token_api_key()`、检测分支 | 🟠 新增分支 |
| `kiro/account_manager.py` | +`type=api_key` 校验/account_id/匹配/构造分支 | 🟠 新增分支 |
| `main.py` | +`KIRO_API_KEY` 迁移与校验（最高优先级） | 🟢 新增分支 |
| `.env.example` | +OPTION 5 + exchange URL 说明 | 🟢 文档 |
| `credentials.json.example` | +`type=api_key` 示例 | 🟢 文档 |
| 4 个测试文件 | +20 个测试 | 🟢 测试 |

### 3.2 认证类型检测（API_KEY 最高优先级）

```mermaid
flowchart TD
    INIT[KiroAuthManager.__init__] --> C1{api_key 提供?}
    C1 -->|是| AK[auth_type = API_KEY]
    C1 -->|否| C2{clientId + clientSecret?}
    C2 -->|是| OIDC[AWS_SSO_OIDC]
    C2 -->|否| DESK[KIRO_DESKTOP]
    style AK fill:#90EE90
```

### 3.3 多账号融合（每个 Key = 一个账号）

```mermaid
flowchart LR
    K1["api_key A"] --> A1["账号 api_key_<hashA>"]
    K2["api_key B"] --> A2["账号 api_key_<hashB>"]
    J1["json 文件"] --> A3["账号 <path>"]
    A1 --> POOL[统一账号池]
    A2 --> POOL
    A3 --> POOL
    POOL --> CB[Circuit Breaker]
    POOL --> STICKY[Sticky 粘滞]
    POOL --> FAIL[429/402/403 自动切换]
    style A1 fill:#90EE90
    style A2 fill:#90EE90
```


---

## 4. 安全脱敏（阶段5）

| 风险点 | 处理 | 状态 |
|--------|------|------|
| 日志泄露 API Key | `_redact_secret()` 仅显示前 8 位 + 长度 | ✅ |
| exchange 请求体记录 | 不记录请求体，仅记录脱敏 key | ✅ |
| state.json 存储 | account_id 为 `api_key_{sha256[:16]}`，不存原始 key | ✅ |
| 调试日志 | `debug_logger`/`debug_middleware` 不记录 headers/Authorization | ✅ |
| 校验告警 | api_key 校验告警仅在 key 为空时触发 | ✅ |

```mermaid
flowchart LR
    KEY["ksk_kGR8...（完整）"] --> RED["_redact_secret"]
    RED --> OUT["ksk_kGR8...&lt;redacted len=36&gt;"]
    style OUT fill:#90EE90
```

---

## 5. 测试结果（阶段6 + 7）

### 5.1 新增测试（20 个）

```mermaid
flowchart TD
    subgraph 单元测试_18
        T1["test_auth_manager.py +9<br/>枚举/检测优先级/exchange成功/<br/>未确认报错/缺key报错/缺token报错/脱敏×2"]
        T2["test_account_manager.py +5<br/>加载/确定性ID/缺字段跳过/<br/>多key独立/混合json"]
        T3["test_config.py +4<br/>模板含region/helper替换/<br/>confirmed类型/env覆盖"]
    end
    subgraph 集成测试_2
        I1["test_account_system_flow.py +2<br/>双key failover/单key加载"]
    end
```

### 5.2 全量测试

| 指标 | 结果 |
|------|------|
| 总测试数 | **1711 passed** |
| 失败 | 0 |
| 回归 | 无 |
| 新增 | 20（18 单元 + 2 集成） |
| 运行环境 | Python 3.11.15 |

```
$ pytest -q
1711 passed, 1 warning in 5.76s
```

---

## 6. 当前可用性与后续步骤

### 6.1 当前状态

```mermaid
flowchart LR
    A[架构集成] -->|✅ 完成且测试通过| DONE1[可用]
    B[多账号融合] -->|✅ 完成且测试通过| DONE2[可用]
    C[脱敏/安全] -->|✅ 完成| DONE3[可用]
    D[exchange 端点] -->|⚠️ 待官方确认| PEND[配置后可用]
    style PEND fill:#FFE4B5
```

**POC 结论**：架构、多账号融合、安全、测试全部完成且零回归。
唯一待确认项是 **API Key → access token 的 exchange 端点**（官方未公开文档化），
已隔离为可配置项 `KIRO_API_KEY_EXCHANGE_URL`。

### 6.2 启用方式（待 exchange 端点确认后）

```bash
# .env
KIRO_API_KEY="ksk_xxxxxxxxxxxx"
KIRO_API_KEY_EXCHANGE_URL="https://prod.{region}.auth.desktop.kiro.dev/<已确认操作>"
```

未设置 `KIRO_API_KEY_EXCHANGE_URL` 时，API Key 认证会抛出清晰的可操作错误，
不会静默失败或请求未验证端点。

### 6.3 后续步骤

| # | 步骤 | 说明 |
|---|------|------|
| 1 | 确认 exchange 端点 | 通过官方文档或抓取 Kiro CLI headless 流量 |
| 2 | 校准请求/响应字段 | 调整 `_refresh_token_api_key()` 的 payload/解析字段 |
| 3 | 端到端联调 | 用真实 Key 验证 200 流式响应 |
| 4 | 文档更新 | README 多语言版补充 API Key 选项 |

---

## 7. 总结

| 设计文档问题 | POC 验证答案 |
|--------------|--------------|
| 能否新增 API Key 凭证来源? | ✅ 能，已实现并测试 |
| 是否兼容现有功能? | ✅ 纯增量，1711 测试零回归 |
| 与多账号融合度? | ✅ 每个 Key = 一个账号，复用全部编排能力 |
| 传输协议是直传还是 exchange? | **exchange**（实测证实，非直传） |
| 主要遗留项? | ⚠️ exchange 端点需官方确认（已隔离为可配置） |

---

> 文档性质：POC 实施结果记录
> 关联设计文档：`docs/zh/API_KEY_AUTH_DESIGN.md`
> 外部信息已按授权许可改写
