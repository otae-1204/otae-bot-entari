# md2png

全新实现的 Python/Pillow Markdown 排版引擎：

**Markdown AST → 字体度量与断行 → 布局/绘制指令 → PNG**

独立绘图库，源码随 Hyw Frontier 主仓库管理；不依赖 md2png-lite，不启动浏览器，不截图。默认现代浅色主题保持独立；另提供已接入宿主消息链路的 `md2png.hyw` 灰底红标 HYw 配置。不承诺与浏览器逐像素一致。

## 本地运行

```bash
cd /Users/kumo/git/Hyw-Frontier/md2png
uv sync --extra hyw --extra math --locked
```

CLI：

```bash
uv run md2png input.md -o out/answer.png \
  --asset-root out --debug out/answer
```

默认输出宽度 **840px**；`--scale 2` 为内部超采样，最终宽度不变。未传 `--asset-root` 时不会读取 Markdown 指定的图片文件，未提供图片显示占位并记录诊断。

调试产物：

```text
out/answer/
  layout.json                  # AST 对应组件、行、基线、绘制命令、字体路径/index/SHA-256
  layout-overlay.png           # 红色组件边界 / 绿色基线
  frames/00-background.png     # 累计帧：背景与阴影
  frames/01-panels.png          # + 内容面板
  frames/02-content.png         # + 文字/公式/图像
  frames/03-decoration.png      # + 线条/装饰
```

## Python API

```python
from dataclasses import replace
from pathlib import Path
from md2png import Theme, render

result = render(
    "# 标题\n\n中文与 **Markdown**。",
    width=840,
    theme=replace(Theme(), accent="#3566c9"),
    assets={},  # 由调用者提供已获准的图片 bytes
    cancelled=lambda: False,  # 协作取消；取消抛出 Cancelled
    debug=Path("out/debug"),
)
result.image.save("out/result.png")
print(result.scene.lines)
print(result.scene.diagnostics)
```

主要能力：六级标题、段落、嵌套粗斜体/删除线/链接、行内代码、紧凑/宽松列表、引用、`summary` 摘要、代码高亮与行号/续行、表格列对齐、基础公式、离线图像/两列图库。

星号强调默认启用 CJK 兼容边界：`**《书名》**等外传`、`这是**“结论”**文字`、`**结论。**接下句` 可直接加粗，`*` 斜体和 `***` 粗斜体同样适用。实现参考 [CJK-friendly Markdown 的边界方案](https://github.com/tats-u/markdown-cjk-friendly/blob/main/implementers-tips.md)，只调整解析器中的星号分隔符，不改写原文。英文、代码和转义继续使用原有规则；`_` / `__`、删除线未扩展，`** 内容 **` 这类内部带空格的标记仍按字面保留。这是星号边界扩展，不是完整的 CJK-friendly 规范实现。

`Theme` 可调整字号、行高、颜色、留白与圆角；业务卡片应在布局/绘制层组合，不要塞入 Markdown 解析器。

## 字体

普通主题的系统预设在 macOS 使用 Arial + Menlo + Hiragino Sans GB；不捆绑苹果系统字体。新增 `FontSet.bundled()`，随库原样携带 Noto Sans SC 和 Noto Sans Mono（SIL OFL，来源版本/哈希和许可证见 `src/md2png/font_assets/`），无需运行时联网或安装系统中文字体。可变字体轴和斜体配置可通过 manifest 往返保存。

跨机器精准复现请显式指定字体 manifest：

```python
import json
from dataclasses import asdict
from pathlib import Path
from md2png import FontSet

Path("fonts.json").write_text(json.dumps(asdict(FontSet.system()), indent=2))
```

```bash
uv run md2png input.md -o out/result.png --fonts fonts.json
```

每项是 `{"path": "...", "index": 0}`，`fallback`/`fallback_bold` 是数组；相对路径以 manifest 所在目录为基准。记录使用字体的 SHA-256，字体缺失明确报错。系统预设的字体可用性随操作系统安装情况变化；分发时推荐捆绑字体。不保证不同系统栅格化像素完全相同，目标平台应以实际运行效果为准。

## 构建与实际使用

```bash
uv build
```

自动化测试、测试夹具、CI 门禁、Ruff 开发配置、历史浏览器参考框架和样例生成脚本已移除。CLI 的 `--compare` 及像素差异报告也已移除；布局、字体和绘制诊断保留。需要观察效果时，直接使用 CLI、库 API 或宿主的 `8767` 本地测试网页。

宿主通过本地 `md2png[hyw,math]` 依赖调用 `render_document()`，保留协议容错、日志、鉴权和子进程限制；不采用独立 HYw 快照适配器的前缀/References 删除规则。

### HYw 渲染入口

`python -m md2png.hyw input.json -o answer.png --scale 1` 可渲染调用者提供的文档，不依赖任何历史测试目录。

`render(payload)` 复现原 HYw 快照的兼容语义；`render_document(HywDocument, reject_overflow=True)` 接收已经安全适配的内容，拒绝原版会横向裁掉的超长单词。HYw 在 macOS 默认保留 SF NS/PingFang SC/Menlo 兼容样式，非 macOS 默认使用捆绑 Noto；所有平台均可通过 `font_set=FontSet.bundled()` 或自定义 `FontSet` 显式选用便携字体。生产采用 1 倍设备像素栅格。公式采用纯 Python 的 latex2mathml／Ziamath 排版，再用已有 aggdraw／Pillow 合成，不需要 Node、浏览器、Cairo 或 TeX 安装；不宣称完整 TeX 或 KaTeX 等价。

## 已知边界

- Pillow/FreeType 不是浏览器栅格器；本阶段未做 HYw 像素验收。
- 中文斜体、缺字、复杂双向文字、缺资源、未支持公式输出显式诊断。
- 公式依赖可选 `math` extra，锁定 latex2mathml、Ziamath 和 Ziafont 适配版本；支持常见公式及矩阵嵌入分式／根号等组合，不是完整 LaTeX。自定义宏未开放；不支持或残缺的表达式返回明确诊断。`math_raster.py` 负责输入预算与 MathML 契约，`math_fonts.py` 负责同 em 字形回退，`math_svg.py` 只接收引擎生成的本地矢量，不加载外部资源。
- 原始 HTML 保留为文字；不执行 HTML/CSS，不自动取网络资源。
- 暂不支持彩色 emoji、任务复选框、脚注、合并单元格、分页；默认现代主题图库为行布局，HYw 配置另有两列布局。
- 超宽表格等情况明确失败，不静默剪裁。生产集成仍需保留受限子进程及强制超时。

设计理由、旧项目阅读结论与安全边界见 [docs/design.md](docs/design.md)。
