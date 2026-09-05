# 森空岛奖章数据解析 + 跨源 id 对比

> 分支：`dev`　·　创建：2026-07-28
> 性质：实证调研文档。记录森空岛 `achieveMedals` 解析经验、三源 id 关系、跨源关联方式。
> 配套实现：`plugins/endfield/catalog/service.py` 的 `build_medal_missing_view` / `_parse_player_medal_progress`。

---

## 0. 一句话结论

森空岛 `achievementData.id`（32 位 hex）= **`md5(游戏 achv_id)`**。它和 FZ/Warfarin 的 `achv_*` id 是**哈希一一对应**关系（2026-07-28 实测 **115/115** 命中，且能解释全部 136 枚玩家进度）。

→ 个人缺章（F2）应**按 `md5(FZ.medal_id) == 森空岛 hex` 关联**，而不是按 name。按 name 关联会被「命名滞后」击穿（见 §6 武陵案例）。

> ⚠ 本文推翻了 `handoff_medal_module.md` §4 与 `handoff_medal_f2_fix.md` 早先的结论「id 命名空间不同、不能按 id 关联」。那个结论是因为只做了**直接相等**比较（`achv_*` ≠ hex），没测哈希关系。以本文为准。

---

## 1. 三源 id 命名空间对比

| 源 | id 形态 | 来源 | 示例 |
|---|---|---|---|
| **FZ Wiki** | `achv_*`（语义 id） | 游戏客户端数据 dump | `achv_fac_coupon_wuling_5` |
| **Warfarin Wiki** | `achv_*`（语义 id） | 游戏客户端数据 dump | `achv_fac_coupon_wuling_5` |
| **森空岛**（官方 API） | 32 位 hex | **`md5(achv_id)`**（推测由森空岛侧计算） | `f10835a082696394a1a69f0bc212e045` |

- **FZ ↔ Warfarin**：`achv_` id **直接相等**（实测武陵调度系列 `_1/_2/_3` 逐枚一致）。
- **FZ → 森空岛**：`md5(achv_id) == 森空岛 hex`（实测 115/115）。
- 森空岛 `card/detail` 响应里**不暴露** `achv_` id，整张 `achievementData` 表无该字段；只能靠 md5 反查或 name。

**所以森空岛虽然是官方 API，但它不直接用客户端的 `achv_` id，而是用自己的一套 hex id —— 这套 hex 恰好是 `achv_` 的 MD5。**

---

## 2. 森空岛 `achieveMedals` 结构解析

端点：`GET /api/v1/game/endfield/card/detail?roleId=&serverId=`（签名 GET，query 入签名）。

路径：`data.detail.achieve.achieveMedals[]`。**只有已获得的奖章在列表里**——不在列表即未获得，这是 F2 交叉比对的驱动逻辑。

每枚结构（实测，2026-07-28）：

```
achieveMedals[i]
├─ achievementData   # 静态元数据（10 字段）
│   ├─ id            # 32 位 hex = md5(achv_id)  ← 跨源关联主键
│   ├─ name          # 中文名（可能与 FZ 不一致，见 §6）
│   ├─ initIcon / reforge2Icon / reforge3Icon / platedIcon  # 各等级/镀层图标
│   ├─ cateName / cate        # 分类名 / 分类 id
│   ├─ canCertify             # 可否镀层（对应 FZ canBePlated）
│   └─ initLevel              # 初始等级
├─ level            # 玩家当前等级
├─ isPlated         # 是否已镀层（bool，偶有字符串 "true"）
└─ obtainTs         # 获得时间戳
```

`achieve` 容器另有 `display`（展示用 10 枚）、`count`（计数）。样本：`count=136`、`achieveMedals` 长度 136、去重 name 后 135（命名滞后导致同名重复 1 枚，见 §6）。

### 解析坑点

1. **`isPlated` 不恒为 bool**：实测多为 bool，但防御性处理字符串 `"true"/"1"/"yes"`（见 `_parse_player_medal_progress`）。
2. **`id` 是 hex，不是 achv_**：直接拿去和 FZ `medal_id` 比较会全部不等（这是上一会话踩的坑）。
3. **`name` 不可作主键**：命名滞后会撞名（§6），按 name 去重会丢章。
4. **`level` 含义**：玩家当前章等级；`initLevel` 在 `achievementData` 里是该章初始等级，别混。

---

## 3. `md5(achv_id)` 关联的发现与验证

### 怎么发现的

按 name 关联得到 135/140（5 枚未获得，其中「武陵调度专家奖章·Ⅴ」靠 suspect 启发式标「可能已拥有」）。怀疑 name 不可靠后，把同 name 的「FZ achv_ id」与「森空岛 hex」并排，发现 hex 长度恒为 32（MD5 形态），于是测试 `md5(achv_id)`。

### 验证（115/115）

对 115 枚 name 能匹配上的章，逐一 `hashlib.md5(achv_id.encode()).hexdigest()`：

