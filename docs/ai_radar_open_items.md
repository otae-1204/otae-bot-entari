# AI 智商雷达插件 · 开放问题与未验证事项

> 本文只记录**没查清 / 没验证 / 需要决策**的事，不含结论。
> 每项标注：`未验证`（没做过实验）、`推断`（有依据但未证实）、`待决策`（需要用户拍板）。

---

## A. 许可与合规（**优先级最高，实施前必须确认**）

### A1. 上游是否允许第三方长期只读消费 —— `未验证`（**实施前必须先确认**）

已核实的事实（2026-09-13，逐项实测）：

| 检查项 | 结果 |
| --- | --- |
| 站点 `robots.txt` | `User-agent: * / Allow: /` + Sitemap —— 只约束爬虫，**不构成 API 使用许可** |
| 官方仓库许可证 | **`codex-radar/dradar` 的 `license` 为 `null`**（`GET /repos/codex-radar/dradar/license` → 404）。即**没有任何开源许可证**，不能据此主张「代码开源 ⇒ 数据可自由消费」 |
| API 条款字段 | `openapi.json` 无 `termsOfService` 字段 |
| 站点条款页 | `/terms`、`/api/terms` 均返回 SPA 兜底 HTML（非条款文档）；站点无 terms 页 |
| 隐私页 | `/privacy/` 存在（更新于 2026-09-01），只覆盖 Cloudflare / AdSense / 本地存储，**无 API 消费条款** |
| CORS | 只回显 `codexradar.com` 自有域 → **推断**上游预期消费方是自家站点与官方客户端 |
| 事实上的公开性 | 多个端点**免鉴权 200**，且 CLI 文档称 `/table` 是「公开快照，只查看不占位」 |

- **结论**：上游**事实上**允许匿名只读（端点在无 token 时返回 200，且官方客户端文档把 `/table` 描述为公开快照），但**没有任何书面许可**，也没有许可证可援引。
- **风险**：若上游把插件流量视为滥用，可能加限流或封禁。插件侧自律建议（已写入 framework）：严格按上游 `Cache-Control` 起跳的 TTL、单一 User-Agent 标识、不做高频轮询、不在无人使用时后台定时拉取、不代跑题不写数据。
- **待用户决策**：是否直接上只读消费，或先向上游作者询问。

### A2. 生产限流策略 —— `部分已确认`

**已确认（官方客户端源码，即上游自己的预期负载形态）**：`src/dradar/api_client.py` 的策略常量与算法——

| 常量 / 行为 | 值 |
| --- | --- |
| `_RATE_LIMIT_RETRIES` | `5`（即最多 6 次尝试） |
| `Retry-After` 取值 | 解析为 float；解析失败用默认值；**clamp 到 `[1.0, 60.0]` 秒** |
| 退避等待 | `clamp(retry_after) + uniform(0, min(1.0, retry_after * 0.1))` —— 抖动防「20 worker 同时唤醒再造 429 羊群」 |
| 503 `deployment_maintenance` | **不 clamp**（维护栅栏是服务端承诺，提前重试会破坏它），由 360s 总重试预算裁决 |
| 客户端超时 | `httpx.Timeout(30.0, write=None, read=120.0)`；遥测 POST 单独用 3s 超时 |
| 连接级重试 | `httpx.HTTPTransport(retries=2)`，仅重试**连接建立**失败（不重发已发出的请求，故对 claim/submit 无重复副作用）；**检测到代理时禁用**该 transport |

- 官方文档 `HARNESS_INTEGRATION_BEST_PRACTICES.md` 明确提到限流桶的存在，且记录过「网页金丝雀在模型启动前被 429 拒绝」——即 **429 真实存在**，不是理论风险。
- **仍未测（我们的匿名只读路径）**：突发并发、大 payload（`/table` 8.6MB）连拉、长时间持续拉取；服务端未公开任何匿名配额明文。
- 实测 15 次连打 `/benchmarks` 全部 200、无 rate-limit 头、无 429 —— **只说明当时没触发**。
- **对插件的含义**：不要自己发明退避参数，直接对齐上表（5 次重试 + 尊重 `Retry-After`、clamp 1–60s、加抖动）。

