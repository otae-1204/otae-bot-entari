# 故障排查

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动即退出：`entari.yml not found in the working directory.` | 工作目录不对，或还没建 `entari.yml`。见 [首次安装](setup.md)；旧部署用 `scripts/convert_config.py`（[配置](configuration.md)） |
| `Another bot-entari instance is already running.` | 同目录已有实例持有 `.runtime/bot.lock`。Windows 运行 `scripts\stop.bat`。锁由操作系统持有，进程退出即释放；只剩 `bot.lock` 文件不会挡启动，被挡住说明确实还有进程（含 watchfiles 拉起的子进程） |
| `convert_config.py --apply` / `migrate_platform.py --apply` 拒绝执行，提示锁被占用 | 同上，先停 bot。两个脚本的预演不检查锁 |
| `start.bat` 报 `bot-entari is already running` | 它按命令行查本目录的 `python … bot.py` 与 `watchfiles … bot.py` 进程；先 `stop.bat`。计划任务会把 bot 重新拉起时，先禁用任务 |
| 账号一直不上线、日志反复连接失败 | `basic.network`：`host`/`port`/`path` 与对端不符、令牌错、该走 wss 却没写 `secure: true`（Ir 面板需要）。`plugins.server`：`endpoint` 端口与 LLBot 的 OneBot 正向 WebSocket 不一致、`access_token` 与 LLBot 令牌不一致。见 [连接](connections.md) |
| `[config] entari.yml declares no connection` | `basic.network` 为空且没声明 `plugins.server` |
| `plugins.server is declared but entari_plugin_server failed to load` | 进程内 OneBot 服务没起来（依赖缺失、Windows 上 `127.0.0.1:5140` 被占用等），bot 直接退出 |
| 同一条消息触发两次 | 同一个 QQ 号同时用原生 Satori 与 OneBot 边界接入，删掉其中一种 |
| 指令回复「未配置」 | 缺密钥或设置。插件没配置也照常加载，用到时才提示。对照 `entari.example.yml` 补 `entari.yml` 与 `.env`，重启 |
| `[config] <段>.<键> … using the default` | 值转换不了，或占位符引用的变量未设置（不带 `:-`），已用默认值。日志不打印值 |
| 出图失败：`No browser found. Install Chrome/Edge or run: playwright install chromium` | 运行 `python -m playwright install chromium`（Linux 首次再加 `install-deps chromium`）。先找 Playwright 自带的 Chromium，再找系统 Chrome、Edge |
| 换接入方式后群开关、指令统计、订阅「丢了」 | 平台名变了（`llonebot` ↔ `qq-client`），按旧平台名存的数据不再命中。停 bot，运行 `scripts/migrate_platform.py`，见 [切换到 v2](cutover.md#第-8-步迁移平台名仅切换接入方式时) |
| 推送没到某个群 | 主动消息由 `deliver` 选发送账号：只选 `guild.list` 里有该群的在线账号。群里没有任何在线的本 bot 账号时记一条 `[delivery] no online account can send to group …`。账号刚进群时最多等一次列表刷新（发送前会按需重取）。该群关闭了对应插件时推送被静默跳过（`/功能 列表` 查看） |
| 私聊推送没到 | 优先记录订阅的账号；它不在线时用同平台最近上线的账号；平台未知时依次尝试所有在线账号。新接法下的私聊：未实测 |

## `Received event for unknown account`

satori-python 1.3.10 起按 `Login.identifier`（含 `adapter` 与 `sn`）识别账号。Satori 规范规定非登录事件里的 `login` 只带 `sn`、`user`、`platform`，于是遵守规范的服务端（Ir 面板、LLBot 原生 Satori）发来的事件对不上上线时登记的账号，被丢弃并记这条警告。`otae_bot/accounts/event_login.py` 的 `EventLoginEntari` 在同一连接内按平台、用户 ID（和 `sn`）把事件归回对应账号，所以正常情况下不应再出现；若仍持续出现，说明该连接上找不到唯一匹配的账号（如同一连接上同一用户 ID 有多个登录），记下日志反馈。

## 已知问题

以下见对应功能页：

- `/steamhelp` 没有回复；`/steaminfo @某人` 忽略 @：见 [steamInfo](../features/steamInfo.md)。
- `/mcsm admin add @某人` 忽略 @：见 [mcsm](../features/mcsm.md)。
- `/bi` 只给数字；`/pinglist` 昵称列表原样输出：见 [minecraft_plugin](../features/minecraft_plugin.md)。
- `/mod <名字> <数量>` 匹配不到：见 [McModQuery](../features/McModQuery.md)。
- 帮助文字里的 `<…>` 被当作 XML 元素解析，显示不全：见对应功能页。
