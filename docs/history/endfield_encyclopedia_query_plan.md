# 终末地图鉴查询方案（物品 / 道具 / 敌人 / 词条 / 档案条目）

版本：**v2**（2026-09-23）。状态：**方案，已按 2026-09-22 评审修订，未实现**。本文只定命令、数据口径、模块边界和分期。仓库里还没有 `plugins/endfield/encyclopedia/`。

参照对象是现有干员、武器查询：`/ef 干员 <名>`、`/ef 武器 <名>` 走同一条链路——命令解析、模糊候选、视图模型、HTML 卡片、成品缓存。新资料挂到这条链路上，不另起插件，也不重做已经上线的干员、武器、装备、关卡、奖章和档案收集统计。

---

## 修订说明（v1 → v2）

v1 把 §4.1 探针要回答的问题当成已知结论写进了 §6。v2 先做了探针（AkeData `latest = 1.5.3@10024360-6`，表目录 `public/1.5.3/10024360-6/TableCfg`），再按结果改方案。所有条目数改为 1.5.3 实测；代码行号对应本仓库当前提交。

| # | 位置 | v1 | v2 | 依据 |
| --- | --- | --- | --- | --- |
| 1 | §2 §6.4 §10 | 词条卡带等级表、公式、源石技艺强度代入；第 3 期与速算对账 | 全部撤掉。词条卡只出说明、样式、关联词条、来源技能 | `HyperlinkTextTable` 无 Buff 引用；64 条 `ba.*` 说明无 `{key}`；`SkillPatchTable` 509 行无 `buffId`、无 `ba.` 引用；`BuffData` 无描述模板 |
| 2 | §2 §3 §6.2 | 「食物」是一种实体，有独立子集与目录 | 实体改为「道具」= `UseItemTable` ∪ `EquipItemTable`；食物只是别名 | 101 个物品类型无一含食、料理、饮、菜；炝炒时蔬与柑实冲剂同属「战术物品」，镇痛药膏属「消耗品」 |
| 3 | §4 §6.1 | 物品目录按 `showingType` 汇总；用 id 前缀排除专属卡 | 索引与目录按 `type` 分组，只收白名单类型；排除按 `type` 做 | 2829 条中 1833 条 `showingType = 0`；`item_equip_` 前缀 281 条对「装备」类型 273 条 |
| 4 | §3.1 | 种类同时命中时按八级优先级取胜 | 不加优先级。一个实体只进一种索引；真正同分仍走编号列表 | `_collect_candidates` 合并后只按分数排（`handlers.py:3207`），`choose_candidate` 无种类平局规则（`catalog/commands.py:753`）；加优先级会改现有干员/武器同分行为 |
| 5 | §4 §6.3 | 敌人目录按 `tags` 分组；模板 84、实例 359 | 按 `displayType` 分组；模板 92、实例 381；补 `distributionIds` | 抽样 `tags` 为空；`displayType` 全有值，分布 0:44 / 1:20 / 3:12 / 2:10 / 4:6 |
| 6 | §6.4 | 词条目录按 `TERM_SUFFIXES` 后缀分组；同名多条走多候选 | 按表自带 `richTextId` 分组；定主条目规则处理「法术异常 - X」与「干员受到X」 | 64 条无重名；`richTextId` 分布 key 21 / phy 8 / cryst 8 / fire 7 / natur 7 / pulse 7 / poise 1 |
| 7 | §6.1 | 获取途径只在 rewardId、配方、商店有引用时填 | 直接读 `ItemTable.obtainWayIds` / `noObtainWayHint` | 1874 条非空，293 个不同途径 id |
| 8 | §4 | 表清单无 `EquipItemTable` | 补上；冷却、装填、施放时间只能从它来 | 50 行，与 `UseItemTable` 的 50 条战术物品一一对应 |
| 9 | §3.2 §5 | 「回复该类资料只提供 AkeData」 | 明确在 handler 里显式判断并列入接入点 | 今天不支持的来源取不到 resolver 直接返回空（`handlers.py:2161`），用户只看到「没有找到」 |
| 10 | §7 | 别名 kind 扩展为五种 | 写明 `SUPPORTED_KINDS`、`alias_data.json` 结构与 `/ef 别名 添加` 的范围词 | `catalog/aliases.py:9` 只有三种 |
| 11 | §7 | 索引按「AkeData `latest` 版本号」失效 | 按 `AkeSnapshot.revision`（`version\|sharedRevision`）失效 | `providers/repository.py:43`；成品缓存已用它 |
| 12 | §4.1 §11 | 探针另写脚本；夹具从零做 | 扩 `scripts/inspect_endfield_akedata.py` 的 `--tables`；复用 `tests/fixtures/endfield_akedata_1_5_3.json` | 该脚本已拉 ItemTable / Hyperlink / RichText / SkillPatch；夹具已含这四张表子集 |
| 13 | §3.1 §5 | 档案分流只提「档案」「档案库」；取表放在 `service.py` | 覆盖 `ARCHIVE_ALIASES` 全部五个词；照关卡模块拆出 `akedata.py` | `catalog/commands.py:59`；`stages/akedata.py` 的分工 |

---

## 1. 已经有的，本方案沿用

| 能力 | 现状 | 本方案 |
| --- | --- | --- |
| 干员 / 武器 / 装备详情与目录 | AkeData 优先，缺字段时干员与武器可回退 FZ / Warfarin | 保持原命令、原数据源顺序 |
| 关卡 | 只走 AkeData；敌人只作为关卡卡上的一行 | 保持。敌人单独成查询后，关卡卡继续用自己的视图 |
| 奖章、档案收集 | `/ef 奖章`、`/ef 档案` 是全库统计、版本新增、个人进度 | 统计命令保持。本方案只补「按名字打开一条档案」 |
| 技能里的彩色词 | `HyperlinkTextTable` + `RichTextStyleTable` 只负责颜色和图标 | 词条查询复用这套样式，**不补数值**（见 §6.4） |
| 速算 | `/ef 速算 2腐蚀 200`，倍率写死在 `LOADOUT_STATUS_LEVELS`（导电、腐蚀、碎甲） | **不动**。词条表里没有数值，无从对账 |
| 关卡的 Buff 读取 | `stages/akedata.py:297` 按 `public/Json/BuffData/<buffId>.json` 取 Buff，`_buff_modifiers` 把 blackboard 盖到 `attributeModifier` | 道具效果复用这套读取，不复制第二套 |

