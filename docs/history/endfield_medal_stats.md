# 终末地蚀刻章统计功能需求文档

> 分支：`dev`
> 创建时间：2026-07-26　·　最近修订：2026-08-02（切换 AKEData 主源与个人缺章实现）
> 性质：需求与统计口径文档，作为已实现功能的规格说明

## 1. 背景与目标

蚀刻章（奖章）元数据来自 AKEData 的游戏客户端 TableCfg，玩家进度来自森空岛 SDK：

- **AKEData**（`data.akedata.wiki`）—— 运行时主源，提供当前与历史版本的三张 TableCfg 表。
- **森空岛 SDK**（`zonai.skland.com`）—— 只提供绑定账号的奖章进度，不提供版本元数据。

> FZ/Warfarin 的字段差异仅保留在历史调研记录中；当前奖章命令不把它们作为运行时数据源。

本模块提供两个面向用户的功能：

- **F1（全局·版本对比）**：查当前版本蚀刻章总数；对比上一版本找出**新增**蚀刻章；输出图片（总数 + 新增详情）。
- **F2（个人·绑定账号）**：通过森空岛 SDK 查询自己**未获得 / 未升满 / 未镀层**的蚀刻章；输出详情图，缺过多则只显示部分。

> 本文不涉及具体渲染实现细节，仅对齐需求口径、数据源、字段映射与统计规则。

## 2. 术语与数据源

- **蚀刻章**：指奖章数据。游戏内分类字段写作「奖章」，本文统一称「蚀刻章」。
- **主数据源（AKEData）**：`manifest.json` → 当前版本 `tableCfgPath` → `AchievementTable.json`、`AchievementTypeTable.json`、`I18nTextTable_CN.json`；历史版本只抓 `AchievementTable.json` 的 `achv_id` 集合。
- **版本标签**：manifest 版本 ID 的 `major.minor`（如 `1.4.4@...` → `1.4`）。

## 2.1 字段映射

| 视图字段 | AKEData 来源 | 统计用途 |
|---|---|---|---|
| `medal_id` | `AchievementTable` 的 map key（`achv_*`） | 版本对比主键 |
| `name` | `name` text-id → `I18nTextTable_CN` | 展示 |
| `category_name` / `group_name` | `AchievementTypeTable` 的分类/组 text-id → i18n | 分组 |
| `init_level` / `max_level` | `initLevel` / `levelInfos[].achieveLevel` | 档位统计与进度校正 |
| `can_be_upgraded` / `can_be_plated` | `canBeUpgraded` / `canBePlated` | F1/F2 |
| `order` | `order` | 排序 |
| `icon_url` | `medaliconbig/<achv_id>_lv<NN>.png` | 展示 |
| `description` / `condition` | `levelInfos[].completeDesc` / `conditions[].desc` → i18n | 展示 |

> AKEData 的 `max_level` 直接取 `levelInfos[].achieveLevel` 最大值，`can_be_upgraded` / `can_be_plated` 直接取表字段。

## 3. 功能需求

### FR-1　蚀刻章总数与各等级统计

**需求**：统计蚀刻章总数，以及每个最高等级的蚀刻章数量。

**计数口径（关键）**：每枚蚀刻章一律按其**最高等级 `max_level`** 归类计数。

- **理由**：可升级与多等级蚀刻章升级后低等级形态消失，仅保留最高等级形态。总数与各等级分布都应以「最高等级」为准。
- 定义：`effective_level(medal) = medal.max_level`（AKEData 由 `levelInfos` 解析）。

**输出**：总数 + 各等级（Lv1 / Lv2 / Lv3 …）数量。

### FR-2　可镀层 / 可升级蚀刻章统计

**需求**：分别统计：
1. 可镀层蚀刻章：`can_be_plated = true`；
2. 可升级蚀刻章：`can_be_upgraded = true`。

> F1 图片中以统计数字呈现；F2 进一步用于个人「未镀层 / 未升满」判定。

### FR-3　版本对比：筛选新增蚀刻章

**需求**：对比 akedata 当前版本与上一游戏版本，筛出**新增**蚀刻章。

**机制（源和源对比，双方同为 akedata 版本数据）**：
- 「上一版本」按游戏大版本 major.minor 界定（如 1.4 的上一版本是 1.3；跳过同 major.minor 的多个资源 revision）；
- 刷新时抓 akedata 当前版本全量 → `current` 快照；同时抓上一游戏版本的 `AchievementTable` → `baseline`（仅 achv_id 集合）；
- 以 `medal_id` 为主键做集合差集：**新增** = current 中存在、baseline 中不存在的 id；
- 新增章的展示信息（名字/图标）取自 `current`（即 akedata 当前版本源数据）；
- **无更早游戏版本**时新增列表为空；baseline 临时抓取失败时保留旧基线，不覆盖当前版本对比。

> 不再用本地 `current`/`previous` 滚动基线——版本对比两方都是 AKEData 源数据，口径一致、准确。

**同 id 字段变更（官方修正捕获，可选/首版仅记录）**：同 `medal_id` 但 `name` / `max_level` / `can_be_plated` 等发生变化的记录（即官方修正，如「武陵调度专家奖章·Ⅳ→Ⅴ」）。首版仅记日志，UI「变更」段后续迭代。

