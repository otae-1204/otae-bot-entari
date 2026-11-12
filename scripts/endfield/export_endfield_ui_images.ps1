[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RawRoot,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,

    [string]$AnimeStudioRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.runtime\AnimeStudio-akef')
)

$ErrorActionPreference = 'Stop'

$rawPath = (Resolve-Path -LiteralPath $RawRoot).Path
$toolPath = (Resolve-Path -LiteralPath $AnimeStudioRoot).Path
$project = Join-Path $toolPath 'AnimeStudio.CLI\AnimeStudio.CLI.csproj'
$executable = Join-Path $toolPath 'AnimeStudio.CLI\bin\Release\net9.0-windows\AnimeStudio.CLI.exe'

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    dotnet build $project -c Release -f net9.0-windows
    if ($LASTEXITCODE -ne 0) {
        throw "AnimeStudio.CLI build failed with exit code $LASTEXITCODE"
    }
}

$outputPath = [System.IO.Path]::GetFullPath($OutputRoot)
[System.IO.Directory]::CreateDirectory($outputPath) | Out-Null

& $executable $rawPath $outputPath `
    --game ArknightsEndfield `
    --types Texture2D `
    --group_assets ByContainer

if ($LASTEXITCODE -ne 0) {
    throw "AnimeStudio.CLI export failed with exit code $LASTEXITCODE"
}

$images = Get-ChildItem -LiteralPath $outputPath -Recurse -File -Filter '*.png'
$totalBytes = ($images | Measure-Object -Property Length -Sum).Sum
if ($null -eq $totalBytes) {
    $totalBytes = 0
}

Write-Host ("Exported {0} PNG files ({1:N2} MiB) to {2}" -f $images.Count, ($totalBytes / 1MB), $outputPath)
