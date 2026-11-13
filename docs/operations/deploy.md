# 部署脚本（scripts/deploy.ps1）

把当前检出的代码用 robocopy 复制到一个目标目录。用于把新代码放进**新的**并排目录；
从旧主线切换的完整步骤见 [切换上线](cutover.md)，首次安装见 [安装](setup.md)。

> `deploy.ps1` 未在 Windows 上实测。第一次使用先看预演清单。

## 用法

```powershell
# 预演（默认）：robocopy /L，只列出将复制的文件，不写任何东西
.\scripts\deploy.ps1 -Target C:\Bot\otae-bot-v2

# 确认清单后执行
.\scripts\deploy.ps1 -Target C:\Bot\otae-bot-v2 -Apply
```

| 参数 | 作用 |
|---|---|
| `-Target <目录>` | 必填。目标目录；不能是当前检出本身 |
| `-Apply` | 真正复制；不加时只预演 |
| `-SkipCheck` | 跳过复制前的 `compileall` 语法检查 |

## 不会碰的东西

- 根目录的 `data`、`.runtime`、`output`、`archive`、`.venv`、`.git`：两边都按完整路径排除，
  既不复制进去，也不会删除目标里的这些目录。
- `entari.yml`、`.env`、`.env.*`（含 `.env.example`）不复制。
- 用 `/E` 复制，不用 `/MIR`：目标里多出来的文件保留，不清理。

## 复制之后

在目标目录里：

1. `.\scripts\setup.ps1`：建 `.venv` 并安装 `requirements.txt`。线上已有 Playwright 浏览器时，
   先把 `playwright` 固定成旧环境里的版本，见 [切换上线](cutover.md)。
2. 准备 `entari.yml` 与 `.env`（[配置](configuration.md)）。
3. 启动并核对，见 [安装](setup.md) 的「启动成功的样子」。

不要把 `-Target` 指向正在运行的线上目录：旧线上进程由计划任务里的自动重载器看着
`bot.py plugins utils configs .env entari.yml`，写进去会触发重启。