## 4. 数据快照设计

快照文件：`data/endfield/medal_snapshot.json`（运行时产物，已 gitignore）。

- 结构：`{"current": {version, fetched_at, source, medals[...]}, "baseline": {version, version_id, ids[...], fetched_at} | null}`；
- **刷新机制（手动双命令）**：
  - `奖章 刷新`：抓 akedata 当前版本（`manifest.latest` 的 AchievementTable + AchievementTypeTable + I18nTextTable_CN，~2.5s）→ `current`；同时抓上一游戏版本的 AchievementTable（仅 id 集合，长缓存）→ `baseline`；
  - `奖章`：直接读 `current` + `baseline` 快照（秒回，不触网）；
- diff 靠 id 集合差集，**不依赖版本号比较**；version 字段（major.minor，如「1.4」）仅作展示标签。
- 写盘串行（`asyncio.Lock`），避免并发刷新互相覆盖。

## 5. 命令

| 命令 | 行为 |
|---|---|
| `/zmd 奖章` | 读快照，输出蚀刻章总数 + 各等级/分类统计 + 本版本新增详情（首次快照则提示「已建立，暂无新增对比」） |
| `/zmd 奖章 刷新` | 重抓 AKEData 当前版本与上一游戏版本基线，再输出对比图 |
| `/zmd 奖章 缺章 [账号]` | F2：查询绑定账号未获得 / 未升满 / 未镀层 |

## 6. F2 个人缺章

**需求**：通过森空岛查询玩家自己的蚀刻章进度，与全量快照交叉比对，得出：
- **未获得**：快照中有、玩家进度中无该奖章（按 `md5(achv_id)` 关联，name 仅作兜底）；
- **未升满**：玩家持有但 `can_be_upgraded` 且当前 `level < max_level`；
- **未镀层**：`can_be_plated` 且玩家未镀层。

**截断口径**：三段合计过多时（默认 > 30）各段截断保留前若干，置 `truncated=True`，图顶提示「缺章过多，仅展示前 N 枚」。

**数据来源（已确认）**：`GET /api/v1/game/endfield/card/detail?roleId=<id>&serverId=<id>`（森空岛签名 GET，query 入签名）。奖章进度在 `data.detail.achieve.achieveMedals[]`，每枚含 `achievementData.id`（**32 位 hex = `md5(achv_id)`**）/ `level` / `isPlated`。**只有已获得的奖章出现在列表中**——不在列表即未获得。

> **关联键（2026-07-28 定论，以 `docs/skland_medal_id_mapping.md` 为准）**：森空岛 `achievementData.id` = **`md5(游戏 achv_id)`**，F2 按 `md5(AKEData.medal_id) == 森空岛 hex` 关联（精确）；非 `achv_` 条目才回退按规范化 name。
>
> *历史*：早先以为两源 id 不相关、改按 name 关联（135/140 命中，靠 suspect 启发式补「武陵·Ⅴ」）——但 name 会被命名滞后击穿（武陵·Ⅳ/·Ⅴ 撞名）。改 md5-id 后 owned 136/140、未获得 4（仅活动章），suspect 启发式已删除。

## 6.1 跨源关联实现

- `_parse_player_medal_progress`：解析森空岛响应，返回 `(按 hex id 索引, 按 name 索引)`。
- `build_medal_missing_view`：对每枚 AKEData 章，`md5(medal_id)` 查 hex 索引（主），非 achv_ id 时按 name（兜底）。
- 静态元数据（最高等级、可升级、可镀层）以 AKEData 为准，森空岛只提供玩家进度（level/isPlated/在不在列表）。

## 7. 验收标准

- **FR-1**：给定 medals 数据，输出总数及按 `max_level` 分组的各等级数量，各等级之和等于总数。
- **FR-2**：正确输出 `can_be_plated` 与 `can_be_upgraded` 两类数量。
- **FR-3**：给定 current 与 previous，正确输出新增蚀刻章；首次快照（previous 为空）新增为空；同一份数据与自身对比新增为空。
- **F1 命令**：`奖章 刷新` 能抓取并落盘；`奖章` 读快照秒回且不触网；二次刷新后 previous 已建立、无更新时新增为空。
- **F2**（待端点）：mock 小/大进度数据，三段分类与截断正确。

## 8. 基线数据（v1.4，AKEData 实测，2026-08-03）

> 以下数字为 AKEData（v1.4）实测：总数/等级/可镀层/可升级均来自当前运行时主源。

- 总数 140；按 `max_level` 分布：Lv1 = 24、Lv2 = 58、Lv3 = 58
- 可镀层 = 29；可升级 = 22
- 分类（依 `categoryPriority`）：章节 25 / 技艺 31 / 地区 26 / 锤炼 8 / 建设 24 / 社交 4 / 活动 9 / 奇想 13

## 9. 非目标 / 暂不实现

- 按 `categoryName` 的等级分布统计图（数字已在快照中，可作后续扩展）。
- FR-3 同 id 字段变更的 UI 展示段（首版仅日志）。
- FZ/Warfarin 作为奖章源的运行时降级适配（当前仅保留历史调研记录）。