```
武陵调度专家奖章·Ⅲ   achv_fac_coupon_wuling_3  → af296578d85f9a96c8e724688ba7dc75  == 森空岛 hex ✓
"Delta救星"         achv_adv_tundra_fixablerobot → 33172f3001913a07df3099afdb36d68d == 森空岛 hex ✓
...（115 枚全部相等）
hash matches: {'md5(achv)': 115} out of 115
```

排除了其它候选（`sha1(achv)[:32]`、`md5(name)`、`md5(achv+name)` 均为 0 命中）。

### 反向解释（136 个 hex 全部对得上）

把 FZ 140 枚的 `achv_id` 各取 md5，得到 140 个 hex；玩家森空岛 136 个 hex **全部**落在这 140 个里（剩 4 个 = 真未获得的活动章）。无孤立的、解释不了的 hex，说明 md5 关系完备，没有「另一种 hash」的混入。

### 武陵命名 bug 的彻底定论

直接算 5 个 achv_ id 的 md5，比对玩家 hex：

| achv_ id | md5 | 在玩家账号? | 森空岛显示名 |
|---|---|---|---|
| `achv_fac_coupon_wuling_4` | `ef51e84a…` | ✓ 拥有 | 武陵调度专家奖章·Ⅳ |
| `achv_fac_coupon_wuling_5` | `f10835a0…` | ✓ **拥有** | 武陵调度专家奖章·Ⅳ（标错，实为·Ⅴ） |

→ 玩家**其实拥有·Ⅴ**（id `_5` 的 hex 在账号里），森空岛只是把它和真·Ⅳ一样标成「·Ⅳ」。按 name 关联会把 `_5` 漏判为未获得；按 md5-id 则精确归属为已获得。

---

## 4. 三源字段对照（奖章相关）

| 用途 | FZ 单件 `entry` | Warfarin `/cn/medals` | 森空岛 `achievementData` |
|---|---|---|---|
| 跨源主键 | `id`（`achv_*`） | `id`（`achv_*`） | `id`（**hex = md5(achv_*)**） |
| 名称 | `name` | `name` | `name`（可能滞后） |
| 最高等级 | 推导：`levels[-1].level` | `maxLevel`（直字段） | （静态表无；`initLevel` 非最高） |
| 可升级 | 推导：`len(levels)>1` | `canBeUpgraded`（直字段） | （无，靠 FZ 推导） |
| 可镀层 | `canBePlated` | `canBePlated` | `canCertify` |
| 分类 | `categoryName`/`categoryId` | `categoryName` | `cateName`/`cate` |
| 图标 | `iconUrl`（补 `@raw`） | `icon` | `initIcon`/`reforge*Icon`/`platedIcon`（按 level+isPlated 选） |
| 玩家等级 | — | — | `level`（外层 `achieveMedals[i].level`） |
| 玩家镀层 | — | — | `isPlated`（外层） |

要点：**静态元数据（最高等级、可升级、可镀层）以 FZ 为准**（Warfarin 次之），森空岛只提供**玩家进度**（level/isPlated/是否在列表）。F2 = FZ 元数据 × 森空岛进度，靠 md5-id 关联。

---

## 5. 跨源关联实现方式

代码在 `plugins/endfield/catalog/service.py`：

1. **`_parse_player_medal_progress(raw)`**：解析森空岛响应，返回 `(by_hex, by_name)` 两个 dict：
   - `by_hex`：键 = `achievementData.id`（hex）→ `MedalProgressView(level, plated)`。**主键**。
   - `by_name`：键 = 规范化 name（`_norm_medal_name`：去空白+去引号）→ 同上。**兜底**。

2. **`build_medal_missing_view(...)`**：对 FZ 快照每枚章：
   ```python
   achv_id = medal.medal_id or ""
   info = (progress_by_hex.get(hashlib.md5(achv_id.encode()).hexdigest())
           if achv_id.startswith("achv_") else None)
   if info is None and medal.name:           # 兜底：FZ 无 achv_ id 时按 name
       info = progress_by_name.get(_norm_medal_name(medal.name))
   if info is None: not_obtained             # 未获得
   else: 按 can_be_upgraded/can_be_plated 判未升满/未镀层
   ```

**为什么留 name 兜底**：FZ 单件档案实测 100% 含 `achv_` id，兜底基本不触发；但保留它能防御未来 FZ 出现缺 id 的异常条目（此时回退 name，宁可兜底也别把章判丢）。主路径仍是 md5-id，不受命名滞后影响。

**已删除**：原基于 name 的 `_base_name`/`_ROMAN_SUFFIX` 与 `suspect_names`（「可能已拥有」启发式）。md5-id 精确判定后不再需要猜测；`models.MedalMissingView.suspect_names` 与 `draw.py` 的 suspect 渲染/CSS 一并移除。

---

## 6. 命名滞后案例：武陵调度专家奖章·Ⅳ/·Ⅴ

**背景**：官方曾因命名错误把 `achv_fac_coupon_wuling_5` 和 `_4` 重名（都叫·Ⅳ），后客户端修正 `_5` 为·Ⅴ。各源跟进程度不同：

