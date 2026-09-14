# AI 智商雷达插件 · 基础框架设计

> 状态：**设计稿（未实现）**。本文只定义框架与边界，不含任何代码实现。
> 数据源：[众测雷达 / DRadar](https://deng.codexradar.com/)，后端 `https://api.codexradar.com`。
> 配套接口文档见 [`ai_radar_interface.md`](ai_radar_interface.md)、[`ai_radar_data_dictionary.md`](ai_radar_data_dictionary.md)、[`ai_radar_plugin_api.md`](ai_radar_plugin_api.md)、[`ai_radar_open_items.md`](ai_radar_open_items.md)。

---

## 1. 定位与范围

### 1.1 做什么

把「众测雷达」公开的 AI 编程模型实测数据接进 bot-entari，让群聊里可以问：

- 现在哪个模型最聪明 / 性价比最高 / 最快
- 某个模型（含推理档位）的 IQ、通过率、样本量、耗时、API 等价成本
- 某模型最近有没有「降智」（趋势、降智预警）
- 两个模型档位对比
- 某道题有哪些模型过了

### 1.2 不做什么（硬约束）

| 约束 | 说明 |
| --- | --- |
| **不做前端页面** | 本阶段不产出任何网页 / 卡片模板 / 静态页面。所有能力以接口（Python 层函数契约）形式暴露，渲染留给后续独立决策。 |
| **不实现跑题侧** | 不接 `/api/v1/assignment*`、`/api/v1/runner/*`、`/api/v1/submissions`、`/api/v1/run-plans/*`。本插件是**只读消费方**，不代用户认领/提交/判分。 |
| **不替代上游判分** | IQ / 通过率 / F1 / 成本一律原样引用上游字段，**不在插件侧重算、不二次聚合、不做跨 benchmark 混分**。 |
| **不自建数据源** | 唯一数据源是 `api.codexradar.com`；不爬站点 HTML 取数（站点 HTML 是 SEO 外壳，真值在 API）。 |

### 1.3 命名

- 插件目录：`plugins/radar/`
- 包名 / 命令根别名：`radar`、`智商雷达`、`ai雷达`
- 上游产品名：众测雷达 / DRadar（`site_name` 字段实测为 `"DRadar"`）

---

## 2. 分层架构

```
plugins/radar/
├── __init__.py        # Entari 插件入口（沿用 plugins/endfield 的 `from .handlers import *`）
├── config.py          # RadarConfig：base_url / 代理 / TTL / 默认 benchmark / 显示上限
├── errors.py          # RadarError 及错误码分类（对齐 otae_bot 的 NetworkFailure 风格）
├── provider.py        # RadarClient：唯一出网层，走共享 httpx 客户端 + 三层缓存
├── models.py          # 数据模型：Leaderboard / ModelRow / TaskRow / Cell / Insights …
├── service.py         # RadarService：查询语义、筛选、排序、对比、口径换算（IQ/通过率）
├── formatters.py      # 纯函数：把 service 结果转成结构化文本片段（无渲染、无图片）
└── handlers.py        # 命令注册（on_alconna），只做参数解析 + 调用 service + 回文本
```

### 2.1 各层职责与禁止事项

| 层 | 职责 | 禁止 |
| --- | --- | --- |
| `provider` | HTTP GET、超时、缓存、错误码归一、payload → `models` | 业务筛选、文案拼接 |
| `models` | 只读数据模型 + 单位/口径字段 | 出网、读环境变量 |
| `service` | 查询语义（谁最聪明/最便宜）、对比、排序、缺失值处理 | 出网、直接拼回复文案 |
| `formatters` | 文本片段、表格对齐、数字格式化（含「样本不足」标记） | 网络、文件 IO、渲染成图片 |
| `handlers` | 命令别名、参数校验、并发闸门、回复分段 | 直接调 `provider`、内联 HTTP |

### 2.2 与既有基础设施的复用

| 复用对象 | 路径 | 用法 |
| --- | --- | --- |
| 共享 HTTP 客户端 + TTL + 磁盘缓存 | `otae_bot/infrastructure/http/client.py` | `fetch_json(url, namespace=...)`；新增 `radar` 命名空间，走 API 池 |
| 命令注册 | `otae_bot/adapters/entari` 的 `on_alconna` | 与 `plugins/endfield/handlers.py` 同款 |
| 配置读取 | `otae_bot/config/settings.py` 的 `_env` | 注意：`.env` 加载器会把数字字符串 `json.loads` 成 int/float，数值配置必须用 `_env(name, None)` + `is None` 判空（见 `plugins/hyw/config.py` 的 `_env_int/_env_float`） |
| 并发闸门 | `plugins/hyw/handlers.py` 的全局并发上限写法 | 雷达查询同样设上限（建议 4），避免上游被群聊打爆 |

**缓存分层建议**：上游已给 `Cache-Control`（`/leaderboard` 10s、`/table` 30s、`/radar-insights` 600s），插件侧 TTL 不应短于上游，否则纯属自伤；建议 `/leaderboard` `/intelligence-efficiency` 60s、`/table` 300s、`/radar-insights` 600s、`/benchmarks` 3600s、`/events` 30s。**注意 `/table` 单次 8.6MB（deep-swe）**，必须走磁盘缓存 + 只在需要格子级数据时拉取。

---

## 3. 数据源选择：为什么只用 `/leaderboard` + `/radar-insights`

`/table` 一次 8.6MB 且含 7504 个格子的完整状态，而绝大多数群聊问题只需要模型级聚合。据此分三档：

| 档 | 端点 | 体积（实测） | 服务的问题 |
| --- | --- | --- | --- |
| 轻 | `/leaderboard` | 1.60MB | 排行榜、单模型 IQ/通过率/样本、贡献者天梯、月榜、夺旗赛 |
| 轻 | `/radar-insights` | 31.5KB | 综合 IQ、推荐组合（日常开发/难题攻坚/后台自动化）、降智预警、48h 趋势 |
| 轻 | `/intelligence-efficiency` | 31.7KB | 性价比（平均费用、平均耗时、综合成本指数、平均步数/token/缓存命中率） |
| 轻 | `/model-metrics` | 18KB | 指定 model+effort 的运行特征（步数、token、缓存命中） |
| 轻 | `/iq-history` | 1.30MB | 历史趋势曲线（124 条 series） |
| 重 | `/table` | 8.6MB / 3.7MB | 格子级：某题谁过了、题目难度、认领状态 |

**默认路径**：`/leaderboard` + `/radar-insights` + `/intelligence-efficiency` 三者已覆盖 90% 查询且合计 < 1.7MB；`/table` 仅在「按题查」「查某题难度」时按需拉取并长缓存。

### 3.1 只走 A 面：为什么不用网站侧 `codexradar.com/api/*`

上游有**两套 host 的公开 JSON 面**（详见 [`ai_radar_interface.md`](ai_radar_interface.md) §1.2）：A 面 `api.codexradar.com/api/v1/*`（有 OpenAPI、58 路径）与 B 面 `codexradar.com/api/*`（无 OpenAPI）。本插件**主路径只走 A 面**，理由：

1. **A 面有契约**（`/openapi.json`），B 面是站点前端自用面、无任何契约承诺，字段可随前端改版静默变更。
2. **A 面覆盖本插件全部查询需求**：B 面实测的 `radar-insights` / `intelligence-efficiency-metrics` 与 A 面**同源同键**（仅缓存时差），不构成新增能力。
3. **两套面 = 两套缓存/限流行为**，混用会让 TTL 与退避策略失效。

**B 面唯一值得留意的是两个 A 面没有的端点**，但**都不进默认拉取集**：

| 端点 | 内容 | 为何不默认用 |
| --- | --- | --- |
| `/api/model-ratings?view=public` | 33 模型近 24h 评分 `{id,label,group,average,count}` | `average` 与 IQ **量纲不同**，混用会误导；且无契约 |
| `/api/subscriber-count` | 订阅数 `{count}` | 与「智商雷达」核心问题无关 |

若将来确要用，必须：① 在 formatter 里**显式标注来源为网站侧面**；② 解析容忍缺字段；③ **绝不与 IQ 同图或换算**。

### 3.2 禁止面

- `/api/private/v1/*`（A 面 OpenAPI 内的私有变体）实测 `401 {"detail":"invalid bearer token"}` —— **插件不得触碰任何 private 路径，也不得携带 token 探测**。
- 写侧端点（`assignment*` / `runner/*` / `submissions` / `run-plans/*` / `feedback`）——本插件是纯只读消费方（见 §1.2）。

---

## 4. 查询语义（service 层契约概览）

完整签名见 [`ai_radar_plugin_api.md`](ai_radar_plugin_api.md)。语义要点：

1. **IQ 与通过率同源**：`iq ≈ pass_rate × 100 × 1.5`（0–150 分）。service 层必须同时给出两者与**样本量**，缺一不可——上游方法论页明确要求「同时查看样本量和趋势」。
2. **「最近 3 次有效运行、任务等权」** 是主榜口径（`mode=equal_latest_3` / `rolling_window=3`）；`/intelligence-efficiency` 用同一口径，`/model-metrics` 用 `latest_valid_per_task`（**口径不同，不可混用**，字段里 `mode` 必须透传给用户）。
3. **跨 benchmark 不可比**：`deep-swe`（通过率）与 `pompeii-adjacency`（Macro-F1）分数尺度不同，service 层禁止把两者混成一个总榜；`/radar-insights` 的 `comprehensive_points` 是上游自己做的加权（`recommendation_mode=comprehensive_weighted_mean`），插件只转发、不自造。
4. **缺失即缺失**：上游明确「证据不足就显示缺失，不用回退值制造确定性」。service 层对 `null` / 空数组一律返回「无数据」而不是 0 或默认值。
5. **档位是身份的一半**：`(model, effort)` 才是可比的键，`effort ∈ {low, medium, high, xhigh, max, ultra}`（各模型支持的档位不同，见数据字典）。
6. **旧模型对照**：`gpt-5.5` 仅 `high/xhigh` 两档，是站点刻意的「上一代对照」。

---

## 5. 错误与降级

| 情形 | 行为 |
| --- | --- |
| 上游 5xx / 网络失败 | 归一为 `RadarError(code=upstream_unavailable)`；有陈旧缓存则回陈旧数据并标注 `stale=true` + 缓存时间 |
| 未知 benchmark | 上游 404 `{"detail":"'unknown benchmark: xxx'"}` → `RadarError(code=unknown_benchmark)`，文案提示可用 id |
| 参数类型错 | 上游 422 FastAPI 校验体 → 插件侧在发请求前就本地校验，不该把 422 暴露给用户 |
| 查询无命中（未知 model） | 上游 200 + `points: []` → 返回「无该模型档位的实测数据」，不报错 |
| 数据陈旧 | 用 `source_updated_at` / `baseline_generated_at` / `pedal_speed.window_minutes` 判断并在输出里附「数据时间」 |

**不要**把上游响应体原文回显给用户（可能含内部标识）；只回结构化错误码 + 固定话术。

---

## 6. 阶段划分（每阶段均不含前端）

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P0（本阶段） | 接口文档 + 框架设计（本文） | 文档齐备，端点/字段/口径可追溯 |
| P1 | `provider` + `models` + `config` + `errors`；`/leaderboard`、`/benchmarks` 打通 | 单测用录制的 JSON 夹具；无网络也能跑 |
| P2 | `service` 查询语义 + `formatters` 文本输出；命令 `/radar 榜`、`/radar 模型 X` | 命令级测试用假 payload |
| P3 | `/radar-insights`（推荐 + 降智预警）、`/intelligence-efficiency`（性价比）、对比命令 | 口径字段透传正确 |
| P4 | `/table` 按题查询、`/iq-history` 趋势 | 大 payload 的缓存与截断策略验证 |

**风险点（P1 前需确认）**：见 [`ai_radar_open_items.md`](ai_radar_open_items.md) —— 尤其是「上游是否允许第三方长期只读消费」与「生产限流策略」两项均**未验证**。

---

## 7. 设计取舍记录

1. **只读、不做跑题侧**：跑题侧要 OAuth、设备指纹、本地容器与上传，成本远超收益，且会把插件变成「代刷分工具」，风险不对称。
2. **不缓存到插件私有格式**：直接用共享 HTTP 层的 JSON 缓存（内存 + sqlite 磁盘），避免自造第二份 schema。
3. **不做图片渲染**：用户明确要求不做前端；文本片段足以表达榜单，且规避了 `screenshot_web_element` 的 Playwright 依赖与 ~1s SSL/浏览器开销。
4. **不内置模型别名硬编码表**：站点前端 `radar-report.js` 里只有 5 个模型的展示常量（颜色/orb），而后端有 19 个模型、67 个档位——**以后端 `combos` 为准**，别名表只做「用户口语 → 上游 id」的模糊匹配，缺失就列出候选。
