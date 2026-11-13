# AI 智商雷达插件 · 上游接口文档

> 数据源：**众测雷达 / DRadar** — 站点 `https://deng.codexradar.com/`，API `https://api.codexradar.com`
> 本文是**上游 API 的实测接口文档**（供插件实现时对照），不是插件自身的接口；插件侧契约见 [`ai_radar_plugin_api.md`](ai_radar_plugin_api.md)。
> 实测时间：2026-09-13（UTC+8 上午）。上游数据是**持续更新的快照**，字段会演进，实现时必须以 `/openapi.json` 与实测为准。

---

## 0. 契约来源与可靠性分级

| 来源 | 可靠性 | 说明 |
| --- | --- | --- |
| `GET /openapi.json` | 权威（路径/参数/请求体） | FastAPI 生成，`info.title=dradar`、`version=0.1.0`、`openapi=3.1.0`，共 **58 条路径**、**34 个 components.schemas** |
| 实测响应体 | 权威（响应字段） | **spec 里所有 200 响应 schema 都是空 `{}`**，响应形状只能实测；本文所有字段名均来自实测 JSON |
| 站点静态页 / 方法论页 | 权威（口径解释） | `/methodology/`、`/benchmarks/deepswe/`、`/benchmarks/pompeii-adjacency/`、`/intro` |
| 站点前端 JS | 参考 | `assets/radar-report.js` 只调 4 个端点，不足以覆盖全 API |
| 官方客户端 `codex-radar/dradar` | **权威（客户端侧行为）** | v0.5.200 源码已读（`src/dradar/api_client.py`）：请求头、超时、429/`Retry-After` 策略、端点全集 |
| **网站侧 API `codexradar.com/api/*`** | 参考（**无 OpenAPI 契约**） | 与 `api.codexradar.com` 是**两套面**，见 §1.2 |

**关键坑（必读）**：

- `spec` 中 `securitySchemes = null`、`servers = null` → 鉴权与 base URL 不在 spec 里。
- `spec` 中**所有 200 响应都是 `{}`**（无响应模型）→ 不要指望从 spec 生成客户端类型。
- `cells` 在 `/table` 里是 **dict**（键为 `<task_id>|<model>|<effort>`），不是数组。
- `harness` 枚举在两个 schema 里**不一致**：`/suggest` 用 `grok`，`ClaimBatchPayload` 用 `grok-build`。

---

## 1. 基础信息

| 项 | 值 | 证据 |
| --- | --- | --- |
| API base | `https://api.codexradar.com` | 站点 `radar-report.js` 的 `apiRoot()`；实测可达 |
| 本地调试 base | `http://127.0.0.1:8399` | `radar-report.js`：`host === "localhost" \|\| host === "127.0.0.1"` 时（且非 `file:`） |
| 框架 | FastAPI + Cloudflare 前置 | 响应头 `server: cloudflare`、`cf-ray`、`cf-cache-status` |
| 交互文档 | `GET /docs`（Swagger UI）、`GET /openapi.json` | 实测 200 |
| 站点页 | `/`、`/en`、`/intro`、`/methodology/`、`/benchmarks/deepswe/`、`/benchmarks/pompeii-adjacency/` | `sitemap.xml` 全量 6 条 |
| robots | `User-agent: * / Allow: /` + Sitemap | `/robots.txt` |
| 压缩 | gzip（`content-encoding: gzip`），`transfer-encoding: chunked` | 实测响应头 |

### 1.1 鉴权

- 公开只读端点**不需要任何凭证**（实测 200）。
- 需要 Bearer 的端点返回 `401 {"detail": "missing bearer token"}`；run-plan 相关返回 `401 {"detail": "missing plan bearer token"}`。
- 登录链路（**未实测**）：GitHub OAuth（`GET /api/v1/github/config` 公开返回 `{"client_id": "Ov23liQFleSUsxrCLTAq"}`）→ `POST /api/v1/github/link`（form `access_token`）→ `GET /api/v1/github/oauth/callback?code=`。
- 客户端侧另有设备/会话身份：`run-plans/exchange`（`run_code` + `device_id`）→ `run-plans/start`（`plan_id` + `logical_session_id`，可带 `decision: join_existing|recover_stale`）。

### 1.2 第二套接口面：网站侧 `codexradar.com/api/*`

上游有**两套独立 host 的公开 JSON 面**，此前只覆盖了前者：

| | A 面（主） | B 面（网站侧） |
| --- | --- | --- |
| base | `https://api.codexradar.com` | `https://codexradar.com` |
| 路径前缀 | `/api/v1/*` | `/api/*` |
| OpenAPI 契约 | **有**（`/openapi.json`，58 路径） | **无** |
| 鉴权 | 免鉴权只读 + Bearer 写 | 免鉴权只读 |
| 插件用法 | **主路径** | 仅补充，非默认拉取 |

B 面实测端点：

| 端点 | 状态 | 体积 | 顶层键 |
| --- | --- | --- | --- |
| `/api/radar-insights` | 200 | 29.4KB | 与 A 面 `/api/v1/radar-insights` **键完全相同**（仅 `generated_at` 相差 10 分钟 → 缓存时差） |
| `/api/intelligence-efficiency-metrics` | 200 | 31.7KB | `schema/mode/source_updated_at/runs_24h_total/runs_48h_total/runs_total/points/benchmark_id/scoring_mode/score_label` |
| `/api/visual-spatial-reasoning` | 200 | **1.07MB** | `schema/type/mode/benchmark_id/scoring_mode/score_label/source_updated_at/runs_*/points[25]/history[153]` |
| `/api/model-ratings?view=public` | 200 | 3.6KB | `ok/day/timezone/refresh_seconds/updated_at/models[33]/window/window_hours/since/until/source/cached_at` |
| `/api/subscriber-count` | 200 | 42B | `{ok, count, source}` |

**B 面独有（A 面没有对应端点）**：