| 源 | `_4` 显示名 | `_5` 显示名 |
|---|---|---|
| FZ（跟进修正） | 武陵调度专家奖章·Ⅳ | 武陵调度专家奖章·Ⅴ |
| Warfarin（滞后） | 武陵调度专家奖章·Ⅳ | 武陵调度专家奖章·Ⅳ（未跟进） |
| 森空岛（滞后） | 武陵调度专家奖章·Ⅳ | 武陵调度专家奖章·Ⅳ（未跟进） |

**为什么 name 关联会错**：玩家拥有 `_5`，森空岛上报 name「·Ⅳ」，与 FZ 真·Ⅳ（`_4`）撞名 → 按 name 把 `_5` 当成 `_4`，结果 FZ 的 `_5`（·Ⅴ）被判「未获得」（135/140，漏 1）。

**md5-id 怎么修**：`_5` 的 hex = `md5(achv_fac_coupon_wuling_5)`，与 name 无关；该 hex 在玩家账号 → 直接判为已拥有。 owned 从 135 升到 136，未获得从 5 降到 4（仅剩 4 枚活动章）。

**启示**：凡是「同系列多等级 + 历史命名修正」的章都可能撞名。只有 id（及其 md5）是稳定主键。

---

## 7. 调试方法与脚本

复现这套结论的脚本（在 `scripts/`，均为 `_dev_*` 一次性调研工具）：

| 脚本 | 用途 |
|---|---|
| `_dev_id_compare.py` | FZ ↔ Warfarin 的 `achv_` id 逐枚对照（证两源同命名空间），打印 FZ entry 全字段 |
| `_dev_hex_vs_achv.py` | 森空岛 hex ↔ FZ `achv_` id 并排（读本地 dump + snapshot） |
| `_dev_verify_view.py` | 打印 `build_medal_missing_view` 三段结果 + 潜能章状态（md5-id 关联后） |
| `_dev_medal_repl.py` | 主力：命令行手动测 F1/F2（发码/手机登录/重查，token 缓存复用） |
| `_dev_fz_inspect.py` | FZ 单件 entry 16 字段（确认无 hex id） |
| `_dev_match_check.py` | FZ vs 森空岛 name 匹配率（旧 name 方案的基准） |

### 拿一份真实 dump 的流程

```bash
PYTHONPATH=. .venv/Scripts/python.exe scripts/_dev_medal_repl.py 奖章 刷新   # 建 FZ 快照（~2min，含丢页重试）
PYTHONPATH=. .venv/Scripts/python.exe scripts/_dev_medal_repl.py 发码 <手机号>
PYTHONPATH=. .venv/Scripts/python.exe scripts/_dev_medal_repl.py 手机登录 <手机号> <验证码>
# → card/detail dump 落 data/_manual_test/card_detail_raw_<ts>.json，token 缓存到 .token_cache
PYTHONPATH=. .venv/Scripts/python.exe scripts/_dev_medal_repl.py 重查        # 之后用缓存 token 重查，无需再发码
```

### 验证 md5 关联的一行式

```python
import hashlib, json, glob
from plugins.endfield.catalog.service import _norm_medal_name
raw = json.load(open(sorted(glob.glob('data/_manual_test/card_detail_raw_*.json'))[-1], encoding='utf-8'))
sk = {m['achievementData']['id'] for m in raw['data']['detail']['achieve']['achieveMedals']}
snap = json.load(open('data/endfield/medal_snapshot.json', encoding='utf-8'))
fz = [(m['medal_id'], m['name']) for m in snap['current']['medals']]
hits = sum(hashlib.md5(a.encode()).hexdigest() in sk for a, _ in fz if a.startswith('achv_'))
print(f'md5 命中 {hits}/{len(fz)}')
```

---

## 8. 不要重复做的事 / 历史教训

- **不要按 name 做主关联键**——命名滞后会撞名（武陵·Ⅳ/·Ⅴ实证）。name 仅兜底。
- **不要拿 achv_ 与 hex 直接比相等**——它们是 md5 关系，不是相等关系。这是上一会话误判「id 不可行」的根因。
- **FZ/Warfarin 的 achv_ id 是可信主键**——两源直接相等，且 md5 后等于森空岛 hex。
- **FZ 快照要抓全**——丢页会让 owned 计数偏低、把已拥有的章误判未获得。`fetch_medal_snapshot_fz` 已加重试（最多 3 轮补齐丢页），务必确认快照达 140 再下结论。
- **suspect 启发式已删**——md5-id 精确后无需「可能已拥有」猜测；若日后发现 md5 关系被森空岛改了，再考虑回退方案。

---

## 9. 相关文档

- `docs/endfield_medal_stats.md`：需求与统计口径（§6 已更正：按 md5-id 关联）。
- `docs/handoff_medal_module.md`：F1/F2 实现交接（§4 id 陈述已加更正注）。
- `docs/handoff_medal_f2_fix.md`：上一轮 name 关联修复交接（其 id 命名空间结论已被本文推翻，顶部已加更新注）。
- `docs/skland_endfield_ui_data_inventory.md` §4.7：森空岛 `achieve` 字段结构来源。
- `docs/skland_endfield_personal_api.md`：森空岛签名 GET 与 `card/detail` 端点。
