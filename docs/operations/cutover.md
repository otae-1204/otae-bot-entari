# 从旧版切换到 v2

旧版：分支 `legacy/entari-0.17`，配置在 `.env` + `configs/`，账号经 LLBot 原生 Satori 接入。
新版：`main`，配置在 `entari.yml` + 只含密钥的 `.env`，可选经 OneBot 11 边界接入（[连接](connections.md)）。

生产目录是 `C:\Bot\otae-bot-entari`（下称「旧目录」），新版放在同级的新目录（下称 `<新目录>`）。下文命令均为 Windows。

## 原则

- 旧目录在旧 bot 运行期间一律不写：旧版由计划任务以 watchfiles 自动重载方式运行，监视 `bot.py plugins utils configs .env entari.yml`，写入这些路径会让它重启（现场记录）。只从旧目录读、复制出来。
- 线上操作逐步执行、逐步确认，不批量跑。
- 涉及接入方式变化时先切一个账号，观察一段时间再切其余的。
- 一次上线只做一种数据迁移。推荐分两次上线：第一次只换代码与配置，账号仍走原生 Satori（跳过第 8、9 步）；稳定后再单独上线第 8、9 步，把一个账号切到 OneBot 边界。

## 第 1 步：备份

旧 bot 可以继续运行。备份 `data/`、`.env`、`configs/`、`entari.yml` 到旧目录之外。

SQLite 文件用 backup API 复制，不直接拷正在使用的文件（会漏掉 `-wal` 里的内容）。把下面的脚本存到旧目录之外（如 `C:\Bot\tools\copy_data.py`）：

```python
# 用法：python copy_data.py <源 data 目录> <目标 data 目录>
import pathlib, shutil, sqlite3, sys
src, dst = map(pathlib.Path, sys.argv[1:3])
for f in src.rglob("*"):
    if not f.is_file() or f.name.endswith(("-wal", "-shm", "-journal")):
        continue
    t = dst / f.relative_to(src)
    t.parent.mkdir(parents=True, exist_ok=True)
    with f.open("rb") as h:
        is_db = h.read(16) == b"SQLite format 3\x00"
    if is_db:
        s, d = sqlite3.connect(f), sqlite3.connect(t)
        s.backup(d); d.close(); s.close()
    else:
        shutil.copy2(f, t)
```

```
python C:\Bot\tools\copy_data.py C:\Bot\otae-bot-entari\data C:\Bot\backup-<日期>\data
```

