# Printer's Companion — launcher
# Checks for updates, starts services, opens dashboard.

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$host.UI.RawUI.WindowTitle = "Printer's Companion"

Set-Location $ProjectDir

function Write-Step([string]$msg, [string]$color = 'Cyan') {
    Write-Host "`n$msg" -ForegroundColor $color
}

Write-Host @"
  ____  ____  _
 |  _ \/ ___|| |      Printer's Companion
 | |_) \___ \| |      Launcher
 |  __/ ___) | |___
 |_|   |____/|_____|
"@ -ForegroundColor Cyan

# 1. Docker check
Write-Step "[1/4] Checking Docker..." Yellow
$dockerOk = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $dockerOk = $true; break }
    } catch {}
    if ($i -eq 0) {
        Write-Host "  Docker is not responding, waiting for Docker Desktop..." -ForegroundColor Gray
        try { Start-Process "Docker Desktop" } catch {}
    }
    Write-Host "  [$([int]($i*2))s] waiting..." -ForegroundColor DarkGray
    Start-Sleep 2
}
if (-not $dockerOk) {
    Write-Host "  ERROR: Docker did not start within 60 seconds." -ForegroundColor Red
    Read-Host "`nPress Enter to exit"
    exit 1
}
Write-Host "  Docker is ready." -ForegroundColor Green

# 2. Check for updates
Write-Step "[2/4] Checking for updates..." Yellow
try {
    & "$ProjectDir\update.ps1"
    Write-Host "  Update complete." -ForegroundColor Green
} catch {
    Write-Host "  Warning: update failed: $_" -ForegroundColor Yellow
    Write-Host "  Continuing with current version..." -ForegroundColor DarkGray
}

# 3. Start services
Write-Step "[3/4] Starting services..." Yellow
$running = docker compose ps --services --filter status=running 2>$null
$allServices = @("api", "worker", "watcher", "scheduler")
$missingServices = $allServices | Where-Object { $running -notcontains $_ }

if ($missingServices) {
    Write-Host "  Starting: $($missingServices -join ', ')" -ForegroundColor DarkGray
    docker compose up -d --remove-orphans 2>&1 | ForEach-Object {
        if ($_ -match 'Started|Created|Running') { Write-Host "  $_" -ForegroundColor DarkGray }
    }
} else {
    Write-Host "  All services already running." -ForegroundColor Green
}

# 4. Wait for API
Write-Step "[4/4] Waiting for API..." Yellow
$url = "http://localhost:8000/health"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Write-Host "  [$([int]($i*2))s] waiting for API..." -ForegroundColor DarkGray
    Start-Sleep 2
}

if (-not $ready) {
    Write-Host "  Warning: API did not respond in 2 minutes. Opening dashboard anyway." -ForegroundColor Yellow
}

# Open dashboard
Write-Host "`nOpening dashboard..." -ForegroundColor Cyan
Start-Process "http://localhost:8000"

Write-Host "`n  Done! Dashboard opened in browser." -ForegroundColor Green
docker compose ps --format "table {{.Name}}`t{{.Status}}" 2>$null

Start-Sleep 3