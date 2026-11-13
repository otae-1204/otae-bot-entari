# 终末地图鉴实现设计 v3

状态：**设计，未实现**。参照三份材料：`docs/endfield_encyclopedia_query_plan.md`（v2 方案，定口径）、`docs/endfield_encyclopedia_implementation.md`（Grok 实现计划，定接缝）、Fable 审查（M1–M10 / S1–S11 / N1–N12），以及本仓库当前代码。v2 与实现计划里已经写对的部分本文不重述，只写我这一版怎么做、以及材料里说错的地方。

---

## 1. 已核实事实基线

以下每一条都在本仓库里核过（夹具为 `tests/fixtures/endfield_akedata_1_5_3.json`）。第 2 节起的所有设计都以这张表为地基。

| # | 事实 | 证据 |
| --- | --- | --- |
| 1 | `HyperlinkTextTable` 107 行，`ba.*` 64 条，字段恰好 `id/name/desc/iconPath/richTextId/jumpWikiId` 六个 | 夹具 |
| 2 | `richTextId` 分布：空 5、`ba.key` 21、`ba.phy` 8、`ba.cryst` 8、`ba.fire` 7、`ba.natur` 7、`ba.pulse` 7、`ba.poise` 1 | 夹具（与 v2 §4.3 一致） |
| 3 | **8 条** `ba.*` 的 desc 含 `<image=…>`：`ba.burning`、`ba.conduct`、`ba.corrupt`、`ba.crystinflict`、`ba.fireinflict`、`ba.frozen`、`ba.naturalinflict`、`ba.pulseinflict` | 夹具 → Fable M10 成立 |
| 4 | desc 里 `<@…>` 91 处、`<#…>` 102 处 | 夹具 |
| 5 | `ba.burning` 的 desc 只引用 `ba.burning`、`ba.fire`、`ba.spellinflict`、`ba.fireinflict`，**不引用** `ba.burningonchar` | 夹具 → Fable M7 成立 |
| 6 | `onchar` 条目共 9 条；其中只有 4 条被别的条目引用，引用者全是 `ba.spellinflictonchar` | 夹具 → Fable M7 成立 |
| 7 | companion 两段规则在夹具上 **9/9 命中**：先去 `onchar` 得主条目（burning / corrupt / conduct / frozen / spellinflict 五条），否则试 `<词干>inflict`（cryst / pulse / fire / natural 四条）。注意 `ba.crystonchar` 去后缀得 `ba.cryst` **不存在**，必须落到第二段规则 | 夹具 → 规则固化 |
| 8 | `RichTextStyleTable` 每行只有 `id/preDef/postDef`，`ba.fire` 首个 preDef 是 `<color=#ff8e59>`：**只有颜色，没有族名** | 夹具 → Fable M6 成立 |
| 9 | `SystemJumpTable` 775 行，415 条 `item_obtain_*`；`item_obtain_equip` 经 `localize` 得「装备制造」 | 夹具 + `catalog/akedata.py:734` → Fable M5 成立 |
| 10 | `repository.localize` 是**递归**的：对整张 `SkillPatchTable` localize 后出现 **181 处 `ba.` 引用**（夹具 66 行）。原始表里 0 处 | 夹具 + `providers/repository.py:24` → 词条 `sources[]` 可行，且扫描对象是 localize 后的技能描述 |
| 11 | 夹具 `ItemTable` 33 条，`type` 分布 `{6:26, 4:4, 5:3}`，字段含 `obtainWayIds/noObtainWayHint/showingType/type/rarity/iconId/desc/decoDesc` | 夹具 → 白名单测试必须新增夹具行（现有夹具没有 8 / 48 / 52 / 55） |
| 12 | 夹具缺 `UseItemTable`、`EquipItemTable`、`EnemyTable`、`ItemTypeTable` | 夹具 → 第 0 期必须补 |
| 13 | 实测行号：`choose_candidate` `catalog/commands.py:773`、`_parse_full_option` `:748`、`_collect_candidates` `handlers.py:2136`、`_resolve_candidates_from_sources` `:2162`、`_render_candidate` `:2767`、`_handle_alias_command` `:3099`（调用点 `:383`）、`CARD_RENDER_VERSION` `:218`、`_dedupe_candidates` `:3218` | 代码 → **Fable N1 给的 753 / 728 / 2125 三处都与当前代码不符**；实现计划 §0 的 2136 是对的 |
| 14 | `_render_candidate` 的 except 元组含 `ValueError`（`handlers.py:2802`），两个字面量集合分别在 `:2797` 与 `:2803` | 代码 → Fable M4 成立，且机制与它描述的一致 |
| 15 | `_handle_command` 现有 except：`WarfarinAPIError`→「数据源暂时不可用」（`:473-477`）、`(StageVariantNotFound, StageDataIncomplete)`→`str(exc)`（`:478-479`）、`Exception`→「图片生成失败」（`:480-482`） | 代码 |
| 16 | `_resolve_candidates_from_sources` 只把 `WarfarinAPIError` 收进 `errors`（`:2179-2182`），其余异常仅 warning+continue（`:2183-2185`），末尾 `if errors: raise errors[-1]` 否则 `return []`；`_collect_candidates` 同样只收 `WarfarinAPIError`（`:2150-2155`） | 代码 → `AkeDataIncomplete`（`ValueError`）被吞，Fable M3 成立 |
| 17 | 空查询字面量集合：`handlers.py:403`（operator / weapon / equipment）、`:411`（stage） | 代码 |
| 18 | `add_alias` `catalog/aliases.py:29`、`SUPPORTED_KINDS` `:9`、`normalize_alias_kind` `catalog/commands.py:829` | 代码 |
| 19 | `JsonStore` 只在构造时读一次（`otae_bot/infrastructure/storage/json_store.py:19-24`）；`handlers.py:213` 持有唯一 `ArchiveSnapshotStore`，`load_current_view` 在 `archives/store.py:75` | 代码 → Fable M9 成立 |
| 20 | `DATA_SOURCES` 里 akedata 的 kinds = `{operator, weapon, equipment, medal, stage}` | `providers/registry.py:16` |
| 21 | 帮助图链路：`paths.py:10` → `assets/image/help/endfield.png`；`_finish_endfield_help`（`handlers.py:3023`）有图就只发图；`tests/test_endfield.py:838-855` 同时断言 `scripts/help_pages.json` 的文案**和图片尺寸 `(1075, 761)`**。**仓库里没有生成这张 PNG 的脚本**（`scripts/` 下只有 `help_pages.json`；渲染脚本只存在于 `.pr_audit/wt7` 工作树） | 代码 → 见 §7 |
| 22 | `tests/test_endfield.py:650` 断言源码含子串 `_handle_alias_command(command)` | 代码 → 改成 `await _handle_alias_command(command)` 不破坏该断言 |
| 23 | 测试运行器是 pytest（`pyproject.toml` 的 `[tool.pytest.ini_options]`，`testpaths=["tests"]`；README 用 `.venv/bin/python -m pytest`） | 代码 → 门禁命令用 pytest |

