# Printer's Companion — update from GitHub and restart services
# Run manually: .\deploy\update.ps1
# Or schedule via Task Scheduler (see README)

param(
    [switch]$Rebuild,      # force docker image rebuild
    [switch]$NoRestart     # pull only, don't restart services
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path $PSScriptRoot -Parent

Set-Location $ProjectDir

Write-Host "=== Printer's Companion Update ===" -ForegroundColor Cyan
Write-Host "Dir: $ProjectDir"

# 1. Pull latest code
Write-Host "`n[1/3] Pulling from GitHub..." -ForegroundColor Yellow
$before = git rev-parse HEAD
git pull --rebase origin main
$after = git rev-parse HEAD

if ($before -eq $after) {
    Write-Host "Already up to date. No restart needed." -ForegroundColor Green
    if (-not $Rebuild) { exit 0 }
}

# 2. Show what changed
if ($before -ne $after) {
    Write-Host "`nChanges:" -ForegroundColor Yellow
    git log --oneline "$before..$after"
}

if ($NoRestart) {
    Write-Host "`nSkipping restart (-NoRestart flag)." -ForegroundColor Gray
    exit 0
}

# 3. Restart Docker services
Write-Host "`n[2/3] Restarting services..." -ForegroundColor Yellow

$composeArgs = @("compose", "up", "-d", "--remove-orphans")
if ($Rebuild) { $composeArgs += "--build" }

docker @composeArgs

Write-Host "`n[3/3] Done." -ForegroundColor Green
docker compose ps

# Record the update so the dashboard's "Последнее обновление" card is accurate.
# Best-effort — the API might not be up yet on a cold start; that's fine, the
# version card still reads GIT_COMMIT from the container itself.
try {
    $commit = git rev-parse --short HEAD
    $body = @{ commit = $commit; message = "Обновлено до $commit" } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri "http://localhost:8000/admin/update/notify" `
        -ContentType "application/json" -Body $body -TimeoutSec 5 -ErrorAction SilentlyContinue | Out-Null
} catch { }
