@echo off
chcp 65001 >nul
:: =================================================================
::  Printer's Companion — запуск на Windows (двойной клик)
::  Первый запуск: клонирует проект и собирает (~15–30 мин).
::  Повторный запуск: проверяет обновления и включает систему за ~1 мин.
::  Проверка обновлений — ТОЛЬКО здесь, при запуске этого файла.
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
set JUST_BUILT=0
if not exist "%REPO_DIR%\" (
    set JUST_BUILT=1
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
    call :build_identity
    docker compose -f docker-compose.yml build >> ..\%LOG% 2>&1
    if errorlevel 1 (
        echo ОШИБКА: сборка образов не удалась. Подробности в launch.log.
        cd ..
        pause
        exit /b 1
    )
    cd ..
)

:: 4. Проверить обновления (каждый запуск — кроме только что собранного первого)
if %JUST_BUILT%==0 (
    if not exist "%REPO_DIR%\scripts\maintenance\update_runtime.py" (
        echo Требуется однократное обновление старой установки до релиза с новым обновлятором.
        pause
        exit /b 1
    )
    powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO_DIR%\update.ps1" launch
    if errorlevel 1 (
        echo Запуск или восстановление не подтверждены. Данные сохранены.
        pause
        exit /b 1
    )
    start %URL%
    exit /b 0
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
    echo ОШИБКА: готовность API и хранилища не подтверждена. Проверьте launch.log.
    pause
    exit /b 1
)
curl --connect-timeout 2 --max-time 15 -fs %URL%/health/ready >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto wait_api
)

:open_browser
echo API и хранилище доступны. Фоновые обработчики этой проверкой не проверены.
start "" "%URL%"

echo.
echo Система запущена: %URL%
echo Для остановки закройте Docker Desktop или выполните:
echo   cd %REPO_DIR% ^& docker compose -f docker-compose.yml down
echo.
pause
exit /b 0

:build_identity
for /f %%G in ('git rev-parse HEAD') do set GIT_COMMIT=%%G
set /p APP_VERSION=<VERSION
for /f %%D in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')"') do set BUILD_DATE=%%D
set SOURCE_STATE=clean
for /f %%S in ('git status --porcelain --untracked-files^=normal') do set SOURCE_STATE=dirty
exit /b 0
