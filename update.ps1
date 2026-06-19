$ErrorActionPreference = 'Stop'
# Enable native-command error propagation (PowerShell 7.3+)
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$RepoDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log          = Join-Path $RepoDir "update.log"
$DeployedFile = Join-Path $RepoDir ".last_deployed"

Set-Location $RepoDir

git fetch origin main -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Local        = (git rev-parse HEAD).Trim()
$Remote       = (git rev-parse origin/main).Trim()
$LastDeployed = if (Test-Path $DeployedFile) { (Get-Content $DeployedFile -Raw).Trim() } else { "" }

# Exit only if already at remote HEAD AND last deploy completed successfully.
if ($Local -eq $Remote -and $LastDeployed -eq $Remote) { exit 0 }

$From = if ($LastDeployed) { $LastDeployed } else { $Local }

if ($Local -ne $Remote) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Обновление: $($Local.Substring(0,8)) -> $($Remote.Substring(0,8))" | Add-Content $Log
    git pull origin main -q
    if ($LASTEXITCODE -ne 0) { throw "git pull failed with exit code $LASTEXITCODE" }
}

# Rebuild base image if base-layer files changed (new deps won't appear otherwise).
$Changed = git diff --name-only $From $Remote 2>$null
if ($Changed -match 'Dockerfile\.base|requirements') {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Пересборка базового образа..." | Add-Content $Log
    docker build -f Dockerfile.base -t ghcr.io/artemivanchenko/printers-companion:base . 2>&1 | Add-Content $Log
    if ($LASTEXITCODE -ne 0) { throw "docker build base failed with exit code $LASTEXITCODE" }
}

# Rebuild and restart all currently running app services (dynamic — respects active profiles).
$Running  = docker compose ps --services --filter status=running 2>$null
$Services = if ($Running) { $Running } else { @("api", "worker", "watcher", "scheduler") }

docker compose up -d --build @Services 2>&1 | Add-Content $Log
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed with exit code $LASTEXITCODE" }

$Remote | Set-Content $DeployedFile

$NewCommit = (git rev-parse --short HEAD).Trim()
$Body = "{`"commit`":`"$NewCommit`",`"message`":`"Обновлено до $($Remote.Substring(0,8))`"}"
try {
    Invoke-RestMethod -Uri "http://localhost:8000/admin/update/notify" `
        -Method Post -ContentType "application/json" -Body $Body | Out-Null
} catch {}

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Готово ($NewCommit)" | Add-Content $Log
