# 终末地关卡成员使用率（群内）设计与口径

> 状态：设计已定稿，未实现　·　创建 2026-10-23
> 关联：[关卡资料查询](endfield_stage_query_plan.md) · [持有率展示层](endfield_ownership_stats_renderer.md) · [持有率运维](endfield_ownership_stats_operations.md) · [森空岛个人接口](skland_endfield_personal_api.md)

## 1. 目标

把群内已绑定角色的「战争回响 / 影拓丰碑」最佳记录队伍聚合起来，回答两个问题：

- 打这一关，群里的人都在带谁？（干员使用率排行）
- 最常见的一整队是什么？（队伍组合 Top5）

统计对象是**最佳记录里出现的干员**。官方接口每关只给一条 `bestRecord`，没有逐次挑战记录，因此本功能不统计"上场次数"，只统计"最佳记录阵容"的使用面。

### 非目标

- 不做推荐阵容、强度评价、攻略投稿。
- 不展示任何个人身份（QQ、游戏 UID、昵称、样本级列表）。
- 本期不覆盖常驻关卡、危机合约。
- 不改动持有率的口径、表结构与命令。

## 2. 已确认口径

| 决策 | 结论 | 理由 |
| --- | --- | --- |
| 采集方式 | 后台快照批刷 + 手动刷新 | 命令秒回，风控可控 |
| 搭便车 | 用户查自己挑战卡时顺带落库 | 零额外官方请求，提升新鲜度 |
| 分母 | 有该关卡最佳记录的样本 | 反映打法偏好，不被没玩过的人稀释 |
| 覆盖数 | 同时输出「有记录样本 / 群内绑定角色」 | 让用户看到样本厚度 |
| 首版范围 | 战争回响 + 影拓丰碑，统一数据模型 | 两者采集成本相同（每角色每族 1 次请求） |
| 时间窗 | 默认当前活动期，可显式指定历史赛季/主题 | 默认贴近现状，历史数据接口本来就有 |
| 难度 | 按变体分别统计，默认最难档（残酷/苦难） | 难度决定配队，混算会稀释结论 |
| 展示 | 单关卡卡（排行 + 组合 Top5）；无关卡名 → 当期概览卡（每关 Top3） | 密度与实现成本平衡 |
| 入口 | 挂在挑战命令下，关卡名可选 | 复用现有关卡解析、别名与消歧 |
| 权限 | 与持有率一致 | 不新增第二套权限语义 |
| 管理员 | 单列「管理员（未区分性别）」合并计数 | 挑战记录只有共享 `charId`，无性别 |
| 样本下限 | 少于 3 个样本不出排行 | 避免 1/2 = 50% 这类假信号 |
| 期次归并 | 按活动名 + 时间窗归并，不只看 `activity_id` | 国服/亚服同一期 id 不同，只看 id 会把样本劈开 |
| 熔断 | usage 自带同参数熔断，P1 不重构持有率 | 不动已上线功能的私有状态，避免回归 |

## 3. 数据来源与可行性

### 3.1 接口

| 用途 | 接口 | 数据结构 |
| --- | --- | --- |
| 战争回响 | `GET /api/v1/game/endfield/card/war-echoes?roleId&serverId` | `data.warEchoes.seasons[].weeks[].dungeonGroups[]` → `normalDungeon/hardDungeon/cruelDungeon` |
| 影拓丰碑 | `GET /api/v1/game/endfield/card/indie-hard?roleId&serverId` | `data.indieHard.indieHardGroups[].dungeonGroups[]` → `normalDungeon/hardDungeon` |

每个关卡对象的可用字段：`id / name / isPass / recommendLevel / bestRecord{chars[], ts, passTs, firstPassTs} / enemies[]`；
`chars[]` 含 `charId / level / potentialLevel / rarity / property / evolvePhase / avatarUrl`。

两组响应已经由 `plugins/endfield/account/challenge/parsing.py` 解析成 `WarEchoPayload` / `MonumentPayload`，本功能直接复用，不新增第二套解析器。

### 3.2 已知限制

