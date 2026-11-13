# 终末地图鉴查询实现计划（修订）

状态：**实现计划，未开工**。设计口径仍以 [终末地图鉴查询方案 v2](endfield_encyclopedia_query_plan.md) 为准。本文替换上一版实现计划。

上一版的骨架保留：五种资料、AkeData 单源、不改三个分数常量、不改 `choose_candidate`。这一版改的是落地时会撞上的接缝，以及几处已经过时的探针。

和 v2 探针清单不一致的地方，以本文为准：

- 获取途径用 `SystemJumpTable`。`catalog/akedata.py` 的装备详情已经用 `obtainWayIds` 取这张表的 `desc`。不再反查站点 JS，也不做「只显示条数」的降级。
- 词条图标用 `static_sprite_url`。`TermIcon/x` 会落到 `sprites/termicon/x.png`。不再为这个路径打三次 GET。
- 词条族名不从 `RichTextStyleTable` 读。那张表只有颜色。族名写在 `classify.py`。

行号不锁。`choose_candidate`、`_collect_candidates`、`_render_candidate` 以函数名为准。

---

## 0. 接缝

1. **档案分流在 `_parse_personal_command`。** `parse_command` 先走它，之后才剥 `--source`。五个 `ARCHIVE_ALIASES` 都在这里返回。无额外词仍是 `archive_view`；第二个词是刷新或收集时保持 `archive_refresh` / `archive_progress`。其余尾巴先对剩余词跑 `_parse_source_option` 和 `_parse_rarity_option`，错误返回 `invalid`，成功后再组成 `query` + `scope=archive_entry` + `source`。这样 `/ef 档案 xx --source fz` 的 `source` 是 `fz`，而不是把选项留在查询词里。
2. **空查询。** 干员、武器、装备、关卡之外，物品、道具、敌人、词条也把空查询改成 `__all__`。`archive_entry` 不加：没名字的档案已经在个人解析里变成统计卡。
3. **别名。** `aliases.py` 保持叶子模块，不 import 图鉴。`add_alias(..., lookup=None)`。`lookup` 是同步的 `Callable[[str], tuple[str, ...]]`，返回精确正式名。`_handle_alias_command` 改为 `async`：五个新 kind 先 `async with query_snapshot()`，`await get_index(snapshot, kind)`，再把 `index.exact_names` 传进去。干员、武器、装备不传 `lookup`，仍只认文件。`tests/test_endfield.py` 里那处源码断言要改成含 `await _handle_alias_command(command)`。`alias_data.json` 的 `version` 升到 2 只是标记，没有任何代码读它，不要用它做迁移分支。
4. **道具数字。** 只读 `UseItemTable` 上的 blackboard，不下载 `BuffData`。缺 key 不得走 `_substitute_fz_placeholders` / `_format_fz_template`（缺值会变成 `--`）。key 已经确定是数字之后，可以调用 `_format_template_value`，不必复制百分号规则。
5. **词条主从在进 `choose_candidate` 之前完成。** 名字以「干员受到」开头的不进候选，除非规范化后等于全名。从条目全名不写入主条目的 `extra_names`。`related` 不能只靠正文标记：夹具里 `ba.burning` 的标记没有 `ba.burningonchar`。用 §6 的 companion 规则把从条目挂到主卡上。
6. **物品补查只在范围是物品、且物品候选为空时做，做在 `choose_candidate` 之前。** 已有模糊的物品候选时不补查，避免装备结果把「三个接近的物品」冲掉。编排在 `handlers._item_scope_fallback`，不在 `encyclopedia/service.py`。service 不 import `handlers`。

`encyclopedia/models.py` 只放 dataclass，满足 `tests/test_architecture.py` 对 `models.py` 的 import 禁令。

---

## 1. 分期

| 期 | 用户能用的 | 完成线 |
| --- | --- | --- |
| 0 | 无 | `--report encyclopedia` 的附录写进 v2 方案；白名单常量有 id 和中文名 |
| 1 | `/ef 物品`、`/ef 道具`、`/ef 食物 …` | 目录、详情、来源拒绝、别名、帮助图、全局搜索含这两种 |
| 2 | `/ef 敌人` | 目录和详情。关卡卡无 diff |
| 3 | `/ef 词条` | 说明卡、companion、来源名字。无数值 |
| 4 | `/ef 档案 <名字>`、`/ef 报告 <名字>` | 单条卡。统计、刷新、收集的现有测试仍过 |