- **`/api/model-ratings?view=public`** —— 33 个模型的 `{id,label,group,average,count}`，`window: "rolling_24h"`、`window_hours: 24`、`refresh_seconds: 300`、`source: "public_cache"`。样例：`{"id":"gpt-6-astra-ultra","label":"GPT-6 Astra ultra","group":"GPT-6 Astra","average":8,"count":8}`。
  - **`average` 量纲与 IQ 不同，绝不可与 IQ 混用或同图对比**。`group` 可用于把档位归并到模型族。
- **`/api/subscriber-count`** —— `{"ok":true,"count":4263,"source":"cached"}`。

**私有变体（禁用）**：A 面 OpenAPI 里有 `/api/private/v1/intelligence-efficiency`，实测 `401 {"detail":"invalid bearer token"}`。**插件不得触碰任何 `private` 路径。**

**关系未验证**：`/api/visual-spatial-reasoning` 与 A 面 `/iq-history?benchmark=pompeii-adjacency` 同为 1MB 级视觉频道数据，**推断同源但未证实**；实现时按两个独立端点处理，不要假定可互换。

### 1.3 CORS

| Origin | `Access-Control-Allow-Origin` |
| --- | --- |
| `https://deng.codexradar.com` | 回显同值 |
| `https://codexradar.com` | 回显同值 |
| `https://example.com` | 无 |
| `null` | 无 |

`OPTIONS` 预检 200：`allow-methods: GET, POST`，`allow-headers: Accept, Accept-Language, Authorization, Content-Language, Content-Type, X-DRadar-Capabilities`，`max-age: 600`。**无 `Access-Control-Allow-Credentials`** → 浏览器端不能带 cookie，只能用 Bearer。

> 插件（服务端 Python）不受 CORS 约束；此节记录只为说明「上游把浏览器消费方限定在自家域」，第三方长期消费的许可状态见 [开放问题](ai_radar_open_items.md)。

### 1.4 缓存

> **实测教训（写文档/实现时必读）**：首轮探测时 `/intelligence-efficiency?benchmark=pompeii-adjacency` 与 `/iq-history?benchmark=pompeii-adjacency` 都返回了与 `deep-swe` **长度完全相同**的响应，据此一度误判「`benchmark` 参数被忽略」。复测（含 60s 后的重新请求）证明**参数生效**：pompeii 的 efficiency 是 19679B / 40 个 point，iq-history 是 733600B / 76 条 series。首轮的相同长度是 **Cloudflare 缓存**造成的（`s-maxage` 命中）。→ **对拍上游时必须带 cache-buster 或等待 TTL 过期，否则会把缓存命中误读成「参数无效」。**

| 端点 | `Cache-Control` | ETag | 实测 `cf-cache-status` |
| --- | --- | --- | --- |
| `/api/v1/table` | `public, max-age=30, s-maxage=300` | 有 | HIT |
| `/api/v1/leaderboard` | `public, max-age=10, s-maxage=10` | 有 | EXPIRED |
| `/api/v1/iq-history` | `public, max-age=60` | 无 | EXPIRED |
| `/api/v1/radar-insights` | `public, max-age=600, s-maxage=600` | 有 | HIT |
| `/api/v1/intelligence-efficiency` | `public, max-age=30, s-maxage=30` | 有 | DYNAMIC |
| `/api/v1/model-metrics` | `public, max-age=30, s-maxage=30` | 有 | DYNAMIC |
| `/api/v1/events` | `public, max-age=30` | 无 | MISS |
| `/api/v1/quota` | `public, max-age=60` | 无 | DYNAMIC |
| `/api/v1/benchmarks` | `public, max-age=300` | 无 | — |

`If-None-Match` 复验：`/benchmarks` 无 ETag 时返回 200（非 304）。

### 1.5 限流与重试策略

**匿名只读路径实测**：15 次连打 `/api/v1/benchmarks` 全部 200、耗时 2.87s，响应中无任何 `rate`/`retry` 相关头。**这不等于官方允许高频消费**——服务端未公开匿名配额，且官方客户端文档记录过真实的 429 事件。

**官方客户端策略（权威参照，来自 `codex-radar/dradar` v0.5.200 `src/dradar/api_client.py`）**——插件不要自己发明退避参数，直接对齐下表：

| 项 | 值 / 行为 |
| --- | --- |
| 429 最大重试次数 | `_RATE_LIMIT_RETRIES = 5`（最多 6 次尝试） |
| `Retry-After` 解析 | 解析为 float；解析失败则用默认值 |
| `Retry-After` 取值域 | **clamp 到 `[1.0, 60.0]` 秒** |
| 退避等待 | `clamp(retry_after) + uniform(0, min(1.0, retry_after * 0.1))`（抖动防「多 worker 同时唤醒再造 429 羊群」） |
| 503 `deployment_maintenance` | **不 clamp**（维护栅栏是服务端承诺），由 360s 总重试预算裁决 |
| 请求超时 | `httpx.Timeout(30.0, write=None, read=120.0)`；遥测 POST 单独 3s |
| 连接级重试 | `httpx.HTTPTransport(retries=2)`，仅重试**连接建立**失败（不重发已发出请求）；**检测到代理时禁用** |

**插件侧设计含义**：

- 上游确有 429，但只读 GET 是**幂等**的，重试无副作用——可安全对齐上表。
- 我们的端点体积差异极大（`/table` 8.6MB vs `/benchmarks` 几 KB），**读超时应按端点分级**，不能统一用 120s。
- 退避必须带抖动；**不要**在 429 时立刻重打。
- 代理场景下（本机 `HTTP_PROXY` 常开）不要用连接级重试，避免代理层重复建连。

---

## 2. 只读端点（插件使用面）

### 2.1 `GET /api/v1/benchmarks`

无参数。返回评测频道清单。

