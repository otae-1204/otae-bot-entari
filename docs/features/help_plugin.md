# 帮助（plugins/help_plugin）

发送 `/help` 帮助图片或几段文字帮助。帮助图片是面向用户的说明书，文案源文件为 `scripts/help_pages.json`；其他功能页只写实现细节，用户能看到的用法以帮助图为准。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/help` | 主帮助图（页面 `main`） |
| `/help <主题>` | 专题帮助图，主题见下表 |
| `/help list`、`/help 列表` | 列出可用主题 |

别名：`/Help`、`/h`、`/帮助`。主题不区分大小写；未知主题时回复可用主题列表。群聊、私聊均可。

| 主题 | 页面 |
| --- | --- |
| `main`、`home` | main |
| `steam` | steam |
| `mc`、`minecraft` | minecraft |
| `bili`、`bilibili` | bili |
| `tibo`、`雷达` | tibo |
| `endfield`、`ef`、`zmd`、`终末地` | endfield |
| `ak`、`明日方舟`、`arknights`、`森空岛` | ak |
| `mcsm` | mcsm |

以下主题回复纯文字（写在 `plugins/help_plugin/handlers.py` 的 `TEXT_TOPICS` 里）：`hyw`、`q`、`何意味`；`功能`、`插件`、`plugin`；`更新`、`更新日志`、`changelog`、`版本`；`签到`、`checkin`。

## 图片怎么来

- 页面内容：`scripts/help_pages.json`（页面 id：main、endfield、ak、tibo、steam、minecraft、bili、mcsm）。
- 发送时由 `otae_bot/infrastructure/rendering/help_runtime.py` 随机挑一张插画现场渲染，缓存到 `otae.help_cache_path`（默认 `data/cache/help-cards`）；渲染不可用时回退到 `assets/image/help/<页面>.png`。
- 改文案、重新出图：见 [帮助图片生成框架](../development/help_cards.md)。
- `scripts/tibo_help_pages.json` 目前没有任何代码读取（不参与 `/help`）；`/help tibo` 用的是 `help_pages.json` 里的 `tibo` 页。

## 配置

`otae.help_cache_path`（运行时缓存目录）。插件本身无配置。

## 数据

只有上述渲染缓存。

## 已知问题

- 文字主题里的 `<问题>`、`<插件名>`、`<版本号/序号/关键词>` 发送时会被当成消息元素解析：`session.send` 收到字符串时，Entari 默认 `str_as_message: true` 用 `MessageChain.of` 按 Satori XML 解析，尖括号部分变成未知元素，经 OneBot 发出时标签本身被丢掉（原生 Satori 下的表现未实测）。