---

## 2. 我这一版的四处结构性改动

材料里 M / S / N 共 33 条，绝大多数是点状补丁。其中四条是**根因**，改掉它们可以让一批补丁不再需要：

**D1 — kind 注册表取代散落的字面量。**
`handlers.py` 里现在有三处 kind 字面量集合（`:2797`、`:2803`，以及 `_handle_alias_command` 里的 label 下标字典 `:3113`），加上 `SCOPE_LABELS`、`DATA_SOURCES.kinds`、`CONTENT_RESOLVERS`、`SOURCE_CANDIDATE_RESOLVERS`。新增一个 kind 要同时改六处，漏一处就是 M4（错卡）、S10（无用 resolver）、N9（`KeyError`）。改成一张注册表，其余全部派生。

**D2 — 失败路径：三态结论 + 单一映射表。**
Fable M3 给的修法是"二选一"（要么把 `AkeDataIncomplete` 当 `WarfarinAPIError` 对待，要么改文档）。我取第三条：把"候选阶段的结果"从二值（有 / 无）改成三值（命中 / 空 / 不可用），并在 handler 边界放一张异常→文案映射表。这样干员的 FZ 兜底行为不变，图鉴的 AkeData 故障又能到用户眼前。

**D3 — 选项预解析。**
把 `--source` / `--rarity` / `--full` 的剥离提到 scope 分流**之前**（M8）。档案分支不再需要在分支内部重复调用选项解析器，实现计划 §0 第 1 条与 §7 的做法都可以简化成一处。

**D4 — 行号不作契约，符号名 + 已核实行号作契约。**
Fable 的 N1 本身就是一次行号订正失败（见事实 13）。所以本文所有接缝写"函数名 + 本仓库实测行号"，并加一条 doc-lint 测试校验符号存在。

---

## 3. 架构

### 3.1 模块与依赖方向

