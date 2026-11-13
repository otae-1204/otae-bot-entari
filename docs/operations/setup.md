# 首次安装

新部署按下面顺序做。旧部署（`.env` + `configs/`）不要照这页重装，改看 [切换到 v2](cutover.md)。

## 1. Python 与依赖

在项目根目录执行：

| 步骤 | Windows | Linux |
|---|---|---|
| 建虚拟环境 | `python -m venv .venv` | `python3 -m venv .venv` |
| 装依赖 | `.venv\Scripts\python.exe -m pip install -r requirements.txt` | `.venv/bin/python -m pip install -r requirements.txt` |

- `requirements.txt` 是运行所需的全部依赖，含 `vendor/hyw-frontier/` 下两个以可编辑方式安装的包。`requirements-dev.txt` 在它之上只多 `pytest` 与 `ruff`，开发机才需要。
- Windows 也可以运行 `scripts\setup.ps1`（建 `.venv`、升级 pip、装 `requirements.txt`）或 `scripts\setup.bat`（同样，依次尝试清华、阿里云镜像和默认 PyPI）。两者都不装浏览器。

## 2. 浏览器

出图用 Playwright 的 Chromium：

```
.venv\Scripts\python.exe -m playwright install chromium        # Windows
.venv/bin/python -m playwright install chromium                # Linux
.venv/bin/python -m playwright install-deps chromium           # Linux 首次，装系统依赖
```

找不到 Playwright 自带的 Chromium 时，会依次尝试系统里的 Chrome、Edge（`otae_bot/infrastructure/rendering/browser.py`）。

## 3. 配置文件

```
copy entari.example.yml entari.yml     # Windows；Linux 用 cp
copy .env.example .env
```

- `entari.yml` 放全部设置，`.env` 只放密钥。规则与各段含义见 [配置](configuration.md)。
- 至少要改 `basic.network`（或 `plugins.server`）和 `basic.superusers.qq`。接入方式见 [连接](connections.md)。
- 没填的密钥不影响启动，相关插件被调用时回复「未配置」。

## 4. 启动

| 方式 | 命令 |
|---|---|
| 直接启动 | `.venv\Scripts\python.exe bot.py` 或 `.venv\Scripts\python.exe -m otae_bot`（同一入口 `otae_bot/launcher.py`） |
| Windows 脚本 | `scripts\start.bat`：先检查同目录下是否已有 bot 进程，有则拒绝启动 |
| Windows 热重载 | `scripts\start-reload.bat`：用 watchfiles 监视 `bot.py plugins .env entari.yml`，改动后重启 |
| 停止（Windows） | `scripts\stop.bat`：结束本目录下的 bot 与 watchfiles 进程 |

工作目录必须是项目根目录：`entari.yml`、`.env`、`data/`、`.runtime/` 都按相对路径找。生产部署见 [部署](deploy.md)。

## 5. 启动后的文件

- `.runtime/entari.yml`：启动时从 `entari.yml` 复制的副本，实际加载的是它（Entari 退出时会改写所加载的文件）。不要编辑它，改 `entari.yml` 后重启。
- `.runtime/bot.lock`：进程锁。第二个实例拿不到锁，打印 `Another bot-entari instance is already running.` 后退出。锁由操作系统持有，进程结束即释放，残留的文件本身不挡启动。
- `.runtime/satori-server/`：仅 Linux、且声明了 `plugins.server` 时出现（OneBot 接入的 unix socket）。
- 缺 `entari.yml` 时 `bot.py` 打印 `entari.yml not found in the working directory.` 并退出（返回码 2）。

## 6. 成功的样子

日志里应能看到：

- `Entari version …`；
- 20 条 `loaded plugin 'plugins.<目录名>'`（`plugins/` 下 20 个插件目录；`entari_plugin_server` 等框架插件另计）；
- 每个账号上线时 `account added: …`，随后本项目的 `[runtime] account <平台>:<账号> is in N groups`（群列表取到了）。

没有任何连接时会警告 `[config] entari.yml declares no connection (basic.network / plugins.server)`。连不上的排查见 [故障排查](troubleshooting.md)。
