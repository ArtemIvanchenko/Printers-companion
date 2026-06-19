# Printer's Companion

Система аналитики для SLM-принтеров металлической печати серии M-350/M-450-M (АО «Лазерные системы»). Разбирает логи принтера, группирует файлы в сессии печати, считает ключевые показатели и строит веб-дашборд.

## Функционал

- **Дашборд** — графики длительности, типов сессий, расхода материалов, прогноз ТО; `http://localhost:8000`
- **Архив печатей** — список всех печатей со статусами, STL-моделями, фото и временны́ми метками
- **Загрузка файлов** — перетаскивание STL / magics / фото / логов прямо в браузере
- **Автоимпорт** — watcher следит за папкой с логами, группирует файлы в сессии, не создаёт дубли при повторной загрузке
- **Оценка времени печати** — на основе STL-геометрии (PySLM)
- **Аномалии** — автоматическое обнаружение отклонений в процессе
- **Отчёты** — JSON/Markdown-отчёт по каждой сессии; опциональный LLM-нарратив (LM Studio / Qwen, `LLM_PROVIDER=null` чтобы отключить)
- **Параметры станка** — профили лазерных пресетов (скорость хатчинга, шаг, мощность)
- **Оператор-журнал** — структурированные события: смена порошка, газа, обслуживание
- **REST API** — FastAPI + OpenAPI-документация на `/docs`

## Стек

| Компонент | Назначение |
|---|---|
| FastAPI | REST API + HTML-дашборд |
| PostgreSQL 16 | основная БД |
| Redis 7 | очередь задач |
| MinIO | хранилище файлов (STL, magics, фото) |
| Docker Compose | оркестрация |

## Быстрый старт

Требования: Docker Desktop.

```bash
git clone https://github.com/ArtemIvanchenko/Printers-companion.git
cd Printers-companion
cp .env.example .env
```

В `.env` указать папку с логами принтера:

```env
# Windows
RAW_LOGS_HOST_PATH=C:\PrinterLogs

# macOS / Linux
RAW_LOGS_HOST_PATH=./raw_logs
```

```bash
docker compose up -d
```

Первый запуск собирает образы (~15 мин). Дашборд: `http://localhost:8000`.

**Автозапуск на Windows** — ярлык `deploy\autostart-windows.bat` в `shell:startup`.

## Обновление

```powershell
.\update.ps1       # Windows
```
```bash
./update.sh        # Linux / macOS
```

Скрипт сам проверяет git, пересобирает базовый образ при изменении зависимостей и перезапускает сервисы.

## Форматы логов

Принтер пишет до 10 типов файлов за одну печать — система распознаёт их по имени и группирует автоматически:

| Маска | Содержимое |
|---|---|
| `*.log` | главный лог событий (cp1251 / utf-8) |
| `*_burn.log` | послойные данные процесса |
| `*_time.log` | тайминги слоёв |
| `*_sensors.log` | телеметрия датчиков |
| `*_Monitor100.log` | дискретные переходы состояний |
| `*_Monitor200.log` | вспомогательные коды |
| `*_stateFlow.log` | автоматные состояния |
| `*_stateFlowData.log` | классификатор |
| `table_temp.log` | температура стола |
| `*_error.log` | журнал ошибок |

## Бэкап и восстановление

```powershell
# PostgreSQL
.\deploy\backup\backup_postgres.ps1
.\deploy\backup\restore_postgres.ps1 -SqlFile .\backups\postgres\<file>.sql

# MinIO (STL, фото, magics)
.\deploy\backup\backup_minio.ps1
.\deploy\backup\restore_minio.ps1 -Archive backups\minio\minio-data.tgz
```

## Разработка

```bash
# Тесты (SQLite, без Docker)
DATABASE_URL="sqlite:////tmp/pla-test.db" APP_ENV=test python -m pytest tests/

# Проверка конфига Compose
docker compose config
```

Профили принтеров добавляются в `profiles/<profile_id>/` — реализация `PrinterProfilePlugin` с маппингом сигналов и правилами поверх канонической доменной модели.