```text
plugins/endfield/encyclopedia/
├── __init__.py       # 包说明，不是插件入口
├── models.py         # 全部 dataclass，含 IndexEntry / EncyclopediaIndex
├── registry.py       # ★ D1：kind 注册表（本设计新增的模块）
├── classify.py       # 白名单、道具分桶、词条主从、companion、族名
├── akedata.py        # 在已打开的 snapshot 上取表、翻译、可选表负缓存
├── index.py          # 按 (revision, kind) 分片建索引
├── items.py          # 第 1 期
├── props.py          # 第 1 期
├── enemies.py        # 第 2 期
├── terms.py          # 第 3 期
├── archives.py       # 第 4 期，函数接收 ArchiveSnapshotView
├── service.py        # 候选与视图纯函数；不 import handlers、不开 snapshot
└── draw.py           # HTML 卡片
```

不变量（写成 `tests/test_architecture.py` 的新用例）：

1. `encyclopedia/` 不 import `handlers`，也不 import `catalog.service`；
2. `models.py` 与 `registry.py` 无 I/O、无 `asyncio.Lock`、无 provider import（沿用现有 `models.py` 禁令）；
3. `catalog/aliases.py` 不 import `encyclopedia.*`（打破 M2 的环）；
4. `draw.py` 的依赖线必须含 `rendering.cards` 的 `_prepare_assets` / `_write_temp_html` / `is_height_limit_error` / `optimize_png_container`（照 `stages/draw.py:13-18`）与 `rendering.browser.screenshot_web_element`；
5. 三个分数常量（`CANDIDATE_SCORE_THRESHOLD` 65、`CLEAR_SCORE` 70、`AMBIGUITY_MARGIN` 8，`catalog/commands.py:111-113`）冻结，加快照测试。

### 3.2 kind 注册表（D1）

一行一个 kind，字段：

| 字段 | 用途 | 消费方 |
| --- | --- | --- |
| `kind` | 内部标识 | 全部 |
| `label` | 用户可见中文名 | `SCOPE_LABELS`、`format_not_found`、`format_candidates`、别名命令的 label |
| `aliases` | 范围词集合 | `parse_command`、`_parse_optional_scope`、`normalize_alias_kind` |
| `catalog_kind` | 该 kind 的目录 kind（可为空） | `CONTENT_RENDERERS` |
| `needs_snapshot` | 是否要求 `AkeSnapshot` | 派生 `_AKE_SNAPSHOT_KINDS` |
| `fz_whole_view_fallback` | 是否允许整卡 FZ 回退 | 派生 `_FZ_WHOLE_VIEW_FALLBACK_KINDS`（**显式字段，不由上一项推导**） |
| `empty_query_all` | 空查询是否变 `__all__` | `handlers.py:403` 一带 |
| `index_builder` | 索引分片构造器（可为 `None`，此时别名命令回 `ValueError("该资料尚未开放")`） | `index.get_index` |
| `resolver` / `renderer` | 候选来源 / 渲染入口 | `CONTENT_RESOLVERS`、`CONTENT_RENDERERS` |

按 v2 §2 与实现计划，第 1–4 期落地的是 **物品（item）、道具（prop）、敌人（enemy）、词条（term）、档案条目（archive_entry）** 五个 kind，外加 `redirect` 里的**奖章旁路**（不是 kind，见 §6.3）。若实际是别的五项，改动量是"改五行"。

`registry.py` 只放数据与派生函数，不 import 任何视图模块，因此 `handlers.py` 可以在 import 期安全地由它构造字典。

### 3.3 接缝契约表

| 接缝 | 归属 | 守卫 |
| --- | --- | --- |
| 档案分流在 `_parse_personal_command`（`catalog/commands.py:293`，档案支 `:358-367`） | 解析层 | `parse_command` 用例矩阵 |
| `parse_command` 先走个人解析、后剥选项（`:192-206`） | 解析层 | 由 D3 改为"先剥选项、后分流" |
| 空查询只放行白名单 kind | `handlers.py:402-420` | 空查询矩阵用例 |
| `add_alias` 只认文件里的正式名 | `aliases.py`（叶子） | 别名表口径用例 |
| `_format_fz_template` 缺 key 写 `--`（`catalog/views/common.py:96`） | `views.common` | 道具路径**不走**它，改走 `_format_template_value`（`:481`） |
| `models.py` 的 import 禁令 | 架构 | `tests/test_architecture.py:36-48` |

---

## 4. 数据口径

### 4.1 词条主从与 companion（M7）

两段规则，顺序不可换，两段都被夹具用到：

1. `term_id` 去掉后缀 `onchar` 后仍是一条 `ba.*` → 主条目是去掉后缀那条（burning / corrupt / conduct / frozen / spellinflict 五条）；
2. 否则若存在 `<词干>inflict` → 主条目是那条（cryst / pulse / fire / natural 四条）。

`ba.crystonchar` 去后缀得到 `ba.cryst`，而 `ba.cryst` **不存在**，所以第一段必须"确认存在"才算命中，否则会挂到一个不存在的 id 上。主条目的 `related` = 正文标记指向的其他 `ba.*`，**再追加 companion**。测试断言 `ba.burning.related` 含 `ba.burningonchar`。

