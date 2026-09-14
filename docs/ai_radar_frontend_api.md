# AI 智商雷达 · 前端数据接口文档

> **读者**：负责为「AI 智商雷达」写**前端**的 AI（页面 / 卡片 / 图表 / 榜单渲染）。
> **本文档的来源**：`plugins/radar/` 的**实际实现代码**（以代码为最终事实）+ `tests/fixtures/radar/*.json` 的**真实上游响应裁剪样本**。
> 设计文档（`ai_radar_interface.md` / `ai_radar_data_dictionary.md` / `ai_radar_plugin_api.md` / `ai_radar_plugin_framework.md` / `ai_radar_open_items.md`）是权威的口径说明，但**与代码冲突时以代码为准**，差异集中在本文 §19。
> 本文档里所有示例数值**逐字取自夹具或实测响应**，不是编的。标注「未验证 / 推断」的地方请勿当成事实。

---

## 0. 文档定位与读者

### 0.1 本仓库做什么、不做什么

| | 状态 |
| --- | --- |
| 数据与后端实现（`plugins/radar/`） | **已完成**：配置、错误码、只读数据模型、唯一出网层、查询语义层、纯文本渲染层、命令面 |
| 前端（页面 / 卡片 / 图片 / 图表） | **完全不做**。仓库里没有任何页面、模板、图片渲染、图表依赖 |
| 本仓库提供的 Web 服务 | **没有**。不含任何 HTTP 服务端框架、路由、监听端口、REST 端点 |
| 上游数据源 | `https://api.codexradar.com`，**公开只读、免鉴权** |

**因此本文档描述的是两条消费路径**，不要去找 `/api/...` 之类的本地端点：

| 路径 | 适用 | 说明 |
| --- | --- | --- |
| **A. Python 层数据接口**（推荐） | 前端与后端同进程 / 同语言，或前端由 Python 侧产出 JSON | 直接 `await RadarService.<方法>()`，拿到 §12 的只读 dataclass，自己序列化成 JSON 交给前端 |
| **B. 直接打上游 HTTP 只读接口** | 前端独立部署、只想拿原始数据 | 自己 `GET https://api.codexradar.com/api/v1/*`，**只读 A 面**；字段名见 §12 的「上游原名」列 |

两条路径的**口径规则完全一致**（§4–§9、§15–§16 对两者都成立）。走路径 B 时，本文档 §12 里「插件字段名」一列需要你自己映射回「上游原名」。

### 0.2 前端 AI 最该先读的四节

1. **§4 两个 IQ 口径不可互推** —— 最容易把榜画错的地方。
2. **§3 `(model, effort)` 是原子键** —— 不带档位的榜单没有意义。
3. **§15 展示红线 checklist** —— 可以直接当验收清单用。
4. **§12 返回数据结构字典** —— 「我要画 X，该读哪些字段」。

---

## 1. 数据源与频道

### 1.1 基础

| 项 | 值 | 来源 |
| --- | --- | --- |
| `base_url` 默认 | `https://api.codexradar.com` | `config.DEFAULT_BASE_URL` |
| 端点前缀 | `/api/v1` | `provider.API_PREFIX` |
| 鉴权 | **不需要**（公开只读） | 设计文档实测；`provider` 不发 `Authorization` |
| 写侧端点 | **不实现、不调用** | `provider._url()` 对 `/api/private/` 直接抛错 |
| 自有 UA | 形如 `<项目标识>-radar/<PLUGIN_VERSION> (+<项目主页>)`；常量在 `config.DEFAULT_USER_AGENT`（本文不复制该字符串，避免文档携带身份/链接材料） | `config.py` |

### 1.2 两个频道（**分数不可比**）

夹具 `benchmarks.json` 的**逐字**内容：

| 字段 | `deep-swe` | `pompeii-adjacency` |
| --- | --- | --- |
| `title` | 代码修复 · DeepSWE | 视觉恢复 · 庞贝壁画 |
| `short_title` | DeepSWE | 庞贝壁画邻接恢复 |
| `task_count` | **112** | **86** |
| `scoring_mode` | **`binary-majority`** | **`continuous-macro`** |
| `score_label` | **`Pass rate`** | **`Adjacency F1`** |
| `rolling_window` | 3 | 3 |
| `model_config_count` | **67** | **40** |
| `reference_task_id` | `null` | `RP-group-1-public-example` |
| `reference_url` | `null` | `/assets/pompeii-reference/reference_question.png` |
| `task_bundle` | `null` | `{url: "/api/v1/benchmark-bundles/pompeii-adjacency", sha256: "e969b53…a898", bytes: 63775935, format: "tar.gz"}` |
| `default` | **`true`** | `false` |

- **默认频道**：`deep-swe`（`config.DEFAULT_BENCHMARK`，也是 `RADAR_DEFAULT_BENCHMARK` 的默认值）。
- **两频道分数不可比**：一个是任务通过率（阈值 `pass_threshold = 1.0`），一个是 Macro-F1（`pompeii` 的 `pass_threshold = 0.4`，取自 `table_pompeii.json`）。**任何图表都不得把两个频道的数据画在同一条曲线/同一个排序里**。
- 上游唯一自带的跨频道混合口径是 `/radar-insights` 的 `comprehensive_points`（见 §4）。
- 实测（2026-09-14）`/benchmarks` 返回 2 条频道；`deep-swe` 的 `model_config_count` 为 67、`pompeii-adjacency` 为 40。

### 1.3 前端需要知道的规模（决定加载策略）

| 端点 | 实测体积量级 | 是否适合前端首屏直接拉 |
| --- | --- | --- |
| `/benchmarks` | 几 KB | ✅ |
| `/radar-insights` | 31–45 KB | ✅ |
| `/intelligence-efficiency` | 20–32 KB | ✅ |
| `/model-metrics` | ~0.6–18 KB | ✅ |
| `/events` | 几 KB | ✅ |
| `/quota` / `/suggest` | 几 KB | ✅ |
| `/leaderboard` | **1.3–1.6 MB** | ⚠️ 建议后端缓存后下发 |
| `/iq-history` | **0.7–1.3 MB** | ⚠️ 同上 |
| `/table` | **3.7–8.6 MB**（deep-swe 7504 格） | ❌ 绝不要前端首屏直接拉 |

---

## 2. 实体关系

```
Benchmark（评测频道）
  ├── Task（题目）  ─────────────┐
  └── ModelConfig（模型档位 = model + effort）
                                 └── Cell（格子 = task × modelconfig）
                                        └── Run（一次志愿者实测）
                                               └── Contributor（贡献者）

Leaderboard = Task-id[] + ModelRow[] + Contributor[] + 元数据（含 pedal_speed / flag_race）
Table       = TaskInfo[] + Cell(dict) + ModelConfig[] + 元数据
Insights    = InsightPoint[] + Recommendation[] + DegradationAlert[]
```

| 实体 | 粒度 | 关键点 |
| --- | --- | --- |
| `Benchmark` | 频道 | 两频道分数不可比 |
| `Task` | 一道题 | deep-swe 有 `discrimination`；pompeii **没有**（实测 86/86 条都无此键） |
| `ModelConfig` | `(model, effort)` | **原子键**，见 §3 |
| `Cell` | 题 × 档位 | **聚合态**（滚动窗口），不是单次运行 |
| `Run` | 一次运行 | 单个志愿者的实测（`ran_by[]` 条目） |
| `Contributor` | 一个人 | 身份字段已被插件归一到 `display_name` |

### 2.1 从 Run 聚合到 IQ 的两种方式

| `scoring_mode` | 频道 | 聚合方式 | 含义 |
| --- | --- | --- | --- |
| `binary-majority` | `deep-swe` | **多数票** | 每格取窗口内运行的多数结果；`Cell.p / Cell.n` 是整数计数 |
| `continuous-macro` | `pompeii-adjacency` | **连续宏平均** | 每格是 F1 加权和；`Cell.score_sum` 是浮点，`rate = score_sum / n` |

**前端影响**：`binary-majority` 下 `p / n` 可以写成「3/3 通过」；`continuous-macro` 下必须写「F1 0.794」之类的**分数**，不能写「3 题通过」。

### 2.2 主榜口径 = 最近 3 次有效运行

- `/benchmarks` 与 `/table` 的 `rolling_window = 3`；`/intelligence-efficiency` 的 `mode = equal_latest_3`。
- 每格的窗口值在 `Cell.n` / `Cell.p` / `Cell.score_sum` / `Cell.rate`；**历史累计**在 `Cell.total_n` / `total_p` / `total_score`。**两者不要混**。

---

## 3. `(model, effort)` 是原子键

**规则：任何前端展示、比较、排序、筛选都必须带档位。** 只说模型名等于没给信息。

### 3.1 档位枚举

`config.EFFORT_TIERS`（由低到高，代码里就是这个顺序，`highest_effort()` 依赖它）：

```
low → medium → high → xhigh → max → ultra
```

### 3.2 真实差距（夹具数值）

同一模型不同档位分数差很大 —— 用夹具 `leaderboard.json`：

| 键 | `pass_rate` | `pass_rate × 150`（本地兜底换算） | `graded` | `cells` |
| --- | --- | --- | --- | --- |
| `gpt-6-astra@low` | 0.682 | 102.3 | 135 | 110 |
| `gpt-6-astra@ultra` | 0.688 | 103.2 | 147 | 112 |
| `gpt-5.6-sol@max` | 0.75 | 112.5 | 2177 | 112 |
| `gpt-5.5@high` | 0.634 | 95.1 | 1745 | 112 |

同一模型跨档位的真实 IQ 差距（实测 2026-09-14，`/iq-history`）：

| 序列 | 末点 IQ | `n` |
| --- | --- | --- |
| `gpt-5.6-sol@low` | 81.7 | 336 |
| `gpt-5.6-sol@max` | 107.1 | 336 |

→ 差 **25.4 IQ**。`service.py` 的模块 docstring 写「差 25 IQ 以上」，与实测一致。

### 3.3 各模型支持的档位不同（**不能假设对称**）

夹具 `table.json` 的 `combos` 只有 `gpt-6-astra` 的 6 档（裁剪样本）。设计文档 §2.3 记录的**完整** `combos` 表（deep-swe 67 档 / pompeii 40 档）要点：

- 6 档全开：`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`（`gpt-5.6-luna` 缺 `ultra`）。
- **`gpt-5.5` 只有 `high` / `xhigh`** —— 站点刻意的「上一代对照」。
- 视觉频道只覆盖 10 个档位（含 `dsh-deepseek-v4-flash-vision-exp`）。
- **档位清单的唯一事实源是 `/table` 的 `combos`，不是价格表**：实测 `deepseek-v4-pro` 在 `token_pricing` 里有价却在任何频道的 `combos` 里都不出现。插件对应方法 `RadarService.model_catalog()`。

> ⚠️ 设计文档写 `combos` 表，实现里 `model_catalog()` **确实**读 `/table` 的 `combos`（`service.py:263`），一致。但**价格表 ≠ 已评测模型**这一点前端也要知道：不要用价格表的键去生成模型下拉框。

### 3.4 前端 UI 建议

- 列表/表格**必须**有一列（或徽章）显示 `effort`。
- 模型下拉框建议按「模型族 → 档位」两级：模型族从 `model_catalog()` 的 `model` 去重得到。
- 对比视图的标题必须写成 `gpt-6-astra@low vs gpt-5.6-sol@max`，不能只写两个模型名。

---

## 4. 两个 IQ 口径不可互推（**最容易踩的坑**）

### 4.1 事实

系统里存在**两个都叫「IQ」但含义不同的数**：

| | 主榜口径 | 综合口径 |
| --- | --- | --- |
| 来源 | `/leaderboard` 的 `pass_rate`（口径 = 该频道的 `score_label`） | `/radar-insights` 的 `comprehensive_points[].iq` |
| 算法 | 单频道内，最近 3 次有效运行 | **跨频道加权**，`mode = rolling_equal_per_task`，`recommendation_mode = comprehensive_weighted_mean` |
| 插件字段 | `ModelRow.pass_rate`（0–1 比例） | `ModelRow.iq`（`iq_derived=False` 时）/ `InsightPoint.iq` |
| 量纲 | 比例，需 ×150 才是 IQ 尺度 | 已经是 0–150 的 IQ |

### 4.2 实测数字（**这是本仓库代码 docstring 与上游实测同时确认的**）

`deep-swe` 的 `gpt-6-astra@low`：

| 量 | 值 | 出处 |
| --- | --- | --- |
| `pass_rate` | **0.676** | 实测 `/leaderboard`（2026-09-14）；夹具样本里是 0.682 |
| `pass_rate × 150` | **101.4** | 本地兜底换算 |
| insights `iq` | **108.62** | 实测 `/radar-insights`（2026-09-14）；夹具样本里是 109.19 |

→ **同一个档位，两个口径给出 101.4 与 108.62，差 7.2 IQ。**

### 4.3 插件怎么处理（`service._apply_iq`）

