# AKEData 取数指南（终末地）

> 写给想从 [AKEData](https://www.akedata.wiki) 拉数据的同学。自包含，示例用 Python 标准库（复制即跑，不依赖任何框架）。
> 数据基于 2026-07-29 的版本 `1.4.4@8764515-7` 实测；表结构随游戏更新可能变化，但取数套路不变。

---

## 0. AKEData 是什么

AKEData 是明日方舟：终末地的同人数据站，数据**直接来自游戏客户端的 TableCfg**（官方配置表），字段最贴近客户端、CDN 稳定。

两个域名：
- **站点**：`www.akedata.wiki` / `cf.akedata.top`（前端页面，走 service worker，**不要从这抓数据**）
- **数据 CDN**：`data.akedata.wiki`（裸 JSON，**直接打这里**）

请求建议带这两个头（部分资源校验 Referer）：
```
User-Agent: <你的应用名>
Referer: https://cf.akedata.top/
```

---

## 1. 三步套路

所有模块的数据都按同一套套路取：

1. **manifest → 定位版本目录**：`manifest.json` 给出每个版本的 `tableCfgPath`。
2. **取表 JSON**：`<dataBase>/<tableCfgPath>/<表名>.json`。
3. **解析名字/描述 + 取图标**：名字是 text-id，要查 `I18nTextTable_CN`；图标按固定路径拼。

下面逐步展开。

---

## 2. 第一步：manifest 定位版本

```
GET https://data.akedata.wiki/manifest.json
```

结构：
```jsonc
{
  "latest": "1.4.4@8764515-7",            // 当前版本 id
  "sharedRevision": "2026-07-23T...",      // 资源公共修订
  "versions": [                            // 历史版本列表（最新在前）
    { "id": "1.4.4@8764515-7", "tableCfgPath": "public/1.4.4/8764515-7/TableCfg", ... },
    { "id": "1.4.4@8692565-6", "tableCfgPath": "public/1.4.4/8692565-6/TableCfg", ... },
    { "id": "1.3.3@8190425-29", "tableCfgPath": "public/1.3.3/8190425-29/TableCfg", ... },
    ...
  ]
}
```

- **版本 id 语义**：`游戏版本@资源修订-子修订`，如 `1.4.4@8764515-7`。`@` 前两段（`1.4`）跟着**游戏大版本**走；同一大版本会有多个资源修订（`1.4.4` 下有 -5/-6/-7），它们的数据通常一致。
- **`versions[]` 保留了多个历史版本**，每个版本的 `tableCfgPath` 都能独立访问——这是 AKEData 的一大优势：**可以拉任意历史版本的数据做对比**（不用自己存档）。
- 拿到 `latest` 对应条目的 `tableCfgPath`，就是当前版本的表目录。

```python
import urllib.request, json

DATA = "https://data.akedata.wiki"
HDRS = {"User-Agent": "my-app/1.0", "Referer": "https://cf.akedata.top/"}

def get_json(path):
    req = urllib.request.Request(f"{DATA}{path}", headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

manifest = get_json("/manifest.json")
latest = manifest["latest"]
table_cfg = next(v["tableCfgPath"].lstrip("/") for v in manifest["versions"] if v["id"] == latest)
# → "public/1.4.4/8764515-7/TableCfg"
```

---

## 3. 第二步：取表

表 URL 固定格式：
```
https://data.akedata.wiki/<tableCfgPath>/<表名>.json
```

### 表的通用结构

几乎所有表顶层都是 **dict（对象）**，**key 是实体 id**，value 是该实体的字段：

```jsonc
{
  "chr_0002_endminm": { "charId": "chr_0002_endminm", "name": {"id": -7078064683023630592, "text": ""}, "rarity": 5, ... },
  "chr_0003_endminf": { ... }
}
```

**实体 id 的前缀区分模块**，非常好认：

| 前缀 | 模块 | 示例 |
|---|---|---|
| `chr_` | 角色 | `chr_0002_endminm` |
| `eny_` | 敌人 | `eny_0007_mimicw` |
| `wpn_` | 武器 | `wpn_claym_0003` |
| `item_` | 物品 / 装备 / 消耗品 | `item_equip_t0_parts_...`、`achv_adv_tundra_box_1` |
| `achv_` | 奖章（成就） | `achv_adv_tundra_box` |
| `dung…` | 关卡（地下城） | `dung01_actmonster01` |
| `suit_` | 装备套装 | `suit_agi01` |

### 字段的三种形态

1. **text-id 字段**（名字、描述等文本）：`{"id": <整数>, "text": ""}`。`text` 几乎总是空的，**必须用 `id` 查 I18n**（见第 4 步）。
2. **标量字段**（数值/枚举）：`rarity: 5`、`profession: 2`、`weaponType: 1`。枚举值是数字，含义要查对应的「枚举表」（如职业查 `CharProfessionTable`）。
3. **嵌套/关联字段**：`attrTemplateId`、`suitID`、`levelTemplateId` 等，值是别的表的 key，用来跨表关联。

---

## 4. 第三步：解析文本（I18n）

所有中文文本都在一张大表里：

```
GET https://data.akedata.wiki/<tableCfgPath>/I18nTextTable_CN.json
```

- **约 17 MB，13.8 万条**，结构 `{ "<text-id 字符串>": "中文文本" }`。
- **key 是 text-id 的字符串形式，可能是负数或很大的整数**，如 `"-9223254181335467105"`。
- 解析方式：`i18n[str(entry["name"]["id"])]`。

```python
i18n = get_json(f"/{table_cfg}/I18nTextTable_CN.json")   # 只抓一次，缓存起来复用

def text(i18n, text_field):
    """text_field 形如 {"id": -7078064683023630592, "text": ""} → 中文"""
    if not isinstance(text_field, dict):
        return ""
    return i18n.get(str(text_field.get("id")), "") or text_field.get("text", "")
```

> ⚠️ I18n 很大且每次版本更新才变，**务必本地缓存**（落盘或内存长驻），不要每条数据都重抓。

---

## 5. 第四步：取图标

图标都在同一个 sprites 根目录下，按模块分子目录，**用 id 直接拼 URL**：

```
BASE = https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites
```

| 模块 | 路径模板 | 说明 |
|---|---|---|
| **角色头像** | `BASE/charremoteicon/icon_<charId>.png` | 如 `icon_chr_0002_endminm.png` |
| **物品 / 通用** | `BASE/itemiconbig/<iconId 或 id>.png` | 优先用条目里的 `iconId`，没有就用 `id` |
| **敌人** | `BASE/monstericonbig/<enemyId>.png` | 如 `monstericonbig/eny_0007_mimicw.png` |
| **奖章** | `BASE/medaliconbig/<achvId>_lv<NN>.png` | `NN` = 该奖章最高等级（零填充，如 `_lv03.png`） |
| **关卡** | `BASE/dungeon/<dungeonPicPath>_bg.png` | `dungeonPicPath` 取自 DungeonTable 条目 |
| **活动** | `BASE/activity/<tabImg>.png` | `tabImg` 取自 ActivityTable 条目 |

（规则从站点 `v3-table-data.js` 反推，覆盖了主要模块；个别子资源可去该 JS 里查。）

---

## 6. 各模块速查（表清单）

> 条目数为 1.4.4 实测；「主键」指顶层 dict 的 key 前缀；「名字来源」指 name 字段所在表。

### 角色
| 表 | 条目 | 主键 | 用途 / 关键字段 |
|---|---|---|---|
| `CharacterTable` | 31 | `chr_` | 角色主表：`charId`、`name`、`profession`、`rarity`、`weaponType`、`mainAttrType`、`subAttrType`、`department`、`cvName`、`defaultWeaponId` |
| `CharGrowthTable` | 31 | `chr_` | 成长：`skillGroupMap`（技能）、`talentNodeMap`（天赋）、`skillLevelUp`、`charBreakCostMap`（突破消耗） |
| `CharacterPotentialTable` | 31 | `chr_` | 潜能：`potentialUnlockBundle` |
| `CharProfessionTable` | 6 | — | 职业枚举（`profession` 数字 → `name`/`iconId`） |

> 角色名字：`CharacterTable[id].name`；头像：`charremoteicon/icon_<charId>.png`。

### 敌人
| 表 | 条目 | 主键 | 用途 / 关键字段 |
|---|---|---|---|
| `EnemyTable` | 359 | `eny_` | 敌人实例：`enemyId`、`attrTemplateId`、`modelId`、`isDangerous`、`templateId`（**无 name**） |
| `EnemyTemplateDisplayInfoTable` | 84 | `eny_` | **名字在这**：`name`、`nickname`、`description`、`abilityDescIds`、`tags` |
| `EnemyAttributeTemplateTable` | 131 | `eny_` | 属性：各种抗性（`cryst/fire/natural/physical/pulseResistance`）、韧性（`maxResilience`…） |
| `EnemyAbilityDescTable` | 170 | — | 能力描述：`name`、`description` |

> ⚠️ EnemyTable 没有名字字段——**名字在 `EnemyTemplateDisplayInfoTable`**（按 `enemyId` 或 `templateId` 关联）。EnemyTable(359) 是每个敌人实例，DisplayInfo(84) 是合并后的展示模板。

### 武器
| 表 | 条目 | 主键 | 用途 / 关键字段 |
|---|---|---|---|
| `WeaponBasicTable` | 76 | `wpn_` | 武器主表：`weaponId`、`engName`、`weaponDesc`、`rarity`、`weaponType`、`maxLv`、`weaponSkillList`、`levelTemplateId`、`breakthroughTemplateId` |
| `WeaponBreakThroughTemplateTable` | 30 | — | 突破模板（`list`） |
| `WeaponTalentTemplateTable` | 2 | — | 天赋模板（`list`） |
| `WeaponUpgradeTemplateTable` | — | — | 升级数值模板 |

> 武器的名字字段是 `engName`（text-id）；图标走 `itemiconbig`（武器也是一种 item）。

### 装备
| 表 | 条目 | 主键 | 用途 / 关键字段 |
|---|---|---|---|
| `EquipTable` | 243 | `item_equip_` | 装备：`itemId`、`partType`、`suitID`、`domainId`、`equipAttrModifiers`、`displayAttrModifiers` |
| `EquipItemTable` | 49 | `item_` | 可用装备/消耗：`equipDesc`、`castTime`、`cooldown`、`chargeCount` |
| `EquipSuitTable` | 23 | `suit_` | 套装效果：`equipList`、`list` |
| `EquipEnhanceCostTable` | — | — | 强化消耗 |
| `EquipFormulaTable` 等 | — | — | 装备配方（合成/分解） |

### 物品（总表）
| 表 | 条目 | 主键 | 用途 / 关键字段 |
|---|---|---|---|
| `ItemTable` | 2678 | 多种 | **所有物品的总表**：`id`、`name`、`desc`、`rarity`、`type`、`showingType`、`iconId`、`iconCompositeId`、`sortId1/sortId2` |
| `ItemTypeTable` | 101 | — | 物品类型枚举（`itemType` → `name`） |
| `ItemShowingTypeTable` | 11 | — | 展示类型枚举 |
| `UseItemTable` | 84 | `item_` | 使用效果：`effectType`、`useActions`、`duration` |
| `ItemIconCompositeTable` | 63 | — | 复合图标规则 |

> ItemTable 是个大杂烩——奖章、装备、材料、消耗品都在里面，用 `type` / `showingType` 区分类别。图标用 `iconId`（如 `item_achievement_icon`）走 `itemiconbig`。

### 关卡（地下城）
| 表 | 条目 | 主键 | 用途 / 关键字段 |
|---|---|---|---|
| `DungeonTable` | 299 | `dung` | 关卡：`dungeonName`、`dungeonDesc`、`dungeonCategory`、`dungeonSeriesId`、`enemyIds`、`enemyLevels`、`recommendLv`、`rewardId`、`costStamina`、`dungeonPicPath` |
| `DungeonSeriesTable` | 127 | `dung…_group` | 关卡系列：`name`、`desc`、`includeDungeonIds` |

### 活动 / 商店 / 奖章 / 其他
| 表 | 条目 | 主键 | 用途 |
|---|---|---|---|
| `ActivityTable` | 84 | — | 活动：`name`、`desc`、`type`、`timeId`、`tagIds`、`tabImg` |
| `ShopTable` / `ShopGoodsTable` | 30 / 769 | — | 商店分组 / 商品明细（`price`、`moneyId`、`rewardId`、`limitCount`） |
| `AchievementTable` | 140 | `achv_` | 奖章：`name`、`canBeUpgraded`、`canBePlated`、`initLevel`、`levelInfos`、`groupId` |
| `AchievementTypeTable` | 8 | — | 奖章分类（`categoryName`、`categoryPriority`、`achievementGroupData[]`） |
| `RewardTable` | — | — | 奖励内容（被多处 `rewardId` 引用） |
| `Factory*Table` | — | — | 工厂（基建）：建筑/配方 |
| `Spaceship*Table` | — | — | 飞船：技能/制造配方 |
| `ContingencyContract*Table` | — | — | 危机合约 |

> 想发现**全部**表名：抓站点 `https://www.akedata.wiki/plugin/js/v3-table-data.js`，里面的 `XxxTable` 字符串就是完整清单。

---

## 7. 完整示例：拉角色列表（名字 + 头像 + 职业）

```python
import urllib.request, json

DATA = "https://data.akedata.wiki"
HDRS = {"User-Agent": "my-app/1.0", "Referer": "https://cf.akedata.top/"}
SPRITES = f"{DATA}/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"


def get_json(path):
    req = urllib.request.Request(f"{DATA}{path}", headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# 1) 定位版本
manifest = get_json("/manifest.json")
tc = next(v["tableCfgPath"].lstrip("/")
          for v in manifest["versions"] if v["id"] == manifest["latest"])

# 2) 一次性抓 I18n + 职业 + 角色表
i18n = get_json(f"/{tc}/I18nTextTable_CN.json")
prof = get_json(f"/{tc}/CharProfessionTable.json")          # profession(int) → name(text-id)
chars = get_json(f"/{tc}/CharacterTable.json")


def tx(field):
    return i18n.get(str(field["id"]), "") if isinstance(field, dict) else ""


prof_name = {k: tx(v["name"]) for k, v in prof.items()}     # profession 数字 → 职业名

# 3) 组装
for cid, c in chars.items():
    print({
        "id": c["charId"],
        "name": tx(c["name"]),
        "rarity": c["rarity"],
        "profession": prof_name.get(str(c["profession"]), "?"),
        "icon": f"{SPRITES}/charremoteicon/icon_{c['charId']}.png",
    })
```

同理，敌人把 `chars` 换成 `EnemyTable` + 名字从 `EnemyTemplateDisplayInfoTable` 取、图标走 `monstericonbig`；物品用 `ItemTable`、图标走 `itemiconbig/<iconId>.png`，依此类推。

---

## 8. 取历史版本数据（做版本对比）

`manifest["versions"]` 里每个版本都能独立访问。比如取上一游戏版本的奖章，跟当前版本比新增：

```python
def game_major(version_id):           # "1.4.4@8764515-7" → "1.4"
    return ".".join(version_id.split("@")[0].split(".")[:2])

versions = manifest["versions"]
latest_label = game_major(manifest["latest"])
prev = next(v for v in versions if game_major(v["id"]) != latest_label)  # 上一游戏版本

cur_achv = get_json(f"/{tc}/AchievementTable.json")
prev_achv = get_json(f"/{prev['tableCfgPath'].lstrip('/')}/AchievementTable.json")

added = set(cur_achv) - set(prev_achv)   # 新增奖章 id
```

> 同一游戏大版本的多个资源修订（如 `1.4.4@…-5/-6/-7`）数据通常一致，**对比时按前两段（major.minor）跳过它们**，找第一个不同的游戏版本。

---

## 9. 注意事项

- **直接打 `data.akedata.wiki`**，别走站点 `cf.akedata.top/public/CH/...`（那是预构建数据，走 service worker，抓取麻烦）。
- **I18n 17MB 且是文本解析的必需品**：每个版本一张，更新不频繁——务必缓存复用，别每次都重抓。
- **text-id 是大整数（含负数）**：查 I18n 时一定要 `str(id)`，且别用 `int` 当 dict key 直接匹配 JSON 的字符串 key。
- **名字不一定在「主表」里**：敌人名字在 `EnemyTemplateDisplayInfoTable`，不在 `EnemyTable`；遇到主表没 name 的，想想是否有配套 `Display`/`Template` 表。
- **枚举字段是数字**：`profession`/`rarity`/`weaponType`/`partType` 等要查对应枚举表或站点约定才能转成可读文本。
- **带 Referer**：部分资源校验，带上 `Referer: https://cf.akedata.top/` 最稳。
- **尊重数据源**：AKEData 是同人项目，抓取适度、加缓存、别高频打。表是大文件，建议本地落盘缓存按版本管理。

---

## 附：发现新表/新规则的方法

1. **完整表清单**：抓 `https://www.akedata.wiki/plugin/js/v3-table-data.js`，grep `XxxTable`。
2. **图标/路径规则**：同文件里 grep `icon`、`sprite`、`.png`，看前端怎么拼 URL。
3. **字段含义**：抓对应表 JSON，看第一个 entry 的 key；text-id 字段查 I18n，枚举字段找配套枚举表。
