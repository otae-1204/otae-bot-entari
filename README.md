# otae Bot Entari

基于 Entari / Satori 的 QQ 机器人。启动与公共设施位于 `otae_bot/`，
功能插件位于 `plugins/`，可写数据仍使用原有的 `data/`、`configs/` 和资源目录。

代码结构、模块职责及扩展方式见 [代码结构说明](docs/code_structure.md)。

End 插件的公共资料默认优先 AKEData；账号接口仍用官方，缺失资料保留兼容回退。
覆盖范围、缓存变化与实测见 [AKE 迁移记录](docs/endfield_ake_migration_execution.md)。

HYW 搜索问答插件已接入：`/q 问题`、图片解释和引用追问。
配置模型后使用，详见 [HYW 配置与用法](docs/hyw_plugin.md)。

AI 智商雷达默认 `/radar` 返回米白纸色与墨绿的“模型观察手册”长图，按国内外和研发厂商集中展示全部模型与档位，带模型图标，同屏查看 IQ、趋势、样本、耗时与成本。
也支持榜、模型、对比、推荐、预警、性价比和趋势查询；布局与预览见 [新版总览设计](docs/ai_radar_matrix_ui.md)。

Cursor Grok Bot 应用可通过私网网关接入：`/grok 问题`，支持识别发送或引用的图片、转发回复图片与文件，按群聊/私聊隔离会话，默认花园多惠人设。
Grok Bot 默认关闭，需要 SuperUser 在目标群执行 `/功能 开启 grok`。
Windows 配置和连接检查见 [Grok Bot 配置与用法](docs/grok_bot_plugin.md)。

群内插件开关：`/功能 列表`、`/功能 关闭 hyw`、`/功能 开启 hyw`。
仅 SuperUser、本群管理员或群主可执行，详见 [群内功能管理](docs/group_features.md)。

明日方舟森空岛签到：`/ak 绑定`（私聊，Token 或手机号验证码）、`/ak 账号`、
`/ak 主账号 <选择器>`、`/ak 解绑 <选择器>`（私聊）、`/ak 签到 [全部|编号|昵称|UID后四位]`。
别名 `/明日方舟`、`/arknights`。账号按 QQ 隔离，凭据加密保存在独立的
`data/arknights/arknights.db`，不启用自动签到定时任务。
协议、密钥与限制见 [明日方舟森空岛签到](docs/arknights_signin.md)。

`/ak 签到` 与 `/ef 签到` 先按选择器签到本游戏；同一 QQ 还绑定了另一款游戏时，
会顺带签到另一款的全部已绑定角色，两张结果卡上下拼成一张图片（本游戏在上）。
未绑定另一款时只返回本游戏、不额外提示；本群关闭了另一款游戏时不会顺带签到。
图片发送失败会改发完整文字结果，不会重新签到。两个游戏仍各自使用自己的数据库、凭据与客户端。
详见 [双游戏签到合并](docs/unified_signin.md)。

更新日志：`/更新日志` 用图片卡片展示历史版本更新了什么，
支持目录、版本号、序号、关键词检索与统计；别名 `/更新`、`/changelog`、`/版本`。
数据来自仓库提交历史，版本号按时间段划定，详见 [更新日志插件](docs/changelog_plugin.md)。

## 分支

`main` 已于 2026-09-05 同步 `refactor/project-architecture` 的重构版本，
包含新的程序架构、End 插件的缓存优化和 AKE 数据源迁移。
切换前的旧主分支保存在 `backup/main-before-refactor-2026-09-05`。
已完成的重构、性能优化、缓存修复及独立预览分支已清理，后续更新使用 `main`。

独立 UI 预览目录 `design/endfield-preview/` 已从主分支移除，未接入正式插件。
预览源码及分支历史保存在归档标签
[`archive/endfield-ui-code-preview-2026-09-05`](https://github.com/otae-1204/otae-bot-entari/tree/archive/endfield-ui-code-preview-2026-09-05/design/endfield-preview)，
需要时可从该标签找回。

## Development and checks

在项目根目录运行（Windows 使用 `.venv\Scripts\python.exe`）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest
.venv/bin/python bot.py
```

Linux 首次运行浏览器截图时可使用
`.venv/bin/python -m playwright install-deps chromium` 安装系统依赖。
也支持 `.venv/bin/python -m otae_bot`，与 `bot.py` 使用同一启动流程。

测试基线及环境限制见 [重构验证记录](docs/refactor_validation.md)。

## Run Locally

```powershell
cd C:\Code\qqbot\bot-entari
.\scripts\setup.ps1
.\scripts\start.bat
```

Direct start:

```powershell
.\.venv\Scripts\python.exe bot.py
```

## Satori

The entrypoint reads `SATORI_CLIENTS` from `.env`. Each object in the list creates
one Satori WebSocket connection, so one Entari backend can connect to multiple
LLOneBot accounts/endpoints:

```dotenv
SATORI_CLIENTS=[{"host":"127.0.0.1","port":5500,"path":"","token":"TOKEN_1"},{"host":"127.0.0.1","port":5501,"path":"","token":"TOKEN_2"}]
```

Use the Satori WebSocket port and token configured in each LLOneBot instance.
When several instances run on the same host, give them different ports. If the
Satori server exposes several logins through one endpoint, that endpoint only
needs one list entry. `entari.yml` is not the network source for this custom
`bot.py` entrypoint.

Every online account is kept by its self id (`otae_bot/adapters/runtime.py`).
`get_bot()` returns the default one (the latest login still online); `get_bots()`
lists all of them and `get_bot_for_guild(group_id)` returns one that is in the
group, based on each account's Satori `guild.list`. Bilibili pushes use that to
send each group's notification from an account in the group, switching account
at once on "not in group" errors (see `docs/bilibili_refactor_plan.md` §5.7).

Some actions only exist in OneBot 11 (reading a quoted merged forward, native
forward sending). An entry may add `"onebot_url"` (and `"onebot_token"`) for that
instance's OneBot HTTP server; the account then uses only its own endpoint.
Entries without it fall back to `ONEBOT_HTTP_URL`. Reading a quoted message needs
neither when LLOneBot exposes its Satori passthrough `/v1/internal/onebot11/*`
(LLBot 8.2.1 does): the read goes through the receiving account's own connection.
See `docs/hyw_plugin.md`.

## Deploy To Windows Server

Default production directory:

```text
D:\Bot\BotEntari
```

Deploy from the development directory:

```powershell
cd C:\Code\qqbot\bot-entari
.\scripts\deploy.ps1 -Prod
```

On the server:

```powershell
cd D:\Bot\BotEntari
.\scripts\setup.ps1
.\scripts\start.bat
```