每期合并前跑：

```text
python -m unittest tests.test_endfield_encyclopedia tests.test_endfield tests.test_architecture tests.test_endfield_archive -q
```

测试里禁止打 `data.akedata.wiki`。做法与 `tests/test_endfield_ake_migration.py` 相同：`AkeSnapshot(版本, 表目录, "fixture")`，`_tables` 填夹具，`patch.object(repository, "_get", side_effect=AssertionError)`。

第 1 期卡片落地时把 `CARD_RENDER_VERSION` 从 `endfield-card-v49` 加一。其后只在版式变化时再加。

---

## 2. 第 0 期

`scripts/inspect_endfield_akedata.py` 增加 `--report encyclopedia`。默认下载行为不变。报告模式只读已经落下的 JSON，不发新的表请求，打印：

| 输出 | 填到 |
| --- | --- |
| `ItemTypeTable` 全部 id → 中文名，含 type 76 / 12 / 73 / 72 | `ITEM_TYPE_WHITELIST` 与排除类型 |
| 矿物、植物、产物、采集材料、培养素材、生产工具、随身装置各自落到哪些 `type` | 白名单里由展示类型反推的那部分 |
| `displayType` 0–4 能否在同目录枚举表对上名字 | `DISPLAY_TYPE_NAMES`。对不上就用 `类型{n}`，全文只用这一种兜底 |
| `distributionIds` 的表名 | `DISTRIBUTION_TABLE`。没有就留空字符串 |
| 85 条道具里，没有任何一条 action 的 blackboard 能单独填满 `itemUseDesc` 全部 key 的条数；以及跨 action 同 key 异值的条数 | 确认 §4.3 的逐 action 规则是否会误杀 |
| `EnemyTable.attrTemplateId` 为空的行数 | 确认 §5 的回退会用到多少 |

`--tables` 默认串追加 `ItemTypeTable`、`ItemShowingTypeTable`、`UseItemTable`、`EquipItemTable`、`EnemyTable`、`EnemyTemplateDisplayInfoTable`、`EnemyAttributeTemplateTable`、`EnemyAbilityDescTable`。`SystemJumpTable` 已在默认串里，不必再加。

`BuffData.applyTags` 是否含 `ba.*` 仍只记一行，不进任何一期。

附录未写进 v2 方案之前，不写 `items.py`、`props.py`、`draw.py`。`classify.py` 的白名单在附录之后填，不在第 0 期用猜测 id 先写死。

夹具补进 `tests/fixtures/endfield_akedata_1_5_3.json`，已有的 Hyperlink、RichText、Item、SkillPatch、I18n、`SystemJumpTable` 保留。新增子集至少包括：白名单类型、装备 6、武器 5、蚀刻章 100、一条白名单外类型、一条带百分号和整数占位符的战术物品、一条缺 key 的道具、一条不在 `EquipItemTable` 的消耗品、与战术物品同 id 的装备行、一个敌人模板加两个同属性实例和一个第二属性模板、一条无名能力。`SystemJumpTable` 里要有 `item_obtain_equip`，供翻译测试。

---

## 3. 模块

```text
plugins/endfield/encyclopedia/
├── __init__.py       # 包说明。不是插件入口
├── models.py         # 全部 dataclass，含 IndexEntry / EncyclopediaIndex
├── classify.py       # 白名单、道具分桶、词条是否进候选、companion、族名
├── akedata.py        # 在已打开的 snapshot 上取表、翻译。可选表 404 负缓存
├── index.py          # 按 (revision, kind) 分片建索引。无锁以外的业务判断
├── items.py
├── props.py
├── enemies.py        # 第 2 期
├── terms.py          # 第 3 期
├── archives.py       # 第 4 期。函数接收 ArchiveSnapshotView，不构造 Store
├── service.py        # 候选与视图。不 import handlers，不打开 snapshot
└── draw.py
```

依赖：

