@echo off
title otae-Bot Entari
cd /d "%~dp0.."

echo ========================================
echo   otae Bot Entari
echo   %date% %time%
echo ========================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found. Run: scripts\setup.ps1
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$project = (Resolve-Path '.').Path; $running = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(\.exe)?$' -and $_.CommandLine -and ((($_.ExecutablePath -like ($project + '*')) -and ($_.CommandLine -match 'bot\.py')) -or ($_.CommandLine -like ('*' + $project + '*watchfiles.exe*bot.py*'))) }; if ($running) { Write-Host '[ERROR] bot-entari is already running:'; $running | Select-Object ProcessId,CommandLine | Format-List; exit 1 }"
if errorlevel 1 (
    echo [ERROR] Run scripts\stop.bat before starting another instance.
    pause
    exit /b 1
)

echo [INFO] Starting bot...
call ".venv\Scripts\activate.bat"
.venv\Scripts\python.exe bot.py

echo.
echo [INFO] Bot stopped
pause