### A3. 官方客户端 `SecurityMind/dradar` 的实际请求行为 —— **已确认**

- `https://github.com/SecurityMind/dradar` → **HTTP 301 → `https://github.com/codex-radar/dradar`**，是**同一仓库改名**（非两个项目）；两处 `raw.githubusercontent` 路径仍返回相同字节。`install.sh` 里的 `DRADAR_REPO_URL` 仍写旧名，属未更新的历史值。
- 仓库只含**客户端 CLI**（`dradar` v0.5.200，Python，159 stars，无 LICENSE）；**服务端（调度/判分/榜单）不在仓库内**。
- 客户端请求头：`X-DRadar-Client-Version: <version>`、`X-DRadar-Capabilities: <逗号分隔>`、以及（有 token 时）`Authorization: Bearer <drt_…|drp_…>`。
- **重要**：CLI 的自定义头**不是访问公开端点所必需的**——我们的匿名只读插件**不应伪造 `X-DRadar-*` 头**（伪装成官方客户端既不诚实也无必要）。若要标识自己，用**自有 UA**（见 framework 的合规节）。
- 端点覆盖：CLI 使用 31 个 `/api/v1/*` 路径；其中我们关心的公开读端点（`/table`、`/benchmarks`、`/leaderboard`、`/iq-history`、`/intelligence-efficiency`、`/events`、`/suggest`）**都在 OpenAPI 中且免鉴权**。
- 第三方先例：`MorphieEndless/codex-radar-mcp`（MIT，0 stars）已在消费**网站侧** API（`codexradar.com/api/*`，见 B10），其自述「与官方无隶属/合作关系、数据版权归源站、请勿高频请求」。**这是先例，不是授权**。

---

## B. 数据口径未澄清项

### B1. `/intelligence-efficiency` 的 `benchmark` 参数 —— **已确认生效（曾误判）**

- ~~首轮实测两频道返回完全相同长度（31677B），推断参数被忽略~~ → **该推断已被复测推翻**。
- 复测：deep-swe 31677B / 67 point / `Pass rate`；pompeii-adjacency 19679B / 40 point / `Adjacency F1`。参数**确实生效**。
- 首轮相同长度的原因：**Cloudflare 缓存**（`s-maxage` 命中），不是参数无效。
- **教训**：对拍上游时必须带 cache-buster（如加无用 query 参数）或等 TTL 过期，否则会把缓存命中误读成「参数不生效」。

### B2. `/iq-history?benchmark=pompeii-adjacency` —— **已确认生效（曾误判）**

- ~~推断参数被忽略~~ → **推翻**。复测：deep-swe 124 series / 1303622B，pompeii 76 series / 733600B，且 pompeii 的 series 是 `gpt-5.6-sol@high/max/...` 等**视觉频道**数据。参数生效，同样受首轮缓存误导。
- **新发现的未文档化口径**：series 名有三种形态——`模型`（跨档位合并）、`模型@effort`（单档位）、**`latest:模型` / `latest:模型@effort`（「最近一次有效运行」口径）**。deep-swe 124 条 = 62 裸 + 62 `latest:`；pompeii 76 = 38 + 38。
- 实测同一时刻 `gpt-5.6-sol` = 96.7（n=2016）而 `latest:gpt-5.6-sol` = 97.3（n=672）—— 两套口径分数不同，**混画会自相矛盾**。
- 未验证：`latest:` 口径的精确定义（推断为「每题取最近一次有效运行」，与 `/model-metrics` 的 `latest_valid_per_task` 可能同源）。

### B3. `/leaderboard?view=` 的语义 —— **已确认：完全无效**

- `total` / `month` / `history` / `nope` 四种取值与默认响应**逐字节相同**（均 1602389B，sha256 前 16 位 `744e505fea1d6aa6`）。
- **结论**：`view` 是前端展示开关，后端不做区分；月榜/总榜数据已全部内嵌在 `contributors[].month_points` / `points` 与 `month` / `history` 元字段里。实现时**不要传 `view`**。

### B4. `degradation_alerts` 的 item 字段结构 —— `未验证（当时为空）`

