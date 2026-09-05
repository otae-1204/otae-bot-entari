# 终末地资料图：AkeData 统一方案

本文件保留两阶段方案的历史背景。2026-09-05 已执行方案 B：公共资料默认 AKE，完整覆盖路径不再请求 FZ/Warfarin 正文；个人接口仍用官方，缺失数据保留明确回退。当前实现、例外与实测以 [迁移实施记录](endfield_ake_migration_execution.md) 为准。

## 方案 A：只统一图（历史阶段，已完成）

### 目标

所有用户能看到的远程图，只向 AkeData sprites 要：

```
https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/{folder}/{stem}.png
```

Warfarin、FZ 不再进入候选 URL。正文、技能数值、图鉴名单仍走 FZ。

### 拼图规则

| 图 | 目录 | 用什么 id |
|---|---|---|
| 干员头像 | `charremoteicon`，不行再 `charicon` | `icon_{charId}` |
| 圆头 | `charroundicon` | `icon_round_{charId}` |
| 立绘 / 卡池头图 | `characterportrait` | `{charId}` |
| 技能 / 天赋 | `skillicon` | `CharGrowthTable` 的 `icon` / `iconId` |
| 武器 / 装备 / 信物 | `itemiconbig`，不行再 `itemicon` | 优先 `ItemTable.iconId`，无映射才用物品 id |
| 术语小图标 | `termicon` | `icon_term_*` |
| 账号页 / 图鉴职业、属性 | `charprofessionicon` / `elementicon` | 中文职业、属性名对到表里的 `iconId` |
| 图鉴武器类型角标 | — | 目前没有稳定 sprite id，不打 FZ 哈希链 |

技能、天赋的 sprite id **不按角色名猜**，从 `CharGrowthTable` 取：

- 技能：`skillGroupMap[].icon`，按 `skillGroupType`（0 普攻 / 1 战技 / 2 终结技 / 3 连携技）对到 FZ 技能分类
- 天赋：`talentNodeMap[].passiveSkillNodeInfo.iconId`，按 `talentEffectId` 里的 `_talent_{n}_` 槽位对到卡片上的 T1/T2

表里还没有该干员时，才回退一次 Warfarin 详情对 id。manifest 只缓存 60 秒，成长表按 `latest` 版本换表，避免卡在上一期。

### 下载

每组图仍两轮：先打 AkeData 首选路径，失败再打同系列备用（例如 `charremoteicon` → `charicon`）。上限 24MB、超时 20 秒、失败重试 3 次。抽卡本地缓存逻辑不变。

图鉴页职业/元素角标按中文名对到 AkeData sprites。武器类型角标还没有对应表字段，不打 FZ 哈希链。

### 已知代价

- AkeData 立绘偏大（提弗洛斯约 17MB），卡池头图第一次会慢。
- Ake 表偶发落后版本时，新干员可能暂时不在 `CharGrowthTable`；CDN 上图在、表还没行时，才回退 Warfarin 对 id。

### 不在本期

不重写干员卡/武器卡正文解析，不改 FZ 为默认数据源。

---

## 方案 B：连正文也统一到 AkeData（现已执行）

### 目标

干员卡、武器卡、图鉴名单的**数据**也改从 AkeData 表来，不再依赖 FZ 文章模板。

### 要接的表

- 干员：`CharacterTable`、`CharGrowthTable`、`CharacterPotentialTable`、`I18nTextTable_CN`
- 武器：`WeaponBasicTable`、`WeaponUpgradeTemplateTable`、技能 patch
- 装备：`EquipTable`、`EquipSuitTable`、`ItemTable`
- 图鉴：上面几张表按职业/元素/武器类型聚合

账号养成、奖章、关卡已经在用 AkeData 表，可以复用 `akedata_client` 和 manifest。

### 当时的工作量评估

按周计。FZ 现在提供的技能多形态描述、paramTable、富文本术语，都要在表结构上重新对齐；`build_fz_operator_view` / `build_weapon_view` 等于重写。收益是图和字同一源、新干员不再等 FZ 文章。风险是 Ake 表更新节奏、i18n 体积、以及卡片排版对 FZ 富文本的既有假设。

### 当时建议的顺序

1. 先做方案 A，把 CDN 收成一家。
2. 技能/天赋 id 已改读 `CharGrowthTable`；表缺行时仍可能打一次 Warfarin。
3. 若 FZ 新干员文章长期缺字段或 404，再评估方案 B。
