#!/bin/bash
# =================================================================
#  Printer's Companion — запуск на macOS
#  Первый запуск: клонирует проект и собирает (~15–30 мин).
#  Повторный запуск: включает систему за ~1 мин.
# =================================================================
cd "$(dirname "$0")" || exit 1

REPO_URL="https://github.com/ArtemIvanchenko/Printers-companion.git"
REPO_DIR="./printers-companion"
COMPOSE="docker compose -f docker-compose.yml"
URL="http://localhost:8000"
LOG="./launch.log"

say() { printf "\n>>> %s\n" "$1" | tee -a "$LOG"; }
fail() { say "ОШИБКА: $1"; echo ""; read -r -p "Нажмите Enter, чтобы закрыть…"; exit 1; }

echo "=== $(date) ===" >> "$LOG"

# 1. Git установлен?
if ! command -v git >/dev/null 2>&1; then
  say "Git не установлен."
  echo "Нажмите 'Установить' в появившемся окне, затем перезапустите этот файл."
  git --version 2>/dev/null  # вызывает диалог установки Xcode CLT на macOS
  echo ""; read -r -p "После установки нажмите Enter…"
  exit 0
fi

# 3. Docker установлен?
if ! command -v docker >/dev/null 2>&1; then
  say "Docker не установлен."
  echo "Установите Docker Desktop для Mac: https://www.docker.com/products/docker-desktop/"
  open "https://www.docker.com/products/docker-desktop/" 2>/dev/null
  echo ""; read -r -p "Нажмите Enter, чтобы закрыть…"
  exit 1
fi

# 4. Запустить Docker-движок и дождаться готовности
say "Запускаю Docker…"
# Пробуем известные GUI-приложения (Docker Desktop, OrbStack); если ни одно не найдено —
# Docker Engine, скорее всего, уже запущен как служба (Linux) или будет запущен вручную.
open -a OrbStack 2>/dev/null || open -a Docker 2>/dev/null || true
for i in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done
docker info >/dev/null 2>&1 || fail "Docker не запустился. Запустите OrbStack, Rancher Desktop или Docker Desktop вручную и повторите."

# 5. Первый запуск: клонировать и собрать
if [ ! -d "$REPO_DIR" ]; then
  say "Первый запуск: скачиваю проект…"
  git clone "$REPO_URL" "$REPO_DIR" >>"$LOG" 2>&1 \
    || fail "Не удалось скачать проект. Нужно интернет-соединение."

  say "Настраиваю конфигурацию…"
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  # Исправить путь к логам (Windows→macOS) и отключить LLM
  sed -i '' 's|C:\\\\PrinterLogs|./raw_logs|g' "$REPO_DIR/.env"
  sed -i '' 's|C:\\PrinterLogs|./raw_logs|g'   "$REPO_DIR/.env"
  sed -i '' 's|LLM_PROVIDER=lmstudio|LLM_PROVIDER=null|g' "$REPO_DIR/.env"
  # Добавить недостающие MinIO-бакеты
  grep -q MINIO_BUCKET_STLS   "$REPO_DIR/.env" || echo "MINIO_BUCKET_STLS=stls"   >> "$REPO_DIR/.env"
  grep -q MINIO_BUCKET_MAGICS "$REPO_DIR/.env" || echo "MINIO_BUCKET_MAGICS=magics" >> "$REPO_DIR/.env"
  grep -q MINIO_BUCKET_PHOTOS "$REPO_DIR/.env" || echo "MINIO_BUCKET_PHOTOS=photos" >> "$REPO_DIR/.env"
  grep -q MINIO_BUCKET_DOCS   "$REPO_DIR/.env" || echo "MINIO_BUCKET_DOCS=docs"   >> "$REPO_DIR/.env"
  mkdir -p "$REPO_DIR/raw_logs"

  say "Собираю образы (это займёт 15–30 минут, в зависимости от скорости интернета)…"
  (cd "$REPO_DIR" && $COMPOSE build >>"$LOG" 2>&1) \
    || fail "Сборка образов не удалась. Подробности в файле launch.log."
fi

# Папка для связи с дашбордом (туда кнопка «Обновить» кладёт запрос).
mkdir -p "$REPO_DIR/control"

# 5.5 Применить запрошенное обновление (если в прошлой сессии нажали «Обновить»)
if [ -f "$REPO_DIR/control/update.request" ]; then
  say "Запрошено обновление — скачиваю новую версию и пересобираю…"
  (cd "$REPO_DIR" && git pull --rebase origin main >>"$LOG" 2>&1) \
    || say "Предупреждение: не удалось скачать обновление (нет сети?). Запускаю текущую версию."
  (cd "$REPO_DIR" && $COMPOSE build >>"$LOG" 2>&1) \
    || fail "Пересборка после обновления не удалась. Подробности в файле launch.log."
  rm -f "$REPO_DIR/control/update.request"
  say "Обновление установлено."
fi

# 6. Запустить систему
say "Запускаю систему…"
GIT_COMMIT=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
(cd "$REPO_DIR" && GIT_COMMIT="$GIT_COMMIT" $COMPOSE up -d >>"$LOG" 2>&1) \
  || fail "Не удалось запустить. Подробности в файле launch.log."

# 7. Дождаться API
say "Жду готовности (обычно 1–2 минуты)…"
for i in $(seq 1 90); do
  curl -fs "$URL/health" >/dev/null 2>&1 && break
  sleep 2
done
curl -fs "$URL/health" >/dev/null 2>&1 || say "Предупреждение: система долго стартует — попробуйте открыть браузер вручную: $URL"

# 8. Открыть дашборд
say "Готово! Открываю дашборд: $URL"
open "$URL" 2>/dev/null

echo ""
echo "Чтобы остановить систему: откройте Docker Desktop и нажмите Stop"
echo "или запустите: cd $(pwd)/printers-companion && docker compose -f docker-compose.yml down"
echo ""
read -r -p "Можно закрыть это окно (Enter)…"