- 实测 `items: []`，因此**从未见过一个真实的预警条目**，其字段结构只能从 `rule` 文案反推（`current_iq` / `avg_24h` / `avg_48h` / 下降幅度）。
- 实现时必须**按存在性解析**（`get()` + 默认值），不能在 `models.py` 里假定字段名。
- 待办：等出现真实预警时再抓一次，固化 `DegradationAlert` 的字段。

### B5. `src` 各枚举值的精确语义 —— `推断`

- 实测到 5 种取值（`measured` / `cross-model-median-shape` / `task-level-fallback` / `official-shape` / `null`），但**上游未文档化其定义**，本文的释义是从命名与方法论页「不用回退值制造确定性」推断的。
- 实现时只需「`measured` 之外一律标注为估算」，**不要**对每种值写不同的用户文案。

### B6. `passed` 为浮点的加权口径 —— `推断`

- `/radar-insights` 的 `recommendation.items[].passed` 实测为 `136.85726418227966`。
- **推断**：任务等权 + 窗口加权的加权通过数。
- 未验证：具体权重算法。实现时**不要当题数展示**。

### B7. `flag_race` 与赛季 `history` 的完整结构 —— **已确认**

**`flag_race`**（实测已开奖的一期）：

```json
{"status":"won","target_usd":150000.0,"reward_points":1500.0,"current_usd":170335.57,
 "winner":{"nickname":"雷达站长","github_login":null},
 "submission":{"task_id":"psd-tools-blend-range-api","model":"gpt-5.6-sol","effort":"medium",
               "submitted_at":"2026-08-26T14:42:14+00:00"},
 "cumulative_usd":150001.57,"awarded_at":"2026-08-26T14:55:48+00:00",
 "announcement_until":"2026-09-02T14:55:48+00:00"}
```

`winner.github_login` 可为 `null`（仅昵称）——**展示时不能假定 github 字段存在**。

**`history`**：`{starts_at:"2026-07", top:10, seasons:[Season, ...]}`，实测 2 个赛季（`2026-08` 在前，`2026-07` 在后，**倒序**）。`Season` = `{label, top10[]}`，每个 `top10` 条目 11 字段：

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `nickname` / `github_login` / `avatar_url` / `avatar_seed` | string? | 身份（与 `contributors[]` 同构） |
| `points` | float | 赛季积分 |
| `tokens` | int | 赛季 token 量 |
| `folded_usd` | float | 折算成本 |
| `deepseek_api_usd` / `deepseek_api_runs` / `deepseek_api_unpriced_runs` | float/int | DeepSeek 直连部分 |
| `usd` | float | 合计（= `folded_usd` + `deepseek_api_usd`） |
| `rank` | int | 名次 |

**`month`** = `{label:"2026-09", settles_at:"2026-10-01T00:00:00+08:00"}` —— 含结算时间，可直接展示「本月榜将于 X 结算」。

### B8. `pedal_speed` 的字段全集 —— **已确认（18 字段）**

```json
{"window_minutes":60,"scope":"fleet_submitted","submitted_runs":10,"token_runs":10,
 "tokens_per_hour":61950772,"input_tokens_per_hour":61250392,
 "cached_input_tokens_per_hour":57668224,"output_tokens_per_hour":700380,
 "cache_hit_ratio":0.9415,"api_equivalent_runs":9,"api_equivalent_usd_per_hour":25.07,
 "paid_api_runs":0,"paid_api_usd_per_hour":0.0,"estimated_runs":1,"unpriced_runs":0,
 "actual_runs":10,"usd_per_hour":27.55}
```

- `scope: "fleet_submitted"` —— 口径是**全队已提交运行**，不是单模型。
- 三组并行计数：`api_equivalent_*`（API 等价成本）/ `paid_api_*`（真实付费）/ 合计 `usd_per_hour`。
- `estimated_runs` / `unpriced_runs` 是**可信度标记**——非 0 时该窗口成本含估算成分。
- 实测 `tokens_per_hour` = `input_tokens_per_hour` + `output_tokens_per_hour`（61250392 + 700380 = 61950772 ✓）。

### B9. `token_pricing.usd_per_million` 的完整模型覆盖 —— **已确认：18 个模型**

