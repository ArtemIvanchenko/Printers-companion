# Printer's Companion

Система аналитики для SLM-принтеров металлической печати серии M-350/M-450-M (АО «Лазерные системы»). Разбирает логи принтера, группирует файлы в сессии печати, считает ключевые показатели и строит веб-дашборд.

## Функционал

- **Дашборд** — графики длительности, типов сессий, расхода материалов, прогноз ТО
- **Архив печатей** — список печатей со статусами, STL-моделями, фото и временны́ми метками
- **Загрузка файлов** — перетаскивание STL / magics / фото / логов прямо в браузере
- **Автоимпорт** — система следит за папкой с логами, группирует файлы в сессии, не создаёт дубли
- **Оценка времени печати** — на основе STL-геометрии (PySLM)
- **Аномалии** — автоматическое обнаружение отклонений в процессе
- **Отчёты** — JSON/Markdown-отчёт по каждой сессии; опциональный LLM-нарратив
- **Параметры станка** — профили лазерных пресетов (скорость хатчинга, шаг, мощность)
- **Оператор-журнал** — структурированные события: смена порошка, газа, обслуживание
- **REST API** — FastAPI + OpenAPI-документация на `/docs`

## Установка с нуля

### macOS

#### Шаг 1 — Homebrew

Homebrew — менеджер пакетов для macOS. Если не установлен:

1. Откройте **Терминал** (Spotlight → `Terminal`)
2. Вставьте команду и нажмите Enter:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

> Потребуется пароль от компьютера и подтверждение установки Xcode Command Line Tools. Следуйте инструкциям на экране (~5–10 минут).

#### Шаг 2 — Git

```bash
brew install git
git config --global user.name "Имя Фамилия"
git config --global user.email "ваш@email.com"
```

#### Шаг 3 — OrbStack (Docker)

OrbStack — лёгкий способ запускать Docker-контейнеры на Mac:

```bash
brew install --cask orbstack
open -a OrbStack
```

Подождите, пока в строке меню появится иконка OrbStack — это значит Docker готов к работе.

#### Шаг 4 — Репозиторий и запуск

```bash
git clone https://github.com/ArtemIvanchenko/Printers-companion.git
cd Printers-companion
docker compose up -d
```

Первый запуск скачает образы (~3–5 минут). После этого откройте в браузере:

**http://localhost:8000**

---

### Windows

#### Шаг 1 — Git

Скачайте и установите [Git для Windows](https://git-scm.com/download/win). При установке оставьте все настройки по умолчанию.

После установки откройте **Git Bash** (появится в меню Пуск) и настройте:

```bash
git config --global user.name "Имя Фамилия"
git config --global user.email "ваш@email.com"
```

#### Шаг 2 — Docker Desktop

Скачайте и установите [Docker Desktop для Windows](https://www.docker.com/products/docker-desktop/).

> Требуется Windows 10/11 64-bit. При запросе — включите WSL 2 (Docker предложит сам).

#### Шаг 3 — Репозиторий и запуск

В Git Bash:

```bash
git clone https://github.com/ArtemIvanchenko/Printers-companion.git
cd Printers-companion
docker compose up -d
```

Откройте в браузере: **http://localhost:8000**

---

### Linux

```bash
# Git
sudo apt install git           # Debian/Ubuntu
# или: sudo dnf install git    # Fedora/RHEL

git config --global user.name "Имя Фамилия"
git config --global user.email "ваш@email.com"

# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER  # чтобы не писать sudo перед docker
newgrp docker

# Проект
git clone https://github.com/ArtemIvanchenko/Printers-companion.git
cd Printers-companion
docker compose up -d
```

Откройте в браузере: **http://localhost:8000**

---

## Папка с логами принтера

По умолчанию система смотрит в папку `raw_logs/` рядом с проектом. Создаётся автоматически при первом запуске.

Чтобы указать другую папку (например, сетевой диск), создайте файл `.env` рядом с `docker-compose.yml`:

```
RAW_LOGS_HOST_PATH=C:\PrinterLogs        # Windows
RAW_LOGS_HOST_PATH=/Volumes/PrinterLogs  # macOS
```

## Управление

```bash
docker compose up -d        # запустить
docker compose down         # остановить (данные сохраняются)
docker compose down -v      # остановить и сбросить все данные
docker compose ps           # статус сервисов
docker compose logs -f api  # логи API в реальном времени
```

## Обновление

Кнопка **«Обновить»** на вкладке «О системе» в дашборде.

Или вручную из папки проекта:

```bash
git pull && GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

## Стек

| Компонент | Назначение |
|---|---|
| FastAPI | REST API + HTML-дашборд |
| PostgreSQL 16 | основная БД |
| Redis 7 | очередь задач |
| MinIO | хранилище файлов (STL, magics, фото) |
| Docker Compose | оркестрация |

## Разработка

```bash
# тесты без Docker
DATABASE_URL="sqlite:////tmp/pla-test.db" APP_ENV=test python -m pytest tests/
```
