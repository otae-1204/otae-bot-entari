# 终末地 · 蚀刻章/奖章模块（贡献说明）

> 面向 otae-bot-entari 维护者。本模块新增两张卡片：**F1 蚀刻章统计**（版本对比 + 新增详情）与 **F2 个人缺章**（未获得 / 未升满 / 未镀层）。

---

## 1. 功能

| 卡片 | 命令 | 内容 |
|---|---|---|
| **F1 蚀刻章统计** | `/zmd 奖章` | 蚀刻章总数 + 三级（金/银/灰）分布 + 相较上一游戏版本的新增奖章详情（双列） |
| **F2 个人缺章** | `/zmd 奖章 缺章` | 玩家未获得 / 未升满 / 未镀层；未升满显示「当前档 → 升级后」左右双卡（各带图标 + 描述 + 条件）；页头右侧挂本人**奖章墙**（名片展示位的 10 枚） |

F1/F2 详情每条显示 **描述**（深色）+ **获取条件**（浅色），分别取自 AKEData 的 `completeDesc` 与 `conditions`，不再显示 Lv 标签。页头为直角墨色头带 `#20252a` + 与档案卡共用的半透明亮黄绿色底线（`--card-header-rule`，5px、`rgba(223,236,50,.5)`，`background-clip: padding-box`），不加圆角、外阴影和文字阴影。`.medal-header` 只放 F1/F2 有意共享的骨架；F1 专属样式挂在 `.medal-header--stats`（右侧 VERSION 版本块），F2 挂在 `.medal-header--missing` / `.medal-header--wall`（奖章墙与展示位计数），改一张卡不会连带另一张。正文露出中性卡的网格底，统计区与条目都是直角硬边面板：墨色左脊、1px 边、label 在上数值在下；分组标题为墨色竖条 + 3px 底线 + 炭底计数徽标。唯一语义强调色是镀层琥珀 `#a86500`，只用于可镀层/未镀层计数、「可镀层」标签、未镀层分组竖条和「镀层后」右卡。升级/镀层箭头为 CSS 三角，不用 Unicode 字符。保留原有奖章图和金/银/铁 PNG，页头不重复放置等级图标，不给章图叠加光晕；描述和条件完整换行，不添加「获取条件」「镀层条件」前缀，超出截图高度时自动分页。

---

## 2. 数据源

- **AKEData**（游戏客户端 TableCfg，CDN 稳定）：`AchievementTable` + `AchievementTypeTable` + `I18nTextTable_CN`。奖章名字 / 描述 / 条件 / 分类名都是 text-id，经 `i18n` 表解析。
- **森空岛 SDK**（玩家进度）：`card/detail` 的 `achieve.achieveMedals[]`，需账号绑定。只携带 `level` / `isPlated` / `initLevel` / 各档图标 URL，**不含描述与条件**（这两者来自 AKEData）。
- **森空岛 SDK**（奖章墙）：同一响应的 `achieve.display`，是**展示位槽位 → hex id** 的映射（`{"1": "<hex>", …, "10": "<hex>"}`），指向 `achieveMedals[].achievementData.id`。这是玩家在游戏名片上自己选的 10 枚，与「缺章」无关，仅作 F2 页头装饰。
  - **值是真 id，不是图片**：hex = `md5(achv_id)`，故能直接关联 AKEData 奖章记录（实测 10/10 命中），做「展示奖章统计」不需要任何图像识别；用 `build_medal_id_index` / `resolve_medal_wall` 还原。
  - **槽位序号即蜂窝坐标**：奇数为上排、偶数为下排，同列两格是相邻的一对（1/2、3/4 …），不是「前 5 后 5」。

> 取数细节见 `docs/akedata_data_access_guide.md`；森空岛字段见 `docs/skland_endfield_personal_api.md`。

---

## 3. 架构（文件分工）