`.env`、`configs\`、`entari.yml` 直接复制到同一备份目录。备份含凭据，只留在服务器上。

回滚：无需。

## 第 2 步：在新目录准备代码

```
git clone --branch main <仓库地址> <新目录>
```

不要在旧目录里 `git pull` / `checkout`，也不要用旧目录的 `.git` 建 worktree。

回滚：删除 `<新目录>`。

## 第 3 步：新虚拟环境

查旧环境的 Playwright 版本，新环境装同一版本，以复用本机已下载的 Chromium：

```
C:\Bot\otae-bot-entari\.venv\Scripts\python.exe -m pip show playwright
cd <新目录>
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt "playwright==<旧版本号>"
.venv\Scripts\python.exe -m playwright install chromium
```

同版本时最后一步应无需下载（未实测）。

回滚：删除 `<新目录>\.venv`。

## 第 4 步：转换配置

从旧目录复制（只读旧目录）到 `<新目录>`：`.env` → `.env`，`configs\` → `configs\`，旧 `entari.yml` → `entari.legacy.yml`。然后在 `<新目录>` 里：

```
.venv\Scripts\python.exe -X utf8 scripts\convert_config.py
.venv\Scripts\python.exe -X utf8 scripts\convert_config.py --apply
```

检查预演报告：

- 「未识别」「不再读取」的键是否确实无用；
- `SATORI_CLIENTS` 条目里被列为不再读取的字段（如 `onebot_url`、`onebot_token`）：记下来，第 9 步改用 `plugins.server` 时要用；
- `basic.network` 里连 Ir 面板的条目要有 `secure: true`（端口 443 时脚本会自动写，否则手动加）。

详细行为见 [配置](configuration.md#从旧配置转换scriptsconvert_configpy)。

回滚：在 `<新目录>` 里 `--rollback`，或删掉 `<新目录>` 的 `.env`、`entari.yml` 重新复制。

## 第 5 步：开启 LLBot 的 OneBot WebSocket

仅当本次要把账号切到 OneBot 边界时做。按 [连接](connections.md#onebot-11-边界pluginsserver) 为该账号的 LLBot 实例开正向 WebSocket（`127.0.0.1`、独立端口、令牌）。原生 Satori 先保持开启，旧 bot 仍在用。LLBot 改设置是否需要重启：未实测。

回滚：在 LLBot 里关掉这一项。

## 第 6 步：停旧 bot

1. 先禁用计划任务（任务计划程序里禁用，或 `Disable-ScheduledTask -TaskName <任务名>`），否则它会把 bot 再拉起来；
2. 在旧目录运行 `scripts\stop.bat`，它结束该目录下的 bot 与 watchfiles 进程；
3. 再运行一次，应显示 `No bot-entari process found.`

回滚：`Enable-ScheduledTask` 并启动任务，旧 bot 原样恢复。

## 第 7 步：刷新数据

旧 bot 停止后，把最新数据复制进新目录（`<新目录>\data` 此时只有转换生成的 `peek\state.json`）：

```
python C:\Bot\tools\copy_data.py C:\Bot\otae-bot-entari\data <新目录>\data
copy C:\Bot\otae-bot-entari\configs\peek\config.json <新目录>\data\peek\state.json
```

第二行用停机后的 peek 状态覆盖第 4 步时的快照（旧目录没有该文件时跳过）。

回滚：旧目录未被改动，无需处理。

## 第 8 步：迁移平台名（仅切换接入方式时）

账号从原生 Satori（`llonebot`）改走 OneBot 边界（`qq-client`）后，按平台名存的数据要改名。在 `<新目录>` 里，只迁移要切换的那个账号：

```
.venv\Scripts\python.exe -X utf8 scripts\migrate_platform.py --from llonebot --to qq-client --self-id <账号>
.venv\Scripts\python.exe -X utf8 scripts\migrate_platform.py --from llonebot --to qq-client --self-id <账号> --apply
```

- 涉及：群功能开关、指令统计、Tibo 雷达订阅、终末地公告订阅、终末地通知回执。
- 指令统计不记录账号，带 `--self-id` 时默认跳过；要一并迁移加 `--include-unscoped`。
- `--apply` 前自动备份到 `data\_migration_backup\<时间戳>\`；bot 运行时（`.runtime\bot.lock` 被占用）拒绝执行；重复执行不再改动。

回滚：`--from qq-client --to llonebot --self-id <账号> --apply`，或从备份目录拷回。

## 第 9 步：启用 OneBot 边界（仅切换接入方式时）

在 `<新目录>\entari.yml`：

- 在 `plugins:` 下加 `server.adapters`，一条对应第 5 步的 LLBot 实例；令牌写 `.env` 的 `ONEBOT_TOKEN_1`；
- 从 `basic.network` 删掉该账号的原生 Satori 条目（同一账号两种接法同时开会收到两份事件）。

Windows 上进程内服务会监听 `127.0.0.1:5140`，确认该端口空闲。

回滚：删掉 `plugins.server`，恢复 `basic.network` 条目，并按第 8 步回滚数据。

## 第 10 步：启动

在 `<新目录>` 运行 `scripts\start.bat`（前台窗口，便于观察）。

回滚：`<新目录>\scripts\stop.bat`，然后按第 6 步回滚启用旧计划任务。

## 第 11 步：验证

- 日志：20 条 `loaded plugin 'plugins.…'`；每个账号 `account added`，及 `[runtime] account <平台>:<账号> is in N groups`，平台名符合预期（`llonebot` 或 `qq-client`）；无 `Traceback`，无持续的 `Received event for unknown account`；
- 群里发 `/status`，看在线账号与连接；
- `/功能 列表` 的开关与切换前一致（平台名迁移命中）；
- 一条出图的指令能收到图片（浏览器与上传）；
- 等一次定时推送（B 站、终末地公告等）到达；
- 私聊指令与私聊推送：未实测，需专门确认。

观察一段时间再切其余账号，每个账号重复第 5、8、9、10、11 步。

## 第 12 步：固定下来

- 新建（或修改）计划任务指向 `<新目录>`，确认开机后能起来；旧任务保持禁用；
- 新版 `scripts\start-reload.bat` 监视的是 `bot.py plugins .env entari.yml`；
- 旧目录保留一段时间作回滚用，确认无需回滚后再清理。

部署与更新的日常操作见 [部署](deploy.md)。
