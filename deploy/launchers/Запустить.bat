@echo off
chcp 65001 >nul
:: =================================================================
::  Printer's Companion — запуск на Windows (двойной клик)
::  Первый запуск: клонирует проект и собирает (~15–30 мин).
::  Повторный запуск: включает систему за ~1 мин.
::  Если в дашборде нажали «Обновить» — обновляет при следующем старте.
:: =================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set REPO_URL=https://github.com/ArtemIvanchenko/Printers-companion.git
set REPO_DIR=printers-companion
set URL=http://localhost:8000
set LOG=launch.log

echo === %date% %time% === >> %LOG%

:: 1. Docker установлен?
where docker >nul 2>&1
if errorlevel 1 (
    echo Docker не установлен. >> %LOG%
    echo.
    echo Установите Docker Desktop для Windows:
    echo https://www.docker.com/products/docker-desktop/
    echo.
    echo Или WSL2 + Docker Engine (см. README).
    start https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

:: 2. Docker запущен?
echo Запускаю Docker... >> %LOG%
docker info >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop не запущен. Запускаю...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    :wait_docker
    timeout /t 3 /nobreak >nul
    docker info >nul 2>&1
    if errorlevel 1 goto wait_docker
)
echo Docker готов. >> %LOG%

:: 3. Первый запуск: клонировать и собрать
if not exist "%REPO_DIR%\" (
    echo Первый запуск: скачиваю проект...
    git clone %REPO_URL% %REPO_DIR% >> %LOG% 2>&1
    if errorlevel 1 (
        echo ОШИБКА: не удалось скачать проект. Проверьте интернет-соединение.
        echo ОШИБКА: git clone >> %LOG%
        pause
        exit /b 1
    )

    echo Настраиваю конфигурацию...
    copy "%REPO_DIR%\.env.example" "%REPO_DIR%\.env" >nul
    powershell -Command "(Get-Content '%REPO_DIR%\.env') -replace 'C:\\\\PrinterLogs','./raw_logs' | Set-Content '%REPO_DIR%\.env'"
    powershell -Command "(Get-Content '%REPO_DIR%\.env') -replace 'LLM_PROVIDER=lmstudio','LLM_PROVIDER=null' | Set-Content '%REPO_DIR%\.env'"

    :: Добавить недостающие MinIO-бакеты если нет
    findstr /c:"MINIO_BUCKET_STLS" "%REPO_DIR%\.env" >nul || echo MINIO_BUCKET_STLS=stls >> "%REPO_DIR%\.env"
    findstr /c:"MINIO_BUCKET_MAGICS" "%REPO_DIR%\.env" >nul || echo MINIO_BUCKET_MAGICS=magics >> "%REPO_DIR%\.env"
    findstr /c:"MINIO_BUCKET_PHOTOS" "%REPO_DIR%\.env" >nul || echo MINIO_BUCKET_PHOTOS=photos >> "%REPO_DIR%\.env"
    findstr /c:"MINIO_BUCKET_DOCS" "%REPO_DIR%\.env" >nul || echo MINIO_BUCKET_DOCS=docs >> "%REPO_DIR%\.env"

    if not exist "%REPO_DIR%\raw_logs\" mkdir "%REPO_DIR%\raw_logs"

    echo Собираю образы (15–30 минут, не закрывайте окно)...
    cd %REPO_DIR%
    docker compose -f docker-compose.yml build >> ..\%LOG% 2>&1
    if errorlevel 1 (
        echo ОШИБКА: сборка образов не удалась. Подробности в launch.log.
        cd ..
        pause
        exit /b 1
    )
    cd ..
)

:: Папка для связи с дашбордом
if not exist "%REPO_DIR%\control\" mkdir "%REPO_DIR%\control"

:: 4. Применить запрошенное обновление (если нажали «Обновить» в дашборде)
if exist "%REPO_DIR%\control\update.request" (
    echo Запрошено обновление — скачиваю новую версию...
    cd %REPO_DIR%
    git pull --rebase origin main >> ..\%LOG% 2>&1
    if errorlevel 1 (
        echo ПРЕДУПРЕЖДЕНИЕ: не удалось скачать обновление. Запускаю текущую версию.
        echo WARNING: git pull failed >> ..\%LOG%
    ) else (
        echo Пересобираю образы...
        docker compose -f docker-compose.yml build >> ..\%LOG% 2>&1
        if errorlevel 1 (
            echo ОШИБКА: пересборка не удалась. Подробности в launch.log.
            cd ..
            pause
            exit /b 1
        )
    )
    del "control\update.request" >nul 2>&1
    echo Обновление завершено. >> ..\%LOG%
    cd ..
)

:: 5. Запустить систему
echo Запускаю систему...
cd %REPO_DIR%
docker compose -f docker-compose.yml up -d >> ..\%LOG% 2>&1
if errorlevel 1 (
    echo ОШИБКА: не удалось запустить. Подробности в launch.log.
    cd ..
    pause
    exit /b 1
)
cd ..

:: 6. Дождаться API
echo Жду готовности (обычно 1–2 минуты)...
set /a attempts=0
:wait_api
set /a attempts+=1
if %attempts% gtr 90 (
    echo ПРЕДУПРЕЖДЕНИЕ: API долго стартует. Откройте браузер вручную: %URL%
    goto open_browser
)
curl -fs %URL%/health >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto wait_api
)

:open_browser
echo Готово! Открываю дашборд...
start "" "%URL%"

echo.
echo Система запущена: %URL%
echo Для остановки закройте Docker Desktop или выполните:
echo   cd %REPO_DIR% ^& docker compose -f docker-compose.yml down
echo.
pause