```json
{
  "default_benchmark": "deep-swe",
  "benchmarks": [
    {
      "id": "deep-swe",
      "title": "代码修复 · DeepSWE",
      "short_title": "DeepSWE",
      "description": "真实开源仓库任务，恢复代码行为并由容器测试判分。",
      "task_count": 112,
      "scoring_mode": "binary-majority",
      "score_label": "Pass rate",
      "rolling_window": 3,
      "model_config_count": 67,
      "reference_task_id": null,
      "reference_url": null,
      "task_bundle": null,
      "default": true
    },
    {
      "id": "pompeii-adjacency",
      "title": "视觉恢复 · 庞贝壁画",
      "short_title": "庞贝壁画邻接恢复",
      "description": "从无旋转壁画碎片中恢复直接邻接拓扑；不评绝对坐标、画布尺度或旋转。",
      "task_count": 86,
      "scoring_mode": "continuous-macro",
      "score_label": "Adjacency F1",
      "rolling_window": 3,
      "model_config_count": 40,
      "reference_task_id": "RP-group-1-public-example",
      "reference_url": "/assets/pompeii-reference/reference_question.png",
      "task_bundle": {
        "url": "/api/v1/benchmark-bundles/pompeii-adjacency",
        "sha256": "e969b5321a541245e81e2e6ae5315c279382867f5966441336f3bd9ec51fa898",
        "bytes": 63775935,
        "format": "tar.gz"
      },
      "default": false
    }
  ]
}
```

---

### 2.2 `GET /api/v1/leaderboard`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `benchmark` | string? | `deep-swe` | 未知值 → **404** `{"detail":"'unknown benchmark: nope'"}` |
| `view` | string? | — | **未校验**：任意值（`total`/`month`/`history`/`nope`）都返回同一份 1.60MB 响应 |

体积：1.60MB（deep-swe）/ 1.30MB（pompeii）。顶层键：

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `tasks` | string[112] | 任务 id 列表（deep-swe 112 / pompeii 86） |
| `models` | object[67] | 模型档位聚合（见下） |
| `contributors` | object[984] | 贡献者天梯 |
| `benchmark_id` / `benchmarks` | string / object[] | 同 `/benchmarks` |
| `scoring_mode` / `score_label` / `pass_threshold` | string/string/float | `binary-majority`/`Pass rate`/`1.0`（pompeii：`continuous-macro`/`Adjacency F1`/`0.4`） |
| `pending_grades` / `error_grades` | int | 实测 `0` / `458` |
| `k` | int | 实测 `1` |
| `online_volunteers` | int | 实测 `2` |
| `site_name` / `site_url` | string | 实测 `"DRadar"` / `""` |
| `month` | object | `{"label":"2026-09","settles_at":"2026-10-01T00:00:00+08:00"}` |
| `history` | object | 键 `starts_at` / `top` / `seasons`（赛季历史） |
| `pedal_speed` | object | `{window_minutes:60, scope:"fleet_submitted", submitted_runs, token_runs, tokens_per_hour, input_tokens_per_hour, cached_input_tokens_per_hour, output_tokens_per_hour, cache_hit_ratio, api_equivalent_runs, api_equivalent_usd_per_hour, paid_api_runs, paid_api_usd_per_hour, estimated_runs, unpriced...}` |
| `latest_burn` | object | `{tokens, usd, cost_kind:"folded", cost_pending, submitted_at}` |
| `flag_race` | object | `{status:"won", target_usd:150000.0, reward_points:1500.0, current_usd, winner:{nickname,github_login}, submission:{task_id,model,effort,submitted_at}, cumulative_usd, awarded_at, announcement_until}` |

**`models[]`**：

```json
{
  "model": "gpt-6-astra", "effort": "low",
  "graded": 135, "passed": 88, "score_sum": 88.0,
  "cells": 110, "cells_passed": 75, "pass_rate": 0.682,
  "score_label": "Pass rate", "scoring_mode": "binary-majority", "pass_threshold": 1.0,
  "tasks": {
    "abs-module-cache-flags": {"votes":2,"pass_votes":1,"majority_pass":false,"score_sum":1.0,"score_rate":0.5},
    "abs-stepped-slices":     {"votes":2,"pass_votes":2,"majority_pass":true, "score_sum":2.0,"score_rate":1.0}
  }
}
```

`tasks` 是 dict（`task_id` → 该档位在这道题上的多数表决结果）；`votes` 是有效运行数，`majority_pass` 是 `binary-majority` 口径下的判定。

**`contributors[]`（34 字段）**：

| 组 | 字段 |
| --- | --- |
| 身份 | `nickname`、`github_login`、`avatar_url`、`avatar_seed`、`is_radar_admin`、`flag_race_winner` |
| 提交 | `submissions`、`graded`、`month_submissions`、`month_graded` |
| 积分 | `points`、`points_by_harness`、`month_points`、`month_points_by_harness` |
| 用量 | `tokens`、`month_tokens` |
| 成本 | `folded_usd`、`usd`、`deepseek_api_usd`、`deepseek_api_runs`、`deepseek_api_unpriced_runs`、`month_folded_usd`、`month_usd`、`month_deepseek_api_usd`、`month_deepseek_api_runs`、`month_deepseek_api_unpriced_runs` |
| 连击 | `contribution_streak: {daily_reward_points, current_days, longest_days, active_days, active_today, today_points, reward_points, month_reward_points, next_milestone_days, next_milestone_bonus}` |
| 排名 | `rank_change_24h`、`month_rank_change_24h` |

样例（榜首）：`{"nickname":"ymcui","submissions":1772,"graded":1745,"points":64813.1,"points_by_harness":{"codex":59132.4,"other":5680.7},"tokens":33983024324,"folded_usd":17762.97,"deepseek_api_usd":91.32,"deepseek_api_runs":195,"usd":17854.29,"rank_change_24h":0,...}`

> `folded_usd` 与 `deepseek_api_usd` 是两个成本口径（折叠/直连 API），**不要相加后当「花费」展示**，按上游语义分别是「折算成本」与「DeepSeek API 实耗」。

---

### 2.3 `GET /api/v1/table`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `benchmark` | string? | 未知值 → 404；缺省 `deep-swe` |

体积：**8.62MB**（deep-swe）/ 3.69MB（pompeii）。这是全站最大的响应，含格子级状态。