`gpt-6-astra`、`claude-sonnet-5`、`claude-opus-5`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.5`、`deepseek-v4-flash`、`deepseek-v4-flash-vision-exp`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`glm-5.3`、`glm-5.3-flash`、`k3`、`grok-4.6`、`gemini-3.7-flash`、`gemini-3.8-flash`、`hy4-preview`。

- 两种形状：扁平 `{input, cached_input, output}` 或带 `bands` + `schedule`（DeepSeek 分时、gpt-6-astra 双上下文档）。
- **注意 `deepseek-v4-pro` 有价目但没有实测档位**（不在任何 benchmark 的 `combos` 里）——**价目表 ≠ 已评测模型**，`model_catalog()` 必须以上游 `combos` 为准。
- **设计上不需要**：插件不自行算钱。

### B10. **第二套公开接口面：网站侧 `codexradar.com/api/*`** —— **已确认（此前漏掉）**

侦察初期只覆盖 `api.codexradar.com`，**漏掉了网站自身的另一套公开 JSON 接口**。实测（2026-09-13）：

| 端点 | 状态 | 体积 | 顶层键 |
| --- | --- | --- | --- |
| `GET https://codexradar.com/api/radar-insights` | 200 | 29.4KB | 与 `api.codexradar.com/api/v1/radar-insights` **键完全相同** |
| `GET /api/intelligence-efficiency-metrics` | 200 | 31.7KB | `schema/mode/source_updated_at/runs_*/points/benchmark_id/scoring_mode/score_label` |
| `GET /api/visual-spatial-reasoning` | 200 | **1.07MB** | `schema/type/mode/benchmark_id/scoring_mode/score_label/source_updated_at/runs_*/points[25]/history[153]` |
| `GET /api/model-ratings?view=public` | 200 | 3.6KB | `ok/day/timezone/refresh_seconds/updated_at/models[33]/window/window_hours/since/until/source/cached_at` |
| `GET /api/subscriber-count` | 200 | 42B | `{ok, count: 4263, source}` |

- **两套面的关系（实测）**：`radar-insights` 两处**键集合完全一致**，仅 `generated_at` 不同（`09:29:46` vs `09:19:43`）→ 是**同一数据的缓存时差**，不是两套数据。`/api/intelligence-efficiency-metrics`（31.7KB）与 `api/v1/intelligence-efficiency`（31.7KB）**同为 deep-swe 口径同源数据**。
- **网站侧独有的价值**：
  - `/api/model-ratings?view=public` 在 `api.codexradar.com` 上**没有对应端点**——33 个模型的 `{id,label,group,average,count}`，`window: rolling_24h`、`refresh_seconds: 300`、`source: public_cache`。这是**独立于 IQ 榜的另一套「模型评分」**（`average` 量纲与 IQ 不同，**不可与 IQ 混用**）。
  - `/api/subscriber-count`（`count: 4263`）也是网站侧独有。
- **`/api/private/v1/intelligence-efficiency`**（在 `api.codexradar.com` 的 OpenAPI 里）实测 **401 `{"detail":"invalid bearer token"}`** —— 私有变体，**插件不得触碰**。
- **实现建议**：主路径仍走 `api.codexradar.com`（有 OpenAPI、有稳定契约）；`/api/model-ratings` 与 `/api/subscriber-count` 若要用，须**单独标注来源为网站侧、无 OpenAPI 契约、可能随时变更**，并**不放进默认拉取集**。
- **`/api/visual-spatial-reasoning` 与 `/iq-history?benchmark=pompeii-adjacency` 的关系未验证**（同为 1MB 级、同为视觉频道）——推断同源，未证实，暂按两个端点各自处理。

---

## C. 工程决策待定

### C1. 是否实现跑题侧（认领/提交） —— `待决策`

- 本设计明确**不做**（见框架文档 §1.2）。
- 若将来要做，需要：GitHub OAuth、run-plan 会话、本地容器执行、补丁上传、`X-DRadar-Capabilities` 协商（实测该头当前无效）。成本与合规风险都远高于只读。

### C2. 是否需要订阅/推送（如每日降智预警） —— `待决策`

- 需要定时任务 + 订阅存储 + 去重，属新阶段。
- 风险：定时拉取会与 A1/A2 的合规担忧冲突（无人使用时仍在打上游）。

### C3. 输出形态：纯文本 vs 后续加卡片 —— `待决策`

- 用户本阶段明确「不做任何前端页面显示」，故 `formatters` 只产文本。
- 若后续要加图卡，应复用仓库既有渲染链路（`otae_bot/infrastructure/rendering`），但需注意 `screenshot_web_element` 的 Playwright 依赖与 SSL 上下文开销。

### C4. 别名表放在代码还是配置 —— `待决策`

- 现状：`model_catalog()` 从上游 `combos` 取权威清单，别名表只做口语映射。
- 待定：别名表写死在 `service.py` 还是放 `configs/` 便于热改。

### C5. `tasks()` 是否值得复用 `/table` —— `待决策（性能权衡）`

- 复用可省一次 8.6MB，但任何「按题查」都会把 8.6MB 拉进缓存。
- 备选：先只支持「按 `task_id` 直查」而不提供「题目清单」，避免首次拉取成本。
- 未测：`/table` 在 8.6MB 下的实际耗时（实测 554–1478ms，但那是带 CF 缓存的 HIT/EXPIRED 场景）。

---

## D. 已实测确认、无需再查的清单（避免重复劳动）

| 事实 | 证据 |
| --- | --- |
| 公开只读端点无需鉴权 | 11 个端点实测 200 |
| 需 Bearer 的端点返回 `missing bearer token` | 8 个端点实测 401 |
| `/table` 的 `cells` 是 dict | 实测 `cells[0]` → `KeyError: 0` |
| spec 的 200 响应无 schema | `/openapi.json` 全部 200 为 `{}` |
| `X-DRadar-Capabilities` 当前无效 | 三种取值响应逐字节等长 |
| 未知 benchmark → 404、非整数参数 → 422 | 实测 |
| 未知 model → 200 + `points: []` | 实测 |
| `usd = folded_usd + deepseek_api_usd` | 榜首数据 17762.97 + 91.32 = 17854.29 |
| IQ = 百分比 × 1.5（0–150） | `/methodology/` 原文 |
| 主榜口径 = 最近 3 次有效运行、任务等权 | `/methodology/` 原文 + `mode=equal_latest_3` |
| 降智预警不含 DeepSeek | `/radar-insights` 的 `rule` 原文 |
| 两个频道分数不可混 | `/methodology/` 原文「不把 DeepSWE 与庞贝分数直接混合」 |
| **存在两套公开接口面**（A 面有 OpenAPI / B 面无） | 两 host 实测，见 B10 |
| **两 host 的 `radar-insights` 键完全相同** | 逐键比对，仅 `generated_at` 差约 10 分钟 |
| **`/api/private/v1/*` 需鉴权** | `401 {"detail":"invalid bearer token"}` |
| **官方客户端仓库无许可证** | GitHub API `license: null`；`/license` → 404 |

---

## E. 本次侦察的产物位置（可复现）

| 产物 | 路径 |
| --- | --- |
| OpenAPI 全量 | `%TEMP%\radar\api_openapi.json`、`openapi_spec.json` |
| 端点实测摘要 | `%TEMP%\radar\probe_summary.json` |
| `/table` 两频道原始响应 | `%TEMP%\radar\deep_table_deep-swe.json`、`deep_table_pompeii-adjacency.json` |
| `/leaderboard` 两频道原始响应 | `%TEMP%\radar\deep_lb_*.json` |
| `/radar-insights` 原始响应 | `%TEMP%\radar\deep_insights.json` |
| **B 面（网站侧）实测输出** | `%TEMP%\radar\v6.txt`（五端点状态/体积/顶层键）、`v7.txt`（双 host 逐键 diff + model-ratings + subscriber-count） |
| **官方 CLI 源码副本** | `%TEMP%\radar\dradar\`（含 `src/dradar/api_client.py`、`README.md`、`docs/HARNESS_INTEGRATION_BEST_PRACTICES.md`） |
| 探针脚本 | `%TEMP%\radar\probe_api.py`、`probe_deep.py`、`probe3.py`–`probe6.py`、`verify.py`（含 `*.out.txt`） |

> 这些是**临时侦察产物**，不是仓库资产；写测试夹具时应从中裁剪 + 脱敏后再入库。