从条目（名字以「干员受到」开头）不进全局候选，除非 `normalize_alias_text(query)` 等于全名；从条目全名不写入主条目的 `extra_names`。

### 4.2 族名（M6）

`RichTextStyleTable` 只有颜色，族名写在 `classify.TERM_FAMILY_NAMES`：`ba.fire` 灼热、`ba.phy` 物理、`ba.cryst` 寒冷、`ba.natur` 自然、`ba.pulse` 电磁、`ba.key` 关键词、`ba.poise` 失衡、`""` 其他；未知 `richTextId` 用 id 本身当组名。颜色仍走既有 `_build_fz_term_styles`（`catalog/views/common.py:372`）。

### 4.3 富文本清洗（M10 / N5）

一个 `clean_rich_text`：先保护 `<[@#]id>` 与 `</>`，再 `re.sub(r"<[^>]+>", "", …)`，最后还原——顺序照抄 `_clean_fz_rich_text`（`catalog/views/common.py:154-170`）。`<@…>` 与 `<#…>` 两种标记的着色正则直接复用 `render_weapon_rich_text`（`rendering/cards.py:3521-3530`）里的两个，不写第三套。

夹具里 8 条 `ba.*` 含 `<image=…>`（事实 3），本期**只删标记**；把 `BuffIcon/x` 画成图需要另探 `sprites/bufficon/x.png` 是否存在，不进任何一期。第 3 期测试断言渲染 HTML 不含 `<image=`。

### 4.4 词条 sources[]：单趟反查（S4 的加强版）

事实 10 证明 `SkillPatchTable` 经递归 `localize` 后确实含 `ba.` 引用，所以 `sources[]` 有数据路径。做法不是"每张卡扫一遍 3.5MB"，而是：

- 按 `snapshot.revision` 建一次**反向映射** `term_id → ((干员/武器名, 技能名), …)`，扫描对象是 localize 后的技能描述；
- 每张词条卡渲染时查表，O(1)；
- 映射不放进 `index`（避免 `/ef 物品` 为词条买单），放在 `terms.py` 的模块级 revision 缓存里；
- 每条最多列 12 项，其余写剩余条数；扫完为空则不画来源区块，卡仍成功。

### 4.5 物品与道具

- 索引与目录按 `type` 分组（v2 修订 3），白名单 `ITEM_TYPE_WHITELIST` 在附录出来之后填，不用猜测 id 先写死。
- 排除按 `type`：`EXCLUDED_TYPES = {5: 武器, 6: 装备, 100: 蚀刻章}`，**不按 id 前缀**（`item_equip_` 前缀 281 条 vs 装备类型 273 条）。
- 获取途径：`OBTAIN_WAY_TABLE = "SystemJumpTable"`，取 `.desc` 再 `localize`，与 `catalog/akedata.py:734` 同一条路径（事实 9）；第 0 期**删掉**这一项探针，改为第 1 期测试「`item_obtain_equip` 译成中文途径名」。
- 道具分桶 `PROP_BUCKETS = {48: 战术物品, 52: 消耗品, 55: 探测器}`，不在表内的（通行证申请、月卡兑换凭证、ID信息更新卡）归**「其他」**（N7）。
- 道具效果：**逐 action 代入**，不合并多 action 的 blackboard。抽 `{key:fmt}` → 逐个 `useActions[]` 看它自己的 blackboard 能否填满全部 key（同一 action 内同 key 两个不同 `value` 视为该 action 失败）→ 第一个能填满的用来格式化 → 没有任何一个能填满才抛 `PropEffectIncomplete`。v2 §6.2 的"只取同一条 action"过窄，实现计划的"合并后冲突即失败"过脆，两者都否决。
- 格式化直接 `from ..catalog.views.common import _format_template_value`（S3），不复制百分号规则；`_blackboard_values` 有两份实现（`stages/akedata.py:1075`、`catalog/views/weapons.py:203`），道具用后者（认 `valueStr`）。
- 图标：`item_icon_urls(item_id, sprite_png("itemiconbig", icon_id))`——第二个参数是 **URL**，不是裸 id（N3）。

### 4.6 敌人