干员卡、武器卡已经把技能 blackboard 代进描述，未解出的 `{参数}` 会拒绝出卡，而不是编一个数。道具效果沿用这条规则。

---

## 2. 要补的五种资料

用户口令和数据实体分开。口令是群里怎么打；实体是卡片背后的一条记录。

| 口令 | 实体 | 一条记录是什么 | 空查询出什么 |
| --- | --- | --- | --- |
| 物品 | 物品 | `ItemTable` 里 `type` 在白名单内、且没有专属命令的物品 | 按 `type` 名分组的目录，每组只写数量 |
| 道具 | 道具 | `UseItemTable` 里的一条（85 条），带使用效果；其中 50 条同时在 `EquipItemTable` | 道具目录，按 `type` 名分组 |
| 敌人 | 敌人模板 | `EnemyTemplateDisplayInfoTable` 的一条展示模板（92 条） | 按 `displayType` 分组的模板目录 |
| 词条 | 战斗术语 | `HyperlinkTextTable` 里一条 `ba.*`（64 条） | 按 `richTextId` 词族分组的目录 |
| 档案 | 档案条目 | 现有档案快照里的一条 `nar_*` | 仍是现在的收集统计卡 |

**「食物」不是实体。** 数据里没有食物类型：料理（炝炒时蔬、手撕虫肉）、药剂（简易镇痛药膏、正本补元汤剂）、冲剂（柑实冲剂）同在「战术物品」与「消耗品」两个类型里，没有字段能区分。用户打「食物」「料理」时按「道具」范围查。

道具是物品的子集。一个 id 只进一种索引：在 `UseItemTable` 里的进道具索引，不再进物品索引。

「等」里仓库已经有数据、但本方案不做的：设施、任务、商店、活动、教程。它们继续留在后续，不进第一期命令。

---

## 3. 命令

根命令不变：`/ef`、`/终末地`、`/zmd`、`/endfield`。新口令都是它后面的范围词，风格和 `干员`、`武器`、`关卡` 相同。

```text
/ef 物品
/ef 物品 <名字或类型名>
/ef 道具
/ef 道具 <名字>
/ef 敌人
/ef 敌人 <名字>
/ef 词条
/ef 词条 <名字>
/ef 档案 <名字>
/ef 搜索 物品|道具|敌人|词条|档案 <关键词>
```

别名：

| 范围 | kind | 中文 | 英文 |
| --- | --- | --- | --- |
| 物品 | `item` | 物品、材料 | item、items |
| 道具 | `prop` | 道具、食物、料理、药剂 | prop、props、food |
| 敌人 | `enemy` | 敌人、怪物 | enemy、enemies |
| 词条 | `term` | 词条、术语 | term、terms |
| 档案条目 | `archive_entry` | 仍用「档案」，见 §3.1 | 不用 `archive`，已被 `CHALLENGE_HISTORY_ALIASES` 占用 |

不新增 `/efitem` 这类根快捷命令。干员、武器、装备的快捷键是历史入口，图鉴五种不再复制一套。

### 3.1 和现有命令怎么共存

`/ef 档案`、`/ef 档案 刷新`、`/ef 档案 收集` 的行为保持今天的实现（`catalog/commands.py:358-368`）。分流规则对 `ARCHIVE_ALIASES` 的五个词（档案、档案库、报告、report、reports）一致：先认固定子命令，剩下的词才当条目名。

| 输入 | 动作 |
| --- | --- |
| `/ef 档案` | `archive_view`，收集统计 |
| `/ef 档案 刷新` | `archive_refresh` |
| `/ef 档案 收集 [账号]` | `archive_progress` |
| `/ef 档案 终末地` 这类带名字 | `archive_entry`，单条档案卡 |
| `/ef 报告 某名字` | 同上，`archive_entry` |

今天 `/ef 档案 多余的词` 落到 `archive_view`；改为 `archive_entry` 后，原来被忽略的尾巴会变成一次查询。这是预期变化，写进测试。

**候选与种类的关系（v2 改）。** 不加跨种类优先级。做法：

- 每个实体只进一种索引，种类在 `classify.py` 里定死：在 `UseItemTable` 的是 `prop`；装备、武器、蚀刻章不进图鉴索引；其余白名单类型是 `item`。
- `/ef <关键词>` 的全局候选把五种加进去，仍走 `_collect_candidates` → `_dedupe_candidates` → `choose_candidate`：门槛 65，明显命中 70，与次名差距小于 8 则出编号列表。这三个常量在 `catalog/commands.py:111-113`，不改。
- 真正同名同分（例如某道具与某干员同名）仍出编号列表，和今天干员与武器同分时一样。

`/ef 物品 <装备名或武器名>`：物品索引里没有这些 id，所以先在物品索引里查不到；handler 再用 `equipment`、`weapon` 两种 kind 补查一次，命中即走原卡，标题仍写实际种类。蚀刻章没有单条查询命令，提示「奖章请用 /ef 奖章」。

### 3.2 数据源参数

这五种只读 AkeData。`providers/registry.py` 的 `DATA_SOURCES` 给 `akedata` 加上 `item`、`prop`、`enemy`、`term`、`archive_entry`，FZ 与 Warfarin 不加。

用户写 `--source fz` 或 `--source warfarin` 时回复「该类资料只提供 AkeData」。**这一句要在 handler 里显式做**：今天不支持的来源在 `_resolve_candidates_from_sources` 里取不到 resolver，直接返回空列表（`handlers.py:2151-2177`），用户看到的是「没有找到」。规则：范围是这五种之一、且 `command.source` 不在 `source_order(kind)` 里，在调用 `_collect_candidates` 之前拒绝。范围是 `all` 时沿用今天的做法，不支持该来源的 kind 静默跳过。

`/ef 数据源` 的说明里加一行。

---

## 4. 数据从哪来

全部走现有 AkeData 客户端：`manifest.json` → 当前 `tableCfgPath` → 表 JSON，中文走 `I18nTextTable_CN`（1.5.3 为 147603 条，约 18MB），沿用现在的磁盘缓存 `data/cache/akedata-tables-v1.sqlite3`，一个 revision 拉一次。

图标根路径与 `docs/akedata_data_access_guide.md` 相同：

```text
https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites
```

