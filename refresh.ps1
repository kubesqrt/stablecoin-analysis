<#
  Refresh the stablecoin team dashboard.

    .\refresh.ps1            # fetch, build, open
    .\refresh.ps1 -NoOpen    # fetch and build only (use for scheduled runs)
    .\refresh.ps1 -Full      # ignore the cache and refetch everything

  The first run downloads roughly 1,600 protocol documents. Later runs send
  If-Modified-Since and mostly get empty 304s back, so they are much faster.
#>
param(
    [switch]$NoOpen,
    [switch]$Full,
    [int]$Workers = 10
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$fetchArgs = @("fetch_llama.py", "--workers", $Workers)
if ($Full) { $fetchArgs += "--no-cache" }

Write-Host "[1/3] Collecting from DefiLlama..." -ForegroundColor Cyan
python @fetchArgs
if ($LASTEXITCODE -ne 0) { throw "fetch_llama.py failed with exit code $LASTEXITCODE" }

Write-Host "[2/3] Building dashboard..." -ForegroundColor Cyan
python build.py
if ($LASTEXITCODE -ne 0) { throw "build.py failed with exit code $LASTEXITCODE" }

Write-Host "[3/3] Verifying..." -ForegroundColor Cyan
python verify.py
if ($LASTEXITCODE -ne 0) { Write-Warning "verification reported problems - see above" }

$index = Join-Path $PSScriptRoot "docs\index.html"
Write-Host "`nDashboard: $index" -ForegroundColor Green
if (-not $NoOpen) { Start-Process $index }