顶层键（22 个）：

`schema`, `client_contract`, `benchmark_id`, `benchmarks`, `combos`, `tier_windows_usd`, `token_pricing`, `tasks`, `cells`, `baseline_generated_at`, `discrimination_generated_at`, `discrimination_method`, `reopen_after_hours`, `rolling_window`, `scoring_mode`, `score_label`, `pass_threshold`, `benchmark_policy_version`, `idle_mult_credit_hours`, `idle_mult_credit_before`, `online_volunteers`, `k`

**`client_contract`** — 客户端能力协商契约：

```json
{
  "schema": "dradar-client-contract-v1",
  "harness_capabilities": {
    "dsh": ["dsh-minimal-deepseek-v4-flash-artifact-v6", "dsh-minimal-deepseek-v4-pro-artifact-v6",
            "dsh-minimal-deepseek-v4.1-flash-artifact-v6",
            "dsh-minimal-deepseek-v4-flash-vision-exp-pompeii-image-v2",
            "dsh-minimal-deepseek-v4-flash-vision-exp-deepswe-text-v2"],
    "claude-code": ["claude-code-5-subscription-oauth-sandbox-v1"],
    "grok": ["grok-build-4.6-subscription-oauth-concurrent-v5"],
    "kimi-code": ["kimi-code-k3-subscription-oauth-node-concurrent-v3"],
    "zcode": ["zcode-glm-5.3-family-bigmodel-coding-plan-v2"],
    "antigravity": ["antigravity-gemini-3.7-flash-subscription-oauth-sandbox-v1",
                    "antigravity-gemini-3.8-flash-subscription-1.1.27-v1"],
    "codebuddy": ["codebuddy-hy4-preview-subscription-oauth-three-effort-low-high-max-concurrent-v4"]
  }
}
```

> 实测：请求头 `X-DRadar-Capabilities`（值 `dradar-client-contract-v1` 或 `bogus`）**不改变响应**（长度均 8622499），响应里也没有协商结果头。该头目前是预留。

**`combos[]`** — 可测的 `(model, effort)` 档位（deep-swe 67 个 / pompeii 40 个）：

| model | deep-swe efforts | pompeii efforts |
| --- | --- | --- |
| `gpt-6-astra` | low, medium, high, xhigh, max, ultra | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-sol` | low, medium, high, xhigh, max, ultra | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-terra` | low, medium, high, xhigh, max, ultra | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-luna` | low, medium, high, xhigh, max | low, medium, high, xhigh, max |
| `gpt-5.5` | high, xhigh | high, xhigh |
| `deepseek-v4-flash` | max, high | — |
| `deepseek-v4.1-flash` | max, high | — |
| `dsh-deepseek-v4-flash` | max, high | — |
| `dsh-deepseek-v4.1-flash` | max, high | — |
| `dsh-deepseek-v4-flash-vision-exp` | max, high | high, max |
| `grok-4.6` | high, medium, low, xhigh | high, medium, low, xhigh |
| `k3` | low, high, max | low, high, max |
| `glm-5.3` | low, high, max | — |
| `glm-5.3-flash` | low, high, max | low, high, max |
| `gemini-3.7-flash` | low, medium, high | low, medium, high |
| `gemini-3.8-flash` | low, medium, high | — |
| `hy4-preview` | max, high, low | — |
| `claude-sonnet-5` | low, medium, high, xhigh, max | — |
| `claude-opus-5` | low, medium, high, xhigh, max | — |

> 站点前端 `radar-report.js` 的 `MODELS` 常量只有 5 个（`gpt-6-astra` / `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` / `gpt-5.5`，带展示色与 orb），**不覆盖后端 19 个模型** —— 插件应以 `combos` 为唯一事实源。`EFFORTS = ["low","medium","high","xhigh","max","ultra"]`。

**`tasks[]`**：

```json
{
  "id": "abs-module-cache-flags",
  "title": "Harden module loading, cache introspection, and script flags",
  "language": "go",
  "repo": "https://github.com/abs-lang/abs",
  "category": "enhancement",
  "discrimination": {
    "raw_score": 0.110518, "model": 10.0, "effort": 7.4, "monotonic": 3.0,
    "config": 11.1, "threshold": 21.3, "confidence": 0.693, "cells": 56, "samples": 138, "score": 79.7
  }
}
```

- deep-swe 语言分布：`go 34 / python 34 / typescript 34 / rust 5 / javascript 5`；类别：`feature_request 105 / bugfix 4 / enhancement 3`。
- pompeii 任务额外带 `fragment_count`（实测 2–44，众数 6）与 `metric: "adjacency_f1"`，`language: "vision"`，`category: "visual-reconstruction"`，`repo: https://zenodo.org/records/15800029`。
- `discrimination_method = "task-discrimination-v2"`（题目区分度模型，用于选题推荐与难度排序）。

**`cells`（dict，非数组！）** 键 = `"<task_id>|<model>|<effort>"`：

```json
{
  "abs-module-cache-flags|gpt-6-astra|low": {
    "st": "cooldown",
    "n": 2, "p": 1, "score_sum": 1.0, "rate": 0.5,
    "total_n": 2, "total_p": 1, "total_score": 1.0,
    "base_mult": 1.0, "wasteland": false, "wasteland_multiplier": 1.0, "mult": 1.0,
    "last_graded_at": "2026-09-11T05:13:50+00:00", "last_points": 2.0,
    "cost": 0.82, "min": 10, "src": "task-level-fallback",
    "ran_by": [
      {"login":"Kurayuri","avatar_url":"https://avatars.githubusercontent.com/u/45909519",
       "passed":false,"score":0.0,"graded_at":"2026-09-11T05:13:50+00:00",
       "points_base":1.0,"points_multiplier":2.0,"duration_sec":285.6,
       "actual_cost_usd":1.185964,"cost_source":"tokens",
       "token_pricing_version":"official-api-equivalent-gemini38-2026-09-10-v16","cost_complete":true},
      {"nickname":"炖花白","avatar_seed":"54779ea59aa547e32165f1bc",
       "passed":true,"score":1.0,"graded_at":"2026-09-05T02:55:08+00:00",
       "points_base":1.0,"points_multiplier":3.0,"duration_sec":295.1,
       "actual_cost_usd":1.086528,"cost_source":"tokens",
       "token_pricing_version":"official-api-equivalent-astra-2026-09-05-v15","cost_complete":true}
    ]
  }
}
```

