@echo off
chcp 65001 >nul
setlocal
set "UPDATE_PROJECT=%~dp0..\.."
if exist "%~dp0printers-companion\update.ps1" set "UPDATE_PROJECT=%~dp0printers-companion"
powershell -NoProfile -ExecutionPolicy Bypass -File "%UPDATE_PROJECT%\update.ps1" apply %*
if errorlevel 1 (
    echo Обновление не подтверждено. Данные сохранены; посмотрите сообщение выше.
    pause
    exit /b 1
)
echo Обновление подтверждено.
pause