| 资料 | 表或文件 | 1.5.3 条目 | 图标 |
| --- | --- | --- | --- |
| 物品 | `ItemTable`、`ItemTypeTable`、`ItemShowingTypeTable` | 2829 / 101 / 11 | `itemiconbig/<iconId>.png`，没有 `iconId` 再用物品 id；复用 `providers/assets.py:133` 的 `item_icon_urls` |
| 道具 | `UseItemTable`、`EquipItemTable` | 85 / 50 | 同物品 |
| 敌人 | `EnemyTemplateDisplayInfoTable`（名字）、`EnemyTable`（实例）、`EnemyAttributeTemplateTable`（抗性）、`EnemyAbilityDescTable`（能力说明） | 92 / 381 / 136 / 181 | `monstericonbig/<templateId>.png`（`stages/akedata.py:754` 已在用） |
| 词条 | `HyperlinkTextTable`（`ba.*` 64 条）、`RichTextStyleTable` | 107 / 96 | 条目 `iconPath`，形如 `TermIcon/icon_term_ba_burning`；对应 `sprites/termicon/<名>.png`，与 `rendering/cards.py` 的 `sprite_png("termicon", …)` 一致 |
| 道具效果的 Buff | `public/Json/BuffData/<buffId>.json`，`buffId` 来自 `UseItemTable.useActions[].buffBBData.buffId` | — | 无单独图标 |
| 档案条目 | 现有 `PrtsPage` / `PrtsCategory` / `PrtsFirstLv` / `PrtsAllItem` 快照 | — | 现有 `sprites/prts/icon/<icon>.png` |

### 4.1 物品的分类事实（v2 实测）

`ItemTable` 2829 条的 `type` 前十二：装备 6（273）、材料 8（264）、装备制造模板 47（258）、蚀刻章 100（226）、基质 19（201）、配方解锁 39（135）、任务物品 13（126）、type 76（100）、type 12（95）、武器 5（79）、type 73（60）、type 72（60）。

`showingType` 只有 11 个值有名字：矿物 1、植物 2、产物 4、可用道具 5、护手 8、护甲 9、配件 10、采集材料 15、培养素材 16、生产工具 17、随身装置 18。**1833 条的 `showingType` 为 0**，它们的类型前十是装备制造模板、蚀刻章、基质、任务物品、干员留影、STA协议模板、武器、沉积具象物、系统蓝图、头像。按 `showingType` 汇总会把 65% 的条目放进「无」，所以目录改按 `type`。

物品索引只收 `ITEM_TYPE_WHITELIST` 内的类型。初版白名单（探针最终确认 id）：

- 收：材料 8、战术物品 48、消耗品 52、探测器 55，以及 `showingType` 为矿物 / 植物 / 产物 / 采集材料 / 培养素材 / 生产工具 / 随身装置 的条目所属的 `type`。
- 不收（有专属命令）：装备 6 → `/ef 装备`；武器 5 → `/ef 武器`；蚀刻章 100 → `/ef 奖章`。按 `type` 排除，不按 id 前缀：`item_equip_` 前缀 281 条，装备类型 273 条，前缀会多排 8 条。
- 不收（不是图鉴内容）：装备制造模板、基质、配方解锁、任务物品、干员留影、STA协议模板、沉积具象物、系统蓝图、头像、货币、活动代币。

白名单放在 `classify.py`，是一张 `{type_id: 组名}` 的常量表，不散落在渲染里。

### 4.2 道具的分类事实（v2 实测）

`UseItemTable` 85 条按物品类型分：战术物品 50、消耗品 26、探测器 5、通行证申请 2、月卡兑换凭证 1、ID信息更新卡 1。

`EquipItemTable` 50 条，全部是战术物品，id 与 `UseItemTable` 的 50 条战术物品完全重合。字段：`castTime`、`cooldown`、`chargeCount`、`recoverTime`、`recoverUpperCount`、`levelUpChargeCount`、`equipDesc`、`equipExtraDesc`、`useTarget` 等。**冷却、装填、施放时间只在这张表**，v1 漏了它。

`UseItemTable` 字段：`duration`、`effectType`、`isPersistentBuff`、`itemUseDesc`、`useActions[]`、`uiType`、`targetNumType`、`gameModeForbidTagList`。85 条里 73 条 `itemUseDesc` 带 `{key:格式}` 占位符，例如：

```text
使用后，每秒回复<@ba.vup>{triggerheal2:0%}</>最大生命值和<@ba.vup>{triggerheal:0}</>点生命值
```

每条 `useActions[]` 自带 `buffBBData.blackboard`（`key` / `value` / `valueStr`）和 `buffId`（如 `buff_common_atk_buff_potion_1`）。占位符用同一条的 blackboard 代入，与干员技能的处理一致。

### 4.3 词条的分类事实（v2 实测）

`HyperlinkTextTable` 的 `ba.*` 条目字段只有六个：`id`、`name`、`desc`、`iconPath`、`richTextId`、`jumpWikiId`。**没有 Buff 引用。**

- 64 条说明文本里 `{key}` 占位符为 0 处；20 条正文里含数字，那是游戏原文，不是模板。
- `SkillPatchTable` 509 行、3.5MB，`buffId` 出现 0 次，`ba.` 引用 0 次。技能不通过词条 id 指向 Buff。
- `BuffData` 文件顶层是 `attributeModifier`、`damageModifier`、`healModifier`、`poiseModifier`、`globalModifier`、`shieldConfigs`、`applyTags`、`duration` 等，没有描述模板；按 `ba.burning`、`buff_ba_burning` 猜文件名均 404。

结论：**词条数值没有数据路径。** v1 的等级表、公式、源石技艺强度代入、与 `LOADOUT_STATUS_LEVELS` 对账全部撤掉。唯一未验证的线索是 `BuffData.applyTags` 是否含 `ba.*` id，列入 §4.5 的后续探针，不进任何一期。

名字形如「法术异常 - 燃烧」「干员受到燃烧」「灼热增幅」「灼热脆弱」「法术附着 - 灼热」「法术爆发 - 灼热」，64 条无重名。`richTextId` 分布：`ba.key` 21、`ba.phy` 8、`ba.cryst` 8、`ba.fire` 7、`ba.natur` 7、`ba.pulse` 7、`ba.poise` 1、空 5。说明里的标记有 `<@id>` 91 处、`<#id>` 102 处两种；`rendering/cards.py:3509` 现在只认 `<#`。

### 4.4 敌人的分类事实（v2 实测）

