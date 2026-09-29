# 终末地 · 蚀刻章/奖章模块（贡献说明）

> 面向 otae-bot-entari 维护者。本模块新增两张卡片：**F1 蚀刻章统计**（版本对比 + 新增详情）与 **F2 个人缺章**（未获得 / 未升满 / 未镀层）。

---

## 1. 功能

| 卡片 | 命令 | 内容 |
|---|---|---|
| **F1 蚀刻章统计** | `/zmd 奖章` | 蚀刻章总数 + 三级（金/银/灰）分布 + 相较上一游戏版本的新增奖章详情（双列） |
| **F2 个人缺章** | `/zmd 奖章 缺章` | 玩家未获得 / 未升满 / 未镀层；未升满显示「当前档 → 升级后」左右双卡（各带图标 + 描述 + 条件）；页头右侧挂本人**奖章墙**（名片展示位的 10 枚） |

F1/F2 详情每条显示 **描述**（深色）+ **获取条件**（浅色），分别取自 AKEData 的 `completeDesc` 与 `conditions`，不再显示 Lv 标签。采用与档案卡片一致的深灰页头和半透明亮黄绿色底线（共用 `--card-header-rule`，保持原有 5px 厚度、颜色 `rgba(223,236,50,.5)`；页头使用 `background-clip: padding-box`，让分隔线与浅色底图混合），正文保持近白底色；新增、未获得、未升满、未镀层条目共用浅灰底色、圆角和紧凑内边距。保留原有奖章图和金/银/铁 PNG，页头不重复放置等级图标，不给章图叠加底色光晕或阴影；类别文字为 14px，描述和条件完整换行，不添加「获取条件」「镀层条件」前缀，超出截图高度时自动分页。

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
- 数据取 `achieve.display`，是**槽位 → hex id 的映射而非数组**，所以渲染顺序按槽位序号排，不能沿用 `achieveMedals` 的数组顺序。
- **槽位序号就是蜂窝坐标**（2026-09-29 拿游戏内名片截图逐格核对）：奇数为上排、偶数为下排，同列两格是相邻的一对。即上排 1/3/5/7/9、下排 2/4/6/8/10；**不是**「前 5 个一行、后 5 个一行」。列号 = `(slot-1)//2`，排号 = `(slot-1)%2`。
- **六边形是尖顶（pointy-top）**：顶点朝上、左右为尖，高 > 宽。这是最容易搞错的一处——写成平顶（`polygon(25% 0,75% 0,…)`）会整整转错 90°，看起来像药丸。正确裁形：
  `clip-path:polygon(50% 0,100% 25%,100% 75%,50% 100%,0 75%,0 25%)`。
- 几何按游戏截图实测标定（`MEDAL_WALL_*` 常量）：章径 200×231、**列步距 = 宽 × 0.96**（左右几乎相切）、**行间距 = 高 × 0.766**（上下深度咬合）。实测方法：在带刻度网格的放大图上读六边形顶点坐标。
- 章图是 **126×126 方图、六边形只占宽 86.5%**，所以用 `<img>` + `object-fit:contain` 保比例；若用 `background-size:100% 100%` 拉伸到非方形格框，六边形会被压窄变形。
- 图标**优先取 AKEData 高清图**：森空岛回的 `achievementData` 图标只有 **126×126**，页头按 2x 出图（单枚显示 96px → 实际 192px）会明显发虚；AKEData 的 `medaliconbig/{achv_id}_lv{NN}.png` 是 **400×400**，相差 3 倍多。取法见 `_wall_icon_url`。
  - 档位要按 `max(real_level, init_level)` 兜底：单档章（`initLevel == maxLevel`，如多数 3 档章）只有 `_lv{initLevel}` 一张，直接用 `level` 会拼出 404。
  - **AKEData 没有镀层图**（命名空间无 *plated* 资源、`AchievementTable` 也无对应字段），所以**已镀层的章回退森空岛 `platedIcon`**——镀层外观比清晰度更不能丢。因此同一面墙上镀层章是 126px、未镀层章是 400px，属有意取舍。
  - 快照里没有这枚章（活动已下架）时不能拼 AKEData 路径，直接回退森空岛。
- 展示位上限 10 个，**没配满的位置渲染成空槽位**（不是留白）。
- 图标复用 `_medal_icon_url`（镀层 > 3 档 > 2 档 > 初始档），档位同样要按 `real_level = level + initLevel - 1` 校正（§4.1），否则 initLevel>1 的章会取错图标。注意单档章（`max_level == init_level`，如多数 3 档章）没有 `reforge2/3Icon`，取 `initIcon` 是对的。
- 降级：展示位指向未拥有的 hex、`display` 整段缺失、或章图没下下来时都不报错——前者只少格，后者退回空槽位底图。
- **样式必须内联**：`_draw_neutral_card`（`rendering/cards.py`）虽然接了 `extra_css` 参数并插值进局部 `css` 变量，但**没有把它拼进返回的 document**，所以 `MEDAL_CARD_CSS` 实际从未生效。墙的排布/裁形/压暗全部写在元素 `style` 属性上，靠类选择器会失效（连带 `.medal-header` 的立体样式也走内联）。修这个 bug 会同时改变档案卡片外观，故未改。
- 空槽位底图：`assets/image/endfield/medal_slot_empty.png`，取自游戏「光荣之路（蚀刻章图鉴）」页面背景 `etchlist_bigbg`（本地游戏资源；**AKEData 只托管奖章图标，不托管 UI 底图**，`end-tools.fffdan.com` 也打不开，故从页面底图裁切）。
  - **裁切要点**：底图必须让六边形**恰好内切**，四周不留多余背景——否则 `object-fit:contain` 一缩放就与 `clip-path` 错位，表现为「只有某一条边贴合」。裁切框比例要与格框一致（尖顶六边形 宽:高 = 1:1.1547）：原图取 x1702–1944 / y944–1223（242×279）。改底图后用 `/tmp` 脚本叠加 clip-path 轮廓自检。
  - 卡片底色比游戏深，故用 `brightness(.72) saturate(.25)` 压暗后嵌入，保留凹面层次。
- 页头做成「墙面装饰块」：矩形 + 垂直渐变 + 内高光/内阴影 + 圆角 + 外投影，奖章墙嵌在其上。
- 卡片里**不加**「勋章展示墙」标题（需求）；章名只进 `title` 提示，不占版面。
- 只挂在**第一页**：奖章墙是名片展示态，不随缺章分页变化。

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
pytest tests/test_endfield_medal.py          # 26 passed
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