- `draw` → `models`，以及 `rendering.cards` 的 `_prepare_assets`、`_write_temp_html`、`is_height_limit_error`、`optimize_png_container`（与 `stages/draw.py` 相同），外加 `rendering.browser.screenshot_web_element`。
- `index` → `akedata`、`classify`、`aliases.normalize_alias_text`。
- `service` → `index`、各视图、`commands.score_entity_candidate` 与 `CANDIDATE_SCORE_THRESHOLD`。
- `handlers` → `service`、`draw`。图鉴包不 import `handlers`，不 import `catalog.service`。

`archives.py` 不 `ArchiveSnapshotStore()`。`JsonStore` 只在构造时读一次文件，第二个实例看不到 `handlers.archive_store` 刷新后的内存。公开函数是 `build_entries(view)` 和 `entry_view(view, item_id)`。handler 传入 `archive_store.load_current_view()`。

索引 dataclass 放在 `models.py`，避免 `draw` / `service` 为了类型去 import 带锁的 `index.py`。

---

## 4. 第 1 期

### 4.1 分类

```python
EXCLUDED_TYPES = {5: "weapon", 6: "equipment", 100: "medal"}

ITEM_TYPE_WHITELIST: dict[int, str] = {}  # 附录之后填写

PROP_BUCKETS = {48: "战术物品", 52: "消耗品", 55: "探测器"}
```

`kind_for_item(type_id, item_id, use_item_ids)`：在 `UseItemTable` 的是 `prop`；排除类型返回 `None`；白名单返回 `item`；其余 `None`。道具优先，战术物品不进物品索引。

`prop_bucket(type_id)`：不在 `PROP_BUCKETS` 的（通行证申请、月卡兑换凭证、ID信息更新卡）归「其他」。

`redirect` 只存蚀刻章的规范化名字。武器和装备不进这张表，补查直接跑现有 resolver。

### 4.2 取表

`load_item_rows(snapshot)`、`load_prop_extras(snapshot)` 都接收已经打开的 `AkeSnapshot`。名字用 `repository.localize`，只 localize 选中行。

图标：`item_icon_urls(item_id, sprite_png("itemiconbig", icon_id))`。`primary` 是 URL。裸 `iconId` 传进去不会被当成 AkeData 图。

获取途径：`OBTAIN_WAY_TABLE = "SystemJumpTable"`，对每个 `obtainWayIds` 取 `desc` 再 `localize`，与装备详情同一条路径。缺 id 的跳过，不写「未知途径」。`noObtainWayHint` 有译文才放上。

`distributionIds` 的表名在附录确认前，`translate_ids` 对空表名直接返回 `[]`。可选表请求失败时，按 `(snapshot.revision, 表名)` 记入图鉴模块内的负缓存。不改 `AkeSnapshot.table`：那个方法不记失败，但装备等现有调用依赖它每次真的去取。

主表（Item、UseItem、I18n）缺失抛 `AkeDataIncomplete`。

### 4.3 道具效果

`PropEffectIncomplete(ValueError)`。

`render_effect_lines(desc, actions)`：

1. 从 `itemUseDesc` 抽出 `{key:fmt}`。
2. 逐个 `useActions[]` 看它自己的 blackboard 能否填满全部 key。数值用 `value`。同一 action 内同一 key 两个不同 `value` 视为这个 action 失败。
3. 第一个能填满的 action 用来格式化。`fmt` 含 `%` 时把数字交给 `_format_template_value`。`valueStr` 不参与百分号。
4. 没有任何 action 能填满 → `PropEffectIncomplete`。不把多个 action 合成一张表。
5. 成功的句子保留 `<@…>` 与 `<#…>`。

第 0 期若打出「没有任何单个 action 能填满、但并集可以」的条数大于 0，先改附录再改这个函数，不要在实现时悄悄改成并集。

`duration == 0` 存 `None`。没有 `EquipItemTable` 行时，冷却、施放、装填、回复都是 `None`，卡仍出。

### 4.4 索引

`get_index(snapshot, kind)` 只建被请求的那一种，缓存键是 `(snapshot.revision, kind)`。超过两个 revision 时丢掉更早的。`/ef 物品` 不读 `SkillPatchTable`。

`EncyclopediaIndex.exact_names(kind, query)` 用 `normalize_alias_text` 做全等，返回正式名元组。多个不同 id 命中则元组长度大于 1，别名添加据此拒绝。

`listed` 默认真。第 3 期词条从条目为假。

建索引只用名字和短描述。词条来源不在这里算。

