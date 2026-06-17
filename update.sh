#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$REPO_DIR/update.log"

cd "$REPO_DIR"

git fetch origin main -q

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M')] Обновление: $LOCAL → $REMOTE" >> "$LOG"
git pull origin main -q
docker compose up -d --build --no-deps api worker watcher scheduler >> "$LOG" 2>&1

NEW_COMMIT=$(git rev-parse --short HEAD)
curl -s -X POST http://localhost:8000/admin/update/notify \
  -H "Content-Type: application/json" \
  -d "{\"commit\":\"$NEW_COMMIT\",\"message\":\"Обновлено с $LOCAL до $REMOTE\"}" \
  >> "$LOG" 2>&1 || true

echo "[$(date '+%Y-%m-%d %H:%M')] Готово ($NEW_COMMIT)" >> "$LOG"
