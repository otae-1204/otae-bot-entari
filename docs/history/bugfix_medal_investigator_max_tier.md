# 谷地调查者奖章 档位判定偏差：原因分析（森空岛 level 偏移）

> 涉及模块：F2 个人缺章（蚀刻章缺章卡）
> 涉事奖章：**谷地调查者奖章**（`achv_adv_tundra_documents`）
> 发现 / 定位：2026-07-29（玩家「大妖精Yousei」实测）→ 2026-07-30 定性
> **结论：AKEData 数据正确（该章 2→3 升级，max=3）。bug 在森空岛侧——其 `level` 字段对 `initLevel>1` 的章存在偏移，实际档位 = `skland level + initLevel - 1`。`谷地调查者奖章` 是全游戏唯一一枚从 2 级升到 3 级的奖章（`initLevel=2`），故只有它会触发这个偏移问题。**

---

## 0. 一句话

原代码把森空岛 `level` 直接拿来和 AKEData `max_level` 比（判「未升满」）、并按 `level` 给等级横条分档。但 `谷地调查者奖章` 的森空岛 `level` 是偏移过的（银=1、金=2），导致：① 把**已升满的金色章误判成未升满**；② 等级横条**计数偏 1**（58/54/24 而非 57/55/24）。

---

## 1. 症状

玩家 136/140 蚀刻章。等级分布（金 / 银 / 灰 = 3 档 / 2 档 / 1 档）：

| 档位 | 程序输出（修复前） | 玩家在游戏 / 森空岛实测 |
|---|---|---|
| 3 档（金） | 58 | **57** |
| 2 档（银） | 54 | **55** |
| 1 档（灰） | 24 | 24 |

- 「未升满」列表里出现了 `谷地调查者奖章`（森空岛 `level=2`），但游戏里它是**金色、已升满**。

---

## 2. 关键发现：森空岛 `level` 对 `initLevel>1` 的章有偏移

森空岛 `level` 字段从 1 开始计：

- `initLevel=1` 的章（绝大多数）：`level` 就是实际档位（1=灰、2=银、3=金）。
- `initLevel=2` 的章：`level` 比实际档位**小 1**。**实际档位 = `level + initLevel - 1`**。
  - `谷地调查者奖章`（`initLevel=2`）：银色（实际 2）记 `level=1`；金色（实际 3）记 `level=2`。

两个独立 live 样本佐证（不同账号）：

| 账号 | 状态 | 森空岛 `level` | `initLevel` | 实际档位 = level+init−1 |
|---|---|---|---|---|
| 大妖精Yousei | 金色（升满） | **2** | 2 | **3** = max ✅ |
| 信翼（刚获得） | 银色 | **1** | 2 | **2**（未升满） |

---

## 3. 为什么全游戏只有这一枚出问题

`谷地调查者奖章` 是**全游戏唯一一枚「从 2 级升到 3 级」的奖章**（`initLevel=2` 且可升到 3）。其余可升级章只有两种：

- `1→2`（如锚定谷地奖章）：`initLevel=1`，无偏移。
- `1→2→3`（如谷地调度专家奖章、潜能解放奖章）：`initLevel=1`，无偏移。

所以只有这枚 `initLevel=2` 的 2→3 章会因为「`level` 偏移」被误判。AKEData 侧 `levelInfos` / `max_level` 全部正确，无需改动。

---

## 4. 数据对比（完整原始条目，未省略字段）

### 4.1 AKEData · 谷地调查者奖章（`AchievementTable.json`，latest `1.4.4@8764515-7`）

```json
{
  "achieveId": "achv_adv_tundra_documents",
  "applyRareEffect": false,
  "canBePlated": false,
  "canBeUpgraded": true,
  "desc": { "id": 0, "text": "" },
  "displayTimeId": "",
  "groupId": "achv_group_adv_tundra",
  "initLevel": 2,
  "levelInfos": {
    "2": {
      "achieveLevel": 2,
      "completeDesc": { "id": -5261208810963925464, "text": "" },
      "conditions": [
        {
          "conditionId": "achv_adv_tundra_documents_2_cond_1",
          "desc": { "id": -8217464337861554041, "text": "" },
          "progressToCompare": 4
        }
      ]
    },
    "3": {
      "achieveLevel": 3,
      "completeDesc": { "id": 4024553912708022849, "text": "" },
      "conditions": [
        {
          "conditionId": "achv_adv_tundra_documents_3_cond_1",
          "desc": { "id": -3494533959060895166, "text": "" },
          "progressToCompare": 6
        }
      ]
    }
  },
  "name": { "id": -5644148015042588683, "text": "" },
  "order": 6,
  "plateConditions": [],
  "specialProgress": false
}
```