### 4.5 service

`candidates(index, kind, query) -> list[EndfieldCandidate]`

- `query == "__all__"`：一条 `item_catalog` 或 `prop_catalog`，`score=100`，`key=""`，`revision` 为 snapshot revision，`source="akedata"`。
- 否则只给 `listed` 条目打分，低于 `CANDIDATE_SCORE_THRESHOLD` 的丢掉。
- 查询规范化后等于某个类型名：再加一条目录候选，`key` 为类型 id，`score=100`，`reason="type"`。

`prop_candidates(index, query)` 是 `candidates(..., "prop", query)` 的薄包装，给 handler 补查用。

`medal_redirect(index, query) -> bool`：规范化名字在蚀刻章旁路集合里。

`build_view(snapshot, candidate)` 成功才返回视图。道具效果失败抛 `PropEffectIncomplete`。主表缺失抛 `AkeDataIncomplete`。service 不捕这些异常。

### 4.6 命令与帮助

`ITEM_ALIASES = {"物品", "材料", "item", "items"}`  
`PROP_ALIASES = {"道具", "食物", "料理", "药剂", "prop", "props", "food"}`

`parse_command` 与 `_parse_optional_scope` 都认识它们。`SCOPE_LABELS` 增加 `item`、`item_catalog`、`prop`、`prop_catalog`。`format_not_found` 对这四个 scope 写出「物品」或「道具」。

`format_candidates` 的非交互尾句除干员、武器、装备外，加上已经上线的图鉴范围词。不要在第 1 期写上还没做的敌人、词条。

`format_help()` 同步加用法。`_finish_endfield_help` 在 `assets/image/help/endfield.png` 存在时只发图。第 1 期以及其后每一期，改 `scripts/help_pages.json` 并重出这张图，和改 `format_help()` 是同一项交付。

`format_source` 加一行：已上线的图鉴种类只使用 AkeData。

`normalize_alias_kind` 认识 v2 的范围词。种类中文用 `SCOPE_LABELS`，`_handle_alias_command` 不再对 kind 做会 `KeyError` 的直接下标。kind 还没有索引构建器时，`ValueError("该资料尚未开放")`，不写文件。

### 4.7 别名写入

```python
def add_alias(kind, canonical_name, alias, *, lookup=None) -> tuple[str, bool]:
    ...
```

新五种：文件里已有正式名则照旧追加。没有时调用 `lookup`。返回一条正式名就创建列表。返回多条就 `ValueError`，提示用全名。`lookup` 为 `None` 或返回空，文案仍是「别名库中不存在正式名称：…」。

`alias_data.json` 增加五个空对象。`_alias_data` 对缺键当空字典。

### 4.8 handler

`CONTENT_RESOLVERS` 增加 `item`、`prop`。不要放 `item_catalog`：`scope=all` 会遍历这个字典的键。

`SOURCE_CANDIDATE_RESOLVERS` 只登记 `akedata`。lambda 是单参 `lambda query:`。`handlers.py` 里只有 `equipment` 会把 `rarity` 传进 resolver，照抄那一行会在调用时多一个参数。

resolver 自己 `async with query_snapshot()`，再 `get_index` + `candidates`。

空查询集合加上 `item`、`prop`。

来源拒绝，在 `_collect_candidates` 之前：

```python
if command.scope in ENCYCLOPEDIA_SCOPES and command.source and command.source not in source_order(command.scope):
    return await matcher.finish("该类资料只提供 AkeData")
```

`scope=all` 不进这句。

**补查** `_item_scope_fallback(query, source)`，仅当 `scope=="item"`、`query!="__all__"`、物品候选列表为空：

1. `prop_candidates` 非空则用这份列表。
2. 否则 `_resolve_candidates_from_sources("equipment", query, source)`。
3. 否则同样调用武器。
4. 否则 `medal_redirect` 为真时返回 `"medal"`。
5. 否则空列表。

返回列表就交给原来的 `choose_candidate`。返回 `"medal"` 则「奖章请用 /ef 奖章」。

**异常怎么到用户：**