实测分布（deep-swe 7504 格）：`st` = `open 7047 / cooldown 448 / leased 7 / running 2`；`src` = `measured 2632 / cross-model-median-shape 2102 / task-level-fallback 1344 / official-shape 1090 / null 336`。

> `ran_by` 条目**两种身份二选一**：有 GitHub 绑定的给 `login` + `avatar_url`，未绑定的给 `nickname` + `avatar_seed`。解析时两种都要兜。
> `src` 表示数值来源：`measured` 是实测，其余是估算/回退 —— 展示时必须能区分（上游方法论页明确「不用回退值制造确定性」）。

**`tier_windows_usd`**：`{"plus": 82.486, "pro-5x": 412.43, "pro-20x": 1649.72}` —— 各订阅档的 7 天额度窗口折算美元。

**`token_pricing`**（版本 `official-api-equivalent-gemini38-2026-09-10-v16`，`observed_at` 2026-09-10）：

```json
{
  "version": "...v16", "observed_at": "...", "effective_at": "2026-08-31T08:00:00+08:00",
  "basis": "Official API-equivalent USD per 1M tokens; ...（长文案，逐家说明取价口径与例外）",
  "source": "https://www.anthropic.com/news/claude-sonnet-5; https://developers.openai.com/api/docs/pricing; ...",
  "usd_per_million": {
    "gpt-6-astra": {"reference_band":"short_context","context_threshold_input_tokens":272000,
                    "requires_cache_write_usage":true,
                    "bands":{"short_context":{"input":10.0,"cached_input":1.0,"output":50.0,"cache_write":12.5},
                             "long_context":{"input":20.0,"cached_input":2.0,"output":75.0,"cache_write":25.0}}},
    "claude-sonnet-5": {"input":2.0,"cached_input":0.2,"output":10.0},
    "claude-opus-5":   {"input":5.0,"cached_input":0.5,"output":25.0},
    "gpt-5.6-sol":     {"input":4.0,"cached_input":0.4,"output":20.0},
    "gpt-5.6-terra":   {"input":2.0,"cached_input":0.2,"output":12.0},
    "gpt-5.6-luna":    {"input":0.2,"cached_input":0.02,"output":1.2},
    "gpt-5.5":         {"input":5.0,"cached_input":0.5,"output":30.0},
    "deepseek-v4-flash": {"reference_band":"peak",
      "schedule":{"timezone":"Asia/Shanghai","default_band":"off_peak",
        "periods":[{"band":"peak","start":"09:00","end":"12:00","weekdays":[0,1,2,3,4]},
                   {"band":"peak","start":"14:00","end":"18:00","weekdays":[0,1,2,3,4]}]},
      "bands":{"off_peak":{"input":0.22,"cached_input":0.007,"output":0.66},
               "peak":{"input":0.44,"cached_input":0.014,"output":1.32}}}
  }
}
```

> 价格有**分档**（`bands`）与**分时**（DeepSeek 工作日高峰）两类；`basis` 里逐家标注了例外（如 GLM 的 Coding Plan 档位与 Flash 五折促销被刻意排除）。插件**不应自行用这张表算钱** —— 上游已算好 `actual_cost_usd` / `average_price_usd`，价格表只用于「展示计费口径」。

**其它元字段**：`baseline_generated_at`（2026-09-13T09:01:19Z）、`discrimination_generated_at`（2026-09-12T20:10:55Z）、`reopen_after_hours: 60.0`（同一格 60 小时后可重跑）、`idle_mult_credit_hours: 6.0`、`idle_mult_credit_before: "2026-07-26T17:03:52+00:00"`、`online_volunteers: 2`、`k: 1`。

---

### 2.4 `GET /api/v1/iq-history`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `benchmark` | string? | 缺省 `deep-swe`；**参数生效**（deep-swe 124 series / 1303622B，pompeii 76 series / 733600B） |

返回 **dict**：key = series 名，value = 时间序列数组。每条 series 168 点（逐小时）。

**series 名有三种形态**（deep-swe 实测 124 条 = 62 裸 + 62 `latest:`）：

| 形态 | 例 | 口径 |
| --- | --- | --- |
| `模型` | `gpt-5.6-sol` | 跨档位合并，全窗口样本 |
| `模型@effort` | `gpt-5.6-sol@low` | 单档位 |
| `latest:模型` / `latest:模型@effort` | `latest:gpt-5.6-sol` | **「最近一次有效运行」口径**（样本量明显更小） |

```json
{
  "gpt-5.6-sol":        [{"ts":"2026-09-06T10:00:03+00:00","score":96.7,"n":2016}, ...],
  "gpt-5.6-sol@low":    [{"ts":"2026-09-06T10:00:03+00:00","score":81.2,"n":336}, ...],
  "latest:gpt-5.6-sol": [{"ts":"2026-09-06T10:00:03+00:00","score":97.3,"n":672}, ...]
}
```

实测对照：同一时刻 `gpt-5.6-sol` = 96.7（n=2016），`latest:gpt-5.6-sol` = 97.3（n=672）——**同一模型两套口径给出不同分数，混画会得到自相矛盾的曲线**。

- `score` 是 **IQ 分（0–150）**；`n` 是该时刻的有效样本数。
- 裸模型名是**跨档位合并**口径；`@effort` 是单档位口径；`latest:` 前缀是**另一套窗口**。三者不可混。
- 体积：1.30MB（deep-swe）/ 733.6KB（pompeii）；无 ETag，`max-age=60`。

---