1. **只有一条最佳记录**：接口不返回历史尝试，统计口径只能是"最佳记录阵容"。
2. **`chars[]` 没有名字**：名称、职业、稀有度靠 `operator_catalog`（主键 `md5(charId)`）补；`OperatorCatalogEntry` 已有 `operator_key/source_id/name/rarity/profession/sort_order`。现场库核对：`operator_catalog` 32 行（AKEData 1.5.3）、`operator_roster_members` 2978 行、32 个 distinct `operator_key` **全部命中目录**，`operator_key` 全部是 32 位 hex，无一例原始 `chr_*` 形式——说明该键归一方式在生产中已经成立。
3. **没有性别字段**：男女管理员共用 `chr_9000_endmin`，持有率靠档案快照拆分，这里无法拆分，按合并条目处理。
4. **一次请求覆盖全部可见期**：不传 `seasonId` 时返回所有可见赛季/主题，历史数据顺带拿到，不需要按活动期多次请求。
5. **关卡名称已有现成对齐机制**：`ChallengeLocale` 用 AKEData `DungeonTable` 的 `dungeonId` 建索引，并额外注册 `md5(dungeonId)` 别名（`plugins/endfield/account/challenge/i18n.py:251`–`273`），正是为了让官方响应里的 id 命中。因此本功能不需要 FZ / `stages` 参与，也不需要猜 id 空间，直接 `get_challenge_locale()` 取中文名即可。
6. **id 形态以自检字段收口**：官方 payload 对 AKEData 实体普遍给 `md5(规范 id)`（见 `plugins/endfield/account/investment/service.py:462`–`465` 的实测注释），因此 `chars[].charId` 预期是 32 位 hex。为防例外，落库时统一走一个 resolver（见 §6.1），并在每次抓取记录一条匿名形态计数（只记 `hex32 / chr / other` 三类数量，不记值）。

### 3.3 复用现有基础设施

- 签名与凭据：`EndfieldOfficialClient._personal_card_payload`（共享签名上下文、单飞换新、405/429 共享冷却）。
- 角色级任务互斥：`gacha.service.ROLE_TASKS`，撞车时跳过且不记失败。
- 干员目录：`operator_catalog` 表 + `OwnershipStatsService.refresh_catalog` 的 6 小时检查，不新增目录采集。
- 群成员范围：`collect_group_member_ids` + `EndfieldStore.list_all_roles(qq_user_ids)`。

## 4. 采集与刷新

| 参数 | 值 | 说明 |
| --- | --- | --- |
| 有效窗口 | 48 小时 | 与持有率 `SNAPSHOT_TTL_SECONDS` 同口径 |
| 目标成功间隔 | 24 小时 | 超出窗口才重新排队 |
| 定时轮询 | 每 10 分钟 | 独立 timer `endfield_usage_refresh`，空轮询写 DEBUG |
| 批大小 | 20 个角色 / 批 | 每角色 2 族 → 每批 ≤ 40 次请求 |
| 并发 | 2 | 与持有率一致 |
| 失败退避 | 15 分钟 → 6 小时 | 指数退避，按错误类型 |
| 鉴权失败退避 | 6 小时 → 24 小时 | 凭据过期不反复打接口 |
| 熔断 | 连续 3 次 / 窗口 10 次出现 405/429/502/503/504 → 冷却 15 分钟 | 复用持有率那套 systemic 判据 |
| 定序 | 过期最久优先 | 同角色的两个玩法同批内串行，避免同角色并发 |

稳态负载：现场库有 141 个有快照的角色，若全部按 24 小时目标间隔轮转，平均每 10 分钟只排到约 6 个角色（≈12 次请求）；只有冷启动会打满 20 个角色的批次，全员覆盖约需 7 批、70 分钟左右，期间不会阻塞其它命令。

手动刷新：`/ef 使用率 刷新 [群内|全局]`（统一入口，影拓与回响一次刷完；旧的 `<玩法> 使用率 刷新` 已下线并提示新写法），群内需群主/群管理员，全局需 `SUPERUSER`；返回文字批次结果（不渲染图）。

搭便车：现有挑战卡查询成功后（`_render_challenge_cards` 拿到 `raw`），把该角色的这一族记录写入快照表；写失败只记 warning，不影响出图。

## 5. 存储设计

在 `EndfieldStore` 新增三张表（不改现有表结构）：

