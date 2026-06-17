# otae Bot Entari

This project is a full local migration of `C:\Code\qqbot\bot-v5.0` to Entari.

The source project was copied as a read-only migration source. Runtime data,
assets, plugin code, configs, and the real `.env` were copied into this project.

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

`entari.yml` is configured from the migrated `.env` `SATORI_CLIENTS` value.
If Satori/LLOneBot is not running on the same machine, update the `host` value in
`entari.yml`.

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