模板字段：`templateId`、`name`、`nickname`、`description`、`abilityDescIds[]`、`tags[]`、`displayType`、`distributionIds[]`。抽样 `tags` 为空；`displayType` 全有值，分布 0:44、1:20、3:12、2:10、4:6，**含义待探**（§4.5）。`distributionIds` 形如 `distribution_map01_lv001`、`distribution_world_energy_point01`，是出现区域，v1 没用上。

`EnemyAbilityDescTable` 181 条，字段 `abilityId`、`name`、`description`；`common_ability` 的 `name` 为空 id 0，能力列表要允许无名条目。

### 4.5 开做之前还要探的

v2 探针已回答 v1 §4.1 的第 1、2、3、4 问，第 5 问（`PrtsAllItem` 有无正文）由 `docs/endfield_archive_guide.md` 与 `catalog/models.py:508` 的注释回答：没有正文。剩余问题：

1. `ItemTypeTable` 全部 101 个 id → 中文名对照，落成 `classify.py` 的白名单常量；确认 type 76 / 12 / 73 / 72 的名字。
2. `displayType` 0–4 的含义。站点 `v3-table-data.js` 或 `EnemyTemplateDisplayInfoTable` 的同目录枚举表里找；找不到就按数值分组并用「类型 N」做组名。
3. `obtainWayIds` 的名字表。`ItemObtainWayTable` 等六个猜测名均 404，从 `v3-table-data.js` 反查。找不到时途径只显示条数，不显示 id。
4. `distributionIds` 的名字表，同上。
5. `iconPath` `TermIcon/x` → `sprites/termicon/x.png` 的映射，抽三条实际请求确认。
6. `BuffData.applyTags` 是否含 `ba.*` id（只记录，不进本方案任何一期）。

探针不另写脚本：扩 `scripts/inspect_endfield_akedata.py` 的 `--tables` 默认值，加 `ItemTypeTable`、`ItemShowingTypeTable`、`UseItemTable`、`EquipItemTable`、`EnemyTable`、`EnemyTemplateDisplayInfoTable`、`EnemyAttributeTemplateTable`、`EnemyAbilityDescTable`。结果写进本文附录。探针期间不对用户开放命令。

---

## 5. 模块

放在终末地插件内，按关卡模块的拆法（`stages/akedata.py` 读表与解析、`stages/service.py` 只做策略），不把逻辑再堆进 `catalog/service.py` 和 `handlers.py`。

```text
plugins/endfield/encyclopedia/
├── __init__.py
├── models.py        # 纯 dataclass
├── classify.py      # ITEM_TYPE_WHITELIST、专属命令让路、道具判定、词条主条目规则
├── akedata.py       # 取表、翻译、Buff 读取（复用 stages/akedata 的 _load_buffs 与 _blackboard_values）
├── index.py         # 按 revision 建名字索引
├── items.py         # 物品视图
├── props.py         # 道具视图（使用效果 + EquipItemTable 冷却装填）
├── enemies.py       # 敌人模板视图
├── terms.py         # 词条说明 + 样式 + 关联词条 + 来源
├── archives.py      # 单条档案；收集统计仍留在 catalog/views/archives.py
├── service.py       # 候选、选择、目录分页策略；不出网
└── draw.py          # HTML 卡片；截图仍走 rendering/cards.py 的 screenshot_web_element
```

接入点，保持薄：

- `catalog/commands.py`：新别名集合、`ParsedEndfieldCommand.scope` 新值、档案子命令分流、帮助文案、`SCOPE_LABELS` 新条目。
- `handlers.py`：`CONTENT_RESOLVERS` / `CONTENT_RENDERERS` 各加五种 kind 及对应目录 kind；`SOURCE_CANDIDATE_RESOLVERS` 只登记 `akedata`；§3.2 的 `--source` 显式拒绝；§3.1 的物品未命中补查装备 / 武器。
- `providers/registry.py`：`DATA_SOURCES` 的 `akedata` 加五种 kind。
- `catalog/aliases.py`：`SUPPORTED_KINDS` 加 `item`、`prop`、`enemy`、`term`、`archive_entry`；`alias_data.json` 加五个空组，`version` 升到 2；`/ef 别名 添加 <范围> <名> <别名>` 接受 §3 的范围词。

`handlers.py` 里不写字段整理。解析失败、多候选、缺数据的用户文案可以留在 handler，和现在的干员查询一样。

视图是纯数据。绘制函数只读视图，不回表里查漏字段。

---

## 6. 视图

字段按卡片区块定，不照搬表结构。

### 6.1 物品

```text
ItemView
  item_id, name, rarity
  type_id, type_name                 # ItemTypeTable
  showing_type_name                  # 有才填
  description, flavor                # desc / decoDesc
  icon_url
  obtain_ways[]                      # obtainWayIds 翻译后的名字；名字表未找到时为空
  obtain_way_count                   # obtainWayIds 条数
  no_obtain_hint                     # noObtainWayHint 翻译文本，有才填
  source_version, source_name = AkeData
```

目录项只留名字、稀有度、类型、图标。`/ef 物品` 不带名字时出 `ItemCatalogView`：白名单内每个 `type` 一行，写数量。用户点名类型时出该类型的图标页，分页对齐档案预览的 `ARCHIVE_PAGE_BUDGETS` 思路（每页有明确截断，写「已展示 / 总数」）。

获取途径直接读 `ItemTable.obtainWayIds`（1874 条非空，293 个不同 id，最多的是 `item_obtain_explore` 308、`item_obtain_equip` 273、`item_obtain_monster_common` 204），不再扫 rewardId、配方和商店。`noObtainWayHint` 是游戏自己的「暂无获取途径」文案，有就原样显示。两者都没有时不出「获取方式未知」占位。

### 6.2 道具

道具是物品的一种视图，多一块使用效果。

```text
PropView
  item: ItemView
  effect_lines[]     # itemUseDesc 代入 blackboard 后的短句，保留 <@ba.x> 着色
  duration           # UseItemTable.duration，0 不显示
  persistent         # isPersistentBuff
  cooldown, cast_time, charge_count, recover_time   # 只有在 EquipItemTable 的 50 条有
  buff_ids[]         # useActions[].buffBBData.buffId，只用于日志与缓存键
```

`/ef 道具` 的目录按 `type` 分：战术物品、消耗品、探测器、其他。`/ef 物品 <道具名>` 命中道具索引时出道具卡，不退回普通物品卡。

效果句的数字只来自同一条 `useActions[].buffBBData.blackboard`。`{key:0%}` 乘 100 加 `%`，`{key:0}` 取整，与干员技能「倍率」行一致。代入后仍有花括号时这张卡失败，提示「道具效果数值未收录」，不发半张图。