```sql
CREATE TABLE IF NOT EXISTS challenge_snapshots (
    role_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    family TEXT NOT NULL,              -- 'monument' | 'war_echo'
    fetched_at INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    failure_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (role_id, server_id, family)
);
CREATE INDEX IF NOT EXISTS idx_endfield_challenge_snapshot_freshness
    ON challenge_snapshots(family, fetched_at DESC);

CREATE TABLE IF NOT EXISTS challenge_records (
    role_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    family TEXT NOT NULL,
    activity_id TEXT NOT NULL DEFAULT '',   -- 赛季 id / 主题 id
    activity_name TEXT NOT NULL DEFAULT '',
    scope_id TEXT NOT NULL DEFAULT '',      -- 轮换周 id（影拓留空）
    scope_name TEXT NOT NULL DEFAULT '',
    stage_id TEXT NOT NULL,
    stage_name TEXT NOT NULL DEFAULT '',
    difficulty TEXT NOT NULL,               -- normal/hard/cruel
    passed INTEGER NOT NULL DEFAULT 0,
    record_ts INTEGER NOT NULL DEFAULT 0,
    pass_time INTEGER NOT NULL DEFAULT 0,
    first_pass_ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (role_id, server_id, family, stage_id, difficulty),
    FOREIGN KEY (role_id, server_id, family)
        REFERENCES challenge_snapshots(role_id, server_id, family) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS challenge_record_members (
    role_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    family TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    slot INTEGER NOT NULL,
    operator_key TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    potential INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (role_id, server_id, family, stage_id, difficulty, slot)
);
CREATE INDEX IF NOT EXISTS idx_endfield_challenge_member_agg
    ON challenge_record_members(family, stage_id, difficulty, operator_key);
```

- 每次成功抓取对同一 `(role_id, server_id, family)` **整族覆盖写**（同一事务内 delete + insert），避免已结束期次的脏行残留。
- `activity_id / scope_id` 保留官方原始 ID，名称只作展示；跨期同名关卡不会被合并。
- 容量：单角色约 60–90 条记录 + 4 倍成员行；100 个绑定角色约 3–4 万行，SQLite 无压力。
- 成员键归一：`32 位 hex` 直接用，否则 `md5(charId)`；`chr_9000_endmin` 统一映射到合成键 `endmin-shared`（展示名「管理员」）。

## 6. 聚合口径

- **样本**：范围为当前群（或全局）的绑定角色，按 `(role_id, server_id)` 去重；该关需 `passed=1` 且存在最佳记录成员。
- **分母** `sample_count`：上述样本数。
- **分子** `used_count`：使用该干员的样本数（同队重复出现只计 1）。
- **使用率** = `used_count / sample_count`；`sample_count == 0` 时为 `None`，不画成 0%。
- **覆盖** = `sample_count / bound_role_count`，两个数都展示。
- **排序**：使用率降序 → 使用次数降序 → 稀有度降序 → 目录顺序升序 → `operator_key` 升序。
- **队伍组合**：成员 `operator_key` 集合去重（忽略顺序与站位），统计频次取 Top5，并列时取 `record_ts` 更晚者。
- **队伍平均用时**：对同一队伍的样本取 `pass_time` 求算术平均（秒，四舍五入），展示为 `m:ss`。
- **用时一定存在**：`bestRecord.passTs` 只要有队伍就一定有值。实测 80 份官方响应、896 条 `bestRecord`，`passTs` 全部为 45–172 秒，没有 0 值也没有空 `chars`。因此「有队伍就有用时」，平均用时不需要降级文案。
- 代码仍按 `pass_time > 0` 过滤，只是防御（`ChallengeRecord` 默认 0 用于「关卡没有 bestRecord」的情况，此时也没有成员，本来就不进样本），不要把它当成常态写进产品文案。
- **难度隔离**：各难度独立统计，不合并。
- **期次隔离**：以 `activity_id + scope_id + stage_id + difficulty` 为键，不跨期相加。
- **样本不足**：`sample_count < 3` 时返回 `insufficient` 状态，只给覆盖数，不出排行。
- **按角色去重，不按 QQ**：同一 QQ 绑了多个角色就是多个样本；同一角色被多人绑定只算一个。
- **期次归并**：期次键取「活动名 + 时间窗重叠」，`activity_id` 只作原始记录。国服和亚服的同一期活动 id 不同但内容相同，只看 id 会把群里的样本劈成两半；报告额外输出 `regions` 说明样本来自哪些服。

### 6.1 干员键归一（resolver）

```text
operator_key_for(raw_id, catalog):
  1. raw_id 是 32 位 hex 且命中 catalog.operator_key   -> 直接用
  2. md5(raw_id) 命中 catalog.operator_key             -> 用 md5
  3. raw_id 命中 catalog.source_id                     -> 用该条目 operator_key
  4. 仍未命中                                          -> md5(raw_id)，名称留空，由展示层兜底
chr_9000_endmin（男女管理员共享 id）                  -> 合成键 endmin-shared，展示名「管理员」
```

同一 resolver 也用于把 `operator_catalog` 的稀缺度、职业、目录序补齐；未命中目录的干员按「未知干员」显示，不参与排序末尾以外的特殊处理。

