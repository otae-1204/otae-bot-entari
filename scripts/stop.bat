@echo off
title otae-Bot Entari Stop
cd /d "%~dp0.."

echo ========================================
echo   otae Bot Entari Stop
echo   %date% %time%
echo ========================================

powershell -NoProfile -ExecutionPolicy Bypass -Command "$project = (Resolve-Path '.').Path; $targets = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(\.exe)?$' -and $_.CommandLine -and ((($_.ExecutablePath -like ($project + '*')) -and ($_.CommandLine -match 'bot\.py')) -or ($_.CommandLine -like ('*' + $project + '*watchfiles.exe*bot.py*'))) }; if (-not $targets) { Write-Host '[INFO] No bot-entari process found.'; exit 0 }; Write-Host '[INFO] Stopping bot-entari processes:'; $targets | Select-Object ProcessId,CommandLine | Format-List; foreach ($p in $targets) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 1; $left = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(\.exe)?$' -and $_.CommandLine -and ((($_.ExecutablePath -like ($project + '*')) -and ($_.CommandLine -match 'bot\.py')) -or ($_.CommandLine -like ('*' + $project + '*watchfiles.exe*bot.py*'))) }; if ($left) { Write-Host '[ERROR] Some processes are still running:'; $left | Select-Object ProcessId,CommandLine | Format-List; exit 1 }; Write-Host '[INFO] Stopped.'"

exit /b %errorlevel%
