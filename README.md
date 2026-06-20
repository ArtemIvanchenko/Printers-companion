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

Кнопка «Обновить» на вкладке «О системе» в дашборде — применяется при следующем запуске.

Или вручную:
```bash
git pull && docker compose up -d --build
```

## Разработка

```bash
# тесты без Docker
DATABASE_URL="sqlite:////tmp/pla-test.db" APP_ENV=test python -m pytest tests/
```
