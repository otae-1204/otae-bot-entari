# Tibo 雷达 · X 动态卡（draw_xfeed）与通用卡（draw_scope）说明

tibo_radar 插件的回复图片由两个渲染器负责：

| 渲染器 | 函数 | 用于 |
| --- | --- | --- |
| `plugins/tibo_radar/draw_xfeed.py` | `render_xfeed` | `/tibo 动态 [数量]`（默认 6 条合一图，显式超过 6 条时按 6 条分页）；群订阅新帖推送（每页 3 条） |
| `plugins/tibo_radar/draw_scope.py` | `render_card` | `/tibo`（总览）、`/tibo 状态`、`/tibo 最近`、`/tibo 历史` |

draw_xfeed 的视觉语言为「X 暗色时间线」：纯黑头部横幅 + X 徽标，近黑页面背景，资料条（头像、名称、认证标、@handle），每条动态一张卡片并带左侧编号时间轨，依次显示英文原文、中文翻译、模型解读、关键词标签与原帖链接。状态语义色：绿=直接相关，琥珀=间接相关，灰=无信号，X 蓝=链接。

draw_scope 的通用卡为「监听站日志」风格（深石油墨底色 + 磷光琥珀 + 青色辅助色），不模仿 X 界面。`draw_scope.py` 另有 `render_scope`（动态日志卡），当前插件未调用，仅预览脚本使用。

## 接入方式

`plugins/tibo_radar/handlers.py` 的导入（现已生效）：

```python
from plugins.tibo_radar.draw_scope import AMBER, CYAN, GREEN, CardSection, event_sections, render_card
from plugins.tibo_radar.draw_xfeed import render_xfeed
```

备用的浅色渲染器 `draw_v2.py` 见 [tibo_radar_draw_v2_usage.md](tibo_radar_draw_v2_usage.md)。

## 公开 API

| 名称 | 说明 |
| --- | --- |
| `render_card(title, subtitle, sections, *, page="") -> bytes` | `draw_scope`：异步渲染通用卡片（总览/状态/最近/历史），返回 PNG 字节 |
| `render_xfeed(posts, relevance_label, *, title, subtitle, page) -> bytes` | `draw_xfeed`：异步渲染 X 时间线动态卡 |
| `CardSection(title, lines, accent)` | `draw_scope`：分节数据结构；`post_sections` / `event_sections` 由数据模型构建分节 |
| `AMBER` / `CYAN` / `GREEN` / `RED` / `X_BLUE` 等 | 颜色常量（两个模块各自定义，取值不同） |

draw_scope 的行分类规则（时间线 `#n` 行、`直接相关 · 时间 · id` 动态行、`键：值` 行、来源健康行、PT 时区行、历史统计磁贴与 24 小时柱状图）对应 `handlers.py` 生成的文案。

## 设计令牌（draw_xfeed）

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `X_BLUE` | `(29, 155, 240)` | 品牌蓝：徽标、链接 |
| `GREEN` | `(43, 213, 118)` | 直接相关 |
| `AMBER` | `(245, 166, 35)` | 间接相关 |
| `RED` | `(240, 96, 109)` | 已定义，动态卡未使用 |
| `GRAY` | `(128, 140, 158)` | 无重置信号 |
| `HEADER_TOP/BOT` | `(0,0,0) / (10,14,20)` | 纯黑头部横幅渐变 |
| `BG_TOP/BG_BOT` | `(8,10,14) / (14,18,26)` | 页面渐变背景 |
| `PANEL` / `PANEL_EDGE` | `(22,28,39) / (56,68,90)` | 面板与描边 |

字体沿用项目既有回退链：`assets/font/steamInfo/MiSans-*.ttf` → Windows 雅黑/黑体。画布 1080 宽，总高自动计算（draw_xfeed 上限 3900，draw_scope 上限 4000）。头像与 X 徽标读自 `assets/image/tibo/`。`render_card` / `render_xfeed` 均经 `otae_bot.infrastructure.rendering.executor.run_image_render` 在线程池渲染，须 `await`。

## 本地预览（不启动 bot）

```bash
.venv/bin/python scripts/preview/preview_tibo_x_all.py
```

用 draw_scope + draw_xfeed 一次渲染五张卡到 `output/`：

- `tibo_x_overview.png` — `/tibo` 总览
- `tibo_x_status.png` — `/tibo 状态`
- `tibo_x_recent.png` — `/tibo 最近`
- `tibo_x_history.png` — `/tibo 历史`
- `tibo_x_feed.png` — `/tibo 动态`

单看动态卡可运行 `scripts/preview/preview_tibo_xfeed.py`（输出 `output/tibo_xfeed_preview.png`）；`scripts/preview/preview_tibo_scope.py` 渲染 draw_scope 全部卡片（含 `render_scope`，输出 `output/tibo_scope_*.png`）。

## 单元测试

`tests/test_tibo_radar.py` 覆盖 `test_draw_card_is_valid_png`（draw_scope 通用卡）与 `test_xfeed_card_is_valid_png`（动态卡），验证输出为 1080 宽 RGB PNG；`tests/test_tibo_delivery.py` 覆盖订阅推送。运行：

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_tibo_radar.py tests/test_tibo_delivery.py
```

## 相关文件

- `plugins/tibo_radar/draw_scope.py` — 通用卡渲染器（另含未接入的 `render_scope`）
- `plugins/tibo_radar/draw_xfeed.py` — X 时间线动态卡渲染器
- `plugins/tibo_radar/draw_v2.py` — 备用浅色通用卡渲染器，未接入
- `plugins/tibo_radar/handlers.py` — 指令与订阅推送
- `scripts/preview/preview_tibo_x_all.py` / `preview_tibo_xfeed.py` / `preview_tibo_scope.py` / `preview_tibo_radar_v2.py` — 预览脚本
