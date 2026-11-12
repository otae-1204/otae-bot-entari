# otae Bot Entari

基于 Entari / Satori 的 QQ 机器人。启动与公共设施位于 `otae_bot/`，
功能插件位于 `plugins/`，可写数据在 `data/`；配置见下文「配置」。

代码结构、模块职责及扩展方式见 [代码结构说明](docs/code_structure.md)；
写代码、改代码与上线必须遵守的约定见 [架构规范](docs/architecture_conventions.md)。

End 插件的公共资料默认优先 AKEData；账号接口仍用官方，缺失资料保留兼容回退。
覆盖范围、缓存变化与实测见 [AKE 迁移记录](docs/endfield_ake_migration_execution.md)。

终末地国服活动公告提醒：`/ef 公告 订阅`，支持群聊与私聊、类型筛选、到点开始推送及开始/结束/维护提前提醒。
同一轮提醒合并为一张图片，展示活动配图、摘要、起止时间和倒计时；订阅和发送队列重启后保留。
用法和时间解析范围见 [活动公告提醒](docs/endfield_announcements.md)。

已绑定账号可用 `/ef 探索` 或 `/zmd 探索` 查看全部地区的六类收集进度，默认查询主账号。
命令用法、图片预览与数据边界见 [地区探索查询](docs/endfield_exploration.md)。

B 站动态订阅默认跳过社交抽奖，只推送游戏资讯；可用 BILI_LOTTERY_FILTER_UIDS
收窄到指定账号或关闭。判定依据与已知限制见
[动态抽奖的过滤规则](docs/bilibili_endfield_lottery_filter.md)。

`/zmd 随机`（或 `/ef random`）随机返回一条公开 Wiki 资料，无需绑定账号。
内容范围与处理规则见 [随机 Wiki](docs/endfield_random_wiki.md)。

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

## 配置

- `entari.yml`：全部结构化配置（连接、前缀、管理员、每个插件的设置）。不进 Git，从
  `entari.example.yml` 复制后修改；示例里列出了每一项、说明与默认值。
- `.env`：只放密钥（令牌、API Key、Cookie、凭据加密密钥），由 `entari.yml` 用
  `"${{ env.名字 :- }}"` 引用；真实环境变量优先于 `.env`。另有少数变量由第三方代码直接从进程
  环境读取，也写在 `.env`：`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`、`SSL_CERT_FILE` /
  `SSL_CERT_DIR`、`GOOGLE_APPLICATION_CREDENTIALS`、hyw-frontier 的 `HYW_RETRY_*`、
  `JINA_API_KEY` / `PARALLEL_API_KEY` 等（完整清单见 `.env.example`）。
- 段落：`basic:` 是 Entari 自己的（连接 `network`、`prefix`、`superusers`、`log`）；`otae:` 是
  bot 核心设置；`plugins: plugins.<插件目录名>:` 是各插件的设置；`plugins: server:` 声明
  OneBot 11 接入。
- bot 启动时把 `entari.yml` 复制到 `.runtime/entari.yml` 再加载（Entari 退出时会改写它加载的
  文件），所以 `entari.yml` 不会被程序改动。改完重启生效。缺少 `entari.yml` 时 `bot.py` 直接退出并提示。

新部署：

```powershell
copy entari.example.yml entari.yml
copy .env.example .env
# 编辑 entari.yml 的 basic.network 与各插件设置，在 .env 填密钥
```

### 从旧配置（`.env` + `configs/`）转换

旧部署的设置都在 `.env` 里，`configs/` 下还有插件 JSON。按顺序在项目根目录执行：

```powershell
.\scripts\stop.bat
# configs\ 与 entari.yml 在旧版本里被 Git 跟踪，新版本删除了它们；先挪到一旁，pull 才不会被本地改动拦住
move configs configs.legacy
move entari.yml entari.legacy.yml
git pull
# 预演：只列出每个键名及其去处，不打印任何值，不写文件
.\.venv\Scripts\python.exe -X utf8 scripts\convert_config.py
# 执行：先把 .env 备份为 .env.bak-<时间>，写出 entari.yml 与只含密钥的 .env，
# 把 configs\peek\config.json（运行时数据）迁到 data\peek\state.json
.\.venv\Scripts\python.exe -X utf8 scripts\convert_config.py --apply
```