> `levelInfos` 含 `2`、`3` 两档（`initLevel=2`，故无 `1` 档）→ `max_level=3`。AKEData 无误。

### 4.2 森空岛 live · 谷地调查者奖章 — 大妖精Yousei（金色 / 升满）

```json
{
  "achievementData": {
    "id": "fc1e1d415a294c3c9c54725a2e121bd2",
    "name": "谷地调查者奖章",
    "initIcon": "https://bbs.hycdn.cn/image/2026/03/30/f76de142df1048e9b993863241c93757.png",
    "reforge2Icon": "https://bbs.hycdn.cn/image/2026/03/30/cfc138a15bafeb71f6789f1da7042b66.png",
    "reforge3Icon": "",
    "platedIcon": "",
    "cateName": "地区奖章",
    "canCertify": false,
    "cate": "achv_type_adventure",
    "initLevel": 2
  },
  "level": 2,
  "isPlated": false,
  "obtainTs": "1771812872"
}
```

> `level=2` + `initLevel=2` → 实际 3 档（金、升满）。`reforge3Icon` 空：因 `initLevel=2` 时图标槽位整体前移（`initIcon`=实际2、`reforge2Icon`=实际3、`reforge3Icon`=实际4 不存在），**不代表 max=2**。

### 4.3 森空岛 live · 谷地调查者奖章 — 信翼（刚获得 / 银色）

```json
{
  "achievementData": {
    "id": "fc1e1d415a294c3c9c54725a2e121bd2",
    "name": "谷地调查者奖章",
    "initIcon": "https://bbs.hycdn.cn/image/2026/03/30/f76de142df1048e9b993863241c93757.png",
    "reforge2Icon": "https://bbs.hycdn.cn/image/2026/03/30/cfc138a15bafeb71f6789f1da7042b66.png",
    "reforge3Icon": "",
    "platedIcon": "",
    "cateName": "地区奖章",
    "canCertify": false,
    "cate": "achv_type_adventure",
    "initLevel": 2
  },
  "level": 1,
  "isPlated": false,
  "obtainTs": "1785342586"
}
```

> `level=1` + `initLevel=2` → 实际 2 档（银、未升满）。与 4.2 对照即可看出 `level` 的偏移规律。

### 4.4 对照组 · 谷地调度专家奖章（`initLevel=1`，1→2→3，无偏移）

AKEData：

```json
{
  "achieveId": "achv_fac_coupon_tundra",
  "applyRareEffect": false,
  "canBePlated": false,
  "canBeUpgraded": true,
  "desc": { "id": 0, "text": "" },
  "displayTimeId": "",
  "groupId": "achv_group_fac_factory",
  "initLevel": 1,
  "levelInfos": {
    "1": {
      "achieveLevel": 1,
      "completeDesc": { "id": 3778133041401822203, "text": "" },
      "conditions": [
        {
          "conditionId": "achv_fac_coupon_tundra_1_cond_1",
          "desc": { "id": -6770942016085881772, "text": "" },
          "progressToCompare": 20000000
        }
      ]
    },
    "2": {
      "achieveLevel": 2,
      "completeDesc": { "id": 3168151537648265111, "text": "" },
      "conditions": [
        {
          "conditionId": "achv_fac_coupon_tundra_2_cond_1",
          "desc": { "id": -3155661590245341778, "text": "" },
          "progressToCompare": 40000000
        }
      ]
    },
    "3": {
      "achieveLevel": 3,
      "completeDesc": { "id": 841001540496848292, "text": "" },
      "conditions": [
        {
          "conditionId": "achv_fac_coupon_tundra_3_cond_1",
          "desc": { "id": -555099771817862414, "text": "" },
          "progressToCompare": 80000000
        }
      ]
    }
  },
  "name": { "id": -739867302794120026, "text": "" },
  "order": 3,
  "plateConditions": [],
  "specialProgress": false
}
```

