# Разовый скрипт: создаёт иконку "Printer's Companion" на рабочем столе.
# Запуск: правой кнопкой на этом файле -> "Выполнить с помощью PowerShell"

$ProjectDir = Split-Path $PSScriptRoot -Parent
$TargetBat  = Join-Path $PSScriptRoot "Printer's Companion.bat"

$WshShell  = New-Object -ComObject WScript.Shell
$Shortcut  = $WshShell.CreateShortcut("$Env:UserProfile\Desktop\Printer's Companion.lnk")
$Shortcut.TargetPath       = $TargetBat
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.IconLocation     = "$Env:SystemRoot\System32\shell32.dll,138"
$Shortcut.Description      = "Printer's Companion — проверка обновлений и запуск дашборда"
$Shortcut.Save()

Write-Host "Ярлык создан на рабочем столе: Printer's Companion" -ForegroundColor Green