### 2.5 `GET /api/v1/radar-insights`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `benchmark` | string? | 实测 deep-swe 与 pompeii 均 200（**响应不同**：31510B vs 44855B） |

```json
{
  "schema": 1,
  "mode": "rolling_equal_per_task",
  "recommendation_mode": "comprehensive_weighted_mean",
  "generated_at": "2026-09-13T09:09:40+00:00",
  "source_updated_at": "2026-09-12T16:37:23+00:00",
  "software_source_updated_at": "2026-09-13T09:03:10+00:00",
  "visual_source_updated_at": "2026-09-12T16:37:23+00:00",
  "comprehensive_points": [
    {"model":"gpt-6-astra","effort":"low","iq":109.19,"software_iq":98.51,"visual_iq":135.71,"samples":188}
  ],
  "recommendations": [
    {"key":"daily_development","title":"日常开发","rule":"...","items":[{...}]},
    {"key":"hard_problems","title":"难题攻坚","rule":"...","items":[{...}]},
    {"key":"background_automation","title":"后台自动化","rule":"...","items":[{...}]},
    {"key":"lobster_tasks","title":"跑龙虾类任务","rule":"...","items":[{...}]}
  ],
  "degradation_alerts": {"rule":"...","items":[]},
  "benchmark_id": "deep-swe"
}
```

**这是唯一把「编程」与「视觉」两频道合成单一 IQ 的端点**（`comprehensive_weighted_mean`）—— 插件做「谁最聪明」类回答时应优先用它，并明确标注这是上游的加权口径，不是单一 benchmark 分数。

**推荐组 `items[]` 字段**：

```json
{"model":"gpt-6-astra","effort":"low","iq":109.19,"passed":136.85726418227966,"samples":188,
 "average_cost_usd":1.921132,"cost_samples":188,
 "average_duration_minutes":8.76,"duration_samples":188,
 "combined_cost_index":128.016,
 "rule":"原始 IQ ≥90，不再分档；在同一候选池中分别以平均耗时和费用与耗时综合成本为基准；更高 IQ 至少领先 1.5 IQ，且每领先 1 IQ 可接受最多 2% 对应指标溢价；去重后取 2 个。",
 "trend_48h":[{"timestamp":"2026-09-11T10:00:18+00:00","iq":99.6,"samples":131}, ...]}
```

**四组推荐规则原文**（用于向用户解释「为什么推荐它」）：

| key | title | rule |
| --- | --- | --- |
| `daily_development` | 日常开发 | 原始 IQ ≥90，不再分档；在同一候选池中分别以平均耗时和费用与耗时综合成本为基准；更高 IQ 至少领先 1.5 IQ，且每领先 1 IQ 可接受最多 2% 对应指标溢价；去重后取 2 个。 |
| `hard_problems` | 难题攻坚 | 所有有实测数据的档位中，按 IQ 从高到低取 2 个；IQ 相同时按模型顺序与综合成本排序。 |
| `background_automation` | 后台自动化 | 成本优先：整数 IQ ≥80，不设上限，以平均费用最低者为基准；更高 IQ 至少领先 1.5 IQ，且每领先 1 IQ 可接受最多 2% 费用溢价；符合时优先 IQ 最高，依次取 2 个。 |
| `lobster_tasks` | 跑龙虾类任务 | IQ ≥55，不设上限，按费用与耗时综合成本最低取 2 个。 |

**`degradation_alerts`（降智预警）** rule 原文：

> 不含 DeepSeek；每个模型档位只与自身历史比较：每格最近三次结果必须达到以下任一固定阈值：当前 IQ 低于 24 小时均值至少 7 IQ，或低于 48 小时均值至少 9 IQ；同时最近 12 小时仍在下降。按相对门槛的均值差严重度排序，最多返回 4 个，不足 4 个不补位。

实测 `items` 为空（当时无预警）。**注意：DeepSeek 系列被排除在预警之外**，插件不应自行给 DeepSeek 算预警。

---

### 2.6 `GET /api/v1/intelligence-efficiency`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `benchmark` | string? | **参数生效**：deep-swe 31677B / 67 point（`Pass rate`），pompeii 19679B / 40 point（`Adjacency F1`） |

```json
{
  "schema": 3,
  "mode": "equal_latest_3",
  "source_updated_at": "2026-09-13T09:12:43+00:00",
  "runs_24h_total": 242, "runs_48h_total": 861, "runs_total": 45323,
  "points": [{
    "model":"gpt-6-astra","effort":"low",
    "passed":88,"total":134,"iq":98.51,
    "average_price_usd":1.978303,"average_price_usd_by_band":null,
    "average_minutes":8.7,"combined_cost_index":129.37,
    "average_agent_steps":25.533834586466167,"agent_steps_samples":133,
    "average_total_tokens":976076.1119402985,"token_samples":134,
    "cache_hit_rate":0.9446170002630452,"cache_token_samples":134,
    "runs_24h":0,"runs_48h":3,"runs_total":135,
    "source_updated_at":"2026-09-12T03:58:59+00:00"
  }],
  "benchmark_id":"deep-swe","scoring_mode":"binary-majority","score_label":"Pass rate"
}
```

- `combined_cost_index` = 费用与耗时的综合成本指数（越小越省）。
- `average_price_usd_by_band` 只在价格分档/分时模型上有值，实测为 `null`。
- **每个 `point` 自带 `source_updated_at`**，与顶层 `source_updated_at` 可能不同（该档位数据可能是几天前的）—— 展示「数据时间」时必须用 point 级的。
- **连续制频道里 `passed` 是浮点**：pompeii 实测 `{"model":"gpt-6-astra","effort":"low","passed":49.79059751561299,"total":55,"iq":135.79,...}` —— 不是「过了 49.79 题」，而是 F1 加权和。**按 `scoring_mode` 决定如何展示**。

---

### 2.7 `GET /api/v1/model-metrics`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `model` | string? | 全部 | 未知 model → **200 且 `points: []`**（不报错） |
| `effort` | string? | 全部 | |
| `benchmark` | string? | `deep-swe` | |

