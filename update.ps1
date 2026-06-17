$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $RepoDir "update.log"

Set-Location $RepoDir

git fetch origin main -q

$Local  = git rev-parse HEAD
$Remote = git rev-parse origin/main

if ($Local -eq $Remote) { exit 0 }

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Обновление: $Local -> $Remote" | Add-Content $Log

git pull origin main -q
docker compose up -d --build --no-deps api worker watcher scheduler 2>&1 | Add-Content $Log

$NewCommit = git rev-parse --short HEAD
$Body = "{`"commit`":`"$NewCommit`",`"message`":`"Обновлено с $Local до $Remote`"}"
try {
    Invoke-RestMethod -Uri "http://localhost:8000/admin/update/notify" `
        -Method Post -ContentType "application/json" -Body $Body | Out-Null
} catch {}

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] Готово ($NewCommit)" | Add-Content $Log
