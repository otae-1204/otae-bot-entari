# otae Bot Entari

基于 Entari / Satori 的 QQ 机器人。启动与公共设施位于 `otae_bot/`，
功能插件位于 `plugins/`，可写数据仍使用原有的 `data/`、`configs/` 和资源目录。

代码结构、模块职责及扩展方式见 [代码结构说明](docs/code_structure.md)。

End 插件的公共资料默认优先 AKEData；账号接口仍用官方，缺失资料保留兼容回退。
覆盖范围、缓存变化与实测见 [AKE 迁移记录](docs/endfield_ake_migration_execution.md)。

## 分支与独立 UI 预览

`refactor/project-architecture` 提供新的程序架构，并保留
`design/endfield-ui-code-preview` 的独立前端预览，方便在同一个检出目录中评审。
架构调整与 UI 预览是两个独立提交，预览没有接入正式插件；`main` 尚未合并这些改动。

只查看 UI 时，不需要安装机器人依赖。在仓库根目录执行：

```bash
python3 design/endfield-preview/tools/serve.py --port 8765
```

Windows 使用 `py -3` 替代 `python3`，随后打开 <http://127.0.0.1:8765/>。
页面覆盖、示例截图与测试方法见 [预览说明](design/endfield-preview/README.md)。

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
