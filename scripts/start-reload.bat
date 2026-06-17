@echo off
title otae-Bot Entari Reload
cd /d "%~dp0.."

echo ========================================
echo   otae Bot Entari Hot Reload
echo   %date% %time%
echo ========================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found. Run: scripts\setup.ps1
    pause
    exit /b 1
)

if not exist ".venv\Scripts\watchfiles.exe" (
    echo [ERROR] watchfiles not found. Run: scripts\setup.ps1
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$project = (Resolve-Path '.').Path; $running = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(\.exe)?$' -and $_.CommandLine -and ((($_.ExecutablePath -like ($project + '*')) -and ($_.CommandLine -match 'bot\.py')) -or ($_.CommandLine -like ('*' + $project + '*watchfiles.exe*bot.py*'))) }; if ($running) { Write-Host '[ERROR] bot-entari is already running:'; $running | Select-Object ProcessId,CommandLine | Format-List; exit 1 }"
if errorlevel 1 (
    echo [ERROR] Run scripts\stop.bat before starting another instance.
    pause
    exit /b 1
)

echo [INFO] Starting bot with hot reload...
echo [INFO] Watching: bot.py plugins utils configs .env entari.yml
call ".venv\Scripts\activate.bat"

".venv\Scripts\watchfiles.exe" ^
  --target-type command ^
  --ignore-paths ".venv,__pycache__,data,assets\image\temp" ^
  ".\.venv\Scripts\python.exe -u bot.py" ^
  bot.py plugins utils configs .env entari.yml

echo.
echo [INFO] Bot stopped
pause