## 7. 命令契约

| 命令 | 行为 |
| --- | --- |
| `/ef 影拓 使用率` | 当前主题（`isInActivity`）· 默认难度 `苦难` → 当期概览卡 |
| `/ef 影拓 使用率 <主题>` | 指定主题概览卡 |
| `/ef 影拓 使用率 <关卡> [普通\|苦难]` | 单关卡卡 |
| `/ef 影拓 使用率 <主题> <关卡> [普通\|苦难]` | 显式限定主题的单关卡卡 |
| `/ef 回响 使用率` | 当前赛季 · 当前轮换 · 默认难度 `残酷` → 当期概览卡 |
| `/ef 回响 使用率 <赛季> [轮换] [关卡] [普通\|困难\|残酷]` | 按层级收敛，末级给单关卡卡 |
| `/ef 使用率 刷新 [群内\|全局]` | 手动批刷（影拓 + 回响），返回文字批次结果 |
| 范围词 `群内\|全局` | 任意位置，语义同持有率；私聊默认 `全局`，私聊写 `群内` 直接报错 |

- 别名：`使用率 / 上场率 / 登场率 / usage / usage-rate`（别名集 `USAGE_ALIASES`）。
- `使用率` 在名称解析之前作为保留字拦截，避免与主题名、关卡名撞词。
- 关卡/主题解析复用 `resolve_monument_detail` / `resolve_war_detail` 的候选与歧义流程，错误提示沿用编号选择。
- 难度词沿用现有别名表（`普通/困难/残酷/苦难` 与 AKEData 名内嵌难度写法）。
- 概览卡与单关卡卡的区别：概览卡每关一行给 Top3，单关卡卡给完整排行与队伍组合；续期使用 `All` 档位时在概览卡上扩展。

## 8. 报告对象

`plugins/endfield/usage/models.py`（只读视图，不含身份字段）：

| 对象 | 字段 |
| --- | --- |
| `UsageStageKey` | `family, activity_id, scope_id, stage_id, difficulty` |
| `UsageOperator` | `operator_key, name, rarity, profession, sort_order, avatar_url, used_count, sample_count, usage_rate` |
| `UsageTeamMember` | `operator_key, name, rarity, avatar_url` |
| `UsageTeam` | `members, count, rate, latest_ts, avg_pass_time` |
| `UsageStageReport` | `key, stage_name, activity_name, scope_name, difficulty, scope, sample_count, bound_role_count, coverage, insufficient, operators, teams, team_combo_total, latest_record_ts, regions, generated_at, snapshot_updated_at` |
| `UsageStageSummary` | `stage_name, stage_id, difficulty, sample_count, insufficient, leads[UsageOperator], bound_role_count` |
| `UsageOverviewReport` | `family, activity_name, scope_name, difficulty, scope, bound_role_count, stages[UsageStageSummary], recorded_role_count, regions, generated_at, snapshot_updated_at` |

`UsageStageReport` 与 `OwnershipStatsReport` 遵守同一条隐私线：不出现 QQ、游戏 UID、昵称、`role_id` 或样本级列表。

## 9. 渲染

版式已单独定稿：[卡片前端设计](endfield_usage_stats_ui.md)，含单关卡卡、样本不足卡、当期概览卡三张示意图与状态矩阵。下面的条目只保留与后端契约相关的部分。

- 新增 `plugins/endfield/usage/draw.py`，版式沿用已上线的蚀刻章统计卡语言（深色头部 + 浅灰圆角面板），画布 1080px 竖版、移动端优先；素材管线复用 `plugins/endfield/rendering/cards.py`。头像走 AKEDATA sprites（`source_id`），管理员条目用文字兜底。
- 单关卡卡：深色头部（玩法 · 期次 · 关卡 · 难度）→ 时效条 → 样本条（有记录 X / 绑定 Y，覆盖率）→ 干员排行（Top8，其余合并一行）→ 队伍组合 Top3 → 页脚（渲染版本）。详细尺寸见前端设计稿。
- **时效必须画在脸上**：标题下方固定一行「最新记录：N 天前 · 快照：M 天前」。口径是"最佳记录"，玩家换队但没刷新记录时看到的仍是旧队，这行是唯一的解释入口。
- **样本不足要降级而不是空卡**：样本 < 3 时不画排行，改画「本期有记录 X / Y 关」＋ 覆盖率，并在历史期有数据时附一行「上一期 N 条记录，可用 `/ef 影拓 使用率 <上期主题>` 查看」。
- 概览卡：每关一块给 Top3 干员与样本数；样本不足的关卡用一行「样本不足（2/3）」占位，不整块丢弃；超过 5 关按页拆分，复用现有拆图与 overflow 检测。
- 头像缺失、未命中目录的干员用首字兜底方框，不把 hex id 印到图上。
- 缓存键：`CARD_RENDER_VERSION` + **聚合结果指纹**（排序后的 `(operator_key, used_count)` 列表、`sample_count`、`max(record_ts)` 的 sha256）+ **成员集合指纹**（群内角色键排序后的 sha256，保证退群/新绑定会失效）+ 范围 + 期次 + 难度 + 渲染模式。
  用聚合结果而不是 `max(fetched_at)`，是为了避免无关角色的一次刷新就让全场所有卡重渲染；用成员集合指纹，是为了避免缓存里留着已退群成员的数据。

