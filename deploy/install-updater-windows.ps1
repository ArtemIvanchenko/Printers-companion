# Run once as Administrator to register the auto-update Task Scheduler task.
# After this, clicking "Обновить" in the dashboard triggers an update within ~1 minute.
#
# Usage (from the project root):
#   powershell -ExecutionPolicy Bypass -File deploy\install-updater-windows.ps1

$RepoDir = Split-Path -Parent $PSScriptRoot
$Script  = Join-Path $RepoDir "deploy\run-update-windows.ps1"

if (-not (Test-Path $Script)) {
    Write-Error "Не найден скрипт: $Script"
    exit 1
}

$Action  = New-ScheduledTaskAction `
    -Execute   "powershell.exe" `
    -Argument  "-NonInteractive -ExecutionPolicy Bypass -File `"$Script`""

# Repeat every minute, indefinitely
$Trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -Once -At (Get-Date)

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName  "PrintersCompanionUpdater" `
    -TaskPath  "\PrintersCompanion\" `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -RunLevel  Highest `
    -Force | Out-Null

Write-Host "Задача зарегистрирована. Кнопка «Обновить» в дашборде теперь работает автоматически."
Write-Host "Для удаления: Unregister-ScheduledTask -TaskName PrintersCompanionUpdater -TaskPath '\PrintersCompanion\' -Confirm:`$false"