### 6.3 敌人

```text
EnemyView
  template_id, name, nickname, description
  display_type, display_type_name    # 名字待探，暂用「类型 N」
  icon_url
  abilities[]        # EnemyAbilityDescTable：名称可空 + 说明
  resistances[]      # 物理 / 灼热 / 电磁 / 寒冷 / 自然，沿用关卡卡的属性色与 StageEnemyResistance 口径
  resilience         # 有韧性字段才显示
  distributions[]    # distributionIds 翻译后的区域名；名字表未找到时只显示条数
  variants[]         # 同模板实例：enemyId、isDangerous、attrTemplateId
```

抗性来自 `EnemyAttributeTemplateTable`，经**实例**的 `attrTemplateId` 关联，与 `stages/akedata.py:738-760` 一致。同模板下所有实例指向同一个属性模板时在卡上显示一组；不一致时省略抗性区块，并在形态列表里标出「N 种属性模板」。关联不上时名字和能力说明仍可出卡。

不写推荐队伍、弱点攻略、掉落期望。那些不是这几张表里的字段。

### 6.4 词条（v2 重写）

词条卡是**说明卡**，不是数值卡。

```text
TermView
  term_id            # ba.*
  name               # 法术异常 - 燃烧
  family             # richTextId：ba.fire / ba.phy / ba.cryst / ba.natur / ba.pulse / ba.key / ba.poise / 空
  color, icon_url    # RichTextStyleTable 的 preDef 颜色；iconPath → termicon
  summary            # desc 去掉 <@>/<#> 标记后的纯文本
  related[]          # desc 里 <@id>、<#id> 指向的其他 ba.* 条目：id + 名字
  jump_wiki_id       # jumpWikiId，仅展示
  sources[]          # 引用该词的技能：干员或武器名 + 技能名，只列名字
```

**主条目规则。** 64 条无重名，但 `/ef 词条 燃烧` 会子串命中「法术异常 - 燃烧」和「干员受到燃烧」两条。规则：名字含「 - 」的（法术异常 / 物理异常 / 法术附着 / 法术爆发 系列）为主条目；「干员受到X」为从条目，在主条目卡的 `related[]` 里列出，不单独参与无范围的全局候选。用户明确打全名仍能查到从条目。

**分组。** 目录按 `richTextId` 分组，组名用 `RichTextStyleTable` 里能取到的族名（灼热 / 物理 / 寒冷 / 自然 / 电磁 / 关键词 / 失衡），空 `richTextId` 的 5 条进「其他」。不再用 `TERM_SUFFIXES` 猜分组。

**标记。** `<@id>` 与 `<#id>` 都要认；`related[]` 两种都收；着色沿用 `FALLBACK_TERM_STYLES` 与 `RichTextStyleTable`。

**明确不做。** 等级表、公式、源石技艺强度参数、与 `LOADOUT_STATUS_LEVELS` 对账、从 `BuffData` 反推数值。原文里出现的数字（20 条）按原文显示在 `summary` 里，不解析成表。`LOADOUT_STATUS_LEVELS` 的数字不出现在词条卡上。

来源列表从已经缓存的技能描述里扫 `<@ba.x>` / `<#ba.x>`，列出干员或武器名称和技能名。列表过长时截断并写剩余条数。来源是导航。

### 6.5 档案条目

```text
ArchiveEntryView
  item_id, name
  page_name, category_name, group_name
  icon_url
  source_version
```

数据直接用现有档案快照（`ArchiveItemView`，`catalog/models.py:511`），不再抓一套 Prts 表。快照里没有的字段（正文、获取条件）就不画。收集统计、版本新增、个人 `docNum` 进度仍由 `catalog/views/archives.py` 和 `archives/store.py` 负责。

---

## 7. 索引与缓存

索引键是 `AkeSnapshot.revision`（`version|sharedRevision`，`providers/repository.py:43`），不是版本号：同一版本内 `sharedRevision` 会变，成品缓存已经按它失效，索引跟它对齐。revision 变了就丢弃重建。索引条目：

```text
kind, entity_id, display_name, extra_names[], search_text
```

`extra_names` 包括英文名、敌人绰号、词条的从条目名、档案组名。打分复用 `score_candidate` / `aliases_for`，别名文件仍是 `alias_data.json`，kind 扩展见 §5。

建索引时 I18n 只解析名字和短描述。`BuffData` 不在建索引时下载，点开道具详情时按 `buffId` 取，走现有 HTTP 磁盘缓存。

成品图沿用 `_CARD_CACHE`：键仍是 `CardCacheKey` 七元组（渲染版本、kind、来源、实体 key、revision、mode、variant），道具的 `buff_ids` 不进键，它由 revision 决定。缺图标时可以出卡（`rendering/health.py` 记录 missing，走 `_IncompletePages`，不写缓存）；缺数值时不出卡。

物品目录因为条数多，缓存的是「type + 页码」而不是单件。单件详情另缓存。

---

## 8. 卡片

版式跟着现有终末地卡：深色底、左侧图标、右侧标题与稀有度、下面分区块。宽度与干员卡同一套截图参数（`device_scale_factor=2.0`、`CARD_MAX_HEIGHT`、`strict_max_height=True`，超高按区块拆页）。

| 卡 | 区块 |
| --- | --- |
| 物品 | 图标、名字、稀有度、类型、描述、获取途径（有才出） |
| 道具 | 物品头 + 效果句 + 持续时间；战术物品再加冷却 / 装填 / 施放时间 |
| 敌人 | 图标、名字、绰号、类型、能力说明、五系抗性（可省）、出现区域、相关形态 |
| 词条 | 彩色名字与图标、族名、说明、关联词条、来源名字 |
| 档案条目 | 组图标、名字、页签、分类、组名 |
| 目录 | 分组标题 + 数量；类型筛选后才出图标网格，并标注截断 |

每张卡脚注写数据版本（manifest 的游戏版本段，如 `1.5`）和来源 AkeData。

实现时递增 `CARD_RENDER_VERSION`（当前 `endfield-card-v49`）。方案阶段不改这个常量。

---

## 9. 失败时用户看到什么

