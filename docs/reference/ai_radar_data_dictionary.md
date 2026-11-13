# AI 智商雷达插件 · 数据字典

> 上游实体与字段的权威释义。字段名均来自 2026-09-13 对 `https://api.codexradar.com` 的实测 JSON。
> 上游接口文档见 [`ai_radar_interface.md`](ai_radar_interface.md)。

---

## 1. 实体关系

```
Benchmark (评测频道)
  └── Task (任务/题目) ──┐
  └── ModelConfig (模型档位 = model + effort)
                         └── Cell (格子 = task × modelconfig) ── Run (一次有效运行)
                                                                  └── Contributor (贡献者)

Leaderboard  = Task[] + ModelRow[] + Contributor[] + 元数据
Table        = Task[] + Cell(dict) + Combo[] + 定价/契约元数据
Insights     = ComprehensivePoint[] + Recommendation[] + DegradationAlert[]
```

一个 **Cell** 是「某模型某档位在某道题上」的聚合状态；一个 **Run** 是一次志愿者实测；**IQ** 是按 Cell 的多数表决（`binary-majority`）或分数（`continuous-macro`）聚合出来的分数。

---

## 2. Benchmark（评测频道）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `id` | string | `deep-swe` \| `pompeii-adjacency` |
| `title` | string | 「代码修复 · DeepSWE」/「视觉恢复 · 庞贝壁画」 |
| `short_title` | string | 「DeepSWE」/「庞贝壁画邻接恢复」 |
| `description` | string | 一句话说明 |
| `task_count` | int | 112 / 86 |
| `scoring_mode` | string | `binary-majority`（二值多数）/ `continuous-macro`（连续宏平均） |
| `score_label` | string | `Pass rate` / `Adjacency F1` |
| `rolling_window` | int | 3 —— 每格取最近 3 次有效运行 |
| `model_config_count` | int | 67 / 40 —— 该频道可测的档位数量 |
| `reference_task_id` | string? | 公开例题 id（仅 pompeii：`RP-group-1-public-example`） |
| `reference_url` | string? | 例题图（仅 pompeii） |
| `task_bundle` | object? | 题目包 `{url, sha256, bytes, format}`（仅 pompeii，63.8MB tar.gz） |
| `default` | bool | 是否默认频道（`deep-swe` 为 true） |

**两个频道分数不可比**：`deep-swe` 是任务通过率（0–1，阈值 1.0 = 必须全过），`pompeii-adjacency` 是 Macro-F1（0–1，阈值 0.4）。**只有 IQ（×1.5 → 0–150）这一层是「设计上同尺度」的，且上游仍要求「不把两者直接混合」** —— 唯一的混合口径是 `/radar-insights` 的 `comprehensive_points`。

---

## 3. ModelConfig（模型档位）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `model` | string | 上游模型 id（如 `gpt-6-astra`、`claude-opus-5`、`glm-5.3-flash`） |
| `effort` | string | 推理档位：`low` / `medium` / `high` / `xhigh` / `max` / `ultra` |

**`(model, effort)` 是不可分割的键**：`gpt-5.6-sol@low` 的 IQ（81.2）与 `gpt-5.6-sol@max`（106.7）差 25 分。任何比较都必须带上档位。

### 3.1 模型清单（deep-swe 19 个 / pompeii 10 个）

