$ErrorActionPreference = "Stop"
$ProjectDir = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")
Set-Location $ProjectDir

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt

Write-Host "Setup finished. Run: .\scripts\start.bat" -ForegroundColor Green