| 情况 | 回复 |
| --- | --- |
| 范围内无命中 | 「没有找到这个物品 / 道具 / 敌人 / 词条 / 档案」 |
| `/ef 物品` 未命中但装备 / 武器命中 | 直接出原卡，标题写实际种类 |
| `/ef 物品` 命中蚀刻章类 | 「奖章请用 /ef 奖章」 |
| 多个接近 | 编号列表，沿用现在的候选格式 |
| 指定了 FZ 或 Warfarin | 「该类资料只提供 AkeData」（handler 显式判断，见 §3.2） |
| 表在、字段解不出 | 「道具效果数值未收录」，不发图 |
| AkeData 超时或表缺 | 「资料暂时不可用」。日志记来源、表名、异常类型，不记整段 JSON |
| `/ef 档案 刷新` 等旧子命令 | 行为与文案保持现状 |

---

## 10. 分期

每一期都可以单独上线，后一期不推翻前一期的命令。

**第 0 期，探针。** 扩 `scripts/inspect_endfield_akedata.py`，回答 §4.5 的六个问题，把对照表补进本文附录。没有附录之前不写视图。

**第 1 期，物品和道具。** 索引、`classify.py` 白名单与让路、物品目录、物品卡、道具卡、`--source` 显式拒绝、别名系统扩展。全局搜索纳入这两种。

**第 2 期，敌人。** 按 `displayType` 的目录和详情，抗性按实例关联，出现区域。关卡卡不改。

**第 3 期，词条。** 说明、样式、关联词条、主条目规则、来源列表。**不做数值。** 速算保持原表，本期不碰 `LOADOUT_STATUS_LEVELS`。

**第 4 期，档案条目。** 只加带名字的查询，分流覆盖 `ARCHIVE_ALIASES` 全部五个词。统计、刷新、收集的测试要原样通过。

设施、任务、商店不排进这四期。词条数值若将来 `applyTags` 探针有结果，另开方案。

---

## 11. 测试

无网络。夹具优先复用 `tests/fixtures/endfield_akedata_1_5_3.json`（已含 `HyperlinkTextTable` 107 条、`RichTextStyleTable` 96 条、`ItemTable` 33 条、`SkillPatchTable` 66 条、I18n 3011 条子集），探针再补 `ItemTypeTable`、`UseItemTable`、`EquipItemTable` 和敌人四张表的小子集。不在测试里请求 `data.akedata.wiki`。

- 命令：`/ef 物品 源石`、`/ef 道具`、`/ef 食物 炝炒时蔬` 等价于道具范围、`/ef 词条 燃烧`、`/ef 档案 刷新` 仍是刷新、`/ef 档案 某个名字` 与 `/ef 报告 某个名字` 都是条目。
- `--source`：`/ef 道具 xx --source fz` 回复「该类资料只提供 AkeData」，不是「没有找到」；`/ef xx --source fz` 无范围时行为不变。
- 分类：装备、武器、蚀刻章按 `type` 排除；`item_equip_` 前缀但类型不是 6 的条目不被误排；白名单外的类型不进索引也不进目录。
- 道具：在 `UseItemTable` 的 id 只出现在道具索引，不出现在物品索引；`/ef 物品 <道具名>` 出道具卡。
- 候选：道具与干员同名同分时出编号列表；`choose_candidate` 与 `_dedupe_candidates` 不改。
- 道具效果：`{triggerheal2:0%}` 得到百分数字符串，`{triggerheal:0}` 保持整数；缺 blackboard key 的夹具必须失败，输出里不能出现猜出来的数。
- 道具：`EquipItemTable` 里的 50 条有冷却与装填，其余 35 条没有这几个字段也能出卡。
- 词条：`<@ba.x>` 与 `<#ba.x>` 都进 `related[]`；「燃烧」命中主条目「法术异常 - 燃烧」，「干员受到燃烧」出现在关联里；全名查询能打开从条目；卡片上没有任何等级或倍率。
- 词条：`richTextId` 为空的条目进「其他」组。
- 敌人：381 个实例收成 92 个模板上的形态列表；同模板多属性模板时抗性区块省略；`common_ability` 这类无名能力仍能列出说明。
- 档案：条目视图只含快照已有字段。
- 索引：`sharedRevision` 变化而版本号不变时索引重建。
- 渲染：每种卡至少一张夹具图，缺图标时仍能出占位且不写缓存；数值缺失时渲染函数不被调用。

---

## 12. 实现时不要做的事

- 不把 `ItemTable` 全量画成一张图；不把 `showingType = 0` 的 1833 条当成一个组。
- 不把敌人实例逐条做成搜索结果。
- 不给词条卡编任何数值：不抄 `LOADOUT_STATUS_LEVELS`，不从 `BuffData` 反推，不解析说明里的数字。
- 不为了档案正文去抓任务文本、地图文本。现有档案口径已经排除这两类。
- 不在物品卡上写掉落关卡；获取途径只显示 `obtainWayIds` 能翻译出的名字。
- 不加跨种类的候选优先级，不改 `choose_candidate`、`_dedupe_candidates` 和三个分数常量。
- 不新增插件，不改森空岛个人接口。
- 不在本方案落地时顺手重写干员卡或配装卡。

---

## 附录 B：图鉴第 0 期探针结果

命令（零请求，只读已落盘的 JSON）：

```text
.venv/Scripts/python.exe scripts/inspect_endfield_akedata.py --report encyclopedia
```

数据源：AkeData `latest = 1.5.3@10024360-6`，`tableCfgPath = public/1.5.3/10024360-6/TableCfg`。
`--tables` 默认串在 v2 §4.5 的八张表之外再加 `DistributionInfoTable`：报告要回答「`distributionIds` 的表名」，
那张表不在本地就只能留空，白丢一个能翻译区域名的表。报告仍然零请求，缺表直接返回非零。

### B.1 `ItemTypeTable` 全部 id → 中文名

第三列是该 type 在 `ItemTable` 2829 行里的行数。