| 文件 | 职责 |
|---|---|
| `plugins/endfield/providers/akedata.py` | `fetch_akedata_medal_tables` / `fetch_akedata_achievement_table`（历史基线） / `fetch_akedata_baseline` |
| `plugins/endfield/catalog/service.py` | `build_akedata_medal_snapshot`（聚合全量） / `build_medal_diff`（F1） / `build_medal_missing_view`（F2，含奖章墙） / `_i18n_text` / `_tier_text` |
| `plugins/endfield/catalog/models.py` | `MedalItemView` / `MedalSnapshotView` / `MedalDiffView` / `MedalMissingView` / `MedalWallItemView` / `MedalProgressView` / `MedalBaselineView` |
| `plugins/endfield/catalog/views/medals.py` | `parse_player_medal_wall`（奖章墙） / `_parse_player_medal_progress` / `_medal_icon_url` / `_clean_plated_flag` |
| `plugins/endfield/medals/store.py` | 快照持久化（`current` + `baseline` 两个槽，SQLite/JSON） |
| `plugins/endfield/rendering/cards.py` | `draw_medal_stats_card` / `draw_medal_missing_card` + `_medal_*` 渲染辅助（HTML→Playwright 截图） |
| `plugins/endfield/catalog/commands.py` | 奖章命令解析（`MEDAL_ALIASES` / `MEDAL_REFRESH_ALIASES` / `MEDAL_MISSING_ALIASES`） |
| `plugins/endfield/handlers.py` | `_handle_medal` / `_handle_medal_missing` |

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
| 描述 | `levelInfos[L].completeDesc` | 深色 `#2e3946`，15px |
| 获取条件 | `levelInfos[L].conditions[].desc`（去重合并） | 浅蓝灰 `#61738a`，14px，无前缀 |

顶层 `entry.desc` 在生产数据里**恒为空**，不要用它。条件文本本身已含数值（如「收集4份」），无需再显示 `progressToCompare` 阈值；玩家当前进度森空岛不提供。

### 4.4 JSON key round-trip
`tier_desc` / `tier_cond` 这类 `dict[int,str]` 经 `medal_snapshot.json` 存盘后 key 会变成字符串。查询统一走 `_tier_text(d, lv)`（兼容 int/str key），否则会查空。

### 4.5 关联键
`md5(achv_id) == 森空岛 achievementData.id`（实测 115/115 命中），比按名字关联可靠（不受命名滞后影响）。详见 `docs/skland_medal_id_mapping.md`。

### 4.6 共享引用陷阱
同一枚章可能同时进 F2 的 `not_maxed` 和 `not_plated`，故选档时用 `dataclasses.replace(...)` 复制副本，避免后写覆盖。

### 4.7 奖章墙（F2 页头右侧）
- `achieve.display` 是**槽位 → hex id 的映射**，按槽位排布，不能使用 `achieveMedals` 的数组顺序。
- 槽位范围为 1–10；奇数上排、偶数下排，同列两格相邻。上排 1/3/5/7/9、下排 2/4/6/8/10；列号 = `(slot-1)//2`，排号 = `(slot-1)%2`。无效槽位在截断前过滤。
- **背景为尖顶六边形**：上下为顶点、左右为直边。空槽裁形为 `polygon(50% 0,100% 25%,100% 75%,50% 100%,0 75%,0 25%)`；奖章本体不裁形，保留伸出六边形的复杂装饰。
- `MEDAL_WALL_*` 按游戏参考图约 1.11 倍章宽的列步距、0.81 倍章高的行间距校准：格框 96×111、列步距 106、行间距 90、下排右移 53。普通章面横向约留 10px，章图使用居中的 116×116 方形画布保比例，容器允许边角伸出。蜂窝背板向外扩展 12px，保留窄边沿和内嵌效果。
- **图标映射**：先用 `md5(achv_id)` 配出快照记录，再区分当前档位和镀层状态，见下表。普通图按 `max(real_level, init_level, 1)` 选档，并限制在 `max_level` 内；镀层图固定用最高档位，不能把镀层当作下一等级。

| 状态 | AKEData 高清资源 | 森空岛备用资源 |
|---|---|---|
| 普通章 | `medaliconbig/{achv_id}_lv{tier:02d}.png` | 按实际档位取 `reforge3Icon` / `reforge2Icon` / `initIcon` |
| 已镀层 | `medaliconbig/{achv_id}_lv{max_level:02d}_plating.png` | `platedIcon` |

