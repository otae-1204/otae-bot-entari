# Tibo 雷达 · X 信号会话设计（draw_x）接入说明

`plugins/tibo_radar/draw_x.py` 是 tibo_radar 插件**整套回复图片**的统一渲染器，视觉语言为「X 暗色信号会话」：纯黑头部横幅 + 几何 X 徽标 + X 蓝渐变分隔线，近黑页面背景，圆角面板统一带**左侧信号轨**（强调色竖条），状态语义色贯穿所有卡片（绿=已确认/直接相关，琥珀=窗口/疑似/间接相关，红=已否定，灰=无信号，X 蓝=链接与证据）。

插件已默认接入，**五个命令全部换新**：`/tibo 总览`、`/tibo 状态`、`/tibo 最近`、`/tibo 历史`、`/tibo 动态 [数量]`（默认 6 条合一图，显式超过 6 条时按 6 条自动分页）。

![X 设计总览卡](../output/tibo_x_overview.png)

## 接入方式

`plugins/tibo_radar/__init__.py` 只需一行导入（现已生效）：

```python
# 旧版（draw.py 海军蓝雷达 / draw_v2.py 浅色信号日志）
from .draw import AMBER, CYAN, GREEN, CardSection, event_sections, render_card

# 新版（draw_x 统一 X 设计，同时导出动态 feed 渲染器）
from .draw_x import AMBER, CYAN, GREEN, CardSection, event_sections, render_card, render_xfeed
```

保存后重启 bot（或重载插件）即可；回退时把导入改回 `.draw`，`/tibo 动态` 如需一并回退则把 `render_xfeed(...)` 调用换回原来的 `_finish_card(..., post_sections(...), ...)`。`draw.py` / `draw_v2.py` 均未改动，可随时切换。

## 公开 API（与 draw.py 完全一致）

| 名称 | 说明 |
| --- | --- |
| `render_card(title, subtitle, sections, *, page="") -> bytes` | 异步渲染通用卡片（总览/状态/最近/历史），返回 PNG 字节 |
| `render_xfeed(posts, relevance_label, *, title, subtitle, page) -> bytes` | 异步渲染 X 时间线动态卡（`/tibo 动态`），由 `draw_xfeed.py` 实现、此处重导出 |
| `CardSection(title, lines, accent)` | 分节数据结构；`post_sections` / `event_sections` 由数据模型构建分节 |
| `AMBER` / `CYAN` / `GREEN` / `RED` / `X_BLUE` 等 | 颜色常量 |

行分类规则（时间线 `#n` 行、`直接相关 · 时间 · id` 动态行、`键：值` 行、来源健康行、PT 时区行、历史统计磁贴与 24 小时柱状图）与旧版一致，`__init__.py` 生成的所有文案无需改动。

## 设计令牌

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `X_BLUE` | `(29, 155, 240)` | 品牌蓝：徽标、链接、证据、官方预告 |
| `GREEN` | `(43, 213, 118)` | 已确认完成 / 直接相关 / 窗口进行中 |
| `AMBER` | `(245, 166, 35)` | 预计窗口 / 疑似 / 间接相关 / 模型解读 |
| `RED` | `(240, 96, 109)` | 预告未兑现/已否定 |
| `GRAY` | `(128, 140, 158)` | 无重置信号 |
| `HEADER_TOP/BOT` | `(0,0,0) / (10,14,20)` | 纯黑头部横幅渐变 |
| `BG_TOP/BG_BOT` | `(8,10,14) / (14,18,26)` | 页面渐变背景 |
| `PANEL` / `PANEL_EDGE` | `(22,28,39) / (56,68,90)` | 面板与描边 |

字体沿用项目既有回退链：`assets/font/steamInfo/MiSans-*.ttf` → Windows 雅黑/黑体（显示=MiSans Bold，正文=Regular，引用/数据=Light）。画布 1080 宽，总高自动计算（上限 3900），`render_card` / `render_xfeed` 均经 `utils.image_executor.run_image_render` 在线程池渲染，须 `await`。

## 本地预览（不启动 bot）

```bash
.venv/Scripts/python.exe scripts/preview_tibo_x_all.py
```

一次渲染五张卡到 `output/`：

- `tibo_x_overview.png` — `/tibo` 总览
- `tibo_x_status.png` — `/tibo 状态`
- `tibo_x_recent.png` — `/tibo 最近`
- `tibo_x_history.png` — `/tibo 历史`
- `tibo_x_feed.png` — `/tibo 动态`（3 条示例 + 分页标记）

单看动态卡可运行 `scripts/preview_tibo_xfeed.py`（6 条示例）。

## 单元测试

`tests/test_tibo_radar.py` 覆盖 `test_x_design_card_is_valid_png`（draw_x 通用卡）与 `test_xfeed_card_is_valid_png`（动态卡），验证输出为 1080 宽 RGB PNG。运行：

```bash
.venv/Scripts/python.exe -m unittest tests.test_tibo_radar
```

## 改动清单

- 新增 `plugins/tibo_radar/draw_x.py` — 统一 X 设计渲染器（通用卡 + 重导出 `render_xfeed`）
- 新增 `plugins/tibo_radar/draw_xfeed.py` — X 时间线动态卡渲染器
- 修改 `plugins/tibo_radar/__init__.py` — 全部命令切换到 draw_x，帮助文案更新
- 新增 `scripts/preview_tibo_x_all.py` / `scripts/preview_tibo_xfeed.py` — 预览脚本
- 修改 `tests/test_tibo_radar.py` — 新增两个渲染测试

`draw.py` 与 `draw_v2.py` 保留未动，回退只需改导入。
