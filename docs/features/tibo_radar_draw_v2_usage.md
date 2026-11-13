# Tibo 雷达 · 浅色图片渲染（draw_v2）使用说明

`plugins/tibo_radar/draw_v2.py` 是 tibo_radar 插件的一套备用卡片渲染器，视觉风格为浅色"信号日志"卡片（浅色纸面 + 深蓝头部雷达表盘 + 白色浮层面板）。

当前插件**未使用** draw_v2：`plugins/tibo_radar/handlers.py` 的通用卡片（总览、状态、最近、历史）由 `draw_scope.py` 的 `render_card` 渲染，`/tibo 动态` 与群订阅推送由 `draw_xfeed.py` 的 `render_xfeed` 渲染。draw_v2 只被预览脚本引用。

## 接口兼容情况

draw_v2 导出的通用卡片接口与 `draw_scope.py` 相同：

- `render_card(title, subtitle, sections, *, page="") -> bytes` — 异步渲染整张卡片，返回 PNG 字节
- `CardSection(title, lines, accent)` — 卡片分节数据结构
- `post_sections(posts, relevance_label)` / `event_sections(events, event_label)` — 由数据模型构建分节
- 颜色常量 `AMBER` / `CYAN` / `GREEN`（以及 `BG`、`PANEL`、`RED` 等）

分节内容的行分类规则（时间线 `#n` 行、`直接相关 · 时间 · id` 动态行、`键：值` 行、来源健康行、PT 时区行、历史统计磁贴与 24 小时柱状图）与 draw_scope 一致，`handlers.py` 生成的通用卡片文案都能排版。

## 改用 draw_v2

把 `plugins/tibo_radar/handlers.py` 中 draw_scope 的导入换成 draw_v2：

```python
# 当前
from plugins.tibo_radar.draw_scope import AMBER, CYAN, GREEN, CardSection, event_sections, render_card

# 改用 draw_v2
from plugins.tibo_radar.draw_v2 import AMBER, CYAN, GREEN, CardSection, event_sections, render_card
```

重启 bot 后，总览、状态、最近、历史的回复图片切换为 draw_v2 样式；`/tibo 动态` 和订阅推送仍走 `render_xfeed`，不受影响。回退时把导入改回 `draw_scope`。（未实测）

## 本地预览（不启动 bot）

仓库中提供了预览脚本，用示例数据渲染一张总览卡：

```bash
.venv/bin/python scripts/preview/preview_tibo_radar_v2.py
```

输出文件为 `output/tibo_radar_v2_preview.png`。如需调整样式（配色、字号、面板间距等），改完 `draw_v2.py` 后重新运行该脚本即可快速核对效果。

## 在其他代码中单独调用

```python
from plugins.tibo_radar.draw_v2 import CardSection, GREEN, render_card

png: bytes = await render_card(
    "标题",
    "副标题",
    [CardSection("分节标题", ["正文第一行", "键：值"], GREEN)],
    page="1/2",  # 可选，分页标记
)
```

注意 `render_card` 是异步函数（内部经 `otae_bot.infrastructure.rendering.executor.run_image_render` 在线程池中渲染），需要在事件循环中 `await`；字体回退顺序为 `assets/font/steamInfo/MiSans-*.ttf` → Windows 雅黑/黑体。
