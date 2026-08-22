# Tibo 雷达 · 新版图片渲染（draw_v2）使用说明

`plugins/tibo_radar/draw_v2.py` 是 tibo_radar 插件回复图片的新版渲染器，视觉风格为浅色"信号日志"卡片（浅色纸面 + 深蓝头部雷达表盘 + 白色浮层面板）。它与旧版 `draw.py` **公开接口完全一致**，是一个即插即用的替换件，原插件代码无需任何逻辑改动。

## 接口兼容情况

draw_v2 导出的名称与 draw.py 相同：

- `render_card(title, subtitle, sections, *, page="") -> bytes` — 异步渲染整张卡片，返回 PNG 字节
- `CardSection(title, lines, accent)` — 卡片分节数据结构
- `post_sections(posts, relevance_label)` / `event_sections(events, event_label)` — 由数据模型构建分节
- 颜色常量 `AMBER` / `CYAN` / `GREEN`（以及 `BG`、`PANEL`、`RED` 等）

分节内容的行分类规则（时间线 `#n` 行、`直接相关 · 时间 · id` 动态行、`键：值` 行、来源健康行、PT 时区行、历史统计磁贴与 24 小时柱状图）与旧版保持一致，因此 `plugins/tibo_radar/__init__.py` 生成的所有文案都能被正确排版。

## 启用新版渲染

只需修改 `plugins/tibo_radar/__init__.py` 中的一行导入（第 19 行）：

```python
# 旧版
from .draw import AMBER, CYAN, GREEN, CardSection, event_sections, post_sections, render_card

# 新版
from .draw_v2 import AMBER, CYAN, GREEN, CardSection, event_sections, post_sections, render_card
```

保存后重启 bot（或重载插件）即可，所有 `/tibo` 系列命令（总览、动态、状态、最近、历史）的回复图片都会切换为新版样式。回退时把导入改回 `.draw` 即可，两个渲染器互不干扰。

## 本地预览（不启动 bot）

仓库中提供了预览脚本，用示例数据渲染一张总览卡：

```bash
.venv/Scripts/python.exe scripts/preview_tibo_radar_v2.py
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

注意 `render_card` 是异步函数（内部经 `utils.image_executor.run_image_render` 在线程池中渲染），需要在事件循环中 `await`；字体回退顺序为 `assets/font/steamInfo/MiSans-*.ttf` → Windows 雅黑/黑体，与旧版一致。