`_resolve_candidates_from_sources` 把 `AkeDataIncomplete` 和 `WarfarinAPIError` 放进同一个错误列表，循环继续，后面的源仍能兜底。某个 resolver **返回了列表**（包括空列表）算成功。全部尝试都抛了、没有任何一次成功返回，才把最后一个错误抛出去。这样干员在 AkeData 失败、FZ 正常返回空列表时，仍然是「未找到」，不会变成「资料暂时不可用」。图鉴只有 akedata 一个 resolver，它抛 `AkeDataIncomplete` 时没有下一次成功返回，错误会抛出。

`_collect_candidates` 对 `AkeDataIncomplete` 与 `WarfarinAPIError` 同样收集，没有任何候选时把该错误抛出。不要让它掉进现有的 `logger.warning` 后当成空列表。

`_handle_command` 在关卡那对 `except` 旁边增加：

- `PropEffectIncomplete` → 「道具效果数值未收录」
- `AkeDataIncomplete` → 「资料暂时不可用」

日志只记 kind、表名、异常类型。

**渲染：** 抽出两个集合。

- `_AKE_SNAPSHOT_KINDS`：今天包在 `query_snapshot(candidate.revision)` 里的干员、武器、装备及其目录，加上新的 `item`、`prop`、`item_catalog`、`prop_catalog`。
- `_FZ_WHOLE_VIEW_FALLBACK_KINDS`：只保留现在会整卡回退 FZ 的那些。新 kind 不进去。

`PropEffectIncomplete` 是 `ValueError`。若新 kind 留在回退集合里，`_render_candidate` 会把它当成 AkeData 不完整并再去调 FZ。新 kind 只进第一个集合，这个 `ValueError` 原样抛到上面的 `except`。

缓存键仍是现有七元组。`buff_ids` 不进键。

### 4.9 绘制

物品卡：图标、名字、稀有度、类型、描述、获取途径（有译文才画）、`noObtainWayHint`（有才画）。道具卡加上效果句、持续时间、持久标记，以及非 `None` 的冷却 / 施放 / 装填 / 回复。

目录：空查询只出分组计数。点了类型名才出图标网格。分页预算 `(24, 18, 12, 6)` 写在 `draw.py`。返回 `tuple[bytes, ...]`。

截图参数与干员卡相同。缺图走 `_prepare_assets` 已有的 missing 记录，从而进 `_IncompletePages`，不写成品缓存。

效果句里的 `<@>`、`<#>` 用与 `render_weapon_rich_text` 相同的两种标记着色。不要直接调那个函数：它要求 `WeaponView`，还会把没替换掉的 `{key}` 画成强调文本。颜色来自本卡视图上的 term style，不读武器视图。

### 4.10 第 1 期测试

`tests/test_endfield_encyclopedia.py`。夹具按 §1 的 snapshot 方式注入。

- `食物 炝炒时蔬` 的 scope 是 `prop`
- `道具 xx --source fz` 得到「该类资料只提供 AkeData」；无范围的 `--source fz` 不进这句
- type 6、5、100 不在物品或道具条目里
- `item_equip_` 前缀但 type 在白名单内的行仍在物品索引
- `UseItemTable` 的 id 只在道具索引；`/ef 物品` 的补查在物品候选为空时能选出它
- 物品候选已有两条接近结果时，补查函数不被调用
- 白名单外的 type 不进索引
- `{a:0%}` 得到百分数字符串，`{b:0}` 得到整数
- 缺 key 抛 `PropEffectIncomplete`，draw 不被调用
- 有装备行的道具冷却非空；没有的为 `None` 且视图能建成
- 两个 action 各有一部分 key、没有一个能填满时，按第 0 期条数：条数为 0 的夹具不覆盖这条；条数大于 0 时这条必须失败而不是并集填数
- `item_obtain_equip` 译成中文途径名
- 同名同分、分差小于 8 时 `choose_candidate` 给出编号列表。用构造好的 `EndfieldCandidate`，不改函数
- 同一 version、不同 `shared_revision` 时 `get_index` 各建一次；只请求 `item` 时不读 `SkillPatchTable`
- `add_alias("prop", 正式名, "炒菜", lookup=...)` 写入临时文件；lookup 空则失败；lookup 返回两个名字则失败
- 渲染四种卡。缺图标不写缓存
- `format_help()` 含新用法；`format_candidates` 尾句含「物品」或「道具」

---

## 5. 第 2 期：敌人

