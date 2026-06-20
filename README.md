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

## Установка

**macOS**
```bash
brew install orbstack
git clone https://github.com/ArtemIvanchenko/Printers-companion.git
cd Printers-companion
docker compose up -d
```

**Windows**
```powershell
winget install Docker.DockerDesktop
# перезапустить терминал после установки
git clone https://github.com/ArtemIvanchenko/Printers-companion.git
cd Printers-companion
docker compose up -d
```

**Linux**
```bash
curl -fsSL https://get.docker.com | sh
git clone https://github.com/ArtemIvanchenko/Printers-companion.git
cd Printers-companion
docker compose up -d
```

Дашборд: `http://localhost:8000`

`.env` не нужен — все настройки работают из коробки. Для изменений (путь к логам, LLM): `cp .env.example .env`.

```bash
docker compose down     # остановить (данные сохраняются)
docker compose down -v  # сбросить все данные
```

## Обновление

Обновление **ручное** — система ничего не качает и не перезапускает сама.

**Через дашборд** (основной способ). На вкладке «О системе» видно текущую
версию, коммит и доступно ли обновление. Кнопка «Обновить» запускает пересборку
и перезапуск с индикатором прогресса.

**Через скрипт** (для развёртывания на ПК принтера). Запускать вручную, по
необходимости — он делает `git pull`, пересобирает базовый образ при изменении
зависимостей и перезапускает сервисы:

```powershell
.\update.ps1       # Windows
```
```bash
./update.sh        # Linux / macOS
```

> Скрипты предназначены для запуска вручную. Не ставьте их в cron / планировщик
> задач — для обновления достаточно кнопки в дашборде.

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
