param(
    [switch]$Prod,
    [switch]$SkipCheck,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$ProjectDir = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")
$TargetDir = if ($Prod) { "D:\Bot\BotEntari" } else { $ProjectDir }

Write-Host "otae Bot Entari deploy -> $TargetDir" -ForegroundColor Cyan

if (-not (Test-Path $TargetDir)) {
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
}

$Python = if (Test-Path (Join-Path $ProjectDir ".venv\Scripts\python.exe")) {
    Join-Path $ProjectDir ".venv\Scripts\python.exe"
} else {
    (Get-Command python -ErrorAction Stop).Source
}

& $Python -m pip install -r (Join-Path $ProjectDir "requirements.txt")

if (-not $SkipCheck) {
    & $Python -m compileall -q $ProjectDir
}

if ($Prod) {
    robocopy $ProjectDir $TargetDir /E /XD .git .venv __pycache__ .idea .vscode .claude .codex_preview /XF *.pyc /R:1 /W:1 | Out-Host
    if ($LASTEXITCODE -gt 7) {
        exit $LASTEXITCODE
    }
}

if (-not $NoRestart) {
    if ($Prod) {
        Write-Host "Start on server: cd D:\Bot\BotEntari; .\scripts\start.bat" -ForegroundColor Green
    } else {
        & (Join-Path $ProjectDir ".venv\Scripts\python.exe") (Join-Path $ProjectDir "bot.py")
    }
}