- 抗性 id 用 `attrTemplateId or template_id`（照 `stages/akedata.py:749`），空的 `attrTemplateId` 不算第二种模板；去重后恰好一个 id 且属性表有该行才填五系，否则 `resistances` 为空并写「N 种属性模板」（S8）。
- 颜色沿用 `stages/akedata.py:1123-1127` 的 `"888888"/"FF623D"/"FFC000"/"21C6D0"/"9EDC23"`，**视图里不带 `#`**，`draw.py` 输出时再加（S8 的形态统一）。常量写在 `enemies.py`，不 import 关卡模块私有函数。
- `display_type_name` 兜底全文只用一种写法 `类型{n}`（N11），能对上枚举表才用名字。
- 出现区域：`DISTRIBUTION_TABLE` 非空才翻译，否则只留条数。
- 能力按 `abilityDescIds` 顺序取，`name` 为空也保留说明（`common_ability` 这种）。
- 夹具：两实例同一属性模板出一组；其中一条 `attrTemplateId` 为空时仍是一组；再加一个不同模板则抗性为空且计数为 2；无名能力仍在列表里。不把 381 个实例灌进夹具。

### 4.7 夹具缺口

第 0 期必须补进 `tests/fixtures/endfield_akedata_1_5_3.json`（事实 11、12）：`ItemTypeTable`、`UseItemTable`、`EquipItemTable`、`EnemyTable`、`EnemyTemplateDisplayInfoTable`、`EnemyAttributeTemplateTable`、`EnemyAbilityDescTable` 的小子集，且至少含：白名单类型、装备 6、武器 5、蚀刻章 100、一条白名单外类型、一条带百分号与整数占位符的战术物品、一条缺 key 的道具、一条不在 `EquipItemTable` 的消耗品、与战术物品同 id 的装备行、一个敌人模板 + 两个同属性实例 + 一个第二属性模板、一条无名能力。`SystemJumpTable` 里要有 `item_obtain_equip`（已有）。

---

## 5. 控制流与失败路径

### 5.1 解析（D3）

`parse_command`（`catalog/commands.py:192`）改为：`_split` → **先** `_parse_source_option`（`:1012`）与 `_parse_rarity_option`（`:1042`）→ **再** `_parse_personal_command`（`:293`）→ 其余分支。

结果：`parse_command("档案 xx --source fz")` 得到 `scope="archive_entry"`、`query="xx"`、`source="fz"`；档案分支内部不再需要重复解析（实现计划 §0 第 1 条可简化），第 4 期的「`--source` 对档案条目显式拒绝」从死代码变成可达分支。

`parse_shortcut_command`（`:761`）已有同样的顺序，无需改动。

### 5.2 候选阶段：三态（D2）

`_resolve_candidates_from_sources`（`handlers.py:2162`）加一个 `succeeded` 标志：某个 resolver **返回了列表（包括空列表）** 即算成功；只把 `WarfarinAPIError` 与 `AkeDataIncomplete` 收进 `errors`；末尾 `if succeeded: return []`，否则 `if errors: raise errors[-1]`，否则 `return []`。

于是：

| 情形 | 结果 |
| --- | --- |
| 有候选 | 命中 |
| 所有尝试都抛错、无一次成功返回 | 抛 `AkeDataIncomplete` → 「资料暂时不可用」 |
| 干员 AkeData 抛错、FZ 正常返回空列表 | 「未找到」（**干员现有兜底行为不变**） |
| 图鉴只有 akedata 一个 resolver，它抛错 | 没有下一次成功返回 → 抛出 → 「资料暂时不可用」 |

`_collect_candidates`（`:2136`）对 `AkeDataIncomplete` 与 `WarfarinAPIError` 同等收集，没有任何候选时把错误抛出，不再掉进 `:2153-2155` 的 `logger.warning` 后当成空列表。

### 5.3 补查触发点（S1）

仅当 `scope == "item"` 且 `query != "__all__"` 且**物品候选列表为空**时，在进入 `choose_candidate`（`commands.py:773`）之前调 `handlers._item_scope_fallback(query, source)`，顺序：`service.prop_candidates` → `_resolve_candidates_from_sources("equipment", …)` → `("weapon", …)` → `service.redirect_kind` 为真则返回 `"medal"` → 空列表。返回列表交给原 `choose_candidate`；返回 `"medal"` 则回「奖章请用 /ef 奖章」。

**候选有但模糊（`selected is None and ambiguous`）时不补查**，否则「有三个接近的物品」会被装备候选冲掉。

编排必须在 `handlers.py`（M1）：`service.py` 不 import `handlers`，也拿不到 `CONTENT_RESOLVERS` / `SOURCE_CANDIDATE_RESOLVERS`（`handlers.py:260`、`:279`）里的 lambda。`service.py` 只留纯函数 `prop_candidates(snapshot, query)` 与 `redirect_kind(index, query) -> "medal" | ""`。

### 5.4 渲染（M4）

抽出两个常量，由注册表的 `needs_snapshot` 与 `fz_whole_view_fallback` 派生：

