# 终末地「档案库收集检查」功能指南

> 状态：本地实现及预览验证完成（2026-09-10，缓存样本游戏版本 1.5.3）；部署状态需单独确认。本文档面向后续维护者，记录数据链路、
> 口径决策与已验证/未验证的边界，避免重复踩坑。

## 功能概览

| 命令 | action | 说明 |
|---|---|---|
| `/ef 档案`（别名 档案库/报告/report/reports） | `archive_view` | 档案库三页签总数 + 分类统计 + 本版本新增清单（图片卡） |
| `/ef 档案 刷新` | `archive_refresh` | 重抓 AKEData 全量 + 上一游戏版本基线并落盘，随后返回统计卡 |
| `/ef 档案 收集 [账号]`（别名 进度/progress） | `archive_progress` | 个人「已获得 docNum / 全库总数」进度卡（需绑定森空岛账号） |

设计完全对齐蚀刻章模块（`docs/endfield_medal_guide.md`）：AKEData 全量快照 × 上一版本
基线做版本新增；手动刷新 + JSON 双槽快照缓存。**关键差异**：森空岛对档案只提供
`card/detail → data.detail.base.docNum` 一个总数（无逐条明细，见
`docs/skland_endfield_ui_data_inventory.md` §11），因此**没有**「缺什么」明细查询，
个人侧只有「已获得/总数/缺 N 条」。

## 数据链路（2026-09-10 实测 1.5.3）

AKEData 档案库表（`https://data.akedata.wiki/<tableCfgPath>/`，Referer 必须
`https://cf.akedata.top/`）：

```
PrtsPage (3)          三大页签：document=中枢档案 / multi_media=音像存档 / text=见闻辑录
  └─ PrtsCategory (6) 子分类：document=中枢档案59 / report=调查报告16 / media=多媒体26
                      / paper=纸质记录276 / digital=电子档案93 / collection=藏品25
      └─ PrtsFirstLv (447 组，含 3 个空组)  firstLvId → categoryId + itemIds[]
          └─ PrtsAllItem (495 条，nar_* id)  firstLvId + type + name(text-id) + order
```

**页签归属链（核心口径决策）**：`PrtsAllItem.type` 与 `PrtsPage.pageType` 一一对应
（document/multi_media/text），实测 495 条全部可归属、无混合类型组、无孤儿条目。
因此：

- **有效范围 = PrtsAllItem 中 type 出现在 PrtsPage 的条目**（当前 495 条）：
  中枢档案 75（中枢档案 59 + 调查报告 16）、见闻辑录 394（纸质记录 276 + 电子档案
  93 + 藏品 25）、音像存档 26（多媒体 26）。
- AKEData 站上「任务文本」「地图文本」两个 tab 是由 `DialogTextTable` /
  `LevelDescTable` 合成的**虚拟分类**，其内容不在 PrtsAllItem 内——实现上不抓这两张
  表即天然排除（用户确认口径：中枢档案、见闻辑录、音像存档有效，任务/地图文本不算）。
- `PrtsReading`（补充阅读，21 组）的 `prtsId` 引用**不在** PrtsAllItem 中（实测 0/3
  命中），不属于本口径，已排除。

名字全部是 `{id: int, text: ""}` text-id（含负数 int），查 `I18nTextTable_CN`
（~18MB，与奖章共用 `akedata` namespace 的 HTTP 磁盘缓存，同版本只拉一次）。

## 版本新增 diff

- `pick_previous_game_version()`（manifest `versions[]` 跳过 latest 及同 major.minor
  revision）→ 抓其 `PrtsAllItem`（历史版本恒定，HTTP 缓存 TTL 7 天）→ 取 nar_id 集合为
  baseline（只存 id 黑名单，`data/endfield/archive_snapshot.json` 的 `baseline` 槽）。
- 新增 = current 快照中 id 不在 baseline 的条目。**id 跨版本稳定**：实测 1.4.4(462 条)
  → 1.5.3(495 条) 重叠 100%，新增 33、删除 0。
- 快照完整度校验：构建条目数 < 表条目数 80% 时拒绝落盘（`_MIN_AKEDATA_ARCHIVE_COMPLETENESS`，
  防 manifest 先于表文件可见时抓到残缺数据覆盖好快照）。

## docNum 口径（未完全验证，维护者注意）

森空岛 `card/detail → data.detail.base.docNum` 是玩家已获得档案数（单人数字，无明细）。
进度卡用 `missing = 快照总数(495) − docNum`，`docNum > 总数` 时显示口径异常提示而非负数。

**已核对（2026-09-10，真实账号实测）**：森空岛 docNum 计数范围与「三大页签全量」完全
一致——玩家 docNum=472，游戏内三页签显示 372/394 + 26/26 + 74/75，快照总数 495，
missing=23 与游戏内缺口吻合（见闻 22 + 中枢 1）。个人进度卡口径无需修正。