## 10. 改动面

新增：

```text
plugins/endfield/usage/__init__.py
plugins/endfield/usage/models.py     # 报告对象与键
plugins/endfield/usage/service.py    # 抓取、落库、聚合、批次
plugins/endfield/usage/draw.py       # 概览卡与单关卡卡
tests/test_endfield_usage_stats.py
docs/endfield_usage_stats_design.md  # 本文件
```

修改：

- `plugins/endfield/account/store.py`：新增三张表与访问方法（不动现有表）。
- `plugins/endfield/handlers.py`：`使用率` 分派、刷新、定时任务、搭便车落库。
- `plugins/endfield/catalog/commands.py`：保留字、参数与范围解析、帮助文案。
- `scripts/help_pages.json` 与 `assets/image/help/endfield.png`：补充语法。

明确不做：改持有率口径或表、改账号卡视觉、把官方原始响应整份存库（只存成员键与时间戳）。

## 11. 隐私与日志

- 日志前缀 `[endfield-usage]`，只记批次 id、数量、耗时、匿名错误分类，不记 QQ、UID、昵称、`charId` 列表。
- 群内范围只在内存里传 role 集合，不写日志；缓存键里只出现成员集合的 sha256 指纹，不出现任何原始 id。
- 失败分类沿用持有率的匿名键风格（`api:战争回响:405`、`role-busy`）。

## 12. 测试与验收

`tests/test_endfield_usage_stats.py`：

1. 解析落库：monument / war 各一份 fixture；成员键归一（32 hex 直通、`chr_*` 转 md5、管理员合并）。
2. 聚合：分母只含有记录样本；无记录样本不入分母；同角色多绑定去重；空样本 `None` 而不是 0。
3. 排序与并列；队伍组合忽略顺序去重、同组合取最近 `record_ts`；Top5 截断。
4. `sample_count < 3` 状态与覆盖数；群成员列表失败直接失败、不回退全局。
5. 难度隔离与跨期同名关卡不合并。
6. 批次：限速、退避、熔断、角色占用不记失败；搭便车写入幂等且不覆盖更新鲜数据。
7. 渲染：长排行拆页、头像缺失兜底、超长关卡名、overflow 检测。
8. 命令解析：别名、范围词位置、私聊群内报错、默认难度、歧义候选。
9. 干员键 resolver：hex 直通、`chr_*` 转 md5、目录 source_id 命中、目录外兜底、管理员合成键（含 `md5(chr_9000_endmin)` 也被并入）。
10. 期次归并：同名不同 `activity_id` 合并计数；同名但时间窗不重叠不合并。
11. 写冲突：旧 `fetched_at` 的搭便车写入必须被丢弃，不能覆盖更新鲜的数据。
12. 缓存指纹：刷新一个无关角色后，目标群的卡仍命中缓存；成员集合变化后必然失效。
13. 时效字段：`record_ts`/`firstPassTs` 缺失或为 0 时显示「时间未知」，排序不抛异常。

