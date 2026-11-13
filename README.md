# otae-bot

基于 [Entari](https://github.com/ArcletProject/Entari) / Satori 的 QQ 机器人。`otae_bot/` 是启动与共享代码，
`plugins/` 下是 20 个功能插件，运行时数据在 `data/`。

用户说明书是 `/help` 发出的帮助图片（源文件 `scripts/help_pages.json`）。全部文档见 [文档索引](docs/index.md)。

## 插件

目录名就是插件的键（群功能开关、指令统计、`/功能` 参数都按它），不要重命名。

| 目录 | 主要指令 | 文档 |
|---|---|---|
| `arknights` | `/ak 绑定`、`/ak 签到`（别名 `/明日方舟`、`/arknights`） | [明日方舟签到](docs/features/arknights_signin.md) |
| `bilibilibot` | `/bili follow`、`/bili list`；B 站链接预览 | [B 站订阅](docs/features/bilibilibot.md) |
| `changelog` | `/更新日志`（`/更新`、`/changelog`、`/版本`） | [更新日志](docs/features/changelog_plugin.md) |
| `eft_helper` | 无（空包，不注册指令） | [eft_helper](docs/features/eft_helper.md) |
| `endfield` | `/ef`（`/zmd`、`/终末地`、`/endfield`） | [终末地](docs/features/endfield.md) |
| `forkout` | `/叉出去 @某人`、`/forkrank` | [叉出去](docs/features/forkout.md) |
| `group_manager` | `/功能 列表`、`/功能 关闭 <插件>` | [群内功能管理](docs/features/group_features.md) |
| `help_plugin` | `/help`、`/help <主题>` | [帮助](docs/features/help_plugin.md) |
| `hyw` | `/q <问题>`（`/hyw`、`/何意味`） | [HYW 搜索问答](docs/features/hyw_plugin.md) |
| `McModQuery` | `/mod <模组名>`、`/资料 <资料名>` | [MC 百科](docs/features/McModQuery.md) |
| `McWikiQuery` | `/wiki <内容>` | [MC Wiki](docs/features/McWikiQuery.md) |
| `mcsm` | `/mcsm`、`/mcsm <子命令>` | [MCSM 面板](docs/features/mcsm.md) |
| `minecraft_plugin` | `/ping`、`/pinglist`、`/addserver` | [Minecraft](docs/features/minecraft_plugin.md) |
| `peek` | `/peek [昵称]`（`/窥视`） | [窥视](docs/features/peek.md) |
| `radar` | `/radar`（`/智商雷达`） | [AI 智商雷达](docs/features/radar.md) |
| `request_handler` | 无指令；处理好友与入群请求 | [请求审批](docs/features/request_handler.md) |
| `status` | `/status`（`/状态`） | [运行状态与指令统计](docs/features/status_and_usage_stats.md) |
| `steamInfo` | `/steambind`、`/steaminfo`、`/steamcheck` | [Steam](docs/features/steamInfo.md) |
| `tibo_radar` | `/tibo`（`/雷达`） | [Tibo 雷达](docs/features/tibo_radar.md) |
| `usage_stats` | `/指令统计`（`/统计`） | [运行状态与指令统计](docs/features/status_and_usage_stats.md) |

## 快速开始

详细步骤见 [安装](docs/operations/setup.md)。Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。

1. 虚拟环境与依赖：

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m playwright install chromium
   ```

2. 设置：`cp entari.example.yml entari.yml`，改连接（`basic.network` 或 `plugins.server`）、
   管理员（`basic.superusers`）和各插件设置。`entari.yml` 不进 Git。
3. 密钥：`cp .env.example .env`，只填令牌、API Key、Cookie、凭据加密密钥；`entari.yml` 用
   `"${{ env.名字 :- }}"` 引用。见 [配置](docs/operations/configuration.md)。
4. 启动：`.venv/bin/python bot.py`（或 `python -m otae_bot`；Windows 可用 `scripts\start.bat`）。
5. 成功的样子：日志里加载 20 个插件、账号上线，没有 ERROR / Traceback。缺密钥的插件照常加载，
   被调用时回复「未配置」。

从旧版（`.env` + `configs/`）升级不要照上面做，按 [切换上线](docs/operations/cutover.md) 的步骤来；旧变量与新配置键的对照、`scripts/convert_config.py` 与 `scripts/migrate_platform.py` 的用法见
[配置](docs/operations/configuration.md) 与切换上线。

## 连接方式

两种，可以并用，见 [连接](docs/operations/connections.md)：

- **原生 Satori**：`entari.yml` 的 `basic.network`，每条一个 WebSocket 连接（如 LLBot 的 Satori 服务、Ir 面板）。
  走 wss 时加 `secure: true`（Ir 面板需要）。
- **QQ 经 OneBot 11 边界**：`plugins.server.adapters`，进程内的边界适配器连接 LLBot 的 OneBot 11 WebSocket，
  对内仍是 Satori。改用这种接入后平台名会变，需要先迁移数据（见切换上线）。

## 文档

- 运维：[安装](docs/operations/setup.md) · [配置](docs/operations/configuration.md) · [连接](docs/operations/connections.md) ·
  [切换上线](docs/operations/cutover.md) · [部署脚本](docs/operations/deploy.md) · [排障](docs/operations/troubleshooting.md) ·
  [实测](docs/operations/live_testing.md)
- 开发：[架构规范](docs/development/architecture_conventions.md) · [代码结构](docs/development/code_structure.md) ·
  [新增插件](docs/development/adding_a_plugin.md) · [测试](docs/development/testing.md)
- 测试命令、守卫与已知失败用例见 [测试](docs/development/testing.md)；协作约定见 [CLAUDE.md](CLAUDE.md)。

## 分支

- `main`：v2 架构（Entari 0.19.4，配置在 `entari.yml`）。
- `legacy/entari-0.17`：上一条主线（配置在 `.env` + `configs/`），只用于回滚与对照。