```python
point = index.get(row.key)              # key = "model@effort"
if point is not None:
    iq = point.iq;  iq_derived = False  # 优先用 insights 的上游值
else:
    iq = row.pass_rate * 100.0 * 1.5    # 兜底：pass_rate × 150
    iq_derived = True
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `ModelRow.iq` | `float \| None` | 上面算出的值 |
| `ModelRow.iq_derived` | `bool` | **`True` = 本地兜底换算，不是上游综合 IQ** |

- 只有 insights 里**没有该档位**时才兜底。实测 `deep-swe` 的 `/leaderboard` 有 67 行，而 insights 的 `comprehensive_points` 只有 40 点 → **大部分行都会 `iq_derived=True`**（实测 `top_models()` 默认返回 15 行里 5 行是 derived）。
- 纯文本层把 derived 标成 `IQ 112.5*`，并在末尾加一行 `* 本地换算 pass_rate×150，非上游综合 IQ`。

### 4.4 前端必须做什么

1. **`iq_derived=True` 时必须显式标记**（角标、星号、tooltip 文案都行），例如：`IQ 112.5（本地换算）`。
2. **绝不把 `pass_rate` 和 insights `iq` 混进同一个排序的同一个含义里**。要排主榜就全用 `pass_rate`（或全用 `iq` + 标记），要排综合就全用 insights 的 `iq`。
3. **绝不做反向换算**（不要从 `iq` 反推 `pass_rate`）。
4. 如果一屏里同时出现两种数，**分别给出**：`IQ 108.62（综合）· 通过 67.6%（Pass rate）`。

### 4.5 `visual_iq` 普遍高于 `software_iq` —— 是频道难度差异，不是「视觉更强」

夹具 `insights.json` 的 `comprehensive_points` 逐字：

| `model@effort` | `iq` | `software_iq` | `visual_iq` | `samples` |
| --- | --- | --- | --- | --- |
| `gpt-6-astra@low` | 109.19 | 98.51 | **135.71** | 188 |
| `gpt-6-astra@medium` | 115.48 | 106.15 | 132.79 | 200 |
| `gpt-6-astra@high` | 115.9 | 108.2 | 135.5 | 170 |

→ 三个数**必须一起给**。前端不要只画 `visual_iq` 高就下「视觉能力更强」的结论；文案应说明这是两个频道难度不同导致的。

---

## 5. `/iq-history` 的三种键形态（**不可混画同一条曲线**）

### 5.1 三种形态

`provider.parse_history()` 把顶层扁平 dict 的键解析成 `HistorySeries`：

| 形态 | 例 | `HistorySeries.effort` | `HistorySeries.latest` | 口径 |
| --- | --- | --- | --- | --- |
| 裸模型名 | `gpt-6-astra` | `None` | `False` | **跨档位合并** |
| `模型@effort` | `gpt-6-astra@low` | `"low"` | `False` | 单档位 |
| `latest:` 前缀 | `latest:gpt-6-astra` / `latest:gpt-6-astra@low` | 同上 | **`True`** | **另一套「最近一次有效运行」窗口** |

### 5.2 实测对照（**同一时刻，分数不同**）

夹具 `iq_history.json` 的 4 条序列（每个点都是 `{ts, score, n}`，被归一成 `TrendPoint(timestamp, iq, samples)`）：

| `key` | 首点 | 末点 |
| --- | --- | --- |
| `gpt-6-astra` | `2026-09-06T10:00:03+00:00` / 105.4 / n=471 | `2026-09-06T14:00:00+00:00` / 105.6 / n=551 |
| `gpt-6-astra@low` | 98.3 / n=87 | 97.9 / n=95 |
| `latest:gpt-6-astra` | 106.8 / n=424 | 107.3 / n=502 |
| `latest:gpt-6-astra@low` | 104.6 / n=76 | 103.6 / n=84 |

设计文档记录的实测（`gpt-5.6-sol`，2026-09-13）：裸名 **96.7（n=2016）** vs `latest:` **97.3（n=672）** —— **样本量明显更小、分数不同**。实测 2026-09-14 复现同一现象：96.9（n=2016）vs 98.2（n=672）。

### 5.3 前端规则

1. **`latest=True` 的序列必须单独标注**（例如图例写 `gpt-5.6-sol（latest 窗口）`），**不得与裸名序列画在同一坐标系**。
2. `service.trend()` **已显式跳过** `latest:` 前缀的序列（`service._series_points` / `service.trend`），并在 `meta.note` 里写「跨档位合并口径」或「单档位 <effort>」。前端应把 `meta.note` 显示在图标题旁。
3. 裸名与 `@effort` 也不能混：实测 `gpt-6-astra` 末点 105.3（n=806）vs `gpt-6-astra@low` 末点 97.9 量级 —— 口径不同。
4. 序列规模：deep-swe **124 条**（62 裸 + 62 `latest:`），pompeii **76 条**（38 + 38）；每条 **168 点**（逐小时）。

---

## 6. `mode` 必须透传

`RadarMeta.mode` 是**口径标记**，前端必须显示（或至少放进 tooltip / 脚注）。实测（2026-09-14）各端点的 `mode`：

| 上游端点 | `RadarMeta.mode` | 说明 |
| --- | --- | --- |
| `/leaderboard` | **`None`** | ⚠️ 上游该响应**不含** `mode` 字段，所以插件里是 `None`。主榜的「最近 3 次」由 `rolling_window`（`/table`、`/benchmarks` 里为 3）与下面的 `equal_latest_3` 佐证 |
| `/table` | `None` | 有 `rolling_window = 3` |
| `/iq-history` | — | 该端点无 `meta`（返回 `tuple[HistorySeries, ...]`） |
| `/radar-insights` | **`rolling_equal_per_task`** | 跨频道综合口径；另有 `recommendation_mode` |
| `/intelligence-efficiency` | **`equal_latest_3`** | 主榜同窗口口径 |
| `/model-metrics` | **`latest_valid_per_task`** | **与主榜窗口口径不同** |
| `/events` | `None` | 有 `pass_threshold = 1.0` |
| `/quota` / `/suggest` | `None` | — |

**前端规则**：

- `/model-metrics` 的「平均步数 / 平均 token / 缓存命中率」用的是 `latest_valid_per_task`（每题取最近一次有效运行），**与主榜的 `equal_latest_3` 不是同一口径**。不要把 `/model-metrics` 的 `runs_total` 与 `/leaderboard` 的 `graded` 当成同一个数直接相除或互相校验。
- `/intelligence-efficiency` 的 `mode = equal_latest_3`，与主榜同窗口；但它的 `iq` 是**上游在该端点给的 IQ**，与 §4 的 insights `iq` 也不是同一个数（实测 `gpt-6-astra@low`：efficiency 97.78 / insights 108.62 / `pass_rate×150` 101.4 —— **三个数都不同**）。
- `recommendation_mode`（`comprehensive_weighted_mean` / `software_engineering`）也必须一起显示：它是上游自己给的加权口径说明。

---

## 7. 连续制频道的 `passed` 是浮点

**`pompeii-adjacency`（`scoring_mode = continuous-macro`）下 `passed` 不是「过了多少题」，而是 F1 加权和。**

夹具 `efficiency_pompeii.json` 逐字：

| 字段 | 值 |
| --- | --- |
| `model` / `effort` | `gpt-6-astra` / `low` |
| `passed` | **`49.79059751561299`** |
| `total` | `55` |
| `iq` | `135.79` |
| 其余（`average_price_usd` / `average_minutes` / `combined_cost_index` / `average_agent_steps` / `average_total_tokens` / `cache_hit_rate` / `runs_*` / `source_updated_at`） | 全部 `null` |

第二条 point：`gpt-6-astra@medium`，`passed = 75.681635`，`total = 85`，`iq = 133.56`。

推荐项里的 `passed` 同样是加权值 —— 夹具 `insights.json` 的 `daily_development` 首项：

| 字段 | 值 |
| --- | --- |
| `model@effort` | `gpt-6-astra@low` |
| `passed`（插件字段名 `weighted_passed`） | **`136.85726418227966`** |
| `samples` | 188 |
| `iq` | 109.19 |
| `average_cost_usd` | 1.921132 |
| `average_duration_minutes` | 8.76 |
| `combined_cost_index` | 128.016 |

**前端规则**：展示必须**按 `scoring_mode` 分支**：

| `scoring_mode` | `passed` 展示 |
| --- | --- |
| `binary-majority` | 「通过 88 / 134」（整数计数） |
| `continuous-macro` | 「F1 加权 49.79 / 55」或直接显示 `iq`；**不要**写「通过 49.79 题」 |
| 未知 / 缺失 | 显示 `—`，不要猜 |

---

## 8. 估算 vs 实测（`src`）

### 8.1 `src` 枚举（**只有 `measured` 是实测**）

数据字典 §5 记录的 5 个值 + 插件解析出的 `None`：

| `src` | 含义 | 实测计数（deep-swe 7504 格） |
| --- | --- | --- |
| `measured` | 真实实测 | 2632 |
| `cross-model-median-shape` | 由同类模型中位形态推算（**推断**） | 2102 |
| `task-level-fallback` | 按任务级回退估值（**推断**） | 1344 |
| `official-shape` | 按官方形态估算（**推断**） | 1090 |
| `calibrated-estimate-v1` | 标定估算（**推断**；pompeii 夹具里出现） | — |
| `null` / 键缺失 | 无标注 | 336 |

> ⚠️ **`src` 各值的精确语义是「推断」**（上游未文档化）。实现只做一件事：`src != measured` 一律当估算。前端也**只应做这一件事**，不要为每个值写不同的用户文案。
>
> ⚠️ 上游对「空」的表达是**整个键不出现**，而不是给 `null`。实测 deep-swe 7504 格里 **336 格没有 `src` 键**、1844 格没有 `ran_by` 键；原文 `"src": null` 与 `"ran_by": []` 各出现 **0 次**。插件把「键缺失」与「显式 null」都解析成 `None`。

### 8.2 插件怎么表达

| 字段 | 说明 |
| --- | --- |
| `CellState.cost` + `CellState.cost_src` | **成对出现**，强制调用方处理「实测还是估算」 |
| `CellState.cost_is_estimate`（property） | `is_estimate(cost_src)`：**`cost_src` 缺失或 ≠ `measured` 即为 `True`** |
| `RadarEvent.cost_is_estimate` / `cost_is_fallback_estimate` / `cost_is_api_equivalent` | 三个上游布尔**原样透传**，不合并、不推导 |

夹具里的真实组合：

| 单元格 | `cost` | `cost_src` | `cost_is_estimate` |
| --- | --- | --- | --- |
| `abs-module-cache-flags\|gpt-5.6-sol\|low` | 0.73 | `measured` | `False` |
| `abs-module-cache-flags\|gpt-6-astra\|low` | 0.82 | `task-level-fallback` | **`True`** |
| `adaptix-name-mapping-aliases\|gpt-6-astra\|max` | 1.65 | `task-level-fallback` | **`True`** |
| `pompeii-adjacency-rp-015\|gpt-5.6-terra\|max` | 4.95 | `calibrated-estimate-v1` | **`True`** |
| `abs-module-cache-flags\|gemini-3.8-flash\|low` | `null` | `null` | **`True`**（缺失按估算处理） |

纯文本层落地：非实测时前缀 `~` 并标「估算」，例如 `~$0.82 估算`。

### 8.3 历史成本不得用最新价表重算

夹具 `table.json` 里同一个格子 `abs-module-cache-flags|gpt-5.6-sol|low` 的 3 条 `ran_by`：

| `display_name` | `actual_cost_usd` | `token_pricing_version` |
| --- | --- | --- |
| `volunteer-01` | 0.775488 | `official-api-equivalent-gemini-3.7-flash-2026-08-28-v12` |
| `volunteer-02` | 1.197468 | `official-api-equivalent-subscription-bands-2026-08-25-v7` |
| `volunteer-03` | 0.955072 | `official-api-equivalent-subscription-bands-2026-08-21-v5` |

→ **同一格子里 3 条 run 的价表版本各不相同**。`RunRecord.token_pricing_version` 保留了这个信息。

**前端规则**：

1. **不得自行加总 / 换算金额**。用上游算好的 `actual_cost_usd` / `average_price_usd` / `cost` / `usd`。
2. **不得用最新价表重算历史成本**。
3. 展示金额时，`cost` 与 `cost_src` **必须成对**；`cost_is_estimate=True` 或 `src != measured` 时必须带 `~` 与「估算」字样。
4. 若要在 tooltip 里给出计价版本，用 `RunRecord.token_pricing_version`，**逐条显示**，不要合并。

---

## 9. 样本量与数据时间

### 9.1 每次展示都要带样本量

| 数据位置 | 样本量字段 |
| --- | --- |
| `ModelRow` | `graded`（运行数）与 `cells`（题目数） |
| `TaskVote` | `votes` |
| `CellState` | `n`（窗口内）与 `total_n`（累计） |
| `InsightPoint` | `samples` |
| `RecommendationItem` | `samples` |
| `EfficiencyPoint` | `runs_total` / `runs_24h` / `runs_48h`，以及 `agent_steps_samples` / `token_samples` / `cache_token_samples` |
| `MetricPoint` | 同上 |
| `TrendPoint` | `samples`（上游 `n`） |
| `Discrimination` | `samples` / `cells` |
| `RadarMeta` | `samples`（仅「有唯一主体」的结果才填，见 §10） |

> ⚠️ **`graded`（运行数）≠ `cells`（题目数）** —— 同一题可能跑多次。实测 `gpt-6-astra@low`：`graded = 135`、`cells = 110`。**前端不得自己相除**（不要算 `graded/cells`），`pass_rate` 是上游给的。

### 9.2 数据时间：**优先 point 级**

| 场景 | 用哪个时间 |
| --- | --- |
| `/intelligence-efficiency` | **`EfficiencyPoint.source_updated_at`**（每个 point 自带），其次才回落到 `RadarMeta.source_updated_at` |
| 单元格 | `CellState.last_graded_at` |
| 单条 run | `RunRecord.graded_at` |
| 单条流水 | `RadarEvent.graded_at` |
| 题目基线 | `TablePayload.baseline_generated_at` / `discrimination_generated_at` |
| 综合洞察 | `InsightsPayload.generated_at`（生成时间）与 `RadarMeta.source_updated_at`（**数据**时间，两者不同） |
| 趋势点 | `TrendPoint.timestamp` |

实测证据（同一响应里两个时间不同）：夹具 `efficiency.json` 顶层 `source_updated_at = 2026-09-12T03:58:59+00:00`，而 point 2 的 `source_updated_at = 2026-09-12T12:32:58+00:00`。**展示时必须用 point 级的那个。**

同理 `/radar-insights`：`generated_at = 2026-09-13T09:09:40+00:00`（生成）而 `source_updated_at = 2026-09-12T16:37:23+00:00`（数据）。实测 2026-09-14 复现：generated `2026-09-14T05:34:54+00:00` vs source_updated `2026-09-12T16:37:23+00:00`。

---

## 10. `RadarMeta` 字段表

`RadarMeta` 是**每个结果必带**的口径信封（`models.py:48`，`frozen=True, slots=True`，12 个字段）。

| 字段 | 类型 | 可为 `None` | 含义 | 前端怎么用 |
| --- | --- | --- | --- | --- |
| `benchmark_id` | `str` | 否 | 频道 id（`deep-swe` / `pompeii-adjacency`） | 脚注第一项；**决定这个数属于哪个频道** |
| `scoring_mode` | `str` | 否（可能是 `""`） | `binary-majority` / `continuous-macro` | 决定 `passed` 怎么展示（§7）。⚠️ `/radar-insights`、`/quota`、`/suggest` 的响应不含该字段 → `""` |
| `score_label` | `str` | 否（可能是 `""`） | `Pass rate` / `Adjacency F1` | **必须显示**：它说明「这个百分比是什么口径」。同样有 `""` 的情况 |
| `mode` | `str \| None` | 是 | 窗口口径（§6） | 透传显示；`None` 表示上游未给 |
| `rolling_window` | `int \| None` | 是 | 主榜滚动窗口，实测 `3` | 显示成「最近 3 次有效运行」 |
| `pass_threshold` | `float \| None` | 是 | 通过阈值：deep-swe `1.0`、pompeii `0.4` | 可用于 tooltip 解释「什么算通过」 |
| `source_updated_at` | `str \| None` | 是 | **数据**更新时间（优先 point 级） | **必须显示**（§9.2） |
| `stale` | `bool` | 否 | **`True` = 来自过期缓存**（上游不可用时的降级结果） | **必须显式提示「数据可能过期」** |
| `fetched_at` | `str` | 否 | 插件取数时间（ISO8601，UTC，秒级） | 可显示为「本页数据抓取于 …」 |
| `recommendation_mode` | `str \| None` | 是 | 上游加权口径（`comprehensive_weighted_mean` / `software_engineering`） | 与 `iq` 一起显示 |
| `note` | `str` | 否（可能是 `""`） | 本条结果的口径被如何选取（如「已取各模型最高档」） | **必须显示**，否则读者不知道榜是怎么筛的 |
| `samples` | `int \| None` | 是 | 本条结果背后的样本量。**只有「有唯一主体」的结果才填**（单模型档案 / 单条趋势 / 单题详情）；列表类留 `None` | `None` 时不要造一个含糊的总数；每行自带 `n=` |

### 10.1 `stale=True` 的处理

- 触发：上游不可用（5xx / 超时 / 限流耗尽）且本地缓存未超过 900 秒宽限期（`provider._STALE_GRACE_SECONDS`）。
- `stale` 会**传播**：`_mark_stale()` 用 `dataclasses.replace` 造 `stale=True` 的副本，同时刷新 `fetched_at`。`HistorySeries` 元组会逐条打标（`HistorySeries.stale`）。
- 前端：**必须**在显眼位置提示「数据可能过期（上游暂不可用，显示缓存）」，并显示 `fetched_at`。不要静默显示陈旧数据。

### 10.2 各端点的 `RadarMeta` 实测取值（2026-09-14）

| 端点 | `scoring_mode` | `score_label` | `mode` | `rolling_window` | `pass_threshold` | `source_updated_at` |
| --- | --- | --- | --- | --- | --- | --- |
| `/benchmarks` | — | — | — | — | — | —（返回 `tuple[BenchmarkInfo,...]`，**无 meta**） |
| `/leaderboard` | `binary-majority` | `Pass rate` | `None` | `None` | `1.0` | `None` |
| `/table` | `binary-majority` | `Pass rate` | `None` | `3` | `1.0` | `None` |
| `/iq-history` | — | — | — | — | — | —（返回 `tuple[HistorySeries,...]`，**无 meta**） |
| `/radar-insights` | `""` | `""` | `rolling_equal_per_task` | `None` | `None` | `2026-09-12T16:37:23+00:00` |
| `/intelligence-efficiency` | `binary-majority` | `Pass rate` | `equal_latest_3` | `None` | `None` | `2026-09-14T04:59:08+00:00` |
| `/model-metrics` | `binary-majority` | `Pass rate` | `latest_valid_per_task` | `None` | `None` | `2026-09-14T04:59:08+00:00` |
| `/events` | `binary-majority` | `Pass rate` | `None` | `None` | `1.0` | `None` |
| `/quota` | `""` | `""` | `None` | `None` | `None` | `None` |
| `/suggest` | `""` | `""` | `None` | `None` | `None` | `None` |

> 前端务必**容忍 `""` 与 `None`**：`scoring_mode=""` 时按「未知」处理（`passed` 显示为原始值 + `—` 说明），不要当 `binary-majority`。

---

## 11. `RadarService` 方法接口表

构造（§18 有可抄片段）：

```python
service = RadarService(RadarClient(RadarConfig.from_env()), RadarConfig.from_env())
```

### 11.1 频道与档位

| 方法 | 签名要点 | 返回 | 语义 / 口径注意 |
| --- | --- | --- | --- |
| `benchmarks()` | 无参数 | `tuple[BenchmarkInfo, ...]` | 频道清单。**无 meta**。两个频道分数不可比，UI 应给频道选择器 |
| `model_catalog(*, benchmark=None)` | | `tuple[ModelConfig, ...]` | 档位清单。事实源是 `/table` 的 `combos`，**不是价格表**（§3.3） |
| `table(*, benchmark=None)` | | `TablePayload` | 格子级明细。**大**（deep-swe 实测 7504 格 / 8.6 MB）—— 后端缓存后下发，不要前端直拉 |

### 11.2 榜单

| 方法 | 签名 | 返回 | 语义 / 口径注意 |
| --- | --- | --- | --- |
| `top_models(*, benchmark=None, effort=None, by="iq", limit=None, min_samples=None)` | `by ∈ {"iq","pass_rate","cost"}` | `tuple[tuple[ModelRow, ...], RadarMeta]` | 单频道内档位排行。`effort=None` → **取每个模型的最高档**并写 `meta.note="已取各模型最高档"`；`by="cost"` 时 `note` 追加「；成本为上游估算口径」。`limit=None` → 用 `config.max_models_listed`（默认 15）。`by` 非法 → `invalid_argument` |
| `model_profile(query, *, effort=None, benchmark=None, trend_hours=72, recent_limit=5)` | `query` 支持口语别名 | `ModelProfile` | 单模型档案：全部档位 + 效率 + 运行特征 + 综合 IQ + 趋势 + 最近流水。`effort=None` 时返回**全部档位**并写 `note="含全部档位"`（仅当 >1 个档位） |
| `compare(left, right, *, effort=None, benchmark=None)` | | `Comparison` | **delta 一律 `左 - 右`**（正数 = 左边更高/更贵/更慢）。任一侧无数据 → 该 delta 为 `None` |
| `resolve_model(query, known)` | 同步方法 | `str` | 口语名 → 上游 id。歧义 → `unknown_model` 带候选；空 → `invalid_argument` |
| `resolve_effort(effort)` | 同步方法 | `str \| None` | 归一档位名（大小写/分隔符无关）；非法 → `invalid_argument` |
| `highest_effort(rows)` | **staticmethod**，同步 | `tuple[ModelRow, ...]` | 每模型只留最高档（`ultra > max > xhigh > high > medium > low`） |

**排序规则（`by` 的稳定三级排序）**：

| `by` | 排序键 |
| --- | --- |
| `iq` | `iq` 降序 → `graded` 降序 → `model` 升序 |
| `pass_rate` | `pass_rate` 降序 → `graded` 降序 → `model` 升序 |
| `cost` | `average_price_usd` 升序（`None` 排最后）→ `pass_rate` 降序 → `model` 升序 |

> ⚠️ `by="cost"` 的排序用的是 `/intelligence-efficiency` 的 `average_price_usd`，**不是** `ModelRow` 上的字段。返回的仍是 `ModelRow`，**成本不在行里** —— 前端要显示成本得另外调 `value_picks()` 或直接读 `EfficiencyPoint`。

**`model_profile` 的一个坑（实测）**：`variants` 是**请求档位过滤后的**结果，而 `efficiency` / `metrics` / `insight` 都只取 **`variants[0]`（最低档）** 的那个。实测 `model_profile("astra")`（不带 effort）：

| 字段 | 值 |
| --- | --- |
| `variants` | `low, medium, high, xhigh, max, ultra`（6 个，全部） |
| `best` | `ultra`（最高档） |
| `profile.effort`（property） | `ultra`（= `best.effort`） |
| `insight.effort` | **`low`** |
| `efficiency.effort` | **`low`** |
| `metrics.effort` | **`low`** |

→ **`insight` / `efficiency` / `metrics` 说的是最低档（`low`），而 `best` 说的是最高档。** 前端必须用各自的 `.effort` 字段标注，不要假定它们和 `best` 同档。要精确数据请**显式传 `effort=`**。

另外 `trend_hours` 参数**当前未生效**（实测传 2 与 72 都返回 168 点）—— 见 §19。

### 11.3 推荐与预警

| 方法 | 签名 | 返回 | 语义 / 口径注意 |
| --- | --- | --- | --- |
| `recommendations(*, benchmark=None)` | | `tuple[Recommendation, ...]` | 上游 4 组固定 key，**原样转发**，不改排序、不重算 |
| `recommendations_meta(*, benchmark=None)` | | `RadarMeta` | 推荐组的口径信封（**单独一个方法**，因为 `recommendations()` 不返回 meta） |
| `degradation_alerts(*, benchmark=None)` | | `tuple[tuple[DegradationAlert, ...], RadarMeta]` | 降智预警。**只转发，不本地重算**（§13） |
| `value_picks(*, benchmark=None, limit=5, max_cost_usd=None, min_iq=None)` | | `tuple[tuple[EfficiencyPoint, ...], RadarMeta]` | 性价比：按 `combined_cost_index` **升序**（越低越划算），单频道内。`max_cost_usd` / `min_iq` 过滤掉 `None` 值行 |

### 11.4 趋势

| 方法 | 签名 | 返回 | 语义 / 口径注意 |
| --- | --- | --- | --- |
| `trend(model, *, effort=None, hours=72, benchmark=None)` | | `tuple[tuple[TrendPoint, ...], RadarMeta]` | 裸模型名 → 跨档位合并序列；带 `effort` → 单档位。**显式跳过 `latest:` 序列**。`meta.note` 是 `"跨档位合并口径"` / `"单档位 <effort>"` / `"无匹配序列"`。`meta.samples` 取**序列末点**的 `samples`。`hours` 是「保留最后 N 个点」（不是小时过滤），`points[-hours:]` |

### 11.5 题目

| 方法 | 签名 | 返回 | 语义 / 口径注意 |
| --- | --- | --- | --- |
| `task_detail(task_id, *, benchmark=None)` | 支持 id 或**关键词**模糊匹配 | `TaskDetail` | 题目元信息 + 所有档位格子 + `solved_by`（`p > 0` 的格子）。关键词命中多个 → `invalid_argument` 带候选；无命中 → `invalid_argument` |
| `task_ranking(*, benchmark=None, min_discrimination=None, limit=10)` | | `tuple[tuple[TaskInfo, ...], RadarMeta]` | 好题榜，按 `discrimination.score` 降序。**只有 deep-swe 有区分度**（pompeii 实测 0/86）→ pompeii 返回空元组 + `note="N 道题无区分度数据，未参与排序"`。`limit=None` → `config.max_tasks_listed`（默认 10） |
| `who_solved(task_id, *, benchmark=None)` | | `tuple[CellState, ...]` | 哪些档位做出来了（`p > 0`）。**无 meta** |

### 11.6 社区与实时

| 方法 | 签名 | 返回 | 语义 / 口径注意 |
| --- | --- | --- | --- |
| `top_contributors(*, scope="month", limit=10, benchmark=None)` | `scope ∈ {"month","total"}` | `tuple[tuple[ContributorRow, ...], RadarMeta]` | `month` 用 `month_points` 排序，`total` 用 `points`。同分按 `display_name` 升序。非法 `scope` → `invalid_argument` |
| `fleet_pulse(*, benchmark=None)` | | `tuple[FleetPulse \| None, RadarMeta]` | 全队提交吞吐。**`scope` 是全队，不是单模型** —— 文案必须说清，否则会被读成「这个模型每小时烧 27 美元」。**可能为 `None`** |
| `flag_race(*, benchmark=None)` | | `tuple[FlagRace \| None, RadarMeta]` | 夺旗赛。**可能为 `None`** |
| `recent_events(*, limit=10, benchmark=None)` | **`limit > 50` → `invalid_argument`** | `tuple[tuple[RadarEvent, ...], RadarMeta]` | 最近判分流水。`limit ≤ 0` 返回空元组（不报错）。实测 `limit=50` 返回 50 条 |

---

## 12. 返回数据结构字典

> 每个表都给出：**插件字段名** / 类型 / 单位 / 口径 / 是否可能 `None` / **上游原名**（走路径 B 时用）。
> 「夹具样例值」一列逐字取自 `tests/fixtures/radar/*.json`。
> 所有模型都是 `@dataclass(frozen=True, slots=True)`：**只读、不可变、无 `__dict__`**（序列化用 `dataclasses.asdict()`）。

### 12.1 `RadarMeta`

见 §10 的完整字段表。

### 12.2 `BenchmarkInfo`（频道）

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `id` | `str` | 频道 id | 否 | `id` | `deep-swe` |
| `title` | `str` | | 否 | `title` | `代码修复 · DeepSWE` |
| `short_title` | `str` | | 否 | `short_title` | `DeepSWE` |
| `description` | `str` | | 否 | `description` | `真实开源仓库任务，恢复代码行为并由容器测试判分。` |
| `task_count` | `int` | 题数 | 否 | `task_count` | `112` |
| `scoring_mode` | `str` | 聚合方式 | 否 | `scoring_mode` | `binary-majority` |
| `score_label` | `str` | 分数口径名 | 否 | `score_label` | `Pass rate` |
| `rolling_window` | `int` | 窗口 | 否 | `rolling_window` | `3` |
| `model_config_count` | `int` | 档位数 | 否 | `model_config_count` | `67` |
| `reference_task_id` | `str \| None` | | **是** | `reference_task_id` | `null`（pompeii 为 `RP-group-1-public-example`） |
| `reference_url` | `str \| None` | | **是** | `reference_url` | `null`（pompeii 有值） |
| `task_bundle` | `TaskBundle \| None` | | **是** | `task_bundle` | `null`（pompeii 有值） |
| `default` | `bool` | 是否默认频道 | 否 | `default` | `true` |

**`TaskBundle`**（4 字段）：`url` / `sha256` / `bytes`（`int \| None`）/ `format`。pompeii 样例：`{url: "/api/v1/benchmark-bundles/pompeii-adjacency", sha256: "e969b5321a541245e81e2e6ae5315c279382867f5966441336f3bd9ec51fa898", bytes: 63775935, format: "tar.gz"}`。

> 缺失时前端怎么显示：`reference_task_id` / `reference_url` / `task_bundle` 为 `None` 时**整块隐藏**（不要显示空行）。

### 12.3 `TaskInfo`（题目）

| 字段 | 类型 | 口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `id` | `str` | | 否 | `id` | `abs-module-cache-flags` |
| `title` | `str` | | 否 | `title` | `Harden module loading, cache introspection, and script flags` |
| `language` | `str` | | 否 | `language` | `go`（pompeii：`vision`） |
| `repo` | `str` | | 否 | `repo` | `https://github.com/abs-lang/abs` |
| `category` | `str` | | 否 | `category` | `enhancement`（pompeii：`visual-reconstruction`） |
| `fragment_count` | `int \| None` | **仅 pompeii** | **是** | `fragment_count` | `null`（pompeii rp-002 为 `7`） |
| `metric` | `str \| None` | **仅 pompeii** | **是** | `metric` | `null`（pompeii 为 `adjacency_f1`） |
| `discrimination` | `Discrimination \| None` | **仅 deep-swe** | **是** | `discrimination` | 见下 |

**关键**：`fragment_count` / `metric` 只在 pompeii 出现，`discrimination` 只在 deep-swe 出现 —— **两频道的键集不同，前端必须按存在性分支**。

### 12.4 `Discrimination`（题目区分度）

| 字段 | 类型 | 口径 | 上游原名 | 夹具样例值（`abs-module-cache-flags`） |
| --- | --- | --- | --- | --- |
| `score` | `float` | **合成区分度（0–100）**，高 = 好题 | `score` | `79.7` |
| `confidence` | `float` | 置信度，低 = 结论不稳 | `confidence` | `0.693` |
| `raw_score` | `float` | 原始区分度（0–1 量级） | `raw_score` | `0.110518` |
| `model` | `float` | 模型维度分 | `model` | `10.0` |
| `effort` | `float` | 档位维度分 | `effort` | `7.4` |
| `monotonic` | `float` | 单调性分 | `monotonic` | `3.0` |
| `config` | `float` | 配置维度分 | `config` | `11.1` |
| `threshold` | `float` | 达标阈值 | `threshold` | `21.3` |
| `cells` | `int` | 参与统计的格子数 | `cells` | `56` |
| `samples` | `int` | 参与统计的样本数 | `samples` | `138` |

> ⚠️ `Discrimination.model` / `.effort` 是**维度分（float）**，不是模型名/档位名 —— 不要和 `ModelRow.model` / `.effort` 混淆。
> ⚠️ `parse_discrimination()` 对所有字段用 `_num(..., 0.0)` 兜底 → **上游缺键时会得到 `0.0`，不是 `None`**。前端遇到全 0 应视为「无数据」。
> 排序方法 `task_ranking()` 会跳过 `discrimination is None` 的题目（pompeii 全部 86 题）。

### 12.5 `ModelRow`（榜单一行）

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `model` | `str` | | 否 | `model` | `gpt-6-astra` |
| `effort` | `str` | 档位 | 否 | `effort` | `low` |
| `graded` | `int` | **运行数**（≠ 题目数） | 否 | `graded` | `135` |
| `passed` | `int` | 通过数（二值制） | 否 | `passed` | `88` |
| `score_sum` | `float` | 窗口分数和 | 否 | `score_sum` | `88.0` |
| `cells` | `int` | **题目数** | 否 | `cells` | `110` |
| `cells_passed` | `int` | 通过的题目数 | 否 | `cells_passed` | `75` |
| `pass_rate` | `float` | **0–1 比例**（口径 = `score_label`） | 否 | `pass_rate` | `0.682` |
| `iq` | `float \| None` | 0–150 IQ | **是** | **无**（插件派生） | `108.62`（insights 值）或 `102.3`（兜底） |
| `iq_derived` | `bool` | **`True` = 本地兜底换算** | 否 | **无**（插件派生） | `false` |
| `tasks` | `Mapping[str, TaskVote]` | `task_id` → 多数表决 | 否（可为空 dict） | `tasks` | 5 个键（夹具裁剪后） |
| `meta` | `RadarMeta \| None` | | **是**（provider 会填） | **无** | — |
| `key`（property） | `str` | `"model@effort"` | 否 | — | `gpt-6-astra@low` |

**`pass_rate` 是 0–1 比例，不是百分数。** 前端要乘 100 再显示 `%`。纯文本层用 `_pct()` 做这件事（`0.682 → 68.2%`）。

### 12.6 `TaskVote`（一道题上的多数表决）

| 字段 | 类型 | 口径 | 夹具样例值（`abs-module-cache-flags`，`gpt-6-astra@low`） |
| --- | --- | --- | --- |
| `votes` | `int` | 该题该档位的有效运行数 | `2` |
| `pass_votes` | `int` | 其中通过数 | `1` |
| `majority_pass` | `bool` | **多数表决结果**（`binary-majority` 判定） | `false` |
| `score_sum` | `float` | 分数和 | `1.0` |
| `score_rate` | `float` | 分数率 | `0.5` |

另一条样例（`aiomonitor-task-snapshots-diff`，同档位）：`votes=3, pass_votes=2, majority_pass=true, score_sum=2.0, score_rate=0.6666666666666666`。

### 12.7 `CellState`（格子）

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `cell_id` | `str` | **`"<task_id>\|<model>\|<effort>"`** | 否 | dict 键 | `abs-module-cache-flags\|gpt-5.6-sol\|low` |
| `task_id` | `str` | 从 `cell_id` 拆出 | 否 | — | `abs-module-cache-flags` |
| `model` / `effort` | `str` | 从 `cell_id` 拆出 | 否 | — | `gpt-5.6-sol` / `low` |
| `st` | `str` | `open`/`cooldown`/`leased`/`running` | 否 | `st` | `open` |
| `n` | `int` | **窗口内**运行数（≤ `rolling_window`） | 否 | `n` | `3` |
| `p` | `int` | 窗口内通过数 | 否 | `p` | `3` |
| `score_sum` | `float` | 窗口分数和（连续制下是 F1 和） | 否 | `score_sum` | `3.0` |
| `rate` | `float` | `score_sum / n` | 否（**上游 `null` → `0.0`**） | `rate` | `1.0` |
| `total_n` | `int` | **历史累计**运行数 | 否 | `total_n` | `23` |
| `total_p` | `int` | 历史累计通过数 | 否 | `total_p` | `22` |
| `total_score` | `float` | 历史累计分数和 | 否 | `total_score` | `22.0` |
| `base_mult` | `float` | 基础积分倍率 | 否 | `base_mult` | `2.0` |
| `wasteland` | `bool` | 是否「荒地」 | 否 | `wasteland` | `false` |
| `wasteland_multiplier` | `float` | 荒地加成 | 否 | `wasteland_multiplier` | `1.0` |
| `mult` | `float` | 最终倍率 | 否 | `mult` | `2.0` |
| `last_graded_at` | `str \| None` | ISO8601 | **是** | `last_graded_at` | `2026-08-29T16:05:20+00:00` |
| `last_points` | `float \| None` | 积分 | **是** | `last_points` | `1.6` |
| `cost` | `float \| None` | USD（**预估**） | **是** | `cost` | `0.73` |
| `cost_src` | `str \| None` | **数值来源**（§8） | **是** | **`src`** | `measured` |
| `minutes` | `int \| None` | 预估耗时（分钟） | **是** | **`min`** | `5` |
| `ran_by` | `tuple[RunRecord, ...]` | 窗口内运行记录 | 否（可为空） | `ran_by` | 3 条 |
| `cost_is_estimate`（property） | `bool` | `cost_src != "measured"` | 否 | — | `false` |

**⚠️ 上游 `rate: null` 被解析成 `0.0`**（`parse_cell` 用 `_num(..., 0.0) or 0.0`）。实测夹具里 `gemini-3.8-flash@low` 的 `rate` 原文是 `null`（该格 `n=0`）。**前端不能把 `0.0` 当成「实测 0 分」** —— 请用 `n == 0` 或 `st`/`wasteland` 判断「无数据」，显示 `—`。

**其他「显式 null」样例**（`pompeii-adjacency-rp-006|gpt-6-astra|low`）：`n=0, p=0, score_sum=0, rate=null→0.0, total_n=0, base_mult=2.0, wasteland=true, wasteland_multiplier=1.5, mult=3.0, src=null→None, ran_by=[]`。

**同一格子里多条 run 的价表版本不同**（§8.3）—— `ran_by` 里逐条给出 `token_pricing_version`。

**上游有、但插件未建模的 cell 键**（走路径 B 时你会看到，插件路径拿不到）：`ns`、`base`、`provider`、`agent`、`agent_version`、`manual_only`、`billing_mode`、`cost_status`、`cost_is_estimate`、`pricing_band`、`cost_by_band`、`reopen_after_hours`。夹具里 `abs-module-cache-flags|deepseek-v4.1-flash|max` 就有 `provider: "deepseek"`、`billing_mode: "api"`、`pricing_band: "off_peak"`、`cost_by_band: {"off_peak": 0.41243, "peak": 0.82486}`、`reopen_after_hours: 12.0`。**要显示这些必须走路径 B 或改后端。**

### 12.8 `RunRecord`（一次有效运行）

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `display_name` | `str` | **身份已归一**（`login` 优先，否则 `nickname`） | 否 | `login` / `nickname` | `volunteer-01` |
| `avatar_url` | `str \| None` | 头像 URL（**夹具里一律 `null`**） | **是** | `avatar_url` | `null` |
| `passed` | `bool` | 是否通过 | 否 | `passed` | `true` |
| `score` | `float` | 该次得分 | 否 | `score` | `1.0` |
| `graded_at` | `str` | ISO8601 | 否 | `graded_at` | `2026-08-29T16:05:20+00:00` |
| `points_base` | `float \| None` | 基础积分 | **是** | `points_base` | `0.8` |
| `points_multiplier` | `float \| None` | 倍率 | **是** | `points_multiplier` | `2.0` |
| `duration_sec` | `float \| None` | **秒** | **是** | `duration_sec` | `373.1` |
| `actual_cost_usd` | `float \| None` | USD | **是** | `actual_cost_usd` | `0.775488` |
| `cost_source` | `str \| None` | | **是** | `cost_source` | `tokens` |
| `cost_complete` | `bool \| None` | token 证据是否完整 | **是** | `cost_complete` | `true` |
| `token_pricing_version` | `str \| None` | **计价版本**（历史成本不可用最新价表重算） | **是** | `token_pricing_version` 或 `settled_token_pricing_version` | `official-api-equivalent-gemini-3.7-flash-2026-08-28-v12` |

> 上游身份字段是**二选一**：有 GitHub 绑定的给 `login` + `avatar_url`；未绑定的给 `nickname` + `avatar_seed`。**`avatar_seed` 未建模**（走路径 B 才能拿到）。`avatar_url` 为 `null` 时前端应显示默认头像/首字母，不要显示破图。

### 12.9 `EfficiencyPoint`（性价比 / 效率点）

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `model` / `effort` | `str` | | 否 | 同 | `gpt-6-astra` / `low` |
| `iq` | `float \| None` | 0–150 | **是** | `iq` | `98.51` |
| `passed` | `float \| None` | **整数制=计数；连续制=F1 加权和** | **是** | `passed` | `88.0`（pompeii：`49.79059751561299`） |
| `total` | `float \| None` | 同上 | **是** | `total` | `134.0`（pompeii：`55`） |
| `average_price_usd` | `float \| None` | USD | **是** | `average_price_usd` | `1.978303` |
| `average_minutes` | `float \| None` | 分钟 | **是** | `average_minutes` | `8.7` |
| `combined_cost_index` | `float \| None` | **费用+耗时综合指数，越小越省** | **是** | `combined_cost_index` | `129.37` |
| `average_agent_steps` | `float \| None` | 步数 | **是** | `average_agent_steps` | `25.533834586466167` |
| `agent_steps_samples` | `int \| None` | **该均值的样本量** | **是** | `agent_steps_samples` | `133` |
| `average_total_tokens` | `float \| None` | token | **是** | `average_total_tokens` | `976076.1119402985` |
| `token_samples` | `int \| None` | | **是** | `token_samples` | `134` |
| `cache_hit_rate` | `float \| None` | 0–1 比例 | **是** | `cache_hit_rate` | `0.9446170002630452` |
| `cache_token_samples` | `int \| None` | | **是** | `cache_token_samples` | `134` |
| `runs_24h` | `int` | | 否（缺键→`0`） | `runs_24h` | `0` |
| `runs_48h` | `int` | | 否 | `runs_48h` | `3` |
| `runs_total` | `int` | | 否 | `runs_total` | `135` |
| `source_updated_at` | `str \| None` | **point 级数据时间（优先用它）** | **是** | `source_updated_at` | `2026-09-12T03:58:59+00:00` |
| `key`（property） | `str` | `"model@effort"` | 否 | — | `gpt-6-astra@low` |

**⚠️ 三种「样本量」不要混**：`runs_total`（运行数）≠ `agent_steps_samples`（步数均值基于几次）≠ `token_samples`（token 均值基于几次）。实测 `gpt-6-astra@low`：`runs_total=135`、`agent_steps_samples=133`、`token_samples=134`。

**⚠️ 上游有、插件未建模**：`average_price_usd_by_band`（分档/分时价格下的平均，实测 `null`）。

### 12.10 `MetricPoint`（运行特征）

`mode = latest_valid_per_task`（**与主榜不同口径**）。

| 字段 | 类型 | 单位 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `model` / `effort` | `str` | | 否 | 同 | `gpt-6-astra` / `low` |
| `average_agent_steps` | `float \| None` | 步 | **是** | `average_agent_steps` | `27.256880733944953` |
| `agent_steps_samples` | `int \| None` | | **是** | `agent_steps_samples` | `109` |
| `average_total_tokens` | `float \| None` | token | **是** | `average_total_tokens` | `1078396.7181818183` |
| `token_samples` | `int \| None` | | **是** | `token_samples` | `110` |
| `cache_hit_rate` | `float \| None` | 0–1 | **是** | `cache_hit_rate` | `0.9475937143209523` |
| `cache_token_samples` | `int \| None` | | **是** | `cache_token_samples` | `110` |
| `runs_24h` / `runs_48h` / `runs_total` | `int` | | 否 | 同 | `0` / `3` / `135` |
| `key`（property） | `str` | | 否 | — | `gpt-6-astra@low` |

**注意**：同一个 `gpt-6-astra@low`，`/intelligence-efficiency` 给 `average_agent_steps = 25.533834586466167`（`agent_steps_samples = 133`），`/model-metrics` 给 `27.256880733944953`（`agent_steps_samples = 109`）—— **两个数不同，因为口径不同**（`equal_latest_3` vs `latest_valid_per_task`）。前端不要以为其中一个错了。

### 12.11 `InsightPoint`（综合 IQ 点）

| 字段 | 类型 | 单位 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `model` / `effort` | `str` | | 否 | 同 | `gpt-6-astra` / `low` |
| `iq` | `float` | **0–150 综合 IQ** | 否 | `iq` | `109.19` |
| `software_iq` | `float \| None` | 编程频道 IQ | **是** | `software_iq` | `98.51` |
| `visual_iq` | `float \| None` | 视觉频道 IQ | **是** | `visual_iq` | `135.71` |
| `samples` | `int` | | 否 | `samples` | `188` |
| `key`（property） | `str` | | 否 | — | `gpt-6-astra@low` |

**三个 IQ 必须一起给**（§4.5）。

### 12.12 `TrendPoint`（趋势点）

`provider.parse_trend_point()` 把两个上游形状归一到这里：`/iq-history` 的 `{ts, score, n}` 与 `/radar-insights` 的 `{timestamp, iq, samples}`。

| 字段 | 类型 | 单位 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `timestamp` | `str` | ISO8601 | 否（缺失→`""`） | `timestamp` 或 **`ts`** | `2026-09-06T10:00:03+00:00` |
| `iq` | `float` | 0–150 | 否（缺失→`0.0`） | `iq` 或 **`score`** | `105.4` |
| `samples` | `int` | | 否（缺失→`0`） | `samples` 或 **`n`** | `471` |

> ⚠️ 缺失时兜底成 `""` / `0.0` / `0`，**不是 `None`**。前端遇到 `iq == 0.0` 且 `samples == 0` 应视为无效点并跳过，不要画到图上。

### 12.13 `Recommendation` / `RecommendationItem`

`Recommendation`（4 字段）：

| 字段 | 类型 | `None`? | 夹具样例值 |
| --- | --- | --- | --- |
| `key` | `str` | 否 | `daily_development` / `hard_problems` / `background_automation` / `lobster_tasks` |
| `title` | `str` | 否 | `日常开发` / `难题攻坚` / `后台自动化` / `跑龙虾类任务` |
| `rule` | `str` | 否 | 上游规则**原文**（中文长句，逐字保留） |
| `items` | `tuple[RecommendationItem, ...]` | 否 | 每组 2 个（实测） |

`RecommendationItem`（9 字段）：

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `model` / `effort` | `str` | | 否 | 同 | `gpt-6-astra` / `low` |
| `iq` | `float` | 综合 IQ | 否 | `iq` | `109.19` |
| `weighted_passed` | `float` | **加权通过数，不是题数** | 否 | **`passed`** | `136.85726418227966` |
| `samples` | `int` | | 否 | `samples` | `188` |
| `average_cost_usd` | `float \| None` | USD | **是** | `average_cost_usd` | `1.921132` |
| `average_duration_minutes` | `float \| None` | 分钟 | **是** | `average_duration_minutes` | `8.76` |
| `combined_cost_index` | `float \| None` | 越小越省 | **是** | `combined_cost_index` | `128.016` |
| `trend_48h` | `tuple[TrendPoint, ...]` | 48 小时趋势 | 否（可为空） | `trend_48h` | 夹具裁剪成 3 点；**实测 48 点** |

**⚠️ 插件丢弃了上游 item 的两个样本量字段**：`cost_samples`（188）与 `duration_samples`（188）**未建模**。如果前端要显示「这个平均成本基于多少样本」，需要走路径 B 或改后端。

### 12.14 `DegradationAlert`（降智预警）

见 §13 的完整字段表。

### 12.15 `ContributorRow`（贡献者）

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `display_name` | `str` | **归一**（`nickname` 优先，否则 `github_login`） | 否 | `nickname` / `github_login` | `volunteer-01` |
| `github_login` | `str \| None` | | **是** | `github_login` | `volunteer-01`（volunteer-02 为 `null`） |
| `avatar_url` | `str \| None` | | **是** | `avatar_url` | `null` |
| `submissions` | `int` | 累计提交 | 否 | `submissions` | `1772` |
| `graded` | `int` | 累计判分 | 否 | `graded` | `1745` |
| `points` | `float` | **总榜**积分 | 否 | `points` | `64813.1` |
| `month_points` | `float` | **月榜**积分 | 否 | `month_points` | `2296.5` |
| `tokens` | `int` | 累计 token | 否 | `tokens` | `33983024324` |
| `folded_usd` | `float` | 「折叠」口径 USD | 否 | `folded_usd` | `17762.97` |
| `deepseek_api_usd` | `float` | DeepSeek 直连口径 USD | 否 | `deepseek_api_usd` | `91.32` |
| `usd` | `float` | **上游口径 `folded_usd + deepseek_api_usd`** | 否 | `usd` | `17854.29` |
| `is_radar_admin` | `bool` | | 否 | `is_radar_admin` | `false` |
| `flag_race_winner` | `bool` | | 否 | `flag_race_winner` | `true`（volunteer-01） |
| `rank_change_24h` | `int \| None` | 正 = 上升 | **是** | `rank_change_24h` | `0` |

> **`usd` 直接用上游值，插件不自行加总**（避免与上游口径漂移）。校验：`17762.97 + 91.32 = 17854.29` ✓。
> **插件丢弃的贡献者字段**（上游有 34 字段，插件只建模 14）：`avatar_seed`、`points_by_harness`、`month_points_by_harness`、`month_graded`、`month_submissions`、`month_tokens`、`month_folded_usd`、`month_usd`、`month_deepseek_api_*`、`contribution_streak`（连击体系）、`month_rank_change_24h`、以及 `riding_*`（骑行中）系列。**要显示连击/分工具积分必须走路径 B 或改后端。**
>
> `display_name` 与 `github_login` **语义不同**：`display_name` 是 `nickname` 优先，`github_login` 是 GitHub 登录名（可能 `null`）。前端用 `display_name` 做主显示。

### 12.16 `RadarEvent`（判分流水）

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `graded_at` | `str` | ISO8601 | 否 | `graded_at` | `2026-09-13T09:12:43+00:00` |
| `passed` | `bool` | | 否 | `passed` | `false` |
| `score` | `float` | | 否 | `score` | `0.0` |
| `task_id` | `str` | | 否 | `task_id` | `pest-character-class-coalescing` |
| `model` / `effort` | `str` | | 否 | 同 | `grok-4.6` / `high` |
| `harness` | `str` | 运行工具 | 否 | `harness` | `grok-build` |
| `cost_usd` | `float \| None` | USD | **是** | `cost_usd` | `2.83`（第 3 条为 `null`） |
| `cost_source` | `str \| None` | | **是** | `cost_source` | `api_equivalent_tokens`（第 3 条为 `unavailable`） |
| `cost_is_estimate` | `bool` | | 否 | `cost_is_estimate` | `false` |
| `cost_is_fallback_estimate` | `bool` | | 否 | `cost_is_fallback_estimate` | `false` |
| `cost_is_api_equivalent` | `bool` | | 否 | `cost_is_api_equivalent` | `true` |
| `points` | `float \| None` | 给贡献者的积分 | **是** | `points` | `16.97`（第 3 条为 `null`） |
| `points_deferred` | `bool` | 积分是否延后发放 | 否 | `points_deferred` | `false`（第 3 条为 `true`） |
| `display_name` | `str \| None` | 归一身份 | **是** | `login` / `nickname` | `volunteer-01` |
| `avatar_url` | `str \| None` | | **是** | `avatar_url` | `null` |

**三个成本布尔必须原样透传**（`估算` / `回退估算` / `API 等价`），纯文本层的做法是拼成 `（估算/回退估算/API 等价）` 后缀。

**注意「证据不足」的形态**（夹具第 3 条）：`cost_usd: null` + `cost_source: "unavailable"` + `points: null` + `points_deferred: true`。前端应显示 `—` 与「积分延后」标记，**不要显示 `$0.00`**。

### 12.17 `HistorySeries`（历史序列）

| 字段 | 类型 | 口径 | `None`? | 夹具样例值 |
| --- | --- | --- | --- | --- |
| `key` | `str` | **原始键**（含 `latest:` 前缀） | 否 | `latest:gpt-6-astra@low` |
| `model` | `str` | 去前缀后的模型名 | 否 | `gpt-6-astra` |
| `effort` | `str \| None` | **裸模型名时为 `None`** | **是** | `low` |
| `points` | `tuple[TrendPoint, ...]` | 时间点 | 否（可为空） | 5 点（夹具裁剪）；**实测 168 点** |
| `latest` | `bool` | **`True` = `latest:` 窗口**（必须标注） | 否 | `true` |
| `stale` | `bool` | 是否来自陈旧缓存 | 否 | `false` |

**无 `meta`**：`RadarClient.history()` 返回裸 `tuple[HistorySeries, ...]`，没有 `RadarMeta`。前端要知道这条数据的口径只能看 `key` / `latest` / `effort`。

### 12.18 `FleetPulse`（全队吞吐）

`RadarService.fleet_pulse()` 返回 `tuple[FleetPulse | None, RadarMeta]` —— **可能是 `None`**。

| 字段 | 类型 | 单位/口径 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `window_minutes` | `int` | 窗口（分钟） | 否 | `window_minutes` | `60` |
| `submitted_runs` | `int` | 已提交运行数 | 否 | `submitted_runs` | `10` |
| `tokens_per_hour` | `int \| None` | token/时 | **是** | `tokens_per_hour` | `61950772` |
| `cache_hit_ratio` | `float \| None` | 0–1 | **是** | `cache_hit_ratio` | `0.9415` |
| `api_equivalent_usd_per_hour` | `float \| None` | USD/时 | **是** | `api_equivalent_usd_per_hour` | `25.07` |
| `usd_per_hour` | `float \| None` | USD/时（合计口径） | **是** | `usd_per_hour` | `27.55` |

**⚠️ `scope` 是「全队已提交运行」（上游 `scope: "fleet_submitted"`），不是单模型。** 文案必须写「全队」，否则会被读成「这个模型每小时烧 27 美元」。插件**没有建模** `scope` 字段本身 —— 前端文案请自己写死「全队」。

### 12.19 `FlagRace`（夺旗赛）

`RadarService.flag_race()` 返回 `tuple[FlagRace | None, RadarMeta]` —— **可能是 `None`**。

| 字段 | 类型 | 单位 | `None`? | 上游原名 | 夹具样例值 |
| --- | --- | --- | --- | --- | --- |
| `status` | `str` | | 否 | `status` | `won` |
| `target_usd` | `float \| None` | USD | **是** | `target_usd` | `150000.0` |
| `current_usd` | `float \| None` | USD | **是** | `current_usd` | `170335.57` |
| `reward_points` | `float \| None` | 积分 | **是** | `reward_points` | `1500.0` |
| `winner_name` | `str \| None` | | **是** | `winner.nickname` / `winner.github_login` | `volunteer-06` |
| `winning_task_id` | `str \| None` | | **是** | `submission.task_id` | `psd-tools-blend-range-api` |
| `winning_model` | `str \| None` | | **是** | `submission.model` | `gpt-5.6-sol` |
| `winning_effort` | `str \| None` | | **是** | `submission.effort` | `medium` |

> 上游 `winner.github_login` 实测可能为 `null`（只有昵称）→ 插件按存在性解析，**不要假定它存在**。
> 进度条可以用 `current_usd / target_usd`（样例 170335.57 / 150000.0 > 1，说明已超额完成）。

### 12.20 `ModelProfile`（模型档案，派生结构）

| 字段 | 类型 | 口径 | `None`? | 实测/夹具值 |
| --- | --- | --- | --- | --- |
| `model` | `str` | 解析后的上游 id | 否 | `gpt-6-astra` |
| `variants` | `tuple[ModelRow, ...]` | **请求档位过滤后的**档位行 | 否（至少 1 个，否则抛错） | 6 个（`low`…`ultra`） |
| `best` | `ModelRow \| None` | 最高档那行 | **是** | `ultra` |
| `efficiency` | `EfficiencyPoint \| None` | **`variants[0]`（最低档）**的效率点 | **是** | `low`，`average_price_usd=1.978303` |
| `metrics` | `MetricPoint \| None` | **`variants[0]`** 的运行特征 | **是** | `low` |
| `insight` | `InsightPoint \| None` | **`variants[0]`** 的综合 IQ | **是** | `low`，`iq=108.62` |
| `trend` | `tuple[TrendPoint, ...]` | `variants[0]` 的单档位趋势（**跳过 `latest:`**） | 否（可为空） | 168 点 |
| `recent` | `tuple[RadarEvent, ...]` | 该模型该档位的最近流水（默认 ≤5 条） | 否（可为空） | 可能 0 条 |
| `meta` | `RadarMeta \| None` | | **是** | `note="含全部档位"` |
| `effort`（property） | `str \| None` | `best.effort`，无 `best` 时 `variants[0].effort` | **是** | `ultra` |

**⚠️ 见 §11.2 的坑**：`best` 是最高档，而 `efficiency` / `metrics` / `insight` / `trend` 都是**最低档**。

**容错**：`efficiency` / `metrics` / `trend` / `recent` 任一上游失败都**不致命**（`service` 用 `try/except` 降级为 `None` / 空元组），主查询仍成功。前端必须处理这些字段为 `None` 的情况。

### 12.21 `Comparison`（对比，派生结构）

| 字段 | 类型 | 口径 | `None`? | 实测值（`astra` vs `sol`，无 effort） |
| --- | --- | --- | --- | --- |
| `left` / `right` | `ModelProfile` | 两个档案 | 否 | `gpt-6-astra@ultra` / `gpt-5.6-sol@max` |
| `iq_delta` | `float \| None` | **`左 - 右`** | **是** | `-9.300000000000011` |
| `pass_rate_delta` | `float \| None` | **`左 - 右`**，比例 | **是** | `-0.062000000000000055` |
| `cost_delta_usd` | `float \| None` | `左 - 右`，USD | **是** | `-6.006316` |
| `duration_delta_minutes` | `float \| None` | `左 - 右`，分钟 | **是** | `-25.459999999999997` |
| `cost_index_delta` | `float \| None` | `左 - 右` | **是** | `-33864.625` |
| `meta` | `RadarMeta \| None` | **取自 `left.meta`** | **是** | — |

**规则**：**delta 一律 `左 - 右`**（正数 = 左边更高/更贵/更慢）。任一侧为 `None` → delta 为 `None`（**不要用 0 代替**）。
`meta` 只反映左侧频道（两侧同频道时无影响）。

### 12.22 `TaskDetail`（题目详情，派生结构）

| 字段 | 类型 | 口径 | `None`? | 夹具实测值 |
| --- | --- | --- | --- | --- |
| `task` | `TaskInfo` | 题目元信息 | 否 | `abs-module-cache-flags`（`discrimination.score=79.7`） |
| `cells` | `tuple[CellState, ...]` | 该题**所有档位**的格子，按 `(model, 档位序)` 升序 | 否（可为空） | 5 个（夹具裁剪后） |
| `solved_by` | `tuple[CellState, ...]` | **`p > 0`** 的格子 | 否（可为空） | 2 个 |
| `meta` | `RadarMeta \| None` | 来自 `/table` | **是** | — |

`who_solved()` 就是 `task_detail(...).solved_by`。

**⚠️ `solved_by` 的判定是 `p > 0`（窗口内通过数 > 0），不是 `rate > 0`。** 连续制频道下 `p` 是整数计数、`score_sum` 才是浮点分。设计文档 `plugin_api.md` §5.2 写的是「`rate>0` 的」，与实现（`cell.p > 0`）**不一致** —— 见 §19。

### 12.23 其他 payload（走路径 B 或需要扩展时会遇到）

这些是 `provider` 返回但 `service` 未包装成专用方法的 payload。列在这里供参考（前端一般用不到）：

| 类型 | 字段 | 备注 |
| --- | --- | --- |
| `LeaderboardPayload` | `meta` / `models` / `tasks`(题 id 字符串元组) / `contributors` / `pulse` / `flag_race` / `pending_grades` / `error_grades` / `online_volunteers` | 实测 `pending_grades=0`、`error_grades=458`、`online_volunteers=2`。**未建模**：`history`（赛季）、`month`、`latest_burn`、`k`、`site_name`、`site_url` |
| `TablePayload` | `meta` / `tasks` / `cells`(dict) / `combos` / `baseline_generated_at` / `discrimination_generated_at` | 实测 `baseline_generated_at="2026-09-13T09:16:19.380020+00:00"`、`discrimination_generated_at="2026-09-12T20:10:55+00:00"`。**未建模**（上游 22 个顶层键中，`TablePayload` 与 `RadarMeta` 都没覆盖的 12 个）：`token_pricing`（价表）、`client_contract`、`benchmark_policy_version`、`idle_mult_credit_before` / `idle_mult_credit_hours`、`reopen_after_hours`、`tier_windows_usd`、`online_volunteers`、`k`、`benchmarks`、`discrimination_method`、`schema` |
| `InsightsPayload` | `meta` / `comprehensive_points` / `recommendations` / `degradation_alerts` / `degradation_rule` / `generated_at` | **未建模**：`software_source_updated_at`、`visual_source_updated_at`、`schema` |
| `EfficiencyPayload` | `meta` / `points` | **未建模**：`runs_24h_total` / `runs_48h_total` / `runs_total`（顶层总计）、`schema` |
| `MetricsPayload` | `meta` / `points` | 同上 |
| `EventsPayload` | `meta` / `events` | — |
| `QuotaPayload` | `meta` / `quota_window` / `source` / `measured_at` / `updated_at` / `tier_windows_usd` | 夹具：`quota_window="7d"`、`source="super-account-app-server-measurement"`、`measured_at="2026-08-09T11:49:26.993173+00:00"`、`tier_windows_usd={"plus":82.486,"pro-5x":412.43,"pro-20x":1649.72}`。⚠️ **时间戳比其它端点旧一个多月**，是低频标定端点，不是实时额度 |
| `SuggestPayload` | `meta` / `cells`(`SuggestCell`) / `holding` / `replaceable_unstarted` / `protected_started` | 夹具：`holding=0`、`replaceable_unstarted=0`、`protected_started=0`。⚠️ 这是**待认领格子**，不是成绩 |
| `SuggestCell` | `task_id` / `model` / `effort` / `agent` / `agent_version` / `est_minutes` / `est_quota_pct` / `tier_windows_usd` | 夹具：`{task_id:"yaegi-go-embed-directives", model:"gpt-5.6-sol", effort:"medium", agent:"codex", agent_version:"0.154.0", est_minutes:18, est_quota_pct:3.7}` |
| `ModelConfig` | `model` / `effort` | `model_catalog()` 的返回元素 |

---

## 13. 降智预警

### 13.1 上游实测结构与设计文档不同

**实测（2026-09-14，pompeii 返回 4 条 / deep-swe 返回 0 条）**，每条预警有 **17 个键**：

```
average_cost_usd, average_duration_minutes, average_iq_24h, average_iq_48h,
degradation_12h_iq, degradation_24h_iq, degradation_48h_iq, degradation_severity_score,
effort, from_24h_average_iq, from_24h_high_iq, from_48h_average_iq, from_48h_high_iq,
iq, model, smooth_degradation_24h_iq, trend_48h
```

`provider.parse_alert()` 做的映射（**14 个键 → 字段**）：

| 上游键 | 插件字段 |
| --- | --- |
| `model` / `effort` | `model` / `effort`（**必需**，缺失 → `schema_drift`） |
| `iq` | **`current_iq`** |
| `average_iq_24h` | **`avg_24h`** |
| `average_iq_48h` | **`avg_48h`** |
| `degradation_24h_iq` | **`delta_24h`** |
| `degradation_48h_iq` | **`delta_48h`** |
| `degradation_severity_score` | **`severity`** |
| `average_cost_usd` | `average_cost_usd` |
| `average_duration_minutes` | `average_duration_minutes` |
| `smooth_degradation_24h_iq` | `smooth_delta_24h` |
| `from_24h_high_iq` | `peak_24h_iq` |
| `from_48h_high_iq` | `peak_48h_iq` |
| `trend_48h` | `trend_48h`（`tuple[TrendPoint, ...]`） |
| 全部 17 个键名 | **`raw_keys`**（`tuple[str, ...]`，诊断用） |

**⚠️ 3 个上游键未建模**（只出现在 `raw_keys` 里）：`degradation_12h_iq`、`from_24h_average_iq`、`from_48h_average_iq`。要显示它们必须改后端。

### 13.2 `DegradationAlert` 字段表

| 字段 | 类型 | 单位/口径 | `None`? | 实测样例值（`k3@max`，2026-09-14 pompeii） |
| --- | --- | --- | --- | --- |
| `model` | `str` | | 否 | `k3` |
| `effort` | `str` | | 否 | `max` |
| `current_iq` | `float \| None` | 当前 IQ | **是** | `74.0` |
| `avg_24h` | `float \| None` | 24h 均值 | **是** | `102.82` |
| `avg_48h` | `float \| None` | 48h 均值 | **是** | `103.46` |
| `delta_24h` | `float \| None` | **相对 24h 的降幅**（上游值，正数=降了） | **是** | `30.1` |
| `delta_48h` | `float \| None` | 相对 48h 的降幅 | **是** | `30.1` |
| `severity` | `float \| None` | **严重度**（上游排序依据） | **是** | `5.1356` |
| `average_cost_usd` | `float \| None` | USD | **是** | `8.036755` |
| `average_duration_minutes` | `float \| None` | 分钟 | **是** | `141.42` |
| `smooth_delta_24h` | `float \| None` | 平滑后 24h 变化 | **是** | `-0.1` |
| `peak_24h_iq` | `float \| None` | 上游 `from_24h_high_iq` | **是** | `30.3` |
| `peak_48h_iq` | `float \| None` | 上游 `from_48h_high_iq` | **是** | `30.3` |
| `trend_48h` | `tuple[TrendPoint, ...]` | 48 小时趋势（**实测 48 点**） | 否（可为空） | 48 点 |
| `raw_keys` | `tuple[str, ...]` | 上游原始键名（17 个） | 否 | 见 §13.1 |
| `key`（property） | `str` | `"model@effort"` | 否 | `k3@max` |

其余 3 条实测（同一响应）：

| `model@effort` | `current_iq` | `avg_24h` | `avg_48h` | `delta_24h` | `severity` |
| --- | --- | --- | --- | --- | --- |
| `grok-4.6@xhigh` | 87.0 | 104.52 | 105.1 | 18.2 | 3.2058 |
| `grok-4.6@high` | 87.0 | 99.15 | 99.72 | 13.4 | 2.2612 |
| `grok-4.6@medium` | 85.0 | 96.46 | 97.06 | 12.1 | 2.1576 |

### 13.3 前端规则

1. **不得自行重算预警。** 上游规则已排除 DeepSeek、只与模型自身历史比较、最多 4 条；本地补算会造出上游不承认的结论。
2. **只转发**：直接渲染 `degradation_alerts()` 的结果，保留上游给出的 `severity` 排序。
3. **`items` 可能为空**：实测 **deep-swe 为 0 条**（夹具 `insights.json` 也是 `items: []`）。空时显示「当前无降智预警」，**不要**显示空表格或「加载失败」。
4. 上游规则原文（`degradation_rule`，夹具逐字，140 字符）：

   > 不含 DeepSeek；每个模型档位只与自身历史比较：每格最近三次结果必须达到以下任一固定阈值：当前 IQ 低于 24 小时均值至少 7 IQ，或低于 48 小时均值至少 9 IQ；同时最近 12 小时仍在下降。按相对门槛的均值差严重度排序，最多返回 4 个，不足 4 个不补位。

5. **`degradation_rule` 也要显示**（它解释了为什么这些模型上榜、为什么 DeepSeek 不在）。
6. **`trend_48h` 的最后一个点就是「当前」**：实测 `k3@max` 的趋势末点 `{timestamp: "2026-09-14T05:36:36+00:00", iq: 74.0, samples: 47}` 与 `current_iq` 一致，`samples` 从 199 掉到 47 —— 可以直接画「断崖」。
7. 夹具 `insights_alert_item.json` 是**纯合成**样本（上游采集时 items 始终为空），只用于证明解析器容忍字段不全：`{model: "gpt-6-astra", effort: "max", iq: 71.4}` → `current_iq=71.4`，其余字段全 `None`，`raw_keys=("effort","iq","model")`。**不要把它当上游真实行为的证据。**

---

## 14. 错误码表

`RadarError.code` 是**稳定契约**（`errors.py` 的 8 个 `CODE_*` 常量）。前端应**按 `code` 分支**，不要解析文案。

| `code` | 触发条件 | 插件用户话术（`_MESSAGES`） | 前端应有的提示 |
| --- | --- | --- | --- |
| `upstream_unavailable` | 5xx / 超时 / 连接失败 / 代理故障 | 雷达数据源暂时不可用，请稍后重试。 | 展示「数据源暂不可用」+ **重试按钮**；若有陈旧缓存，同时展示 `stale=True` 提示 |
| `unknown_benchmark` | 上游 404（未知 benchmark） | 没有这个评测频道。 | 提示频道不存在，**列出可用 id**（从 `benchmarks()` 拿） |
| `unknown_model` | 本地别名解析失败 / 歧义 / 该频道无该档位 | 没有该模型档位的实测数据。 | 提示无数据；若 `detail` 带候选（`ambiguous` / `alias-not-in-benchmark`），**列出候选让用户选** |
| `invalid_argument` | 本地校验失败（`effort` 非法 / `by` 非法 / `scope` 非法 / 题名歧义 / `limit > 50` / 空模型名） | 参数不合法。 | 提示合法取值；**这是可以本地拦下的**，前端应在发请求前就校验 |
| `payload_too_large` | `/table` 超限/超时（`ValueError` 含 `exceeds`） | 该查询的数据量过大，请改用轻量命令。 | 建议改用轻量视图（榜单/趋势），不要重试同一请求 |
| `schema_drift` | 必需字段缺失（上游演进） | 数据源结构发生变化，请反馈给维护者。 | 展示「数据结构变化」+ **上报入口**；这是维护者需要知道的信号 |
| `rate_limited` | 429 且重试耗尽（最多 5 次重试 / 6 次尝试） | 数据源正在限流，请稍后再试。 | 展示「限流中」+ **禁用自动轮询**、给冷却提示 |
| `private_path_refused` | 代码误触 `/api/private/*` | 内部错误：插件不应访问私有接口。 | **属实现缺陷，正常路径不应出现**；前端可上报 |

**硬规则：上游响应体原文绝不进用户可见消息。** `errors.redact()` 只把脱敏片段写 debug 日志；`message` 只由固定话术表或**本地**信息（可用 id、候选列表、合法取值）拼成。

**异常类型层级**（前端 `except` 时可用来分组）：

```
RadarError
├── RadarSourceError        # 上游/网络问题
│   ├── UpstreamUnavailable      upstream_unavailable
│   ├── PayloadTooLarge          payload_too_large
│   ├── SchemaDrift              schema_drift
│   ├── RateLimited              rate_limited
│   └── PrivatePathRefused       private_path_refused
└── RadarQueryError         # 参数/语义问题（本地可判）
    ├── UnknownBenchmark         unknown_benchmark
    ├── UnknownModel             unknown_model
    └── InvalidArgument          invalid_argument
```

**实测触发样例**（用夹具跑真实 `RadarService` 得到）：

| 调用 | 结果 |
| --- | --- |
| `top_models(by="bogus")` | `invalid_argument` / `detail="unknown sort"` / `排序字段「bogus」不合法，可选：iq/pass_rate/cost。` |
| `top_contributors(scope="bogus")` | `invalid_argument` / `detail="unknown scope"` / `榜单范围「bogus」不合法，可选：month/total。` |
| `recent_events(limit=51)` | `invalid_argument` / `detail="limit too large"` / `流水条数上限为 50。` |
| `task_detail("rp", benchmark="pompeii-adjacency")` | `invalid_argument` / `detail="ambiguous task"` / `「rp」匹配多道题：pompeii-adjacency-rp-002、…-006、…-015` |
| `model_profile("nope")` | `unknown_model` / `detail="unknown model: nope"` / `没有该模型档位的实测数据。` |
| `compare("astra","sol", effort="low")` | `unknown_model` / `detail="no variant"` / `gpt-5.6-sol 在当前频道没有该档位的实测数据。` |
| `resolve_model("gpt-5.6", known)` | `unknown_model` / `detail="ambiguous"` / `「gpt-5.6」对应多个候选：gpt-5.6-sol、gpt-5.6-terra` |
| `resolve_model("", known)` | `invalid_argument` / `detail="empty model"` / `参数不合法。` |

> ⚠️ **`detail` 字段**是给开发者/日志用的（英文小写短串），**不是给用户看的**。前端展示请用 `str(exc)`（即 `message`）。

---

## 15. 展示红线 checklist（可直接给前端当验收清单）

| # | 红线 | 落地做法 |
| --- | --- | --- |
| 1 | **IQ 与 pass_rate 必须同给** | 同一行/同一卡片里既显示 IQ 又显示通过率；不能只给一个 |
| 2 | **必须带样本量** | `samples` / `n` / `votes` / `graded` / `total_n` / `runs_total` 至少给一个 |
| 3 | **必须带数据时间** | **优先 point 级** `source_updated_at`（§9.2）；其次 `last_graded_at` / `graded_at` / `baseline_generated_at` |
| 4 | **必须带档位** | 每个模型名旁边有 `effort` |
| 5 | **非 `measured` 的成本/耗时前缀 `~` 并标「估算」** | 用 `cost_src` / `cost_is_estimate` 判断 |
| 6 | **缺失值用 `—`，不用 `0`** | `None` → `—`；`0` 只用于「实测为零」 |
| 7 | **不跨频道混排** | 一个图表/一个排序里只有一个 `benchmark_id`；唯一例外是上游自己的 `comprehensive_points` |
| 8 | **`mode` 必须透传** | 显示 `RadarMeta.mode` 与 `recommendation_mode` |
| 9 | **空数据就说空** | 空列表 → 「暂无数据」，不抛错、不显示空表骨架 |
| 10 | **不自行算钱** | 用 `actual_cost_usd` / `average_price_usd` / `cost` / `usd`；不用价表重算 |
| 11 | **不自行算降智** | 只转发 `degradation_alerts()` |
| 12 | **`iq_derived=True` 必须显式标记** | 见 §4.4 |
| 13 | **`latest:` 序列单独标注** | 见 §5.3 |
| 14 | **`stale=True` 必须提示「数据可能过期」** | 见 §10.1 |
| 15 | **B 面数据必须标注来源** | 见 §16.3 |
| 16 | **每行宽度限制** | 插件纯文本层分三档：自由文本 **40 显示宽度**（中文按 2 计，`formatters.MAX_LINE_WIDTH`）；**数据行 120**（`ROW_MAX_WIDTH`，经 `_row()`）；脚注 120（`FOOTER_MAX_WIDTH`）。前端自行决定布局，但需知道这个数字的来源是「群聊等宽不成立」 |

**关于第 16 条的来源**：`formatters.py` 是**纯文本**层，不用 Markdown 表格（群聊等宽不成立），超出用 `…` 截断。宽度常量有**三个**，别只看 `MAX_LINE_WIDTH`：

| 常量 | 值 | 管谁 | 实现 |
| --- | --- | --- | --- |
| `MAX_LINE_WIDTH` | 40 | 自由文本 / 分组标题 | `_clip()` |
| `ROW_MAX_WIDTH` | 120 | **数据行**（排行、档案、对比、题、贡献者、流水…） | `_row()` |
| `FOOTER_MAX_WIDTH` | 120 | `format_meta_footer()` 的脚注（唯一豁免行） | `_clip()`，但必需段永不裁 |

> ⚠️ **数据行不是 40**：`plugin_api §6` 写的「每行 ≤ 40」在实现里无法成立 —— 该文档自己给的行样例就有 52 显示宽度，而真实行（`dsh-deepseek-v4-flash-vision-exp[ultra]` + 三个指标）能到 82。若真按 40 截断，会静默吃掉红线要求的 `n=`。因此**数据行走 `ROW_MAX_WIDTH=120`**，`MAX_LINE_WIDTH=40` 只管自由文本。前端不受这个约束，但如果要复用文案，需要知道它已被截断过。

**缺失值口径（红线 6）的代码位置**：`formatters.EMPTY = "—"`；`_text_or_empty()` 把 `None` / `""` / `"null"` / `"none"` / `"nan"` 统一成 `—`；`_num(None)` → `—`（**不是 `0`**）。

---

## 16. 合规与边界（**前端也要遵守**）

### 16.1 只读，不写

- 本仓库是**纯只读消费方**：不实现、不调用任何写侧端点（认领 / 提交 / 判分 / run-plan / feedback）。
- 前端也**不得**引导用户去调写侧端点，不得「代跑题」。

### 16.2 绝不触碰私有面

- **绝不触碰 `/api/private/*`**（A 面 OpenAPI 内的私有变体；实测 `401 {"detail":"invalid bearer token"}`）。
- **不得携带 token 探测**。
- 实现层保证：`provider._url()` 对 `/api/private/` 前缀**直接抛 `PrivatePathRefused`**，根本不发请求。
- 前端若自行拼 URL，也必须遵守同一条。

### 16.3 不伪造身份头

- **不伪造** `X-DRadar-Client-Version` / `X-DRadar-Capabilities` / `Authorization`。公开读端点免鉴权，伪装成官方客户端既不诚实也无必要。
- 用**自有标识 UA**（形如 `<项目标识>-radar/<版本> (+<项目主页>)`），便于上游识别与联系。

### 16.4 TTL 不短于上游 `Cache-Control`

插件侧 TTL（`config.RadarCacheTTL`）已按上游 `Cache-Control` 取齐或更长：

| 端点 | 插件 TTL（秒） | 上游 `Cache-Control`（实测） |
| --- | --- | --- |
| `/benchmarks` | 3600 | `max-age=300` |
| `/leaderboard` | 60 | `max-age=10, s-maxage=10` |
| `/table` | 300 | `max-age=30, s-maxage=300` |
| `/iq-history` | 120 | `max-age=60` |
| `/radar-insights` | 600 | `max-age=600, s-maxage=600` |
| `/intelligence-efficiency` | 60 | `max-age=30, s-maxage=30` |
| `/model-metrics` | 60 | `max-age=30, s-maxage=30` |
| `/events` | 30 | `max-age=30` |
| `/quota` | 3600 | `max-age=60` |
| `/suggest` | 60 | — |

**规则**：TTL 不得短于上游 `max-age` / `s-maxage`，否则等于**用我们自己的请求量替上游刷缓存**。前端若自己缓存/轮询，也必须遵守；**不要高频轮询**，不要在无人使用时后台定时拉取。

### 16.5 重试策略（不要自行发明）

`config.RadarRetry`（对齐官方客户端 v0.5.200）：

| 项 | 值 |
| --- | --- |
| 429 最大重试 | `rate_limit_retries = 5`（共 6 次尝试） |
| `Retry-After` 取值域 | clamp 到 `[min_wait, max_wait] = [1.0, 60.0]` 秒 |
| 退避等待 | `clamp(retry_after) + uniform(0, min(1.0, retry_after × 0.1))`（抖动防羊群） |
| 503 + 维护栅栏 | `Retry-After` **不 clamp**，由 `total_budget = 360.0` 秒裁决 |
| 其他 5xx / 超时 | 立即放弃 → `upstream_unavailable` |
| 连接级重试 | **不用**（本机常驻代理，重试会放大故障） |

### 16.6 B 面（网站侧 `codexradar.com` 的 `/api/*`）—— **不在默认取数集内**

上游有**两套独立 host 的公开 JSON 面**：

| | A 面（主） | B 面（网站侧） |
| --- | --- | --- |
| base | `https://api.codexradar.com` | `https://codexradar.com` |
| 路径前缀 | `/api/v1/*` | `/api/*` |
| OpenAPI 契约 | **有**（`/openapi.json`，58 路径） | **无** |
| 本插件用法 | **主路径** | **不使用** |

**⚠️ 关键红线**：B 面的 `model-ratings` 端点（路径前缀 `/api/`，端点名 `model-ratings`）返回 33 个模型的 `{id, label, group, average, count}`，其中 **`average` 是另一套量纲**（样例 `average: 8`，`count: 8`），而 **IQ 是 0–150** —— **绝不可与 IQ 混画、混排或换算**。引用 B 面数据**必须标注来源**（网站侧面、无 OpenAPI 契约、可能随时变更）。

B 面其他实测端点（仅供参考，均不默认使用）：`intelligence-efficiency-metrics`（与 A 面同源）、`visual-spatial-reasoning`（1.07 MB，25 point / 153 history）、`subscriber-count`（`{"ok":true,"count":4263,"source":"cached"}`）、以及同名 `radar-insights`（与 A 面键完全相同，仅 `generated_at` 差约 10 分钟 → 缓存时差）。

**未验证**：B 面 `visual-spatial-reasoning` 与 A 面 `/iq-history?benchmark=pompeii-adjacency` **是否同源**（同为 1 MB 级视觉频道数据）—— **推断同源但未证实**，按两个独立端点处理，不要假定可互换。

---

## 17. 未决 / 不确定事项（**别把猜测当事实**）

| # | 事项 | 状态 | 对前端的影响 |
| --- | --- | --- | --- |
| 1 | 上游是否给出**书面授权**第三方长期只读消费 | **未验证 / 无书面许可** | `robots.txt` 的 `Allow: /` **不等于 API 许可**；官方客户端仓库 `license` 为 `null`；`openapi.json` 无 `termsOfService`；站点无条款页。上游**事实上**允许匿名只读（多端点免鉴权 200），但**没有任何书面许可**。→ 前端不要做高频/批量拉取 |
| 2 | `src` 各枚举值的精确语义 | **推断** | 上游未文档化。实现只做「`measured` 之外一律当估算」。**不要**为每个值写不同文案 |
| 3 | `passed` 为浮点的加权算法 | **推断** | 推断为「任务等权 + 窗口加权的加权通过数」；**具体权重算法未验证**。→ 不要当题数展示 |
| 4 | `latest:` 口径的精确定义 | **未验证** | 推断与 `/model-metrics` 的 `latest_valid_per_task` 同源，未证实。→ 只能标注「另一套窗口」，不要解释成因 |
| 5 | B 面 `average` 与 A 面 IQ 的关系 | **未验证** | 量纲明显不同（≈8 vs 0–150）；**关系未验证**，绝不可换算 |
| 6 | B 面 `visual-spatial-reasoning` 与 A 面 `/iq-history?benchmark=pompeii-adjacency` 是否同源 | **未验证（推断同源）** | 按两个独立端点处理 |
| 7 | 生产限流策略（匿名配额） | **部分已确认** | 服务端未公开匿名配额；实测 15 次连打 `/benchmarks` 全 200 **只说明当时没触发**。429 真实存在（官方文档记录过）。→ 前端不得高频轮询 |
| 8 | `/leaderboard?view=` 的语义 | **已确认：完全无效** | `total` / `month` / `history` / `nope` 四种取值与默认响应**逐字节相同**。→ **不要传 `view`**（插件的 `leaderboard(view=...)` 接受但**不发送**，只记 debug 日志） |
| 9 | `degradation_alerts.items` 的真实结构 | **已从空变为非空**（本文档已按实测更新） | 设计文档 §B4 说「从未见过真实条目」，那是 2026-09-13 的状态。**2026-09-14 实测 pompeii 返回 4 条、每条 17 键**（§13.1）。设计文档里的字段名（`current_iq` 等）与上游实测名（`iq` 等）**不同**，插件做了映射 |
| 10 | `degradation_12h_iq` / `from_24h_average_iq` / `from_48h_average_iq` 的语义 | **未验证** | 插件未建模，只在 `raw_keys` 里 |
| 11 | `peak_24h_iq` 的数值含义 | **存疑** | 实测 `k3@max` 的 `peak_24h_iq = 30.3` 与 `delta_24h = 30.1` 几乎相同，怀疑上游该字段本身就是「距高点的差」而非「高点 IQ 值」。**未验证**，展示时建议只当作「相对高点的降幅」 |
| 12 | 两频道 `comprehensive_points` 的身份 | **已确认** | 实测 `deep-swe` 有 40 点、`pompeii-adjacency` 有 **0 点**。→ 前端不要在 pompeii 频道下期待综合 IQ 列表 |

---

## 18. 最小可用示例

### 18.1 构造（可直接抄）

```python
import asyncio
from dataclasses import asdict

from plugins.radar.config import RadarConfig
from plugins.radar.provider import RadarClient
from plugins.radar.service import RadarService


def build_service() -> RadarService:
    """构造服务。RadarConfig.from_env() 会读 RADAR_* 环境变量（见 §18.3）。"""
    config = RadarConfig.from_env()
    client = RadarClient(config)          # 唯一出网层
    return RadarService(client, config)   # 查询语义层


async def main() -> None:
    service = build_service()

    # ── 1. 排行榜：默认 deep-swe，effort=None → 各模型最高档 ──
    rows, meta = await service.top_models(by="iq", limit=10)
    print(f"频道 {meta.benchmark_id} · {meta.score_label} · mode={meta.mode} · note={meta.note}")
    if meta.stale:
        print("⚠ 数据可能过期（上游暂不可用，显示缓存）")
    for rank, row in enumerate(rows, start=1):
        mark = "（本地换算）" if row.iq_derived else ""
        print(
            f"{rank}. {row.model}[{row.effort}] "
            f"IQ {row.iq:.1f}{mark} · 通过 {row.pass_rate * 100:.1f}% · "
            f"n={row.graded} · {row.cells}题"
        )

    # ── 2. 单模型档案（显式带档位，避免 §11.2 的坑） ──
    profile = await service.model_profile("astra", effort="low")
    variant = profile.variants[0]
    print(f"\n{profile.model}@{variant.effort}")
    print(f"  IQ {variant.iq} (derived={variant.iq_derived}) · 通过 {variant.pass_rate:.3f} · n={variant.graded}")
    if profile.insight is not None:
        point = profile.insight
        print(f"  综合 IQ {point.iq}（软件 {point.software_iq} · 视觉 {point.visual_iq}）· n={point.samples}")
    if profile.efficiency is not None:
        point = profile.efficiency
        # ⚠️ 连续制频道（pompeii-adjacency）的 EfficiencyPoint 里这些字段可能是 null，
        #    直接 f"{x:.2f}" 会 TypeError（unsupported format string passed to NoneType）。
        #    先判空再格式化，或统一走 formatters 的 _money/_minutes（缺失渲染成「—」）。
        price = f"${point.average_price_usd:.2f}" if point.average_price_usd is not None else "—"
        minutes = f"{point.average_minutes:.1f}分" if point.average_minutes is not None else "—"
        index = f"{point.combined_cost_index:.0f}" if point.combined_cost_index is not None else "—"
        print(f"  成本 {price} · 耗时 {minutes} · "
              f"性价比指数 {index} · 数据时间 {point.source_updated_at or '—'}")

    # ── 3. 直接交给前端：转成 JSON 可序列化的 dict ──
    payload = {
        "meta": asdict(meta),
        "rows": [asdict(row) for row in rows],   # 注意 ModelRow.tasks 是 Mapping，asdict 会展开
    }
    # json.dumps(payload, ensure_ascii=False)  ← 可直接下发


asyncio.run(main())
```

### 18.2 返回数据形状示意（实测值，`deep-swe` / 2026-09-14）

> ⚠️ **上游数据是持续更新的快照**：下面的数字是 2026-09-14 一次**同批抓取**的结果（`fetched_at = 2026-09-14T05:56:18+00:00`），与本文别处引用的夹具值 / 更早的实测值**会不同**（例如同一档位的 `pass_rate` 在夹具里是 0.682、更早实测是 0.676，这里是 0.676 但 `graded` 已从 135 变成 136）。**形状（键集）是稳定的，数值不是。**

`top_models(by="iq")` 的 `rows`（裁剪到前 3 行）：

```json
[
  {"model": "gpt-6-astra", "effort": "ultra", "graded": 147, "passed": 105,
   "score_sum": 105.0, "cells": 112, "cells_passed": 77, "pass_rate": 0.688,
   "iq": 115.06, "iq_derived": false},
  {"model": "gpt-5.6-sol", "effort": "ultra", "graded": 2323, "passed": 1569,
   "score_sum": 1569.0, "cells": 112, "cells_passed": 77, "pass_rate": 0.688,
   "iq": 102.6, "iq_derived": false},
  {"model": "grok-4.6", "effort": "xhigh", "graded": 705, "passed": 478,
   "score_sum": 478.0, "cells": 112, "cells_passed": 81, "pass_rate": 0.723,
   "iq": 101.98, "iq_derived": false}
]
```

注意第 1、2 行 **`pass_rate` 完全相同（0.688）但 `iq` 差 12.46**（115.06 vs 102.6）—— 这就是 §4 讲的「两个口径不可互推」：`iq` 来自 insights（跨频道加权），`pass_rate` 来自主榜（单频道），两者不是同一个东西。若按 `pass_rate` 排序这两行应当并列，按 `iq` 排序则不是。

对应的 `meta`：

```json
{
  "benchmark_id": "deep-swe", "scoring_mode": "binary-majority",
  "score_label": "Pass rate", "mode": null, "rolling_window": null,
  "pass_threshold": 1.0, "source_updated_at": null, "stale": false,
  "fetched_at": "2026-09-14T05:56:18+00:00", "recommendation_mode": null,
  "note": "已取各模型最高档", "samples": null
}
```

`model_profile("gpt-6-astra", effort="low")` 的形状（同一批实测）：

```json
{
  "model": "gpt-6-astra",
  "variants": [{"model": "gpt-6-astra", "effort": "low", "graded": 136, "passed": 88,
                "cells": 111, "pass_rate": 0.676, "iq": 108.62, "iq_derived": false}],
  "best": {"model": "gpt-6-astra", "effort": "low", "iq": 108.62, "iq_derived": false},
  "insight": {"model": "gpt-6-astra", "effort": "low", "iq": 108.62,
              "software_iq": 97.78, "visual_iq": 135.71, "samples": 189},
  "efficiency": {"model": "gpt-6-astra", "effort": "low", "iq": 97.78,
                 "passed": 88.0, "total": 135.0, "average_price_usd": 1.969895,
                 "average_minutes": 8.67, "combined_cost_index": 127.244,
                 "average_agent_steps": 25.470149253731343, "agent_steps_samples": 134,
                 "average_total_tokens": 971714.2814814815, "token_samples": 135,
                 "cache_hit_rate": 0.9445982467894247, "cache_token_samples": 135,
                 "runs_24h": 1, "runs_48h": 1, "runs_total": 136,
                 "source_updated_at": "2026-09-13T10:50:37+00:00"},
  "metrics": {"model": "gpt-6-astra", "effort": "low",
              "average_agent_steps": 27.25, "agent_steps_samples": 110,
              "average_total_tokens": 1072169.98, "token_samples": 111,
              "cache_hit_rate": 0.9476, "cache_token_samples": 111,
              "runs_24h": 1, "runs_48h": 1, "runs_total": 136},
  "trend": [{"timestamp": "2026-09-14T05:00:54+00:00", "iq": 97.8, "samples": 135}],
  "recent": [],
  "meta": {"benchmark_id": "deep-swe", "score_label": "Pass rate",
           "pass_threshold": 1.0, "note": "", "samples": 136, "stale": false}
}
```

**注意 `trend` 的末点 `97.8`**：这里 `effort="low"` 被显式指定，所以 `_series_points()` 取到的是 `gpt-6-astra@low` 单档位序列（末点 `iq=97.8`、`samples=135`），**不是**裸模型名序列（同批裸名末点是 `iq=105.3`、`samples=806`）。若**不**指定 `effort`，`variants[0]` 是 `low`，行为相同，但 `variants` 会含全部 6 档、`meta.note` 变成「含全部档位」。

**同一批数据里 `insight.iq`(108.62) / `efficiency.iq`(97.78) / `pass_rate×150`(101.4) 三个数都不同** —— 前端必须分别标注来源，不要试图统一。

### 18.3 `RADAR_*` 环境变量（`config.RadarConfig.from_env()` 实际读取的 9 个）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RADAR_BASE_URL` | `https://api.codexradar.com` | 去掉尾部 `/` |
| `RADAR_PROXY` | 空 | ⚠️ **保留字段，当前不生效**：共享 HTTP 层固定 `trust_env=False` 且不挂代理；设了会记一条 warning（值不写日志，防口令泄漏） |
| `RADAR_TIMEOUT` | `20` | 秒，下限 1.0 |
| `RADAR_TABLE_TIMEOUT` | `60` | `/table` 单独放宽，下限 1.0 |
| `RADAR_DEFAULT_BENCHMARK` | `deep-swe` | |
| `RADAR_CONCURRENCY` | `4` | 全局并发闸门，下限 1 |
| `RADAR_MAX_BYTES` | `12582912`（12 MiB） | 单响应上限，下限 1024 |
| `RADAR_MAX_TABLE_BYTES` | `25165824`（24 MiB） | `/table` 专用上限，下限 1024 |
| `RADAR_USER_AGENT` | 自有标识 UA | |

**配置读取的两个坑（前端若自己读环境也要注意）**：

1. **数值型必须用 `_env(name, None)` + `is None` 判空**：本仓库的 `.env` 加载器会把 `"0"` 经 `json.loads` 变成 int `0`，用真值判断会把合法的 `0` 当成「未设置」。
2. **`RadarConfig` 是 `slots=True`**，所以 `RadarConfig.base_url` 取到的是 slot 描述符而不是默认值字符串。默认值在模块级常量里（`DEFAULT_BASE_URL` 等）。

> ✅ `.env.example` 里**已写入**全部 9 个 `RADAR_*` 键（2026-09-14），且由 `tests/test_radar.py::ConfigContractTests` 强制「模板 ↔ `config.py` 读取点」双向零差集。部署时全部可留空，走默认值。
>
> ⚠️ `RADAR_PROXY` 是**保留键**：共享 HTTP 层固定 `trust_env=False` 且不挂代理，设了会被忽略（只记一条 warning，值不落日志）。

---

## 19. 设计文档 vs 实现代码的差异

**规则：以代码为准。** 以下是逐条核对出的差异（左 = 设计文档，右 = 实现）。

| # | 设计文档写 | 实现是 | 影响 |
| --- | --- | --- | --- |
| 1 | `plugin_api.md` §5 的 `RadarService` 方法**大多返回裸元组**：`top_models() -> tuple[ModelRow, ...]`、`degradation_alerts() -> tuple[DegradationAlert, ...]`、`value_picks() -> tuple[EfficiencyPoint, ...]`、`trend() -> tuple[TrendPoint, ...]`、`fleet_pulse() -> FleetPulse`、`flag_race() -> FlagRace` | **返回 `(数据, RadarMeta)` 二元组**：`top_models` / `degradation_alerts` / `value_picks` / `trend` / `task_ranking` / `top_contributors` / `recent_events` 都是 `tuple[tuple[...], RadarMeta]`；`fleet_pulse` / `flag_race` 是 `tuple[X \| None, RadarMeta]`。只有 `benchmarks` / `model_catalog` / `table` / `recommendations` / `recommendations_meta` / `model_profile` / `compare` / `task_detail` / `who_solved` 不是二元组 | **前端必须按实现的签名解包**，否则 `meta` 会丢 |
| 2 | `plugin_api.md` §5 的 `top_models` 没有 `min_samples`；`by` 只有 `"iq" \| "pass_rate"` | `top_models` 多了 **`min_samples: int \| None = None`**；`by` 支持 **`"cost"`** | 成本排序可用；`by="cost"` 时 `note` 会追加「；成本为上游估算口径」 |
| 3 | `plugin_api.md` §5 的 `model_profile` 只有 `query` / `effort` / `benchmark` | 多了 **`trend_hours: int = 72`** 与 **`recent_limit: int = 5`** | `recent_limit` 生效（实测 `recent_limit=1` → `recent` 长度 0/1）；**`trend_hours` 当前未生效**（实测传 2 与 72 都返回 168 点）—— 只有 `trend()` 的 `hours` 生效 |
| 4 | `plugin_api.md` §5 的 `top_contributors` / `fleet_pulse` / `flag_race` **没有 `benchmark` 参数** | 都多了 **`benchmark: str \| None = None`** | 可以查 pompeii 的贡献者/实时面板 |
| 5 | `plugin_api.md` §4 的 `events()` 返回 `tuple[RadarEvent, ...]` | 返回 **`EventsPayload`**（带 `meta` 与 `events`） | `/events` 响应自带口径 envelope，实现选择了保留 |
| 6 | `plugin_api.md` §3 的 `ModelRow` **没有 `iq_derived`**；`iq` 注释为「由 pass_rate×150 换算，或取自 insights」 | **有 `iq_derived: bool`**，且优先取 insights 值、只有缺失才兜底 | 前端**必须**用 `iq_derived` 标注（§4.4） |
| 7 | `plugin_api.md` §3 的 `EfficiencyPoint.passed/total` 是 **`int`** | 是 **`float \| None`** | 连续制频道下是 F1 加权和（§7） |
| 8 | `plugin_api.md` §3 的 `InsightPoint.iq` 是 `float`（无 `None`）；`DegradationAlert.current_iq` 是 `float`（无 `None`） | `InsightPoint.iq` 确实 `float`；`DegradationAlert` 的 15 字段里**只有 `model` / `effort` 是必填 `str`，其余 11 个数值字段全是 `X \| None`**（`current_iq` 也是）；另外两个非数值字段 **`trend_48h: tuple[TrendPoint, ...]` 与 `raw_keys: tuple[str, ...]` 是必填元组**（默认空元组，不是 `None`） | 预警数值字段必须做 `None` 分支；`trend_48h` / `raw_keys` 按元组处理（可空但非 `None`） |
| 9 | `plugin_api.md` §3 的 `CellState` 没有 `cost_is_estimate` | 有 **`cost_is_estimate`** property | 前端用它判断「是否要加 `~`」 |
| 10 | `plugin_api.md` §3 的 `FleetPulse` 字段是 `{window_minutes, submitted_runs, tokens_per_hour, cache_hit_ratio, api_equivalent_usd_per_hour}` | 多了 **`usd_per_hour: float \| None`** | 合计口径的每小时成本 |
| 11 | `plugin_api.md` §3 的 `RunRecord.points_base` / `points_multiplier` / `duration_sec` 是 `float`（无 `None`） | 全部是 **`float \| None`** | 需要 `None` 分支 |
| 12 | `data_dictionary.md` §5 的 `ran_by` 有 `avatar_seed`；§2 的 `Cell` 有 `ns` / `base` / `provider` / `agent` / `manual_only` / `billing_mode` 等 | **插件未建模**这些键（`CellState` 22 字段里没有） | 要显示必须走路径 B 或改后端 |
| 13 | `plugin_api.md` §3 的 `ContributorRow` 14 字段（与实现一致），但数据字典 §7 说上游有 **34 字段** | 实现只建模 14 个（实测夹具里上游是 **29 键**），**丢弃** `avatar_seed` / `points_by_harness` / `contribution_streak` / `nickname` / `month_*` 系列里的 `month_graded` / `month_submissions` / `month_tokens` / `month_usd` / `month_folded_usd` / `month_points_by_harness` / `month_rank_change_24h` / `month_deepseek_api_*` / `deepseek_api_runs` / `deepseek_api_unpriced_runs` 等 **16 键**；**但 `month_points` 是建模的**（14 字段里有它）。另：模型里的 `display_name` 是**派生字段**，上游没有同名键（由 `nickname` 映射而来） | 连击体系、分工具积分、按月运行数**前端拿不到**；`display_name` 不要当作上游原名去路径 B 里找 |
| 14 | `plugin_api.md` §3 的 `DegradationAlert` 字段名是 `current_iq` / `avg_24h` / `avg_48h` / `delta_24h` / `delta_48h` / `severity`，且暗示这就是上游字段名（设计文档说「按存在性解析，不能假定字段名」） | 上游**实际字段名不同**（`iq` / `average_iq_24h` / … / `degradation_severity_score`），实现**做了映射**并保留 `raw_keys`；还多建了 6 个字段（`average_cost_usd` / `average_duration_minutes` / `smooth_delta_24h` / `peak_24h_iq` / `peak_48h_iq` / `trend_48h` / `raw_keys`） | §13 已按实测更新 |
| 15 | `open_items.md` §B4 说「实测 `items: []`，**从未见过一个真实的预警条目**」 | **2026-09-14 实测 pompeii 返回 4 条**，每条 17 键 | 该未决项已部分解决 |
| 16 | `plugin_api.md` §5.2 说 `TaskDetail.solved_by` 是「**`rate>0`** 的」 | 实现是 **`cell.p > 0`** | 连续制频道下两者不同：`rate` 是 `score_sum/n` 浮点，`p` 是整数计数。以 `p > 0` 为准 |
| 17 | `plugin_api.md` §5 的 `trend` docstring 与 `data_dictionary.md` §12 都强调三种键形态不可混 | 实现**显式跳过** `latest:` 前缀，并在 `meta.note` 标注用的是哪种 | 一致（实现比文档更严格） |
| 18 | `plugin_api.md` §1.2 列了 **6 个** `RADAR_*` 变量（`BASE_URL` / `PROXY` / `TIMEOUT` / `TABLE_TIMEOUT` / `DEFAULT_BENCHMARK` / `CONCURRENCY`） | 实现读 **9 个**，多了 `RADAR_MAX_BYTES` / `RADAR_MAX_TABLE_BYTES` / `RADAR_USER_AGENT` | §18.3 已按代码列出 |
| 19 | `plugin_api.md` §1.2 说 `RADAR_PROXY`「空 = 走全局策略」 | 实现**明确不生效**：共享 HTTP 层固定 `trust_env=False` 且不挂代理，设了只记 warning | 想走代理必须改共享层 |
| 20 | `plugin_api.md` §1.2 说 `RADAR_BASE_URL` 便于指向本地调试地址 | 一致（实现 `rstrip("/")`） | — |
| 21 | `data_dictionary.md` §12 / `interface.md` §2.4 说 `/iq-history` 是「**dict**，key = series 名」 | 一致；实现 `parse_history()` 返回 `tuple[HistorySeries, ...]` 并保留原始 `key` | — |
| 22 | `plugin_api.md` §6 说 `format_meta_footer` 形如「`—— DeepSWE · Pass rate 口径 · 最近 3 次有效运行 · 数据 2026-09-13 17:12 · 样本 135`」 | 一致，但实现把 `mode` / `recommendation_mode` / `rolling_window` / `note` 放在**可丢弃的补充段**（装不下就整段丢） | 前端若复用文案需知道可能被截断 |
| 23 | 设计文档多处说主榜口径是 `equal_latest_3` | `/leaderboard` 响应**不含 `mode`** → `RadarMeta.mode` 为 `None`；`equal_latest_3` 出现在 `/intelligence-efficiency` | 前端不能从主榜 `meta.mode` 拿到口径名，要用 `rolling_window` / `score_label` |
| 24 | `.env.example` 计划写入 `RADAR_*` 组 | **已写入**（2026-09-14）：9 个键全在 `.env.example` 末尾，且由 `tests/test_radar.py::ConfigContractTests` 强制「模板 ↔ `config.py` 的 `_env_*` 读取点」双向零差集 | 部署时全部可留空，走默认值 |
| 25 | `plugin_api.md` §7 的 `handlers.py` 命令面列 **15 个子命令**（含 题 / 好题 / 贡献者 / 流水 / 实时） | **命令面按用户要求收窄为「只看智商相关」**（2026-09-14）：只保留 10 个（榜 / 模型 / 对比 / 推荐 / 预警 / 性价比 / 趋势 + 频道 / 档位 / 帮助）。被摘的 5 个**只摘命令面**，`RadarService` / `RadarClient` / `formatters` 里的对应能力**原样保留**（`task_detail` / `task_ranking` / `top_contributors` / `recent_events` / `fleet_pulse` / `flag_race` / `who_solved` 都还在，由 `tests/test_radar.py::test_dropped_commands_still_work_at_the_service_layer` 守住） | **前端不受影响**：本文档描述的是 service 层取数面，被摘命令的数据仍可经路径 A 取到；`/radar 题` 等现在回「未知子命令」 |

---

## 20. 关于本文档的自检

本文档交付时运行了 8 条机械自检（脚本在 `%TEMP%\radar\` 下，**不入库**）：

1. 文件存在、合法 UTF-8、行数 > 300；
2. 文档提到的每个 `RadarService` 方法名都在 `plugins/radar/service.py` 里真实存在（AST 解析取方法名集合比对）；
3. 文档提到的每个模型类名都在 `plugins/radar/models.py` 里真实存在；
4. 文档提到的每个错误码都在 `plugins/radar/errors.py` 的 `CODE_*` 里真实存在；
5. 文档提到的每个 `RADAR_*` 环境变量名都在 `plugins/radar/config.py` 里真实出现；
6. 文档不含真实身份材料（0 命中）；
7. 文档不声称本仓库提供 HTTP 服务；
8. 打印字节数与行数。

**未做到的**：本文档只描述「数据接口」，不含任何 UI 设计、组件结构、状态管理或样式建议 —— 那是前端 AI 的自由发挥空间。文中所有「上游有、插件未建模」的字段（§12.7 / §12.9 / §12.13 / §12.15 / §12.19 / §12.23）**在插件路径下确实拿不到**，需要改后端或走路径 B。