**2026-09-30 更正**：AKEData 有高清镀层图，后缀是 `_plating`，不是 `plated`。规则直接来自 [AKEData 的 v3-table-data.js](https://www.akedata.wiki/plugin/js/v3-table-data.js) 中 achievement detail 的 `plating.icon`。实测版本 `1.5.3@10506507-7` 的 `AchievementTable` 中全部 **34/34** 个可镀层条目都能按上述规则下载并解码为 **400×400 PNG**。例如：

- 银档：`achv_fac_settlement_wuling_1_lv02_plating.png`
- 金档：`achv_bat_defeat_ruanyi_lv03_plating.png`
- 资源目录：`https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/medaliconbig/`

下载行为与空态：
- 优先加载高清图，仅对失败的格子加载 `fallback_icon_url`（森空岛同档位图）；分页复用下载结果。快照未知或无法确认支持镀层时直接用森空岛图，不猜路径。
- 已镀层但两源都缺图时显示“图标暂缺”，保留槽位和章名；不能换成普通章图或假装未设置。
- 部分展示位未设置时，用六边形深色凹槽补齐十格（比奖章本体更暗，先看到章再看到空位）；已设置但缺图的格子在同一凹槽上加虚线六边形轮廓和「图标暂缺」。游戏局部参考图已确认：放射刻线和 ENDFIELD 字样是空槽的设计底纹，不能当作杂质删除。当前用内联 SVG 重绘低对比刻线、字样和细刻度，搭配 CSS 凹面；这是参考图风格的矢量纹理，非游戏原始贴图。不再使用轮廓不贴合的旧 `medal_slot_empty.png` 截图。
- 完全没有展示奖章时用紧凑标题，不铺整面空墙。奖章墙只出现在第一页，不加额外标题；章名保留在 `title`。
- 页头保持完整的深色底，蜂窝放在右侧更深的托盘（`#171b1f`）里，托盘内是沿十格轮廓合成的深枪灰背板；背板外缘使用内阴影和细高光，形成嵌入页头的凹面。左侧标题下显示「展示位 N/10」与「已镀层 N」（无镀层章时省略）两枚徽标，只统计 1–10 的有效槽位；镀层格另带 `data-plated="1"`。`_draw_neutral_card` **会把 `extra_css` 注入 document**，此前“必须内联”的说法不正确。公共样式留在 `MEDAL_CARD_CSS`，动态几何尺寸留在元素上。

可复现预览（只用公开图标和合成玩家进度，不读账号凭据，不修改 bot 快照）：

```bash
python scripts/render_endfield_medal_wall_preview.py
# 或传入已有的公开元数据快照：
python scripts/render_endfield_medal_wall_preview.py --snapshot /path/to/medal_snapshot.json
```

输出至 `output/medal-wall-preview/`：满墙、零散三格空、连续中间四格空、末尾三格空、仅三枚、单枚、全空、缺图、长昵称九种 PNG 和 HTML，包含有复杂装饰边角的镀层章。全空沿用当前行为：隐藏奖章墙，显示紧凑标题。预览账号标注“演示数据”，不能作为真实账号查询结果。

### 4.8 长文案与分页
- 文案不限制行数；未升满标注「当前档位 / 升级后」，未镀层标注「镀层前 / 镀层后」，两侧都保留各自图标、描述与条件。
- F1/F2 优先输出一张图；超过 6144px 高度时缩小每页条目数，直到每页可完整截图。分页不拆开单枚章的前后对照，也不改动业务层已有的缺章条目上限。
- F2 各页统计始终显示完整计数，分组标题注明本页条数与该组总数；图片只加载一次，分页复用。其他渲染错误正常抛出。

---

## 5. bot 实测步骤

前置：私聊 `/zmd 绑定` 绑定一个森空岛账号（手机号验证码）。

1. **建快照**：`/zmd 奖章 刷新` — 抓 AKEData 全量 + 上一版本基线（首次必做，约 1.5s；返回「已刷新 N 枚」）。
2. **F1**：`/zmd 奖章` — 读快照出统计卡（秒回）。检查：标题「游戏版本 X」、两行统计（总数+三级 / 可镀层·可升级·新增）、新增列表双列、每条描述+条件。
3. **F2**：`/zmd 奖章 缺章` — 用绑定账号查森空岛进度。检查：两行统计（已拥有+三级已有 / 版本总数·未获得·未升满，且 已拥有+未获得=版本总数）、未获得双列、未升满左右双卡（当前档→升级后，两图标不同）、页头右侧奖章墙 10 枚蜂窝排布（金/银档与镀层图标各自正确）。

> 网络：AKEData / `zonai.skland.com` 直连即可；森空岛发码 `as.hypergryph.com` 若开**美国代理**会不通（push github 才需代理，两者互斥）。

---

## 6. 测试

```bash
pytest tests/test_endfield_medal.py          # 36 passed
pytest tests/test_endfield_medal_rendering.py   # 17 passed（分页、蜂窝坐标、空槽/缺图、页头作用域）
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