见 [`ai_radar_interface.md` §2.3 的 combos 表](ai_radar_interface.md#23-get-apiv1table)。要点：

- 6 档全开的只有 `gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`（`gpt-5.6-luna` 缺 `ultra`）。
- `gpt-5.5` 只有 `high` / `xhigh` —— **站点刻意的「上一代对照」**，用于观察「降智」。
- 视觉频道只覆盖 10 个档位（含 `dsh-deepseek-v4-flash-vision-exp`），编程频道覆盖 19 个。

### 3.2 运行工具（harness）

`client_contract.harness_capabilities` 的键：`dsh` / `claude-code` / `grok` / `kimi-code` / `zcode` / `antigravity` / `codebuddy`（7 个）。

| 来源 | 枚举值 | 坑 |
| --- | --- | --- |
| `/suggest?harness=` | `codex`、`claude-code`、`dsh`、`kimi-code`、`zcode`、**`grok`**、`antigravity`、`codebuddy` | 8 个，多一个 `codex` |
| `ClaimBatchPayload.harness` | `codex`、`claude-code`、`dsh`、`kimi-code`、**`grok-build`**、`zcode`、`antigravity`、`codebuddy` | `grok` vs `grok-build` **不同名** |

`harness_capabilities` 的每个值是该 harness 的**能力串列表**（如 `claude-code-5-subscription-oauth-sandbox-v1`），含订阅类型、OAuth 方式、沙箱/并发模式与版本号。插件只读消费时**不需要解析这些串**，只需原样展示「由哪个工具跑的」。

---

## 4. Task（任务）

| 字段 | 类型 | deep-swe | pompeii-adjacency |
| --- | --- | --- | --- |
| `id` | string | `abs-module-cache-flags` | `pompeii-adjacency-rp-002` |
| `title` | string | 英文任务标题 | 「庞贝壁画邻接恢复 · RP group N」 |
| `language` | string | `go`/`python`/`typescript`/`rust`/`javascript` | `vision` |
| `repo` | string | 上游开源仓库 URL | `https://zenodo.org/records/15800029` |
| `category` | string | `feature_request`(105)/`bugfix`(4)/`enhancement`(3) | `visual-reconstruction` |
| `fragment_count` | int | — | 2–44（众数 6） |
| `metric` | string | — | `adjacency_f1` |
| `discrimination` | object | 见下 | 同 |

**`discrimination`（题目区分度，`discrimination_method = "task-discrimination-v2"`）**：

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `raw_score` | float | 原始区分度（0–1 量级，如 0.110518） |
| `model` | float | 模型维度分（10.0） |
| `effort` | float | 档位维度分（7.4） |
| `monotonic` | float | 单调性分（3.0，档位越高分越高的一致性） |
| `config` | float | 配置维度分（11.1） |
| `threshold` | float | 达标阈值（21.3） |
| `confidence` | float | 置信度（0.693） |
| `cells` | int | 参与统计的格子数（56） |
| `samples` | int | 参与统计的样本数（138） |
| `score` | float | 合成区分度得分（79.7，0–100） |

用途：判断「这道题能不能区分模型强弱」。`score` 高 = 好题；`confidence` 低 = 结论不稳。

---

## 5. Cell（格子）

键：`"<task_id>|<model>|<effort>"`（**在 `/table` 里是 dict 的键**）。

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `st` | enum | 状态：`open`（可认领）/ `cooldown`（冷却中，60h 内已跑过）/ `leased`（已认领）/ `running`（解题中） |
| `n` | int | 当前滚动窗口内的有效运行数（≤ `rolling_window`=3） |
| `p` | int | 其中通过数 |
| `score_sum` | float | 窗口内分数和（二值制下 = `p`；连续制下是 F1 和） |
| `rate` | float | `score_sum / n` —— 即该格子的「最近 3 次」分数 |
| `total_n` / `total_p` / `total_score` | int/int/float | **历史累计**（不限于窗口） |
| `base_mult` | float | 基础积分倍率 |
| `wasteland` | bool | 是否「荒地」（久无人跑的格子） |
| `wasteland_multiplier` | float | 荒地加成倍率 |
| `mult` | float | 最终积分倍率 |
| `last_graded_at` | string(ISO) | 最后一次判分时间 |
| `last_points` | float | 最后一次判分给贡献者的积分 |
| `ran_by` | object[] | 窗口内的运行记录（见下） |
| `cost` | float? | 该格子的预估成本（USD）；`src=task-level-fallback` 时是回退估值 |
| `min` | int? | 预估耗时（分钟） |
| `src` | enum? | 数值来源，见下 |

**`src`（数值来源，决定「这个数字能不能信」）**：

| 值 | 实测计数（deep-swe） | 含义（推断） |
| --- | --- | --- |
| `measured` | 2632 | 真实实测 |
| `cross-model-median-shape` | 2102 | 由同类模型的中位形态推算 |
| `task-level-fallback` | 1344 | 按任务级回退估值 |
| `official-shape` | 1090 | 按官方形态估算 |
| `null` | 336 | 无标注（多为 `open` 空格子） |

> 上游方法论页明确「不用回退值制造确定性」——**插件展示成本/耗时时必须带 `src`**，非 `measured` 的要标注为估算。

**`ran_by[]` 条目**：

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `login` / `avatar_url` | string | GitHub 绑定身份（二选一） |
| `nickname` / `avatar_seed` | string | 站内昵称身份（二选一） |
| `passed` | bool | 是否通过 |
| `score` | float | 该次得分（0–1） |
| `graded_at` | string(ISO) | 判分时间 |
| `points_base` | float | 基础积分 |
| `points_multiplier` | float | 倍率（实测见 2.0 / 3.0） |
| `duration_sec` | float | 实际耗时（秒） |
| `actual_cost_usd` | float | API 等价成本（USD） |
| `cost_source` | string | `tokens` 等 |
| `token_pricing_version` | string | 计价版本（如 `official-api-equivalent-astra-2026-09-05-v15`） |
| `cost_complete` | bool | token 证据是否完整 |

> **同一格子里不同 run 的 `token_pricing_version` 可能不同**（价格表会更新），所以历史成本不能拿最新价表重算。

---

## 6. ModelRow（`/leaderboard` 的 `models[]`）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `model` / `effort` | string | 档位键 |
| `graded` | int | 已判分运行数 |
| `passed` | int | 通过数（二值制） |
| `score_sum` | float | 分数和（连续制下 ≠ `passed`） |
| `cells` | int | 覆盖的格子数 |
| `cells_passed` | int | 通过的格子数 |
| `pass_rate` | float | `score_sum / graded` 量级（实测 0.682） |
| `score_label` / `scoring_mode` / `pass_threshold` | string/string/float | 口径元数据 |
| `tasks` | dict | `task_id` → `{votes, pass_votes, majority_pass, score_sum, score_rate}` |

**`tasks[task_id]`**：

| 字段 | 释义 |
| --- | --- |
| `votes` | 该题该档位的有效运行数 |
| `pass_votes` | 其中通过数 |
| `majority_pass` | 多数表决结果（`binary-majority` 口径的判定） |
| `score_sum` / `score_rate` | 分数和 / 分数率 |

> 注意 `graded` 与 `cells` 不是一回事：`graded` 是运行次数，`cells` 是不同题目数（同一题可能跑多次）。`pass_rate` 的分子分母口径以上游为准，插件**不要自己除**。

---

## 7. Contributor（贡献者）

34 字段，见 [`ai_radar_interface.md` §2.2](ai_radar_interface.md#22-get-apiv1leaderboard)。语义补充：

| 字段 | 释义 |
| --- | --- |
| `points` / `month_points` | 积分（= 烧掉的额度折算，**过不过都给分**，总榜永久累计） |
| `points_by_harness` | 按运行工具拆分积分，键如 `codex` / `other` |
| `tokens` / `month_tokens` | 累计 token |
| `folded_usd` | 「折叠」口径的美元（订阅额度折算） |
| `deepseek_api_usd` / `deepseek_api_runs` / `deepseek_api_unpriced_runs` | DeepSeek 官方 API 直连口径的花费、运行数、无价格运行数 |
| `usd` | `folded_usd + deepseek_api_usd`（实测 17762.97 + 91.32 = 17854.29 ✓） |
| `is_radar_admin` | 是否站长/管理员 |
| `flag_race_winner` | 是否夺旗赛冠军 |
| `contribution_streak` | 连击体系：`current_days` / `longest_days` / `active_days` / `daily_reward_points` / `reward_points` / `month_reward_points` / `next_milestone_days` / `next_milestone_bonus` / `active_today` / `today_points` |
| `rank_change_24h` / `month_rank_change_24h` | 24h 排名变化（正=上升） |

**两个成本口径的关系已实测核对**：`usd = folded_usd + deepseek_api_usd`。展示「花费」时用 `usd`；要区分口径时分别展示。

---

## 8. ComprehensivePoint（`/radar-insights`）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `model` / `effort` | string | 档位 |
| `iq` | float | **综合 IQ**（软件与视觉加权，`comprehensive_weighted_mean`） |
| `software_iq` | float | 编程频道 IQ |
| `visual_iq` | float | 视觉频道 IQ |
| `samples` | int | 综合样本数 |

实测样例：`{"model":"gpt-6-astra","effort":"low","iq":109.19,"software_iq":98.51,"visual_iq":135.71,"samples":188}`

> 同一个模型在视觉频道普遍更高（135.71 vs 98.51）——**这是频道难度差异，不是「视觉更强」**。插件转述时必须同时给出三个数。

---

## 9. Recommendation（推荐组）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `key` | string | `daily_development` / `hard_problems` / `background_automation` / `lobster_tasks` |
| `title` | string | 「日常开发」/「难题攻坚」/「后台自动化」/「跑龙虾类任务」 |
| `rule` | string | **规则原文**（中文长句，见接口文档） |
| `items[]` | object[] | 推荐档位（每组 2 个） |

`items[]` 字段：

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `model` / `effort` | string | 档位 |
| `iq` | float | 综合 IQ |
| `passed` | float | **加权后的通过数**（非整数，实测 136.857…） |
| `samples` | int | 样本数 |
| `average_cost_usd` / `cost_samples` | float/int | 平均 API 等价成本与其样本数 |
| `average_duration_minutes` / `duration_samples` | float/int | 平均耗时（分钟）与其样本数 |
| `combined_cost_index` | float | 费用与耗时的综合成本指数（越小越省） |
| `rule` | string | 同组 `rule`（重复下发） |
| `trend_48h[]` | object[] | `{timestamp, iq, samples}` —— 48 小时趋势点 |

> `passed` 是浮点，说明这是「任务等权 + 窗口加权」后的**加权通过数**，不是整数计数。**不要把它当「过了多少题」展示**。

---

## 10. DegradationAlert（降智预警）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `rule` | string | 规则原文（见接口文档） |
| `items[]` | object[] | 命中的档位（最多 4 个，不足不补位） |

规则要点（原文见 [`ai_radar_interface.md` §2.5](ai_radar_interface.md#25-get-apiv1radar-insights)）：

1. **不含 DeepSeek** —— DeepSeek 系列不参与预警。
2. 只与**自身历史**比较（不跨模型）。
3. 阈值固定：当前 IQ 低于 24h 均值 ≥7 IQ，**或**低于 48h 均值 ≥9 IQ，**且**最近 12h 仍在下降。
4. 按严重度排序，最多 4 个。

实测当时 `items: []`（无预警）。

---

## 11. EfficiencyPoint（`/intelligence-efficiency`）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `model` / `effort` | string | 档位 |
| `passed` / `total` | **float** / int | 通过数 / 总数。**`binary-majority` 下是整数；`continuous-macro` 下是浮点**（pompeii 实测 `passed: 49.79059751561299, total: 55`）——不是「过了 49.79 题」，是 F1 加权和。按 `scoring_mode` 决定展示方式 |
| `iq` | float | IQ |
| `average_price_usd` | float | 平均 API 等价成本 |
| `average_price_usd_by_band` | float? | 分档/分时价格下的平均（实测 `null`） |
| `average_minutes` | float | 平均耗时 |
| `combined_cost_index` | float | 综合成本指数（费用 + 耗时） |
| `average_agent_steps` / `agent_steps_samples` | float/int | 平均 agent 步数与其样本数 |
| `average_total_tokens` / `token_samples` | float/int | 平均总 token 与其样本数 |
| `cache_hit_rate` / `cache_token_samples` | float/int | 缓存命中率与其样本数 |
| `runs_24h` / `runs_48h` / `runs_total` | int | 运行数分布 |
| `source_updated_at` | string(ISO) | **该档位**的数据更新时间（可能与顶层不同） |

**顶层元数据**：`schema: 3`、`mode: "equal_latest_3"`、`source_updated_at`、`runs_24h_total`、`runs_48h_total`、`runs_total`、`benchmark_id`、`scoring_mode`、`score_label`。

**两频道对照（实测）**：

| | deep-swe | pompeii-adjacency |
| --- | --- | --- |
| 体积 | 31677B | 19679B |
| point 数 | 67 | 40 |
| `score_label` | `Pass rate` | `Adjacency F1` |

---

## 12. HistorySeries（`/iq-history`）

返回的是 **dict**（series 名 → 时间点数组），**key 有三种形态，必须分开处理**：

| 形态 | 例 | 口径 | 实测（同一时刻 `gpt-5.6-sol`） |
| --- | --- | --- | --- |
| `模型` | `gpt-5.6-sol` | 跨档位合并 | score 96.7 / n 2016 |
| `模型@effort` | `gpt-5.6-sol@low` | 单档位 | score 81.2 / n 336 |
| `latest:模型` / `latest:模型@effort` | `latest:gpt-5.6-sol` | **「最近一次有效运行」窗口** | score 97.3 / n 672 |

series 数量：deep-swe 124 条（62 裸 + 62 `latest:`）；pompeii 76 条（38 + 38）。每条 168 点（逐小时）。

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `ts` | string(ISO) | 时间点 |
| `score` | float | 该时刻 IQ（0–150） |
| `n` | int | 该时刻有效样本数 |

> **红线**：同一模型的裸名与 `latest:` 前缀给出**不同分数**（96.7 vs 97.3）。画趋势或做「降智判断」时必须选定一种口径并标注，**不能混用**。
> 未验证：`latest:` 口径的精确定义（推断与 `/model-metrics` 的 `latest_valid_per_task` 同源）。

---

## 13. ModelMetricPoint（`/model-metrics`）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `model` / `effort` | string | 档位 |
| `average_agent_steps` / `agent_steps_samples` | float/int | 平均步数 |
| `average_total_tokens` / `token_samples` | float/int | 平均 token |
| `cache_hit_rate` / `cache_token_samples` | float/int | 缓存命中率 |
| `runs_24h` / `runs_48h` / `runs_total` | int | 运行数 |

`mode = "latest_valid_per_task"` —— **与主榜 `equal_latest_3` 不同口径**，必须透传。

---

## 14. RadarEvent（`/events`）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `graded_at` | string(ISO) | 判分时间 |
| `passed` | bool | 是否通过 |
| `score` | float | 得分 |
| `task_id` | string | 任务 |
| `model` / `effort` / `harness` | string | 档位与运行工具 |
| `cost_usd` | float | 成本 |
| `cost_source` | string | `api_equivalent_tokens` |
| `cost_is_estimate` | bool | 是否估算 |
| `cost_is_fallback_estimate` | bool | 是否回退估算 |
| `cost_is_api_equivalent` | bool | 是否 API 等价口径 |
| `points` | float | 给贡献者的积分 |
| `points_deferred` | bool | 积分是否延后发放 |
| `login` / `avatar_url` | string | 贡献者 |

---

## 15. B 面实体（网站侧 `codexradar.com/api/*`，**非默认拉取**）

> 这一组来自**第二套接口面**（无 OpenAPI 契约，见 [`ai_radar_interface.md`](ai_radar_interface.md) §1.2）。
> 字段为实测，**上游无契约承诺**，可能随时变更 → 解析必须容忍缺字段，且**不放进默认拉取集**。

### 15.1 ModelRating（`/api/model-ratings?view=public`）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `ok` | bool | 固定 `true` |
| `day` / `timezone` | string | 统计日与时区 |
| `refresh_seconds` | int | `300`（上游自述刷新间隔） |
| `updated_at` | string(ISO) | 更新时间 |
| `window` / `window_hours` | string / int | `"rolling_24h"` / `24` |
| `since` / `until` | string(ISO) | 窗口边界 |
| `source` | string | `"public_cache"` |
| `cached_at` | string(ISO) | 缓存时间 |
| `models[]` | array | 见下 |

`models[]` 条目（**共 33 个**）：

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `id` | string | 档位 id，如 `gpt-6-astra-ultra` |
| `label` | string | 展示名，如 `GPT-6 Astra ultra` |
| `group` | string | 模型族，如 `GPT-6 Astra`（可用于把档位归并回模型族） |
| `average` | float | **该窗口内的平均评分** |
| `count` | int | 样本量 |

> ⚠️ **`average` 与 IQ 不是同一量纲**（样例 `average: 8` 而 IQ 是 0–150）——**绝不可与 IQ 混用、同图对比或换算**。用途仅限「近 24h 活跃度/评分」这类独立展示。

### 15.2 SubscriberCount（`/api/subscriber-count`）

| 字段 | 类型 | 释义 |
| --- | --- | --- |
| `ok` | bool | 固定 `true` |
| `count` | int | 订阅数（实测 `4263`） |
| `source` | string | `"cached"` |

### 15.3 VisualSpatialPoint（`/api/visual-spatial-reasoning`）

顶层：`schema` / `type` / `mode` / `benchmark_id` / `scoring_mode` / `score_label` / `source_updated_at` / `runs_24h_total` / `runs_48h_total` / `runs_total` / `points[]` / `history[]`。

- `points[]` 实测 25 条，形状与 A 面 `EfficiencyPoint` **一致**（`model` / `effort` / `passed` / `valid_tasks` / `benchmark_tasks` / `iq` / `score_mode` / `average_price_usd` / `price_samples` / `average_minutes` …），`scoring_mode: "continuous-macro"`。
- `history[]` 实测 153 条。
- **与 A 面 `/iq-history?benchmark=pompeii-adjacency` 是否同源未验证**（同为 1MB 级视觉频道数据）——**按两个独立端点处理，不要假定可互换**。

### 15.4 IntelligenceEfficiencyMetrics（`/api/intelligence-efficiency-metrics`）

顶层键与 A 面 `intelligence-efficiency` 同构：`schema` / `mode` / `source_updated_at` / `runs_24h_total` / `runs_48h_total` / `runs_total` / `points[]` / `benchmark_id` / `scoring_mode` / `score_label`。实测 31.7KB / deep-swe 口径，与 A 面同端点数据同源。

### 15.5 禁用面

`/api/private/v1/intelligence-efficiency`（A 面 OpenAPI 内）实测 `401 {"detail":"invalid bearer token"}`。**插件不得触碰任何 `private` 路径**，也不得尝试携带 token 探测。

---

## 16. 口径红线（写 formatter 时必须遵守）

1. **IQ 与 pass_rate 必须同时给出** —— 单独给 IQ 会隐藏样本量问题。
2. **必须带样本量**（`samples` / `n` / `votes` / `graded`）。
3. **必须带数据时间**（`source_updated_at` / `baseline_generated_at` / `last_graded_at`），且优先用 **point 级**时间。
4. **必须带档位**（`effort`）—— 只说模型名等于没给信息。
5. **`src != measured` 的成本/耗时标注为估算**。
6. **跨 benchmark 不混分**（唯一例外：上游自己给的 `comprehensive_points`）。
7. **`mode` 透传**（`equal_latest_3` vs `latest_valid_per_task` vs `rolling_equal_per_task`）。
8. **空数据就说空**（上游原则：证据不足显示缺失）。
9. **不自行算钱**：用上游的 `actual_cost_usd` / `average_price_usd`；`token_pricing` 只用于展示计费口径。
10. **不自行给 DeepSeek 算降智预警**（上游规则明确排除）。
11. **B 面 `average` 不与 IQ 混用**（量纲不同，见 §15.1）。
12. **B 面数据必须标注来源**：无 OpenAPI 契约、可能随时变更；formatter 展示时应可区分「主面 vs 网站侧面」。
13. **不触碰 `private` 路径**（见 §15.5），也不携带 token 探测。