- `_AKE_SNAPSHOT_KINDS`：新 kind（含目录 kind）**加入** → 缓存键里的 `candidate.revision` 与实际渲染用的 revision 一致；
- `_FZ_WHOLE_VIEW_FALLBACK_KINDS`：新 kind **不加入**。

不加第二个的理由是具体的：`_render_candidate` 的 except 元组含 `ValueError`（`:2802`），而 `PropEffectIncomplete` 是 `ValueError`；若新 kind 进了 `:2803` 的集合，它会被当成"AkeData 不完整"，走 FZ 整卡回退，`prefix` 取到 `None`，再用 `source="fz"` 调一次道具 renderer——把「数值未收录」变成一张错卡。

### 5.5 失败映射表（M3 的落地）

在 `_handle_command`（`handlers.py:357`）的关卡 except 旁加一个表驱动的出口，而不是零散 except：

```python
_ENCYCLOPEDIA_ERROR_REPLIES: dict[type[BaseException], str] = {
    PropEffectIncomplete: "道具效果数值未收录",
    AkeDataIncomplete: "资料暂时不可用",
}
```

匹配按 MRO 回退，一条测试逐项覆盖。与现有 `:473-482` 的关系：`WarfarinAPIError` 与关卡那两个 except 保持原样，新表只补图鉴新增的两类。

| 情况 | 用户看到 |
| --- | --- |
| 候选为空、补查也空 | `format_not_found` |
| 补查为 medal | 奖章请用 /ef 奖章 |
| 显式范围且 `source` 不在 `source_order(kind)`（`providers/registry.py:37`） | 该类资料只提供 AkeData |
| `PropEffectIncomplete` | 道具效果数值未收录 |
| 显式档案范围且快照为 `None` | 先发送 /ef 档案 刷新 |
| `scope=all` 且档案快照为 `None` | 该 kind 不参与，无提示、不打 warning |
| 该 kind 的源全部失败 | 资料暂时不可用 |
| 前一个源失败、后一个源返回空列表 | 未找到 |

来源拒绝在 `_collect_candidates` **之前**判断，且 `scope=all` 不进这句。

---

## 6. 索引、缓存与别名

### 6.1 索引

- 缓存键 `(snapshot.revision, kind)`，`revision = f"{version}|{shared_revision}"`（`providers/repository.py:43`），超过两个 revision 丢更早的；`/ef 物品` 不读 `SkillPatchTable`（S4）。
- 条目：`kind, entity_id, display_name, extra_names[], search_text`；`extra_names` 含英文名、敌人绰号、档案组名。
- `listed` 默认真；第 3 期词条从条目为假。
- `exact_names(kind, query)` 用 `normalize_alias_text`（`aliases.py:115`）做全等；命中多个不同 id 时返回长度 > 1 的元组，别名添加据此拒绝。

### 6.2 缓存与负缓存

- 成品图沿用 `_CARD_CACHE` 的七元组键；`buff_ids` 不进键（由 revision 决定）；`CARD_RENDER_VERSION` 从 `endfield-card-v49`（`handlers.py:218`）在第 1 期加一，其后只在版式变化时加。
- 缺图标可出卡（走 `_prepare_assets` 的 missing 记录 → `_IncompletePages`，不写缓存）；缺数值不出卡。
- `AkeSnapshot.table`（`repository.py:46-60`）只缓存成功、失败每次都重发，所以可选表（`distributionIds` 等）在 `encyclopedia/akedata.py` 里按 `(revision, 表名)` 记**负缓存**，不动 `AkeSnapshot.table`（N6）。
- 物品目录缓存的是「type + 页码」，单件详情另缓存。

### 6.3 别名（M2）

```python
def add_alias(kind, canonical_name, alias, *, lookup: Callable[[str], tuple[str, ...]] | None = None) -> tuple[str, bool]:
```

`aliases.py` 保持叶子（`SUPPORTED_KINDS` 加五个 kind，`alias_data.json` 加五个空对象，`version` 升 2 只作标记，**任何代码都不读它**，不得用它做迁移分支）。`_handle_alias_command`（`handlers.py:3099`）改 `async`：五个新 kind 先 `async with query_snapshot()` → `await get_index(snapshot, kind)` → 把 `index.exact_names` 传进去；干员/武器/装备不传 `lookup`，仍只认文件。kind 没有索引构建器时 `ValueError("该资料尚未开放")`，不写文件。

label 不再用 `handlers.py:3113` 的直接下标，改用注册表的 `label`（N9）。`redirect` 只存蚀刻章的规范化名字（S10）；武器与装备不进这张表，补查直接跑现有 resolver。

---

## 7. 分期与门禁

每期门禁（事实 23：用 pytest）：

