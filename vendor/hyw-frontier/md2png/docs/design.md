# 首版设计与边界

## 当前定位

独立实现现代浅色 Markdown 排版，并提供 HYw 专用外框、来源和图库组件。宿主消息→PNG 已使用 `md2png.hyw`，沿用宿主鉴权、日志、取消和进程限制，不承诺与浏览器逐像素一致。

本文保留首版设计理由及安全边界；下文现代主题的历史限制不等同于当前 HYw 专用字体、公式和布局能力。自动化测试、历史快照与 golden 框架已移除，当前实现以 `src/md2png/` 和 README 为准。

## 不是旧项目换皮

只通过网页阅读 `kumoSleeping/md2png-lite` 的 README、parser.py、fonts.py、renderer.py；未 clone/pull，未复制旧项目代码、主题、图片或字体。

阅读版本：GitHub main 页面显示 `cfd308c`，在线源码为阅读当时的 main。主要观察（静态代码观察，不冒充运行验证）：

- `render_document` 使用 `_render_blocks(image=None)` 测量，然后再次执行 `_render_blocks(image=canvas)`。部分嵌套块还重复测量；几何结果不是独立、可重放的产物。
- `_text_width` 用 `textbbox` 的墨迹宽度并向上取整推进排版；墨迹边界不等于 advance width，多个片段会产生累计误差。
- `_table_column_widths` 用字符长度平方根分配列宽，且 `max(total_width, min_width * column_count)` 可能让表格比可用宽度更宽。
- `_prepare_primary_fonts` 根据整篇内容选择字体；输入别处的变化可能影响同一文字的字体选择。
- `_load_image` 在渲染路径内调用 `httpx.get` 或读取任意本地路径，并以宽泛异常捕获返回空值。把网络、解码和排版绑在一起不适合受限消息渲染。
- `_parse_table_cell` 不保存 Markdown 列对齐信息；新 AST 明确保留左右/居中对齐。

新实现仍使用成熟的通用基础库（CommonMark parser、Pillow、Pygments 等），不重新发明 Markdown 语法。这不等于复用 md2png-lite 的实现。

## 数据流与所有权

```text
api.render
 ├─ parser.parse → immutable Document / Block / Inline / Style
 ├─ Fonts + Resources + Budget（每次请求独立）
 ├─ Layout.document
 │   └─ Typography.atoms → break_units → measured lines → place
 │       └─ Scene（组件矩形、逻辑行、基线、绘制命令、诊断）
 └─ painter.paint → PNG
     └─ inspection → 分层帧、布局、叠加图
```

- AST 不持有 PIL 对象，不读取资源、不知道主题。
- Typography 按 Unicode 字素簇做字体覆盖检查；UAX #14 提供正常断行机会。只有超长不可断片段采用字素簇紧急断行，不拆组合字符/ZWJ 序列。
- 同一行同字体同样式的片段合并后用 `getlength` 测量，保留分数像素。绘制使用同一字体大小、同一文本、显式 `anchor="ls"` 基线。
- 表格先取得 min-content/max-content 度量，优先给短词保留空间，再分配余量；长标识符允许换行。列宽总和始终等于可用宽度，极端列数明确报错。
- 代码的逻辑行与视觉续行分开记录。保留空行/缩进；仅逻辑行首画行号。词法分析对完整代码进行，不逐行破坏多行 token。
- Painter 不调用换行/字宽测量函数；换行与度量归排版层所有。
- 1/2/3 倍超采样影响内部栅格，输出宽度仍为 840（或调用者指定值）。矩形使用半开布局边界，转换 Pillow 的包含式边界时减掉终点像素。
- 四阶段帧为**累计绘制帧**：background → panels → content → decoration；不是把整张基准图作为纹理。图像命令仅来自调用者提供的图片或公式栅格。

## 字体、确定性与样式

默认 macOS 预设：Arial 四种字形、Menlo 四种字形、Hiragino Sans GB W3/W6 中文回退。Linux/Windows 提供小型明确预设，但本轮只验证 macOS。

不扫描全部系统字体、不按全文评分、不按语言猜选字体、不自动下载字体。跨机器复现必须传入相同字体文件/collection index 的 manifest，并固定 lockfile 与栅格库版本。调试记录含实际使用字体 SHA-256。

主题只负责排版 token，不存业务信息。首版 `Theme` 支持颜色、字号、行高、页面边距、内边距、块间距、圆角。代码/表格/图库的部分细节指标目前仍在各自布局方法中；进入 HYw 阶段时应按实际差异提取必要组件样式，而不是现在虚构完整 CSS 引擎。

## 明确限制

- **未宣称浏览器像素一致**。FreeType/Pillow 与 Chrome/Skia/CoreText 的 hinting、抗锯齿、字形栅格和阴影核不同；必须在 HYw 阶段实测记录，而非预先归咎于抗锯齿。
- 缺字会保留布局并报告 `missing-glyph`；不通过联网 emoji PNG 掩盖。未实现彩色 emoji/ZWJ 字形正确性的跨平台保证。
- 中文回退没有斜体，报告 `fallback-italic`，不偷偷做假斜体。缺省未配置的等宽斜体也有诊断。
- 混合 RTL 段落的双向重排尚未实现，报告 `bidi-limited`。单个字素簇不拆分并不代表完整复杂文字排版已通过验收。
- 公式是可选 mathtext，不是 KaTeX/完整 LaTeX；不支持的命令保留源码并报告 `math-unsupported`，不做可能改变含义的正则替换。
- 原始 HTML 按文字显示；`<u>` 不解释为下划线。未实现 HTML/CSS、任务复选框、脚注、合并单元格、分页。
- 图库是保留比例的两列行布局，不是 HYw 瀑布布局；原始引用对象中的图库归未来 HYw 组合层所有。
- 极端窄宽度/多列表格明确失败，不剪裁、放大画布或伪造成功。
- Scene 是首版内部可检查契约，不是已冻结的跨版本序列化重放标准。

## 安全边界

- 渲染库仅接收 `assets: Mapping[str, bytes]`；不支持 HTTP/file/data URL 自动取资源。
- CLI 只有显式传入 `--asset-root` 才读取 Markdown 引用的本地文件；拒绝目录穿越、绝对路径逃逸和符号链接逃逸，网络 URL 只产生未提供诊断。
- Markdown 字符数/节点/嵌套、画布高度/超采样像素、资源数量/字节/累计解码像素、公式长度、时间与取消回调均受限。
- 时间与取消为**协作式**检查，不能中断一次正在执行的第三方字体/公式解码调用。生产仍须沿用现有受限子进程和强制超时/取消，不可把此 API 直接搬进长驻 HTTP 主线程。
- 字体 manifest 与主题是可信调用者配置，不是允许消息内容指定的权限。
- 子项目保留独立 `.venv`、`pyproject.toml` 和 `uv.lock`，源码随 Hyw Frontier 主仓库管理，不使用 Git 子模块。宿主通过 uv 本地路径依赖安装 `md2png[hyw,math]`。

## 观察实际输出

当前使用库 API、CLI 或宿主本地测试网页观察字号、换行、基线、间距、来源、公式和图库。保留渲染器自身的布局及绘制诊断能力，不携带自动化验收框架；构建或静态检查成功不能代替视觉判断。