森空岛 live（大妖精Yousei，金色 / 升满）：

```json
{
  "achievementData": {
    "id": "435456458ddb2f7201dccdd3c2597411",
    "name": "谷地调度专家奖章",
    "initIcon": "https://bbs.hycdn.cn/image/2026/03/30/553c00c0ab33bdddaecd649ad36d8891.png",
    "reforge2Icon": "https://bbs.hycdn.cn/image/2026/03/30/4380a6ba7e12d51df6f9f60505ba2c5c.png",
    "reforge3Icon": "https://bbs.hycdn.cn/image/2026/03/30/a3c86236b58ef14625aca727ca0d9793.png",
    "platedIcon": "",
    "cateName": "建设奖章",
    "canCertify": false,
    "cate": "achv_type_factory",
    "initLevel": 1
  },
  "level": 3,
  "isPlated": false,
  "obtainTs": "1770684157"
}
```

> `initLevel=1` → `level=3` 即实际 3 档，无偏移；`initIcon`/`reforge2Icon`/`reforge3Icon` 对应实际 1/2/3 档，三档齐全。

### 4.5 逐字段对比

| 字段 | 谷地调查者（问题，initLevel=2） | 谷地调度专家（对照，initLevel=1） |
|---|---|---|
| AKEData `levelInfos` 键 | `2`,`3` | `1`,`2`,`3` |
| AKEData `max_level` | 3（正确） | 3 |
| 森空岛 `initLevel` | 2 | 1 |
| 森空岛 `level`（升满时） | **2**（偏移，实际 3） | 3（=实际） |
| 实际档位公式 | `level + initLevel - 1` | `level`（initLevel=1） |
| `initIcon` 对应实际档 | 2 | 1 |
| `reforge2Icon` 对应实际档 | 3 | 2 |
| `reforge3Icon` 对应实际档 | （4，不存在→空） | 3 |

---

## 5. 根因

`build_medal_missing_view` 直接用森空岛 `info.level` 与 `medal.max_level` 比较、并按 `info.level` 给等级横条分档，**未对 `initLevel>1` 的章做偏移校正**。`谷地调查者奖章`（`initLevel=2`、升满金色、`level=2`）因此：

1. `level(2) < max_level(3)` → 误判「未升满」（实际 real=3=max，已升满）；
2. 按 `level=2` 计入银档 → 银档多 1、金档少 1（58/54 而非 57/55）。

> 旧交接 `docs/handoff_akedata_migration.md` §2.2 据 AKEData `levelInfos` 断定该章「max=3、确实未升满」——max=3 正确，但「未升满」是拿偏移过的 `level=2` 误判，实际已升满。

---

## 6. 修复

引入实际档位校正：`real_level = info.level + (info.init_level - 1)`（`init_level>0` 时；否则不偏移）。

- `plugins/endfield/catalog/models.py` · `MedalProgressView`：保留 `init_level`（`achievementData.initLevel`）。
- `plugins/endfield/catalog/service.py` · `_parse_player_medal_progress`：记录 `init_level`。
- `plugins/endfield/catalog/service.py` · `build_medal_missing_view`：等级横条按 `real_level` 分档；未升满判定改 `real_level < medal.max_level`。
- `plugins/endfield/rendering/cards.py` · `draw_medal_missing_card`：页脚「元数据：FZ Wiki」→「AKEData（游戏客户端 TableCfg）」。

**验证**（大妖精Yousei）：等级分布 `{3:57, 2:55, 1:24}`；未升满只剩 `潜能解放奖章`（`谷地调查者奖章` 移出）；`pytest tests/test_endfield_medal.py` → **18 passed**（含回归用例 `test_init_level_offset_for_2_to_3_medal`，以 G=谷地调查者型 / H=潜能解放型 验证偏移校正）。

---

## 7. 备注

- **AKEData 全程正确**，F1 统计卡 / 全量快照无需改动（`max_level=3` 即该章真实最高档，图标 `_lv03.png` 亦正确）。
- 本次仅 F2 个人缺章需要 `level` 偏移校正；F2 已通用兜住「任何 `initLevel>1` 的可升级章」。
- 调查过程中的两个 live 样本（大妖精金色 `level=2`、信翼银色 `level=1`）是定性偏移规律的关键证据。