```json
{
  "schema": 1, "mode": "latest_valid_per_task",
  "source_updated_at": "2026-09-12T03:58:59+00:00",
  "runs_24h_total": 0, "runs_48h_total": 3, "runs_total": 135,
  "points": [{
    "model":"gpt-6-astra","effort":"low",
    "average_agent_steps":27.256880733944953,"agent_steps_samples":109,
    "average_total_tokens":1078396.7181818183,"token_samples":110,
    "cache_hit_rate":0.9475937143209523,"cache_token_samples":110,
    "runs_24h":0,"runs_48h":3,"runs_total":135
  }],
  "benchmark_id":"deep-swe","scoring_mode":"binary-majority","score_label":"Pass rate"
}
```

> `mode = latest_valid_per_task`（每题取最近一次有效运行），**与主榜的 `equal_latest_3` 不同口径**。做「平均步数/token/缓存命中率」类回答时用这个端点，且必须把 `mode` 一并透出。

---

### 2.8 `GET /api/v1/events`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `n` | int | 30 | 非整数 → **422** FastAPI 校验体 |
| `benchmark` | string? | `deep-swe` | |

```json
{
  "benchmark_id":"deep-swe","scoring_mode":"binary-majority","score_label":"Pass rate","pass_threshold":1.0,
  "events":[{
    "graded_at":"2026-09-13T09:12:43+00:00","passed":false,"score":0.0,
    "task_id":"pest-character-class-coalescing","model":"grok-4.6","effort":"high","harness":"grok-build",
    "cost_usd":2.83,"cost_source":"api_equivalent_tokens",
    "cost_is_estimate":false,"cost_is_fallback_estimate":false,"cost_is_api_equivalent":true,
    "points":16.97,"points_deferred":false,
    "login":"F1shhhhh","avatar_url":"https://avatars.githubusercontent.com/u/131987437"
  }]
}
```

「实时判分流水」，适合做「刚刚发生了什么」类回答。注意 `cost_is_estimate` / `cost_is_fallback_estimate` / `cost_is_api_equivalent` 三个布尔要透传（上游方法论页强调「证据不足显示缺失」）。

---

### 2.9 `GET /api/v1/suggest`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `n` | int | 5 | 实测 `n=0` 仍返回数据（未按 0 截断） |
| `replace_unstarted` | bool | false | |
| `benchmark` | string? | `deep-swe` | |
| `harness` | enum? | — | `codex` / `claude-code` / `dsh` / `kimi-code` / `zcode` / `grok` / `antigravity` / `codebuddy`（**此处是 `grok`**） |

```json
{
  "benchmark_id":"deep-swe",
  "cells":[{
    "task_id":"yaegi-go-embed-directives","benchmark_id":"deep-swe",
    "model":"gpt-5.6-sol","effort":"medium",
    "agent":"codex","agent_version":"0.154.0",
    "est_minutes":18,"est_quota_pct":3.7,
    "tier_windows_usd":{"plus":82.486,"pro-5x":412.43,"pro-20x":1649.72}
  }],
  "holding":0,"replaceable_unstarted":0,"protected_started":0
}
```

**这是跑题侧的「选题建议」端点**，插件只读消费时可用来回答「现在跑哪题最划算」——但注意它返回的是**待认领格子**，不是成绩。

---

### 2.10 `GET /api/v1/quota`

```json
{
  "quota_window":"7d",
  "tier_windows_usd":{"plus":82.486,"pro-5x":412.43,"pro-20x":1649.72},
  "source":"super-account-app-server-measurement",
  "measured_at":"2026-08-09T11:49:26.993173+00:00",
  "updated_at":"2026-08-09T11:49:26.993189+00:00",
  "evidence":{"cost_usd":560.9064,"delta_used_pct":34.0,"task_count":711}
}
```

> **注意时间戳是 2026-08-09，比其它端点旧一个多月** —— 这是一个低频更新的标定端点（`source` 写明是「超级账号 App 服务端测量」），不是实时额度。用它解释「订阅档位 7 天窗口大概值多少钱」。

---

### 2.11 其它公开端点

| 端点 | 说明 |
| --- | --- |
| `GET /api/v1/github/config` | `{"client_id":"Ov23liQFleSUsxrCLTAq"}` — GitHub OAuth App 的公开 client_id |
| `GET /install.sh` | `text/x-sh`，955B。内容要点：`DRADAR_REPO_URL="git+https://github.com/SecurityMind/dradar"`、`SERVER_URL="https://api.codexradar.com"`；引导用 `uvx --refresh --from <repo> dradar login --server <url> --token <t>` → `dradar doctor` → `dradar go` |
| `GET /api/v1/avatar/{seed}.svg` | 生成式头像；seed 不存在 → **404** `{"detail":"avatar not found"}` |
| `GET /api/v1/benchmark-bundles/{benchmark_id}` | **需 Bearer**（401）；pompeii 的题目包 63.8MB tar.gz |
| `GET /` | 服务根，返回一个内嵌 CSS 的 HTML 看板（253KB，含大表与贡献者表）——**不是 API**，但可作为「无 JS 降级视图」参考 |
| `GET /api/private/v1/intelligence-efficiency` | 私有变体，**未实测** |

---

## 3. 写侧端点清单（插件不使用，仅登记）

以下端点均来自 `/openapi.json`，**全部未实测**。登记目的是界定边界：插件为只读消费方，不实现这些。

