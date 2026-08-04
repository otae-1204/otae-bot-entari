# 终末地 · 蚀刻章/奖章模块（贡献说明）

> 面向 otae-bot-entari 维护者。本模块新增两张卡片：**F1 蚀刻章统计**（版本对比 + 新增详情）与 **F2 个人缺章**（未获得 / 未升满 / 未镀层）。

---

## 1. 功能

| 卡片 | 命令 | 内容 |
|---|---|---|
| **F1 蚀刻章统计** | `/zmd 奖章` | 蚀刻章总数 + 三级（金/银/灰）分布 + 相较上一游戏版本的新增奖章详情（双列） |
| **F2 个人缺章** | `/zmd 奖章 缺章` | 玩家未获得 / 未升满 / 未镀层；未升满显示「当前档 → 升级后」左右双卡（各带图标 + 描述 + 条件） |

F1/F2 详情每条显示 **描述**（黑）+ **获取条件**（浅色），分别取自 AKEData 的 `completeDesc` 与 `conditions`，不再显示 Lv 标签。

---

## 2. 数据源

- **AKEData**（游戏客户端 TableCfg，CDN 稳定）：`AchievementTable` + `AchievementTypeTable` + `I18nTextTable_CN`。奖章名字 / 描述 / 条件 / 分类名都是 text-id，经 `i18n` 表解析。
- **森空岛 SDK**（玩家进度）：`card/detail` 的 `achieve.achieveMedals[]`，需账号绑定。只携带 `level` / `isPlated` / `initLevel`，**不含描述与条件**（这两者来自 AKEData）。

> 取数细节见 `docs/akedata_data_access_guide.md`；森空岛字段见 `docs/skland_endfield_personal_api.md`。

---

## 3. 架构（文件分工）

| 文件 | 职责 |
|---|---|
| `plugins/endfield/akedata_client.py` | `fetch_akedata_medal_tables` / `fetch_akedata_achievement_table`（历史基线） / `fetch_akedata_baseline` |
| `plugins/endfield/service.py` | `build_akedata_medal_snapshot`（聚合全量） / `build_medal_diff`（F1） / `build_medal_missing_view`（F2） / `_i18n_text` / `_tier_text` |
| `plugins/endfield/models.py` | `MedalItemView` / `MedalSnapshotView` / `MedalDiffView` / `MedalMissingView` / `MedalProgressView` / `MedalBaselineView` |
| `plugins/endfield/medal_store.py` | 快照持久化（`current` + `baseline` 两个槽，SQLite/JSON） |
| `plugins/endfield/draw.py` | `draw_medal_stats_card` / `draw_medal_missing_card` + `_medal_*` 渲染辅助（HTML→Playwright 截图） |
| `plugins/endfield/commands.py` | 奖章命令解析（`MEDAL_ALIASES` / `MEDAL_REFRESH_ALIASES` / `MEDAL_MISSING_ALIASES`） |
| `plugins/endfield/__init__.py` | `_handle_medal` / `_handle_medal_missing` |

---

## 4. 关键技术点（踩坑记录，勿推翻）

### 4.1 森空岛 `level` 对 `initLevel>1` 的章存在偏移
- 实际档位 `real_level = skland.level + initLevel - 1`。
- 全游戏仅「谷地调查者奖章」（`initLevel=2`，2→3 升级）受影响；其余可升级章 `initLevel=1` 无偏移。
- 等级横条按账号**已拥有**的 `real_level`（颜色）统计，未升满判定 `can_be_upgraded and real_level < max_level`。
- 完整数据对比见 `docs/bugfix_medal_investigator_max_tier.md`（权威）。

### 4.2 图标档位规则
- 图标 URL：`{AKEDATA_ICON_BASE}/{achvId}_lv{NN}.png`（每档一张；单档章只有 `_lv01`）。
- 未获得 → 显示 **init 档**（`_lv{initLevel}`）；未升满左卡 → **当前档**（`_lv{real_level}`），右卡 → **下一档**（`_lv{real_level+1}`）；F1 新增列表 → max 档。
- 统计区档位徽记用三档 PNG（`assets/image/endfield/medal_{gold,silver,iron}.png`，3=金 / 2=银 / 1=铁），缺图降级到 FZ 剪影 + CSS mask 改色（`medal_grade.png`）。