- 脚本读取 `.env`、`configs\`（或 `configs.legacy\`）和旧 `entari.yml`（或 `entari.legacy.yml`，
  只取 `prefix`、`log`、`ignore_self_message`）。bot 运行中拒绝执行；再次执行不会再改动。
- 未识别的键与旧版就不读取的键（`MCSM_PANEL_URL`、`MCSM_API_KEY`、`MCSM_GROUP_WHITELIST`、
  `NICKNAME`、`COMMAND_START`）原样保留在新 `.env` 的标记段；没有插件读取的 configs 内容写成
  `entari.yml` 末尾的注释。确认无用后可删。
- 回滚：停止 bot，`git checkout` 回旧版本，再运行
  `.\.venv\Scripts\python.exe -X utf8 scripts\convert_config.py --rollback`（恢复最近一次备份，
  并把 `data\peek\state.json` 拷回 `configs\peek\config.json`）。

旧环境变量与新位置对照（`.env` 表示仍是 `.env` 里的密钥，`entari.yml` 用占位符引用）：

| 旧变量 | 新位置 |
|---|---|
| `SATORI_CLIENTS` | `basic.network`（每条一个 `type: ws`；令牌 → `.env` `SATORI_TOKEN_n`） |
| `ONEBOT_CLIENTS` | `plugins.server.adapters`（`endpoint`；令牌 → `.env` `ONEBOT_TOKEN_n`，键名 `access_token`） |
| `SUPERUSERS` | `basic.superusers.qq` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `.env` / `otae.llm.base_url` / `otae.llm.model` |
| `OTAE_HTTP_ASSET_*` | `otae.http_asset.*`（小写，去前缀；带账号密码的 `PROXY` 进 `.env`） |
| `OTAE_BROWSER_CONCURRENCY` | `otae.browser.concurrency` |
| `OTAE_HTTP_{API,TABLE,I18N,ASSET}_CACHE_MIB`、`OTAE_HTTP_ASSET_LARGE_ENTRY_KIB` | `otae.http_cache.{api,table,i18n,asset}_mib`、`otae.http_cache.asset_large_entry_kib` |
| `OTAE_PUBLIC_{IMAGE,TABLE}_CACHE_{PATH,MIB}` | `otae.http_cache.public_{image,table}_{path,mib}` |
| `OTAE_HELP_CACHE_PATH` | `otae.help_cache_path` |
| `ENDFIELD_CREDENTIAL_KEY` | `.env`，`plugins.endfield.credential_key` |
| `ENDFIELD_GACHA_{LAYOUT,FORWARD_ABOVE,SHOW_STANDARD,RERUN_SIDE}`、`GACHA_SHOW_STANDARD` | `plugins.endfield.gacha_{layout,forward_above,show_standard,rerun_side}` |
| `ARKNIGHTS_CREDENTIAL_KEY` | `.env`，`plugins.arknights.credential_key` |
| `HYW_CONFIG_SOURCE`、`HYW_*`（除 `HYW_API_KEY`、`HYW_RETRY_*`） | `plugins.hyw.*`（小写，去前缀；`HYW_CREDENTIALS_FILE` → `credentials_file`） |
| `HYW_API_KEY` | `.env`，`plugins.hyw.api_key` |
| `HYW_RETRY_*`、`GOOGLE_APPLICATION_CREDENTIALS`、`HTTP_PROXY`、`HTTPS_PROXY` | 仍在 `.env`（第三方代码直接读取） |
| `GROKBOT_GATEWAY_TOKEN` | `.env`，`plugins.grok_bot.gateway_token` |
| `GROKBOT_{GATEWAY_URL,AGENT_ID,TIMEOUT,MAX_PENDING,MAX_CONCURRENT,PERSONA_FILE}` | `plugins.grok_bot.{gateway_url,agent_id,timeout,max_pending,max_concurrent,persona_file}` |
| `STEAM_API_KEYS` / `STEAM_API_KEY` / `STEAM_LLM_API_KEY` | `.env`，`plugins.steamInfo.api_keys` / `api_key` / `llm_api_key` |
| `STEAM_{REQUEST_INTERVAL,DISABLE_BROADCAST_ON_STARTUP,LLM_BASE_URL,LLM_MODEL}` | `plugins.steamInfo.{request_interval,disable_broadcast_on_startup,llm_base_url,llm_model}` |
| `MCSM_DEPLOY_LARGE_PACKAGE_MB` | `plugins.mcsm.deploy_large_package_mb` |
| `MC_BROADCAST_INTERVAL` | `plugins.minecraft_plugin.broadcast_interval` |
| `BILI_SESSDATA` / `BILI_BUVID3` / `BILI_DM_IMG_LIST` / `BILI_DM_IMG_STR` / `BILI_DM_COVER_IMG_STR` | `.env`，`plugins.bilibilibot.{sessdata,buvid3,dm_img_list,dm_img_str,dm_cover_img_str}` |
| `BILI_RSSHUB_BASE_URLS` / `BILI_LOTTERY_FILTER_UIDS` / `BILI_LIVE_BATCH` | `plugins.bilibilibot.rsshub_base_urls`（列表）/ `lottery_filter_uids` / `live_batch` |
| `RADAR_*` | `plugins.radar.*`（小写，去前缀） |
| `TIBO_RADAR_*` | `plugins.tibo_radar.*`（小写，去前缀） |
| `configs/eft_helper/config.json` 的 `use_proxy` | `plugins.eft_helper.use_proxy` |
| `configs/peek/config.json` | `data/peek/state.json`（运行时数据） |

插件文档里出现的旧变量名按上表对应。

## Satori

Each `basic.network` entry in `entari.yml` creates one Satori WebSocket connection,
so one Entari backend can connect to multiple LLOneBot accounts/endpoints. Use the
Satori WebSocket port and token configured in each LLOneBot instance; when several
instances run on the same host, give them different ports. If the Satori server
exposes several logins through one endpoint, that endpoint only needs one entry.

Every online account is kept by its self id (`otae_bot/accounts/runtime.py`).
`get_bot()` returns the default one (the latest login still online); `get_bots()`
lists all of them and `get_bot_for_guild(group_id)` returns one that is in the
group, based on each account's Satori `guild.list`. Bilibili pushes use that to
send each group's notification from an account in the group, switching account
at once on "not in group" errors (see `docs/bilibili_refactor_plan.md` §5.7).

Plugins speak only Satori. Platform protocol lives in `otae_bot/boundary/`:
`plugins.server` in `entari.yml` connects LLBot's OneBot 11 WebSocket through the in-process
boundary adapter, which presents the account as Satori (quoted merged forwards
arrive expanded inside the quote; see `docs/hyw_plugin.md`).

## 平台名迁移（llonebot → qq-client）

改用 OneBot 接入（`plugins.server`）后，账号的平台名从 LLBot 原生 Satori 的 `llonebot` 变为
`qq-client`。群功能开关、指令统计、Tibo 雷达订阅、终末地公告订阅与通知回执都按平台名存储，
切换前需一次性迁移，否则这些数据不再命中。在项目根目录执行：

```powershell
.\scripts\stop.bat
# 预演：只打印各存储将改动的条数，不写文件
.\.venv\Scripts\python.exe -X utf8 scripts\migrate_platform.py --from llonebot --to qq-client
# 执行：先备份到 data\_migration_backup\<时间戳>\，再逐个存储原子写入
.\.venv\Scripts\python.exe -X utf8 scripts\migrate_platform.py --from llonebot --to qq-client --apply
```

然后在 `entari.yml` 声明 `plugins.server` 再启动。机器人运行时（持有 `.runtime\bot.lock`）
`--apply` 会拒绝执行。重复执行不会再改动；`--self-id` 可只迁移指定账号。

Grok 会话绑定默认跳过：云端 Bot 的会话标记由会话键的哈希生成，改键后已绑定的会话会报
“绑定不一致”。不迁移时，新平台名下的会话会创建新的 Grok Bot；确需迁移可加 `--include-grok`。

回滚：停止机器人，用 `--from qq-client --to llonebot --apply` 反向执行，或从备份目录拷回原文件，
再改回 `basic.network` 启动。

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
