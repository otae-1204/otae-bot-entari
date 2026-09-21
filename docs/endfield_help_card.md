# 终末地帮助卡片

`/zmd help`、`/ef help` 和 `/help ef` 共用
`assets/image/help/endfield.png`。运行时仍直接发送本地图片，缺图时保留原有文字帮助回退。

## 内容与布局

- `scripts/help_pages.json` 的 `endfield` 条目是图片文案来源，其他帮助主题保持原格式。
- 按「资料与工具」「我的账号」组织八个功能分区，公开关卡资料与个人挑战记录分开。
- 每条指令单独显示语法和说明，按实际功能标注「需绑定」「仅私聊」「管理权限」。
- 使用仓库内 HarmonyOS Sans 字体和纯色文字面板；每栏、每个分区均随内容增高，长文本自动换行，页脚随内容排列。
- 日常与签到默认处理全部账号；多账号详情支持交互选择。个人挑战的账号选择必须带 `账号` 关键字。

## 重新生成

在项目根目录运行：

```powershell
.venv/Scripts/python.exe scripts/render_endfield_help.py --stress --write-asset
```

Linux 可改用 `.venv/bin/python`。需要已安装项目依赖及 Playwright Chromium。
去掉 `--write-asset` 时只生成预览，不覆盖正式资源。

输出目录默认为 `output/endfield-help/`，包含 HTML、PNG 和 `validation.json`。
HTML 引用本地仓库字体，供本机预览；PNG 可独立发送。
生成过程不启动机器人，不读取账号，不依赖外部网络。

生成器在浏览器实际排版后检查文字边界、分区重叠、页脚位置和字体加载。
`--stress` 另检查超长英文指令、带特殊字符的长说明、增加条目及 600 像素单栏布局。
全部检查通过后才更新正式 PNG，避免把超框图片带入机器人。

修改帮助时需同步核对 `plugins/endfield/catalog/commands.py` 的实际命令解析与
`format_help()` 文字回退；未更改的图形文案不应引入新命令行为。
