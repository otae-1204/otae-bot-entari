# Copy the code of this checkout into a target directory with robocopy.
# Default is a dry run (robocopy /L: list only); pass -Apply to copy.
# Never copies or touches the target's runtime state: data, .runtime, output, archive, .venv,
# entari.yml and .env*. Uses /E (no purge), so files that exist only in the target are kept.
# Untested on Windows (written without a PowerShell host); review the /L listing before -Apply.
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [switch]$Apply,
    [switch]$SkipCheck
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")).Path
$TargetDir = [System.IO.Path]::GetFullPath($Target)

if ($TargetDir.TrimEnd('\') -ieq $ProjectDir.TrimEnd('\')) {
    Write-Error "Target is this checkout itself: $TargetDir"
}

$Mode = if ($Apply) { "APPLY" } else { "DRY RUN (robocopy /L, nothing is copied; add -Apply to copy)" }
Write-Host "otae-bot deploy: $ProjectDir -> $TargetDir [$Mode]" -ForegroundColor Cyan

if (-not $SkipCheck) {
    $Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
    & $Python -m compileall -q (Join-Path $ProjectDir "bot.py") (Join-Path $ProjectDir "otae_bot") (Join-Path $ProjectDir "plugins") (Join-Path $ProjectDir "scripts")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Root-level runtime directories are excluded by full path on both sides, so robocopy neither
# copies into nor deletes from them; __pycache__ etc. are excluded by name anywhere.
$ExcludeDirs = @()
foreach ($name in @("data", ".runtime", "output", "archive", ".venv", ".git")) {
    $ExcludeDirs += (Join-Path $ProjectDir $name)
    $ExcludeDirs += (Join-Path $TargetDir $name)
}
$ExcludeDirs += @("__pycache__", ".idea", ".vscode", ".claude", ".codex_preview")
$ExcludeFiles = @("*.pyc", "entari.yml", ".env", ".env.*")

$Options = @("/E", "/R:1", "/W:1", "/XD") + $ExcludeDirs + @("/XF") + $ExcludeFiles
if (-not $Apply) {
    $Options += "/L"
} elseif (-not (Test-Path $TargetDir)) {
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
}

robocopy $ProjectDir $TargetDir @Options | Out-Host
if ($LASTEXITCODE -gt 7) { exit $LASTEXITCODE }

if ($Apply) {
    Write-Host "Copied. Next, in $TargetDir : set up .venv (scripts\setup.ps1), check entari.yml / .env, then start." -ForegroundColor Green
}
exit 0