### 4.3 描述 ≠ 条件（两个不同字段）
| 卡片显示 | AKEData 字段 | 渲染样式 |
|---|---|---|
| 描述 | `levelInfos[L].completeDesc` | 黑 `#1e2b3c` |
| 获取条件 | `levelInfos[L].conditions[].desc`（去重合并） | 浅 `#5b6f86` |

顶层 `entry.desc` 在生产数据里**恒为空**，不要用它。条件文本本身已含数值（如「收集4份」），无需再显示 `progressToCompare` 阈值；玩家当前进度森空岛不提供。

### 4.4 JSON key round-trip
`tier_desc` / `tier_cond` 这类 `dict[int,str]` 经 `medal_snapshot.json` 存盘后 key 会变成字符串。查询统一走 `_tier_text(d, lv)`（兼容 int/str key），否则会查空。

### 4.5 关联键
`md5(achv_id) == 森空岛 achievementData.id`（实测 115/115 命中），比按名字关联可靠（不受命名滞后影响）。详见 `docs/skland_medal_id_mapping.md`。

### 4.6 共享引用陷阱
同一枚章可能同时进 F2 的 `not_maxed` 和 `not_plated`，故选档时用 `dataclasses.replace(...)` 复制副本，避免后写覆盖。

---

## 5. bot 实测步骤

前置：私聊 `/zmd 绑定` 绑定一个森空岛账号（手机号验证码）。

1. **建快照**：`/zmd 奖章 刷新` — 抓 AKEData 全量 + 上一版本基线（首次必做，约 1.5s；返回「已刷新 N 枚」）。
2. **F1**：`/zmd 奖章` — 读快照出统计卡（秒回）。检查：标题「游戏版本 X」、两行统计（总数+三级 / 可镀层·可升级·新增）、新增列表双列、每条描述+条件。
3. **F2**：`/zmd 奖章 缺章` — 用绑定账号查森空岛进度。检查：两行统计（已拥有+三级已有 / 版本总数·未获得·未升满，且 已拥有+未获得=版本总数）、未获得双列、未升满左右双卡（当前档→升级后，两图标不同）。

> 网络：AKEData / `zonai.skland.com` 直连即可；森空岛发码 `as.hypergryph.com` 若开**美国代理**会不通（push github 才需代理，两者互斥）。

---

## 6. 测试

```bash
pytest tests/test_endfield_medal.py          # 18 passed
pytest tests/test_endfield_medal.py tests/test_endfield.py tests/test_endfield_visual.py   # 全绿
```

---

## 7. 贡献整合（与 upstream 新卡片共存）

upstream 的 `draw.py` / `service.py` 后续新增了日历 / 心情 / 账号等卡片。本模块的奖章函数（`draw_medal_*` / `_medal_*` / `build_medal_*` / `fetch_akedata_*`）均为**独立新增**，与 upstream 卡片函数不重名，可共存。逐文件整合要点：

- **`models.py`**：追加 `Medal*` 几个 view，无冲突。
- **`commands.py`**：追加 `MEDAL_*_ALIASES` 与解析分支。
- **`service.py`**：`EndfieldService` 追加奖章方法（独立），共享的 `_i18n_text` / `_to_int` 等已是模块级工具。
- **`akedata_client.py`** / **`medal_store.py`**：新文件，直接加入。
- **`draw.py`**：奖章渲染函数追加；共享的 `_draw_neutral_card` / `_prepare_assets` / `_image_data_urls` / `_local_image_data_url` 复用现有。
- **`__init__.py`**：`dispatch` 追加 `medal_view` / `medal_refresh` / `medal_missing` 分支；`import` 区合并（这里是主要冲突点，逐行合）。
- **`assets/image/endfield/medal_grade*.png`**：新增资源，直接加入。

建议逐文件 `merge`，冲突基本集中在 `__init__.py` 的 import / dispatch 与 `draw.py` / `service.py` 的 import 区。

---

## 8. 依赖

- AKEData：`zonai.skland.com`（直连）
- 森空岛：`as.hypergryph.com`（发码 / 绑定）、`zonai.skland.com`（查询）
- Playwright Chromium（卡片截图）
- Python ≥3.10（<3.14）
