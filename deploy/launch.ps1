# Printer's Companion — единая точка входа: проверка обновлений + запуск дашборда.
# Вызывается из ярлыка на рабочем столе (см. create-desktop-shortcut.ps1)
# или из автозагрузки Windows (autostart-windows.bat).

param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectDir

function Write-Step($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host $msg -ForegroundColor Red
    Read-Host "Нажмите Enter для выхода"
    exit 1
}

Write-Host "=== Printer's Companion ===" -ForegroundColor Cyan

# 1. Docker Desktop должен быть запущен
Write-Step "[1/4] Проверяем Docker..."
docker info *>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker не запущен, запускаем Docker Desktop..." -ForegroundColor Yellow
    $dockerExe = "$Env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerExe) { Start-Process $dockerExe }
    $timeout = 90
    $elapsed = 0
    while ($true) {
        docker info *>$null
        if ($LASTEXITCODE -eq 0) { break }
        if ($elapsed -ge $timeout) {
            Fail "Docker не запустился за $timeout сек. Запустите Docker Desktop вручную и попробуйте снова."
        }
        Start-Sleep -Seconds 3
        $elapsed += 3
    }
}
Write-Host "Docker готов." -ForegroundColor Green

# 2. Проверяем обновления кода/compose-файлов из GitHub
Write-Step "[2/4] Проверяем обновления..."
git fetch origin main --quiet
$local  = git rev-parse HEAD
$remote = git rev-parse origin/main
if ($local -ne $remote) {
    Write-Host "Найдено обновление, скачиваем..." -ForegroundColor Yellow
    git pull --rebase origin main
} else {
    Write-Host "Уже актуальная версия." -ForegroundColor Green
}

# 3. Подтягиваем свежие образы и (пере)запускаем сервисы
Write-Step "[3/4] Запускаем сервисы..."
docker compose pull --quiet
docker compose up -d --remove-orphans
if ($LASTEXITCODE -ne 0) {
    Fail "Не удалось запустить сервисы."
}

# 4. Ждём готовности API и открываем дашборд
Write-Step "[4/4] Ждём готовности дашборда..."
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000/health" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}

if ($ready -and -not $NoBrowser) {
    Start-Process "http://localhost:8000"
    Write-Host "`nДашборд открыт: http://localhost:8000" -ForegroundColor Green
} elseif (-not $ready) {
    Write-Host "`nСервисы запускаются дольше обычного — откройте http://localhost:8000 вручную через минуту." -ForegroundColor Yellow
}

Start-Sleep -Seconds 3