## 代码地图

| 职责 | 文件 |
|---|---|
| 抓取 | `plugins/endfield/providers/akedata.py`：`fetch_akedata_archive_tables` / `fetch_akedata_prts_all_item` |
| 模型 | `plugins/endfield/catalog/models.py`：`ArchiveItemView/SnapshotView/BaselineView/DiffView/ProgressView` |
| 视图构建 | `plugins/endfield/catalog/views/archives.py`：`build_akedata_archive_snapshot`（纯函数） |
| 存储 | `plugins/endfield/archives/store.py`：`ArchiveSnapshotStore`（current+baseline 双槽） |
| 编排 | `plugins/endfield/catalog/service.py`：`fetch_archive_snapshot_akedata` / `fetch_archive_baseline` / `build_archive_diff` / `build_archive_progress_view` |
| 命令 | `plugins/endfield/catalog/commands.py`：`ARCHIVE_ALIASES`（注意英文 `archive` 已被挑战历史占用，不可用作别名） |
| 处理 | `plugins/endfield/handlers.py`：`_handle_archive`（F1）/`_handle_archive_progress`（F2，走个人命令路径） |
| 渲染 | `plugins/endfield/rendering/cards.py`：`draw_archive_stats_card`（预览上限 `ARCHIVE_PREVIEW_LIMIT`）/ `draw_archive_progress_card` |
| 单测 | `tests/test_endfield_archive.py`（无网络，含分类采样、缺图降级、预览总数与分页回归） |
| 检查脚本 | `scripts/inspect_endfield_archive.py`（实拉表结构/计数/版本 diff，缓存在 `output/endfield-archive-inspect/`） |
| 预览脚本 | `scripts/render_endfield_archive_preview.py`（读取缓存表，获取公开图标，输出统计卡与使用演示数字的个人进度卡；不改运行快照） |

## 渲染备注

- 卡头突出**档案总数 + 本版本新增**；分类区按游戏入口顺序：见闻辑录（纸质记录、
  电子档案、藏品）→ 音像存档（多媒体）→ 中枢档案（中枢档案、调查报告）。截图里的
  玩家已收集数量不作为全库配置总数；1.5 本地样本全库为 495 条。
- 新增档案用**大物品 Icon + 一次名称**展示，不重复组名，不添加描述或获取条件。
  物品图来源为 `PrtsFirstLv.icon`，CDN 使用 `sprites/prts/icon/<icon>.png`；入口图
  使用 `sprites/prts/<PrtsPage.icon>.png`。路径与本地 `v3-archive.js` 的 `assetUrl`
  规则一致，最终预览所用图标已加载验证。入口白色素材在浅底上用 CSS 调深。
- 物品卡背景参考游戏截图：上深下浅的垂直灰色渐变、淡测量刻度/大框线、左上黑色
  装饰标签与独立名称栏。刻度底纹在 Icon 后方，不叠加贯穿物品的扫描竖线。
- `ARCHIVE_PREVIEW_LIMIT = 24` 控制整张图的新增预览上限；按分类轮流选取再恢复
  游戏顺序，尽量保留小分类。**完整 diff、全库数量和新增总数不受截取影响**，明确
  标注已展示/新增总数及未展示数量。设为 0 可关闭预览。预算分配后，被截取且至少
  展示一行的页签向下对齐整行；重新按子分类选取以保留小分类。完整页签和不足一行
  的小预览不裁减。上限无需用满：1.5 样本为见闻 18 + 音像 1 + 中枢 4 = 23 条。
  裁减后若有剩余预算，优先补齐其他页签的短行；1.2 样本可展示见闻 12 + 音像 6 +
  中枢 6 = 24 条。
- `ARCHIVE_GRID_COLUMNS = 6` 控制每行图标数，少量新增的页签可并排。超高时再按
  `ARCHIVE_PAGE_BUDGETS = (24, 18, 12, 6)` 分页；只获取预览范围内的物品图，分页
  复用同一份图片资源。缺图时显示占位与名称。
- 个人卡同步显示全库分类，并明确说明分类数量是全库参考、不是个人分类进度。
  无版本基线时，统计卡新增总数显示「—」，与已确认新增 0 条区分。
- 旧快照不含 `icon_url` 时，执行 `/ef 档案 刷新` 更新一次即可补齐图标。
- 本次 `CARD_RENDER_VERSION` 已更新为 `endfield-card-v49`；以后改版式继续递增。
- 复现当前预览：`.venv\Scripts\python.exe scripts/render_endfield_archive_preview.py`。
  默认读取 1.5 / 1.4 缓存表；可用 `--current` / `--previous` 指定其他已有缓存版本。
