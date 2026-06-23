# Triggered every minute by Task Scheduler.
# Exits immediately if no update was requested — negligible overhead.
$RepoDir = Split-Path -Parent $PSScriptRoot
$Flag    = Join-Path $RepoDir "control\update.request"

if (-not (Test-Path $Flag)) { exit 0 }

Remove-Item $Flag -Force

$LogFile = Join-Path $RepoDir "update.log"
$Stamp   = Get-Date -Format "yyyy-MM-dd HH:mm"

Add-Content $LogFile "[$Stamp] Запрошено обновление через дашборд"

Set-Location $RepoDir

git pull origin main >> $LogFile 2>&1

$Commit = (git rev-parse --short HEAD 2>$null)
$env:GIT_COMMIT = $Commit

docker compose up -d --build api worker watcher scheduler >> $LogFile 2>&1

if ($LASTEXITCODE -eq 0) {
    Add-Content $LogFile "[$Stamp] Обновление установлено ($Commit)"
    # Notify the API so the dashboard shows the new version
    $body = "{`"commit`":`"$Commit`",`"message`":`"Обновлено до $Commit`"}"
    Invoke-RestMethod -Method Post -Uri "http://localhost:8000/admin/update/notify" `
        -ContentType "application/json" -Body $body -ErrorAction SilentlyContinue
} else {
    Add-Content $LogFile "[$Stamp] Ошибка пересборки — см. выше"
}
