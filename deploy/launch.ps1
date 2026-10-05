# Only a host launcher can update Docker. The engine owns offline fallback.
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
Write-Host "Printer's Companion: стабильные обновления и запуск" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $ProjectDir 'scripts\maintenance\update_runtime.py'))) {
    throw 'Установите релиз с общим обновлятором; старый путь git pull/rebase отключён.'
}
& (Join-Path $ProjectDir 'update.ps1') launch
if ($LASTEXITCODE -ne 0) { throw 'Запуск/восстановление не подтверждены. Рабочие данные сохранены.' }
Start-Process 'http://127.0.0.1:8000'