```text
0	无类型	物品行数=0
1	货币	物品行数=1
2	货币	物品行数=1
3	活动代币	物品行数=3
4	招聘合同	物品行数=32
5	武器	物品行数=79
6	装备	物品行数=273
7	干员培养素材	物品行数=5
8	材料	物品行数=264
9	普通设备	物品行数=50
10	特种设备	物品行数=2
11	物流	物品行数=10
12	STA协议模板	物品行数=95
13	任务物品	物品行数=126
14	珍贵物品	物品行数=5
15	收集品	物品行数=0
19	基质	物品行数=201
20	背包储存格	物品行数=0
21	理智	物品行数=1
22	理智药剂	物品行数=19
25	武器培养素材	物品行数=3
26	武器培养素材	物品行数=2
27	干员培养素材	物品行数=5
28	矿点	物品行数=4
29	珍贵物品	物品行数=1
32	拼块玩法道具	物品行数=10
33	礼物	物品行数=18
34	培养基核	物品行数=18
35		物品行数=0
36		物品行数=0
37	行动资历	物品行数=1
38	每日活跃度	物品行数=1
39	配方解锁	物品行数=135
40	产物升级	物品行数=45
41	液体存放点	物品行数=7
42	干员信物	物品行数=33
43	物资箱	物品行数=47
44	协议交换凭证	物品行数=3
45	晶石	物品行数=1
47	装备制造模板	物品行数=258
48	战术物品	物品行数=50
49	珍贵物品	物品行数=3
50	珍贵物品	物品行数=1
51	据点发展值	物品行数=1
52	消耗品	物品行数=26
53	调度券	物品行数=2
54	功能设备	物品行数=41
55	探测器	物品行数=5
56	弹性需求物资	物品行数=26
57	情报	物品行数=1
58	拍照滤镜	物品行数=14
59	唱片	物品行数=16
60	基质刻写箱	物品行数=12
61	基质培养素材	物品行数=1
62	限时理智药剂	物品行数=6
63	限时理智药剂	物品行数=4
64	系统蓝图	物品行数=53
65	地区建设值	物品行数=2
66	头像	物品行数=50
67	头像框	物品行数=28
68	名片主题	物品行数=26
69	帝江号陈列品	物品行数=9
70	拍照贴纸	物品行数=28
71	沉积结核	物品行数=21
72	沉积具象物	物品行数=60
73	沉积具象物	物品行数=60
74	醚质晶块	物品行数=1
75	研发芯片	物品行数=31
76	干员留影	物品行数=100
77	行动刻度	物品行数=1
78	刻写券	物品行数=2
79	货币	物品行数=1
80	据点协议容量	物品行数=4
81	限时珍贵物品	物品行数=23
82	限时珍贵物品	物品行数=24
83	寻访情报书	物品行数=11
84	通行证经验	物品行数=1
85	通行证申请	物品行数=2
86	通行证申请	物品行数=2
87	武器培养素材	物品行数=1
88	装备精锻助剂	物品行数=1
89	月卡兑换凭证	物品行数=1
90	月卡兑换凭证	物品行数=2
91	地区增益	物品行数=18
92	工业科技点数	物品行数=2
93	限时珍贵物品	物品行数=2
94	限时珍贵物品	物品行数=2
95	珍贵培养素材	物品行数=5
96	珍贵物品	物品行数=1
97	理智道具	物品行数=1
98	珍贵物品	物品行数=1
99	档案库文件	物品行数=13
100	蚀刻章	物品行数=226
101	随身装置	物品行数=15
102	气体矿点	物品行数=3
103	ID信息更新卡	物品行数=1
104	ID信息更新卡	物品行数=1
105	拍照动作	物品行数=9
110	历程积分	物品行数=1
111	装饰物品	物品行数=15
112	装备	物品行数=0
spot-check type 76 -> 干员留影
spot-check type 12 -> STA协议模板
spot-check type 73 -> 沉积具象物
spot-check type 72 -> 沉积具象物
spot-check type 5 -> 武器
spot-check type 6 -> 装备
spot-check type 100 -> 蚀刻章
spot-check type 48 -> 战术物品
spot-check type 52 -> 消耗品
spot-check type 55 -> 探测器
spot-check type 8 -> 材料
spot-check type 47 -> 装备制造模板
spot-check type 19 -> 基质
```

### B.2 `showingType` → `type`

```text
showingType 1 [矿物] rows=70
    type 72=沉积具象物×42
    type 71=沉积结核×21
    type 8=材料×7
showingType 2 [植物] rows=25
    type 8=材料×25
showingType 4 [产物] rows=172
    type 8=材料×172
showingType 15 [采集材料] rows=39
    type 8=材料×39
showingType 16 [培养素材] rows=36
    type 8=材料×18
    type 34=培养基核×18
showingType 17 [生产工具] rows=108
    type 9=普通设备×50
    type 54=功能设备×41
    type 111=装饰物品×15
    type 10=特种设备×2
showingType 18 [随身装置] rows=15
    type 101=随身装置×15
showingType 为空/0 的行数=1833（不按展示类型汇总）
```

### B.3 白名单与排除（`classify.py`）

`ITEM_TYPE_WHITELIST` 逐字执行 v2 §4.1 的两条来源：

| 来源 | type |
| --- | --- |
| v2 直接点名 | 材料 8、战术物品 48、消耗品 52、探测器 55 |
| 矿物 1 | 沉积具象物 72、沉积结核 71、材料 8 |
| 植物 2 | 材料 8 |
| 产物 4 | 材料 8 |
| 采集材料 15 | 材料 8 |
| 培养素材 16 | 培养基核 34、材料 8 |
| 生产工具 17 | 普通设备 9、功能设备 54、装饰物品 111、特种设备 10 |
| 随身装置 18 | 随身装置 101 |

```python
ITEM_TYPE_WHITELIST = {
    8: "材料",
    9: "普通设备",
    10: "特种设备",
    34: "培养基核",
    48: "战术物品",
    52: "消耗品",
    54: "功能设备",
    55: "探测器",
    71: "沉积结核",
    72: "沉积具象物",
    101: "随身装置",
    111: "装饰物品",
}
```

白名单按 `type` 的 1.5.3 行数：材料 264、沉积具象物 72 有 60、普通设备 50、战术物品 50、功能设备 41、
消耗品 26、沉积结核 21、培养基核 18、随身装置 15、装饰物品 15、探测器 5、特种设备 2，合计 567 行。
其中 81 行同时在 `UseItemTable`（战术物品 50 + 消耗品 26 + 探测器 5），只进道具索引；物品索引 486 行。

`EXCLUDED_TYPES` = 武器 5（79 行）、装备 6（273 行）、蚀刻章 100（226 行）。按 `type` 排除，不按 id 前缀。

**两处要写进代码注释的机械结论。** 沉积具象物有两个 type：72（60 行，其中 42 行 `showingType = 1`）与
73（60 行，`showingType` 全为 0）。§4.1 的规则只由展示类型反推，所以 73 不进白名单，尽管它与 72 同名。
装备制造模板 47（258 行）、基质 19（201 行）、配方解锁 39（135 行）、任务物品 13（126 行）、干员留影 76（100 行）、
STA协议模板 12（95 行）、系统蓝图 64（53 行）、头像 66（50 行）等的 `showingType` 为 0 或不在上表，
一律不进索引也不进目录。