```text
.venv/Scripts/python.exe -m pytest tests/test_endfield_encyclopedia.py tests/test_endfield.py \
    tests/test_architecture.py tests/test_endfield_archive.py -q
```

后两个模块是补上的（S6）：`test_architecture.py` 守 `models.py` 的 import 禁令，`test_endfield_archive.py` 守第 4 期改到的解析。禁网落到代码：照 `tests/test_endfield_ake_migration.py:31-38`，构造 `AkeSnapshot(版本, 表目录, "fixture")` 后直接 `_tables = 夹具`，再 `patch.object(repository, "_get", side_effect=AssertionError)`。

| 期 | 用户能用 | 完成线 |
| --- | --- | --- |
| 0 | 无 | `inspect_endfield_akedata.py --report encyclopedia` 的附录写回 v2 方案；白名单常量有 id 与中文名 |
| 1 | `/ef 物品`、`/ef 道具`、`/ef 食物 …` | 目录、详情、来源拒绝、别名、帮助图、全局搜索含这两种 |
| 2 | `/ef 敌人` | 目录与详情；`stages/` 无 diff |
| 3 | `/ef 词条` | 说明卡、companion、来源名字；无数值 |
| 4 | `/ef 档案 <名字>`、`/ef 报告 <名字>` | 单条卡；统计/刷新/收集的现有测试原样通过 |

**第 0 期探针**（`--report encyclopedia`，只读已落盘 JSON，不发新表请求）输出：`ItemTypeTable` 全部 id→中文名（含 type 76/12/73/72）、七个 showingType 各落到哪些 type、`displayType` 0–4 能否对上枚举名（对不上就用 `类型{n}`）、`distributionIds` 的表名（没有就留空串）、**85 条道具里"没有任何单个 action 能填满、但并集可以"的条数**与**跨 action 同 key 异值的条数**（S2，用于确认 §4.5 的逐 action 规则会不会误杀）、`EnemyTable.attrTemplateId` 为空的行数（S8）。删掉两项已无必要的探针：`obtainWayIds` 的表名（事实 9）与 termicon 状态码（`FALLBACK_TERM_STYLES` 与 `static_sprite_url` 已在生产里把 `TermIcon/x` 映射到 `sprites/termicon/x.png`）。`BuffData.applyTags` 只记一行，不进任何一期。

**帮助图（事实 21，材料没覆盖的缺口）。** 实现计划要求"每期改 `scripts/help_pages.json` 并重出这张图"，但主仓库里**没有生成这张 PNG 的脚本**，而 `tests/test_endfield.py:843` 把尺寸锁死在 `(1075, 761)`。所以第 1 期的第一件事是把这个脚本落进 `scripts/`（工作树 `.pr_audit/wt7` 里有一版可作起点），把尺寸断言改成"宽固定、高在合理区间"的形式；在那之前，"帮助图"这项交付无法完成，只能改 `format_help()` 文本——而 `_finish_endfield_help` 在图片存在时只发图，用户看不到文本。

**测试清单**（按注册表分批）：`食物 炝炒时蔬` 的 scope 是 `prop`；`道具 xx --source fz` 得「该类资料只提供 AkeData」而无范围的 `--source fz` 不进这句；type 5/6/100 不在物品与道具条目里、`item_equip_` 前缀但 type 在白名单内的行仍在物品索引；`UseItemTable` 的 id 只在道具索引且物品空候选时补查能选出它、物品候选已有两条接近结果时补查不被调用；`{a:0%}` 得百分数字符串、`{b:0}` 得整数；缺 key 抛 `PropEffectIncomplete` 且 `draw` 不被调用；有装备行的道具冷却非空、没有的为 `None` 且视图能建成；`item_obtain_equip` 译成中文途径名；同一 version 不同 `shared_revision` 时索引各建一次；`add_alias("prop", 正式名, "炒菜", lookup=…)` 写入临时文件、lookup 空或返回两名都失败；`ba.burning.related` 含 `ba.burningonchar`；渲染 HTML 不含 `<image=`；`parse_command("档案 xx --source fz").source == "fz"` 且 query 里没有 `--source`；`搜索 档案 xx` 的 scope 是 `archive_entry`；`format_help()` 含新用法、`format_candidates` 尾句（`commands.py:993`）含已上线的范围词。

---

## 8. 对 Fable 清单的逐条处置

**Must-fix（全部采纳，其中两条换实现方式）**