| 分组 | 端点 | 请求体 schema |
| --- | --- | --- |
| 注册/身份 | `POST /api/v1/register`（form `nickname`）、`POST /api/v1/rename`（form `nickname`）、`POST /api/v1/privacy`（form `hide`） | `Body_register_*` / `Body_rename_*` / `Body_set_privacy_*` |
| GitHub | `POST /api/v1/github/link`（form `access_token`）、`POST /api/v1/github/whoami`（form `access_token`）、`GET /api/v1/github/oauth/callback?code=` | `Body_github_*` |
| Run Plan | `POST /run-plans/exchange`、`/start`、`/progress`、`/stop`、`/renew`、`/reinvite`、`GET /run-plans/identity` | `RunPlanExchangePayload`、`RunPlanStartPayload`（`schema_version`+`plan_id`+`logical_session_id`，可选 `concurrency_mode: auto\|fixed`、`concurrency 1..40`、`decision: join_existing\|recover_stale`、`decision_token`）、`RunPlanProgressPayload`、`RunPlanStopPayload`（`scope: this_device\|all_devices`）、`RunPlanRenewPayload`、`RunPlanReinvitePayload`、`RunPlanOptionsPayload`（`refill_enabled`/`refill_to`/`max_tasks`/`locale: zh-CN\|en-US`） |
| 认领 | `GET /api/v1/assignment`、`POST /api/v1/assignment/claim`（form：`task_id`,`model`,`effort`,`benchmark_id`,`tier: plus\|pro-5x\|pro-20x`,`refill_campaign_id`）、`POST /api/v1/assignments/claim-batch`（JSON `ClaimBatchPayload`：`cells[1..40]` of `{task_id,model,effort}` + `harness` + `tier` + `replace_unstarted` + `request_id`）、`POST /api/v1/assignments/release`（form `assignment_ids`/`release_all`/`force`/`request_id`） | 见左 |
| 会话 | `POST /api/v1/assignment/started`（form `assignment_id`,`session_id`,`worker_event_id`）、`/stopped`（form `assignment_id`,`defer_seconds`,`session_id`,`owner_epoch`,`failure_kind`,`failure_diagnostic`）、`/checkout`（form `exclude_assignment_ids`,`session_id`,`prepare_only`,`batch_id`,`benchmark_id`）、`/checkpoint/pause\|resume\|discard`（无体） | `Body_assignment_*` |
| Runner | `POST /api/v1/runner/heartbeat`（`HeartbeatPayload`：`protocol_version 2\|3`、`client_version`、`session_id`、`batch_id`、`seq`、`phase: preparing\|building\|queued\|running\|uploading\|paused`、`active_assignment_id`、`resume_generation`、`owner_epoch`…）、`POST /runner/close`（`SessionClosePayload`：`session_id`,`seq`,`reason: completed\|paused\|interrupted\|error`）、`POST /runner/flight-events`（`FlightEventsPayload`：`events[1..100]` of `FlightEventPayload`，`schema_version:"dradar.flight_event.v1"`、32 字符 `event_id`/`client_id`、`seq`、`event_type`、`component`、`actor`）、`GET /runner/bootstrap-v1/{content_sha256}.py`、`POST\|GET /runner/failures` | 见左 |
| 提交 | `POST /api/v1/submissions`（**multipart**：`assignment_id`*, `nonce`*, `session_id`, `owner_epoch`, `resume_generation`, `upload_intent_id`, `client_meta`（默认 `"{}"`）, `outcome`（默认 `completed`）, `patch`, `trajectory`, `trajectory_bundle`, `result`）、`POST /submission-upload-intents`（form）、`POST /submission-upload-salvage/rebind`（form） | `Body_submit_*` |
| 补货 | `POST /refill-campaign/configure`（`batch_id`,`benchmark_id`,`harness`,`model`,`effort`,`refill_to 1..40`,`max_tasks 1..10000`）、`GET /refill-campaign/status?batch_id=`、`POST /refill-campaign/stop`（`batch_id`,`reason`） | `RefillCampaign*` |
| 其它 | `GET\|POST /api/v1/feedback`（`FeedbackPayload`：`kind: bug\|suggestion`、`title 3..120`、`body 10..4000`、`page_url`）、`GET /mailbox/session`、`POST /integrations/cup/issue\|redeem`、`POST /integrations/pin/github`、`GET /my-cells`、`GET /my-submissions?limit=&cursor=&period=` | 见左 |

---

## 4. 错误语义速查

| 情形 | HTTP | 响应体 |
| --- | --- | --- |
| 未知 benchmark | 404 | `{"detail":"'unknown benchmark: nope'"}`（注意 detail 里带单引号） |
| 参数类型错 | 422 | FastAPI `HTTPValidationError`：`{"detail":[{"type":"int_parsing","loc":["query","n"],"msg":"...","input":"abc"}]}` |
| 缺 token | 401 | `{"detail":"missing bearer token"}` / `{"detail":"missing plan bearer token"}` |
| 头像不存在 | 404 | `{"detail":"avatar not found"}` |
| 未知 model | 200 | `points: []`（**不报错**） |
| `view` 任意值 | 200 | 与默认同响应（未校验） |

---

## 5. 上游口径速查（来自站点方法论页，逐字要点）

- **IQ**：DeepSWE 用任务通过率、庞贝频道用平均 F1，**百分比 ×1.5 映射到 0–150 分**。
- **样本量**：表示当前口径内有效实测数量；**样本不足会明确标记**，与稳定结果区分。
- **时间**：来自有效运行的**实际耗时**，不使用厂商宣传的理想速度。
- **API 等价成本**：用完整 token 用量与页面当前说明的官方 API 价段重算；证据不足显示「–」。**不是订阅服务商实际扣费**。
- **最近样本口径**：主榜每格 IQ 默认用**最近 3 次有效运行**并让**任务等权**；某些公开内测频道可能使用不同窗口，以实时卡片下方的口径文字为准。
- **引用要求**：记录模型、档位、运行工具、频道、样本量和查看日期；**不把 DeepSWE 与庞贝分数直接混合**。
- **复验机制**：志愿者本机跑题 → 客户端上传候选补丁与运行证据（上传前扫密钥）→ **服务端在干净隔离环境重跑测试/隐藏评分器** → 只有完成判分且符合统计条件的有效样本进入聚合。**自报结果不算数**。
- **庞贝评分**：86 道正式题，每题独立算 F1 再等权平均（Macro-F1）；模型只提交能确认的**无向直接邻接边**（A–B 与 B–A 同一条，间接相连不计）；P/R/F1 定义按标准。