`load_enemy_templates(snapshot)` 读四张表。搜索单位是展示模板。`EnemyTable` 里相同 `templateId` 的行收成 `variants`。

抗性 id：`attrTemplateId or template_id`，与 `stages/akedata.py` 里组关卡敌人的那一行相同。空的 `attrTemplateId` 不算第二种模板。去重后恰好一个 id，且属性表有这一行，才填五系。零个或多个时 `resistances` 为空，卡上写「N 种属性模板」。

颜色与 `_enemy_resistances` 一致，视图里存不带 `#` 的 `888888`、`FF623D`、`FFC000`、`21C6D0`、`9EDC23`。`draw.py` 输出时再加 `#`。常量写在 `enemies.py`，不 import 关卡模块的私有函数。

能力按 `abilityDescIds` 顺序取。`name` 为空也保留说明。

`display_type_name` 用附录里的 `DISPLAY_TYPE_NAMES`，没有则 `类型{n}`。

出现区域：`DISTRIBUTION_TABLE` 非空才翻译，否则只保留条数。

目录按 `displayType` 分组，只写数量。类型名查询出该组图标页。

接入：`ENEMY_ALIASES`、resolver、空查询、来源拒绝、帮助图。`stages/` 无 diff。

测试用小夹具：两实例同一属性模板出一组抗性；其中一个 `attrTemplateId` 为空时仍是一组（回退到模板 id）；再加一个不同模板则抗性为空且计数为 2；无名能力仍在列表里。不把 381 个实例灌进夹具。

---

## 6. 第 3 期：词条

`term_listed(name)`：以「干员受到」开头则为假。

`TERM_FAMILY_NAMES`：

```python
{
    "ba.fire": "灼热",
    "ba.phy": "物理",
    "ba.cryst": "寒冷",
    "ba.natur": "自然",
    "ba.pulse": "电磁",
    "ba.key": "关键词",
    "ba.poise": "失衡",
    "": "其他",
}
```

未知的 `richTextId` 用 id 本身当组名。颜色只从 `RichTextStyleTable` 的 `preDef` 取，沿用 `_build_fz_term_styles` 已有的读法，不在这张表里找中文族名。

`term_companion(term_id)`：

- id 去掉后缀 `onchar` 之后仍是一条 `ba.*`（burning、corrupt、conduct、frozen、spellinflict）→ 主条目是去掉后缀的那条。
- 否则若存在 `<词干>inflict`（cryst、pulse、fire、natural）→ 主条目是那条。

主条目的 `related` = 正文里 `<@id>`、`<#id>` 指向的其他 `ba.*`，再追加 companion。companion 不在正文里也要出现。测试断言 `ba.burning` 的 `related` 含 `ba.burningonchar`。

`summary` 用 `_clean_fz_rich_text` 的顺序：先保住 `<@>`、`<#>`、`</>`，再删掉其余 `<…>`，最后去掉这三种标记得到纯文本。`<image="BuffIcon/…" …>` 不进入卡片。本期不探 buff 图标。

图标：`static_sprite_url(iconPath)`。

来源在 `build_view` 里算，不在 `get_index` 里算。扫描对象是 localize 之后的干员技能描述和武器技能描述。原始 `SkillPatchTable` 里没有 `ba.` 只说明标签在文本 id 后面。每条最多 12 个「名字 + 技能名」，其余写剩余条数。扫完为空则不画来源区块，卡仍然成功。成品图缓存按 revision 失效，来源不另做一份索引缓存。

候选：`listed=False` 除非 `normalize_alias_text(query)` 等于全名。精确命中从条目时只返回那一条，分数 100。

视图没有等级或倍率字段。渲染 HTML 不含 `Lv1`、`倍率`、`源石技艺`、`<image=`。`LOADOUT_STATUS_LEVELS` 与速算无 diff。

---

## 7. 第 4 期：档案条目

`_parse_personal_command` 的档案分支：

| 条件 | 返回 |
| --- | --- |
| 只有范围词 | `archive_view` |
| 第二个词是刷新 | `archive_refresh`，后面的词忽略 |
| 第二个词是收集或进度 | `archive_progress`，剩余词是账号 |
| 其他 | 先剥 `--source` / 稀有度；再 `query` + `scope=archive_entry` + 解析出的 `source` |

