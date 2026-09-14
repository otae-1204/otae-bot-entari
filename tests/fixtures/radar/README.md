# `tests/fixtures/radar/` — AI 雷达录制夹具

本目录的 JSON 夹具来自 **api.codexradar.com 的真实上游响应录制**（2026-09-13 / 09-14 采集）。
只做了两件事：**裁剪**（去掉多余条目）与 **脱敏**（身份换成合成值）。
**没有改动任何数值** —— IQ、`pass_rate`、`score`、成本、时间戳一律保持原值；也没有「修正」任何看起来可疑的值。
唯一的人为改动是**为 5 个格子补上了上游省略掉的 `src` / `ran_by` 键**（值为 `null` / `[]`），见文末「已知的键是人补的」一节。

> 全部文件以 **UTF-8** 写出。本机控制台是 GBK，读取时请显式指定 `encoding="utf-8"`。

## 脱敏规则

录制里含真实 GitHub 登录名、昵称与头像 URL。替换规则（**键名保留**，值变合成值，这样解析测试仍然覆盖这些键）：

| 键 | 替换为 |
| --- | --- |
| `login` / `github_login` / `nickname` | `volunteer-01`、`volunteer-02` …（同一真实身份在同一文件内映射到同一个合成值） |
| `avatar_url` | `null` |
| `avatar_seed` | `seed-01`、`seed-02` …（与同一个人对应） |

## 逐文件说明

| 文件 | 来源录制 | 端点 | 裁剪内容 |
| --- | --- | --- | --- |
| `benchmarks.json` | `probe_benchmarks.json` | `/benchmarks` | **未裁剪**，逐字复制。 |
| `leaderboard.json` | `deep_lb_deep-swe.json` | `/leaderboard?benchmark=deep-swe` | 顶层键全保留。`tasks` → 前 5 个；`models` → 4 行（`gpt-6-astra@low`、`gpt-6-astra@ultra`、`gpt-5.6-sol@max`、`gpt-5.5@high`，四行在录制中都存在），每行 `tasks` 收窄到同样 5 个 id；`contributors` → 3 行且**字段一个不删**（录制里 984 行中 982 行 29 字段、2 行 46 字段带 `riding_*`；这里取 2 个 29 字段行 + 1 个 46 字段行，覆盖两种形态）；`history.seasons` → 2 条、每条 `top10` → 3 条；`history.top`（int）与 `history.starts_at` 未动；`flag_race.winner` 已脱敏。 |
| `table.json` | `deep_table_deep-swe.json` | `/table?benchmark=deep-swe` | 保留 22 个顶层键。`tasks` → 3 条（`abs-module-cache-flags`、`abs-stepped-slices` 是 `go`，`adaptix-name-mapping-aliases` 是 `python`），字段全保留。`cells` → 8 格，覆盖 `src` 的 `measured`（3 格）/`task-level-fallback`（3 格）/`null`（1 格）/键缺失（1 格）、`ran_by` 的 `login`+`avatar_url` 形态与 `nickname`+`avatar_seed` 形态、以及 `st="open"` 且 `ran_by` 为空的格子；每格所有 `ran_by` 条目完整保留（未截断）。`combos` → 6。`token_pricing` 元数据保留、`usd_per_million`/`current` → 2 个模型。`client_contract.harness_capabilities` → 仅 `dsh`、`claude-code`、`grok`，每个列表最多 2 条。 |
| `table_pompeii.json` | `deep_table_pompeii-adjacency.json` | `/table?benchmark=pompeii-adjacency` | 同上裁剪。用于证明 **pompeii 的 task 没有 `discrimination` 键**（录制中 86/86 个 task 都无此键，而 deep-swe 的 112 个都有），且 cells 里可以出现 `"src": null`。本夹具 `src` 覆盖 `measured`/`calibrated-estimate-v1`/`null`/键缺失。 |
| `iq_history.json` | `probe_iq_history.json` | `/iq-history?benchmark=deep-swe` | 4 条 series，覆盖四种键形态：裸模型名 `gpt-6-astra`、`gpt-6-astra@low`、`latest:gpt-6-astra`、`latest:gpt-6-astra@low`；每条取真实序列前 5 个点，点内 `{ts, score, n}` 原样。 |
| `insights.json` | `deep_insights.json` | `/radar-insights?benchmark=deep-swe` | 顶层键全保留；`comprehensive_points` → 3；4 个推荐组全保留，`rule` **逐字原文**；每组 `items` → 2；每个 item 的 `trend_48h` → 3 点。`degradation_alerts.rule` 逐字保留，`items: []`（录制当时的真实状态）。 |
| `model_metrics.json` | `probe_model_metrics.json` | `/model-metrics` | **未裁剪**，逐字复制。 |
| `events.json` | `probe_events.json` | `/events` | `events` → 3 条；三个成本布尔与 `cost_source` 全部保留；身份已脱敏。 |
| `quota.json` | `probe_quota.json` | `/quota` | **未裁剪**，逐字复制。 |
| `suggest.json` | `probe_suggest.json` | `/suggest` | **未裁剪**，逐字复制。 |

## 合成 / 手工重建的夹具（**不是**原始录制）

* `insights_alert_item.json` —— **纯合成**。上游 `degradation_alerts.items` 在采集时始终为空，从未出现过有内容的条目。本文件唯一目的是证明**解析器能容忍字段不全的预警条目**，内容就是
  `{"items": [{"model": "gpt-6-astra", "effort": "max", "iq": 71.4}]}`。**不要**把它当成上游真实行为的证据。
* `efficiency.json` —— **手工重建**。顶层形状与 point 1 完全按真实端点结构写出；point 1 的每个数值都与真实录制 `probe_intel_eff.json` 里的 `gpt-6-astra@low` 行**逐字段一致**，point 2 直接取自该录制的 `gpt-5.6-sol@max` 行。
* `efficiency_pompeii.json` —— **手工重建**。用于覆盖连续制频道：`scoring_mode: "continuous-macro"`、`score_label: "Adjacency F1"`。point 0 的 `passed: 49.79059751561299` 是 **F1 加权和**（不是「过了 49.79 题」），`total: 55`；point 1 由真实 pompeii 榜单录制推得（`passed` = `score_sum`、`total` = `graded`、`iq` = `score_sum/graded*150`，该式对 point 0 反推出 135.79，与文档一致）。point 级其余字段文档未给 pompeii 数值，故为 `null`。

## 已知的两处「键是人补的」

上游对「空」的表达是**整个键不出现**，而不是给 `null` / `[]`（实测 deep-swe 7504 格里 336 格没有 `src` 键、1844 格没有 `ran_by` 键；原文 `"src": null` 与 `"ran_by": []` 各出现 0 次）。为满足「必须有 `src` 为 null 的格子」和「必须有 `ran_by` 为空的格子」，对 `table.json` 的 3 处、`table_pompeii.json` 的 2 处**补上了缺失的键**，值为 `null` / `[]`（这两个取值本身是文档列明的合法值）。
同时每个 table 都**另留一格完全不补键**，保持录制原样（`src`/`ran_by` 键缺失），这样「显式 null」和「键缺失」两种读法都被覆盖。其余所有格子、所有行的键集合与录制完全一致。

## 校验

生成 / 校验 / 审计脚本都在 `%TEMP%\radar\w\`（不落在仓库内）：`mkfix.py` 生成，`verify.py` 按交付要求逐条断言，`audit.py` 独立地把每个保留值与原录制逐字段比对。