验证命令（项目根目录）：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_endfield_usage_stats.py
.venv\Scripts\python.exe -m pytest tests/test_endfield_challenge.py
.venv\Scripts\python.exe -m ruff check plugins/endfield/usage plugins/endfield/account/store.py
.venv\Scripts\python.exe -m pytest tests/test_architecture.py
```

真实环境未验证项：多角色群下 `war-echoes` + `indie-hard` 的实际限速表现；官方 `charId` 形态（见 §14 风险 1 与 §15 核对清单第 1 步）。

## 13. 分期

- **P1（本设计）**：采集与快照、单关卡卡、当期概览卡、手动刷新、测试与帮助。
- **P2**：`All` 档位（跨难度/跨期合并）、全局范围排行、干员 × 关卡矩阵、按季节趋势。

## 14. 风险处置

状态口径：**已消解** = 现有代码/现场数据已证明不成问题；**已缓解** = 设计里有了明确的兜底与验收点；**待现场确认** = 只能等真实响应落地后核对一次。

| # | 风险 | 状态 | 处置 | 验收点 |
| --- | --- | --- | --- | --- |
| 1 | 干员 id 形态不确定（hex / `chr_*`） | 待现场确认（已缓解） | §6.1 resolver 四种形态全覆盖；落库时记一条匿名 `char_id_form=hex32\|chr\|other` 计数 | 首轮真实抓取后 `other=0`；若 `other>0` 追加一次形态调研 |
| 2 | 关卡名与静态资料对不上 | 已消解 | 现成 `ChallengeLocale` 已按 `dungeonId` + `md5(dungeonId)` 对齐 AKEData，挑战卡的中文名一直走这条链 | 统计卡的关卡名与个人挑战卡同名 |
| 3 | 小群样本稀疏 | 已缓解 | 样本 <3 时降级卡（覆盖数 + 上一期提示），不出排行；概览卡逐关占位不整块丢弃 | 空群、单样本群、3 样本群各出一张卡 |
| 4 | 最佳记录时效性 | 已缓解 | 卡面固定一行「最新记录 / 快照」天数；文档与帮助都写明"最佳记录"口径 | 手工改 fixture 的 `record_ts`，卡面天数随之变化 |
| 5 | 官方风控与请求量 | 已缓解 | 独立批次（2 并发、每批 20 角色、48h 窗口）、指数退避、熔断；稳态每 10 分钟约 12 次请求 | 批次日志的 `requested` 与稳态估算一致；模拟 3 次 405 后熔断打开 |
| 6 | 搭便车写入与后台批次互相覆盖 | 已缓解 | 只在新 `fetched_at` 更新时写入；同角色 `ROLE_TASKS` 互斥 + 进程内单角色锁 | 先写新数据再让旧批次写入，旧数据必须被丢弃 |
| 7 | 缓存抖动导致全卡重渲染 | 已缓解 | 缓存键用聚合结果指纹 + 成员集合指纹，不用 `max(fetched_at)` | 刷新一个无关角色后，目标群的卡命中缓存 |
| 8 | 新干员不在目录里 | 已缓解 | resolver 兜底为「未知干员」；目录每 6 小时检查；现场 32/32 命中 | 构造一个目录外 id，卡片显示「未知干员」而不是崩 |
| 9 | 国服/亚服同名活动被拆成两期 | 待现场确认（已缓解） | 期次归并键取 `activity_name` + 时间窗重叠，而不是单看 `activity_id`；报告带 `regions` 字段 | 同时有国服/亚服样本时样本不分裂；名称不同则退回按期次 ID 并在卡上标注 |
| 10 | `record_ts`/`firstPassTs` 缺失或为 0 | 已缓解 | 时间字段全走 `_int` 兜底，为 0 时显示「时间未知」，不参与"最新记录"排序 | fixture 缺字段时不抛异常，排序稳定 |

### 已决的技术选择

- **不重构持有率的熔断器**。`OwnershipStatsService` 的熔断状态是实例私有字段，P1 不去动它：usage 自带一份同参数的熔断，让两个功能各自打开。理由是持有率是已上线的稳定功能，为了共享一个冷却状态去改它的私有结构，回归风险大于收益。P2 再考虑把熔断原语提取到 `plugins/endfield/account/` 下共用。
- **不引入 FZ / `stages` 依赖**。名称与难度文案全部来自 `ChallengeLocale`，避免第二套关卡 ID 对齐假设。
- **统计口径按游戏角色去重，不按 QQ**。同一 QQ 多角色是多个样本；同角色多人绑定只算一个。

## 15. 实现第一步的核对清单

按顺序做，任一项不通过就先停下来改设计，不带着不确定写代码：

1. 抓一份真实 `war-echoes` / `indie-hard` 响应（在真实账号上跑一次现有挑战卡查询即可复用同一份数据），记录 `char_id_form` 与 `dungeon_id_form` 的匿名计数。
2. 把该响应喂给 `parse_monument` / `parse_war_echoes`，确认 `bestRecord.chars` 的条数（应为 1–4）与 `dungeon.id` 能命中 `ChallengeLocale.dungeons`。
3. 核对 `operator_catalog` 对这批判定出的 key 的命中率；不足 100% 时先确认是目录过期还是形态问题。
4. 用一个群的真实绑定集合跑一次 `build_report`，检查 `sample_count`、覆盖率、`insufficient` 三个数是否自洽。
5. 出图检查：单关卡卡、样本不足卡、概览卡拆页三种形态。

## 16. 实现落地（2026-10）

已按 §7 契约上线，入口与行为：

| 命令 | 实际行为 |
| --- | --- |
| `/ef 影拓 使用率` / `/ef 回响 使用率` | 当前期次（时间窗含今天，否则取最近结束的一期）· 默认难度（影拓 `苦难`、回响 `残酷`）→ 当期概览卡 |
| `… 使用率 <主题|赛季> [轮换]` | 限定期次后的概览卡 |
| `… 使用率 <…> <关卡>` | 单关卡卡（Top8 干员 + 队伍 Top3） |
| `/ef 使用率 刷新 [群内|全局]` | 手动批刷影拓与回响并回文字批次结果，权限同持有率刷新 |
| `群内|全局` 范围词 | 任意位置各一次；私聊写 `群内` 直接拒绝 |

代码落点：

- `plugins/endfield/account/store.py`：新增 `challenge_snapshots` / `challenge_records` / `challenge_record_members` 三表与 `replace_challenge_snapshot`（整族覆盖、按 `fetched_at` 拒绝更旧的响应）、`mark_challenge_failure`、`list_challenge_snapshots`、`list_challenge_member_rows`、`cleanup_orphan_challenge_snapshots`。
- `plugins/endfield/usage/{models,service,draw}.py`：报告对象、抓取/落库/聚合/批次、三形态卡片。
- `plugins/endfield/catalog/commands.py`：`USAGE_ALIASES` 保留字与 `_parse_usage_command`，帮助文案三行。
- `plugins/endfield/handlers.py`：`_handle_usage_stats`、`_piggyback_usage_snapshot`（挑战卡出图后搭便车落库）、`timer.add_job(..., id="endfield_usage_refresh")`（10 分钟一批，每批 20 角色、2 并发）。
- `tests/test_endfield_usage_stats.py`：11 项，覆盖键归一、两族解析、陈旧写入、聚合口径、难度隔离、概览、解析与路由、批次与失败退避。

与设计稿的差异（有意）：

- **没有做聚合级渲染缓存**：首版每次直接出图（实测 0.35–1.2 s 截图），先拿到真实群里的样本分布再决定缓存键形态。§9 的缓存键设计保留，属于后续优化项。
- **"当前期次"按时间窗判定**：`challenge_records` 存了 `activity_start_ts/end_ts` 与 `scope_start_ts/end_ts`，不依赖各角色响应里的 `isInActivity`，因为汇总的是多个角色的快照。
- **配置类报告字段**：新增 `UsageStageReport.team_combo_total` 与 `UsageOverviewReport.recorded_role_count`，避免渲染层拿 Top N 长度冒充"队伍组合总数"。
- **跨关去重覆盖率**：概览卡第 3 格是"有记录角色 ÷ 绑定角色"，不是单关最高覆盖率。

### 16.1 首轮上线后的修正

1. **中文关卡名（i18n）**：官方挑战接口会忽略 `sk-language` 直接返回英文原文（`Cycle of Phantasia Ⅱ`、`Glaiver File: Yinglung`），中文必须由 AKEData 对照回填。原来只有后台批次调用 `parse_*` 时没带 locale，English 原文被原样落库。现在：批次开跑前取一次 `get_challenge_locale()` 并随 `persist_raw` 落库；出报告时再按 `stage_id`（`ChallengeLocale.dungeon_text`）与文本反查（`ChallengeLocale.text`，EN→CN 精确映射）翻译一次，**历史行也会被就地纠正**，不必重抓。
2. **亚服不参与**：`card/indie-hard` 与 `card/war-echoes` 只有国服接口，刷批次和统计分母都先用 `is_asia_role` 过滤，避免白白请求和把亚服的英文数据混进来。
3. **同一关卡在不同轮换复用时被覆盖（根因）**：`challenge_records` 与 `challenge_record_members` 的联合主键只有 `(role, server, family, stage_id, difficulty)`，少了 `activity_id` 与 `scope_id`。官方大量复用关卡（野性旧事同时出现在谵妄轮换Ⅰ/Ⅱ，弓弩表象同时在谵妄与追忆），于是后写的赛季把先写的顶掉：实测一份真实 `war-echoes` 响应有 45 条记录，落库后只剩 24 条，当期轮换只剩一关。现在两张表的主键都包含 `activity_id`、`scope_id`，旧库在打开时自动重建三张挑战表（纯快照缓存，下一批重抓）。修完后同一份响应 45 条全部保留，当期轮换 3 关齐全。
4. **轮换里的空关卡不再消失**：聚合原来只从「有最佳记录成员」的行来，官方又只给最佳记录，所以轮换里没人通关的关卡会整块不见（实测一期 3 关只出 1 关）。现在落库时记录全部关卡，出卡时给缺数据的关卡补占位行：概览卡显示「样本 0 · 覆盖率 0% · 本群还没有人在这关留下记录」，可统计关卡变成「1 / 3」；点名查空关卡也出样本不足卡而不是报错。
5. **升级不再清空挑战缓存**：老库升级时保留 `challenge_records`（关卡清单还能用），只重建成员表并把快照标成过期，下一批立刻重抓；查询端不会突然变成「没有记录」。旧数据里的关卡名、时间窗都会搬过去，缺失的列补默认值。
6. **空库自愈**：查询时发现统计范围里一条记录都没有，会顺手起一次后台抓取（每批 20 个角色，2 分钟内只触发一次），文案改成「已开始后台抓取，约 1 分钟后重试」，不再只是让你干等。
7. **关卡名去重复难度**：官方关卡名自带「·残酷」，卡片另有难度标签，现在出卡时剥掉后缀（`野性旧事·残酷` → `野性旧事`）。
8. **页脚去版本号**：卡片右下角不再印 `endfield-card-v58`，只留「第 N / M 页」这类真有必要的信息。

### 16.2 历史总使用率与 @群友（2026-10）

两个新口径，都可与现有卡片叠加：

- `/ef 影拓 使用率 历史`：把所有期次的记录**合并**成一份「历史总使用率」，不是一期一行、也不是通过率。分母是**记录条数**（每条最佳记录一支队伍），因此 `谵妄轮换Ⅱ` 与 `追忆轮换Ⅲ` 里的同一支队伍会正确累加；卡片给 Top 8 干员 + 常用户队伍 Top 3，样本格为「有记录角色 / 绑定角色 / 覆盖期数 / 记录样本」。样例见 `docs/images/endfield-usage-stats/check-history-real.png`（5 期合并、24 条记录）。
- 期次名仍可写，作用是**筛期次**（`使用率 历史 追忆赛季` 只合并该赛季）。
- 不分页：`第N页` 会直接报「使用率不需要页码，历史总使用率一次给出全部期次」。
- `/ef 影拓 使用率 @群友`：只看该成员绑定的角色，右上角标记「个人统计」、页脚写「该成员绑定角色」；样本阈值 3 人不再适用（个人数据本来就只有 1 份），因此单人当期卡与单人生涯卡都直接出排行。`@群友` 必须放在最后，可与 `历史` 叠加：`/ef 回响 使用率 历史 @群友` 即该成员跨全部期次的总使用率。
- 刷新命令不接受 `@群友`（只读），私聊不接受 `@群友`。
- **刷新收成一个二级入口**：`/ef 使用率 刷新 [群内|全局]`，与 `/ef 持有率 刷新` 同形。批刷本来就不分玩法（`_family_tasks` 给每个角色同时排影拓与回响），带玩法前缀的两种写法只是让人误以为各刷各的，现已下线，旧写法会回「刷新请用 /ef 使用率 刷新 [群内|全局]，一次刷完影拓与回响」。

未在真实环境验证：真实 QQ 群的 @群友 路径（本地用构造的角色集合与录制响应验证）、森空岛真实返回的字段差异、`ruff check`（`.venv` 未安装 ruff）。

验证记录：

- `.venv\Scripts\python.exe -m pytest tests/test_endfield_usage_stats.py -q` → 11 passed。
- `.venv\Scripts\python.exe -m pytest tests/test_architecture.py -q` → 12 passed, 9 subtests passed。
- 服务层 → 渲染层联调：`docs/images/endfield-usage-stats/check-e2e-stage.png`（1350×1709）、`check-e2e-overview.png`（1350×1634）、`check-e2e-war-overview.png`（1350×1245，含轮换名）；渲染层独立自测：`check-stage.png`（1350×2574）、`check-insufficient.png`（1350×1613）、`check-overview.png`（1350×1634）。
- 视觉口径见 [卡片前端设计](endfield_usage_stats_ui.md)：1080 逻辑宽 × 生产倍率 1.25 = 1350px 输出。