五个别名同一支。`/ef 搜索 档案 xx` 靠 `_parse_optional_scope` 得到 `archive_entry`。

`archives.entry_view(snapshot_view, item_id)` 只拷贝快照已有字段：id、名字、页签、分类、组名、图标、版本。没有描述，没有获取条件。

handler：`load_current_view()` 为 `None` 时，**显式范围**回复「档案资料尚未就绪，先发送 /ef 档案 刷新」。`scope=all` 时这个 resolver 返回 `[]`，不抛、不打 warning。不使用 `DataUnavailable` 表示缺快照。

搜索字段是条目名和组名。组名放在 `extra_names`。

测试：

- `档案`、`档案 刷新`、`档案 收集 2` 的 action 与现在相同
- `档案 终末地` 与 `报告 终末地` 的 scope 是 `archive_entry`
- `parse_command("档案 xx --source fz").source == "fz"`，query 里没有 `--source`
- `parse_command("搜索 档案 xx").scope == "archive_entry"`
- 快照为 `None`、范围是 `all` 时候选为空且无 warning；范围是 `archive_entry` 时是刷新提示
- `tests/test_endfield_archive.py` 原样通过

---

## 8. 注册清单

| 位置 | 第 1 期 | 第 2 期 | 第 3 期 | 第 4 期 |
| --- | --- | --- | --- | --- |
| 别名、`parse_command`、`_parse_optional_scope` | 物品、道具 | 敌人 | 词条 | 档案分流与搜索范围 |
| `SCOPE_LABELS`、`format_help`、`format_source`、`format_candidates` 尾句、帮助图 | 是 | 是 | 是 | 是 |
| `DATA_SOURCES` 里 akedata 的 kinds | 两种 | +enemy | +term | +archive_entry |
| `CONTENT_RESOLVERS` 与单参 akedata resolver | 两种 | +enemy | +term | +archive_entry |
| `CONTENT_RENDERERS` | 含两种目录 | 含敌人目录 | 含词条目 | 只有条目 |
| `_AKE_SNAPSHOT_KINDS` | 加入 | 加入 | 加入 | 加入 |
| `_FZ_WHOLE_VIEW_FALLBACK_KINDS` | 不加入 | 不加入 | 不加入 | 不加入 |
| 空查询 → `__all__` | 两种 | +enemy | +term | 不加 |
| `--source` 拒绝 | 两种 | +enemy | +term | +archive_entry，且个人解析里先剥选项 |
| `SUPPORTED_KINDS` 与 `alias_data.json` 五个空组 | 第 1 期一次加全 | — | — | — |

只改 akedata 那一个 `kinds` 集合。FZ 与 Warfarin 保持原样。

---

## 9. 失败

| 情况 | 用户看到 |
| --- | --- |
| 候选为空，物品补查也空 | `format_not_found` |
| 补查为 medal | 奖章请用 /ef 奖章 |
| 显式范围且 `source` 不在 `source_order` | 该类资料只提供 AkeData |
| `PropEffectIncomplete` | 道具效果数值未收录 |
| 显式档案范围且快照为 `None` | 先发送 /ef 档案 刷新 |
| `scope=all` 且档案快照为 `None` | 该 kind 不参与，无提示 |
| 该 kind 的源全部失败，抛出 `AkeDataIncomplete` | 资料暂时不可用 |
| 前一个源失败、后一个源返回了空列表 | 未找到。这是干员 FZ 兜底仍要保持的行为 |

渲染只在 `build_view` 成功之后调用。

---

## 10. 不做

- 不改 `choose_candidate`、`_dedupe_candidates`、三个分数字常量
- 不改 `LOADOUT_STATUS_LEVELS`、速算、配装卡、干员卡、关卡卡、档案统计卡
- 不让 `encyclopedia` import `handlers`
- 不让 `aliases.py` import 图鉴
- 不在 `service.py` 里调用装备或武器 resolver
- 不把新 kind 放进 FZ 整卡回退集合
- 不下载 `BuffData` 来填道具或词条
- 不把多个 action 的 blackboard 合成一张表，除非第 0 期附录改了这条
- 不把 `<image=` 画到词条卡上
- 不为档案正文去抓任务文本或地图文本
- 不新开 `ArchiveSnapshotStore`
- 不把 `showingType = 0` 或整张 `ItemTable` 画成一张图