### B.4 敌人

```text
EnemyTemplateDisplayInfoTable rows=92
displayType 分布={0: 44, 1: 20, 2: 10, 3: 12, 4: 6}
displayType 枚举表= 未找到 → DISPLAY_TYPE_NAMES 留空，兜底用「类型{n}」
distributionIds 的表名= DistributionInfoTable rows=30
    样例行字段=['areaName', 'distributionId', 'jumpId']
    distribution_dungeon_common → 分布于特定协议空间
    distribution_map01_common → 广泛分布于四号谷地各处
    distribution_map02_lv008 → 北部禁区
    → DISTRIBUTION_TABLE = 'DistributionInfoTable'
EnemyTable rows=381
EnemyTable.attrTemplateId 为空的行数=0
EnemyTable 不同 attrTemplateId 个数=115
EnemyTagTable 形态= EnemyTagTable
```

- `DISPLAY_TYPE_NAMES` 留空：同目录里没有任何表带 `displayType` 字段，枚举名对不上；全文只用「类型{n}」这一种兜底（§2.1、§5）。
- `DISTRIBUTION_TABLE = "DistributionInfoTable"`（30 行，字段 `distributionId` / `areaName` / `jumpId`）。`areaName` 是 i18n 引用，经 `localize` 得到「北部禁区」这类区域名。
- `EnemyTable.attrTemplateId` 为空的行数 = 0，381 个实例指向 115 个不同属性模板。§5 的「`attrTemplateId or template_id`」回退在 1.5.3 上用不到，夹具仍按 §2.2 覆盖一条空值。
- `EnemyTagTable` 在本地（5 行：`tag_boss` / `tag_elite` / `tag_melee` / `tag_normal` / `tag_shooter`），但模板的 `tags` 抽样为空，本期不用。

### B.5 道具效果：逐 action 口径会不会误杀

```text
UseItemTable rows=85，带占位符的条数=73
没有任何一条 action 的 blackboard 能单独填满全部 key 的条数=17
其中「单 action 不行、并集可以」的条数=0
跨 action 同 key 异值的条数=9
多 action 的条数=13
    落空原因 命名空间：9 条
    落空原因 算式：4 条
    落空原因 缺 key：4 条
    样例 [算式] item_agrange_1_lbshamman_bottled_1 keys=['1-value', 'duration']
    样例 [命名空间] item_agrange_1_moss_2_lbmob_1_1 keys=['buff_common_def_buff_potion_1\\duration', 'buff_common_def_buff_potion_1\\value']
    样例 [命名空间] item_hsmob_1_dog_1_1 keys=['buff_common_healrt_buff_potion_2\\value', 'buff_common_usprt_buff_potion_1\\duration', 'buff_common_usprt_buff_potion_1\\value']
    样例 [缺 key] item_jzmonk_1_hsmob_1_1 keys=['-buff_common_def_buff_potion_3\\value', 'buff_common_ctr_buff_potion_1\\duration', 'buff_common_ctr_buff_potion_1\\value']
    样例 [命名空间] item_wgshoal_1_grass_1_grass_2_1 keys=['buff_common_atk_buff_potion_1\\value', 'buff_common_ctr_buff_potion_1\\duration', 'buff_common_ctr_buff_potion_1\\value']
    样例 [算式] item_ethillu_1_wgslime_1_wgthorns_1_1 keys=['1-value', 'duration']
    样例 [命名空间] item_mimicw_1_moss_1_moss_2_1 keys=['buff_common_atk_buff_potion_1\\duration', 'buff_common_atk_buff_potion_1\\value']
    样例 [命名空间] item_corp3_grass1_1 keys=['buff_common_ctr_buff_potion_1\\duration', 'buff_common_ctr_buff_potion_1\\value', 'buff_common_dmg_up_potion_1\\value']
EquipItemTable rows=50（冷却 / 施放 / 装填只在这张表）

# BuffData.applyTags 是否含 ba.* 未在本报告探；只记一行，不进任何一期。
```

读数：

- 没有任何一条 action 的 blackboard 能单独填满全部 key 的条数 = **17 / 73**。
- 其中「单 action 不行、并集可以」的条数 = **0** → §4.3 第 4 条与 §10 那条条件不成立，**不做并集**。
- 跨 action 同 key 异值的条数 = **9**。

17 条的落空原因（`--report` 已把它打进输出，供后人复现）：

| 原因 | 条数 | 说明 |
| --- | --- | --- |
| 命名空间 | 9 | key 写成 `<buffId>\\<key>`，该 buffId 就是本次某条 action 的 `buffId`，数字在同一条 action 里，只是字面 key 对不上 |
| 算式 | 4 | key 是 `1-value`、`-buff_common_def_buff_potion_3\\value` 这类算式 |
| 缺 key | 4 | 整句的 key 分散在多条 action，没有一条能独自填满（正文本身就跨 buff） |

**这是对 §4.3 第 1、2 步的一次口径修订，写在附录里，不在实现里偷偷做。** 依据是 §2.1 要求
「确认 §4.3 逐 action 口径是否会误杀」，实测答案是会：`item_agrange_1_moss_2_lbmob_1_1` 只有一条 action，
blackboard 就是 `value` 与 `duration`，句子写的是 `{buff_common_def_buff_potion_1\\value:0%}`，
字面口径把它判成缺 key。修订内容只有一条：

> key 的字面量先规范化：`<buffId>\\<key>` 且 `buffId` **等于本条 action 自己的 `buffId`** 时，按 `<key>` 查找。
> 仍然只在这一条 action 的 blackboard 里查，仍然不合并 action、不用并集、不跨 action 找 key。
> 规范化后仍填不满 → `PropEffectIncomplete`（算式那 4 条与跨 buff 那 4 条照旧失败，卡上写「道具效果数值未收录」）。

不做的：不把算式代入（`1-value` 这类要算术求值，超出本期口径，与 §10「不把多个 action 的 blackboard 合成一张表」同一类克制），
不下载 `BuffData`。

### B.6 其他

- `BuffData.applyTags` 是否含 `ba.*`：本期未探，只记一行，不进任何一期。
- 夹具仍按 §2.2 补齐；探针结论只进附录，不当作夹具的断言。