| 项 | 处置 |
| --- | --- |
| M1 补查不能放 `service.py` | 采纳：编排在 `handlers._item_scope_fallback`，返回 `list[EndfieldCandidate] | Literal["medal"]` |
| M2 `add_alias` 同步/异步与循环依赖 | 采纳：`aliases` 叶子 + 注入 `lookup` + `_handle_alias_command` 改 async |
| M3 异常到不了用户 | **换成三态 + 单一映射表**（§5.2 / §5.5），比"二选一"更精确：干员 FZ 兜底不变 |
| M4 两个硬编码 kind 集合 | **换成注册表派生**（D1），两个常量分别由 `needs_snapshot` / `fz_whole_view_fallback` 生成 |
| M5 `obtainWayIds` 用 `SystemJumpTable` | 采纳，并已实测（事实 9）；第 0 期删该项探针 |
| M6 族名写死 | 采纳（§4.2） |
| M7 companion | 采纳，规则顺序已按夹具校正（§4.1，`ba.crystonchar` 走第二段） |
| M8 选项在个人解析前没剥 | **换成选项预解析**（D3），档案分支不再重复解析 |
| M9 不 new `ArchiveSnapshotStore` | 采纳：`build_entries(view)` / `entry_view(view, item_id)`，handler 传 `archive_store.load_current_view()` |
| M10 `<image=…>` | 采纳（§4.3） |

**Suggestions**

| 项 | 处置 |
| --- | --- |
| S1 补查触发点 | 采纳（§5.3） |
| S2 blackboard | 采纳但改写：逐 action 代入（§4.5），探针加冲突计数 |
| S3 不复制 `_format_template_value` | 采纳 |
| S4 `get_index` 分片 | 采纳，并把词条 `sources` 从"每卡扫一遍"改成"每 revision 单趟反查"（§4.4） |
| S5 `--report encyclopedia` | 采纳，探针增删按 §7 |
| S6 门禁与夹具注入 | 采纳（§7） |
| S7 帮助图 | 采纳，但指出缺口：主仓库无渲染脚本且测试锁死尺寸（事实 21、§7） |
| S8 敌人抗性回退与颜色形态 | 采纳（§4.6） |
| S9 `archive_entry` 噪音 | 采纳：`scope=all` 静默返回 `[]` 不打 warning，显式范围才提示 |
| S10 `redirects` 只对 medal 有用 | 采纳（§6.3） |
| S11 dataclass 归位 | 采纳：`IndexEntry` / `EncyclopediaIndex` 进 `models.py` |

**Nits**：N1 已核实 Fable 自身行号有误（事实 13），上升为 D4；N2 `draw.py` 依赖线补 `rendering.cards` 四个 helper；N3 `primary` 是 URL；N4 `alias_data.json` 的 `version` 无消费方；N5 复用两个正则不写第三套；N6 可选表负缓存；N7 「其他」桶；N8 用常量；N9 label 并入注册表；N10 第 4 期测试补两条；N11 兜底文案统一 `类型{n}`；N12 resolver 统一单参 `lambda query:`（只有 equipment 传 rarity，照 `handlers.py:2178`）。

---

## 9. 第 0 期之后仍需确认的开放项

1. `ITEM_TYPE_WHITELIST` 的具体 id 与中文名（含 type 76 / 12 / 73 / 72）。
2. `displayType` 0–4 的含义；对不上就用 `类型{n}`。
3. `distributionIds` 的表名；没有就留空串、只显示条数。
4. 逐 action 规则的误杀率：`--report encyclopedia` 的"并集能填满但单 action 不能"条数与"跨 action 同 key 异值"条数。非零则改附录再改 `render_effect_lines`，不在实现时悄悄改成并集。
5. `EnemyTable.attrTemplateId` 为空的行数（决定 `attrTemplateId or template_id` 回退的覆盖面）。
6. `BuffData.applyTags` 是否含 `ba.*`（只记录，不进任何一期）。

---

## 10. 不做

- 不改 `choose_candidate`、`_dedupe_candidates`、三个分数常量。
- 不改 `LOADOUT_STATUS_LEVELS`、速算、配装卡、干员卡、关卡卡、档案统计卡；`stages/` 无 diff。
- 不让 `encyclopedia` import `handlers` 或 `catalog.service`；不让 `aliases.py` import 图鉴；不在 `service.py` 里调装备/武器 resolver。
- 不把新 kind 放进 FZ 整卡回退集合；不新开 `ArchiveSnapshotStore`。
- 不下载 `BuffData` 来填道具或词条数值；不给词条编任何等级或倍率；不解析说明里的数字。
- 不把多个 action 的 blackboard 合成一张表（除非第 0 期附录改了这条）。
- 不把 `<image=` 画到词条卡上；不为档案正文去抓任务文本或地图文本。
- 不把 `ItemTable` 全量画成一张图；不把 `showingType = 0` 的 1833 条当成一个组；不把敌人实例逐条做成搜索结果。
- 不新增插件，不改森空岛个人接口。
