@echo off
:: ============================================================
::  Printer Log Analytics — Windows автозапуск
::  Поместите ярлык этого файла в папку автозагрузки Windows:
::    Win+R → shell:startup → создать ярлык на этот .bat
:: ============================================================

setlocal
cd /d "%~dp0\.."

:: Ждём пока Docker Desktop полностью запустится (обычно 15-30 сек)
echo Ожидание запуска Docker...
:wait_docker
docker info >nul 2>&1
if errorlevel 1 (
    timeout /t 5 /nobreak >nul
    goto wait_docker
)

echo Docker готов. Запускаем сервисы...
docker compose up -d

echo Сервисы запущены. Дашборд: http://localhost:8000
timeout /t 3 /nobreak >nul
