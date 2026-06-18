#!/bin/bash
set -e -o pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$REPO_DIR/update.log"
DEPLOYED_FILE="$REPO_DIR/.last_deployed"

cd "$REPO_DIR"

git fetch origin main -q

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
LAST_DEPLOYED=$(cat "$DEPLOYED_FILE" 2>/dev/null || echo "")

# Exit only if already at remote HEAD AND last deploy completed successfully.
# If LAST_DEPLOYED differs from REMOTE, a previous deploy failed mid-way → retry.
if [ "$LOCAL" = "$REMOTE" ] && [ "$LAST_DEPLOYED" = "$REMOTE" ]; then
    exit 0
fi

FROM="${LAST_DEPLOYED:-$LOCAL}"

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M')] Обновление: ${LOCAL:0:8} → ${REMOTE:0:8}" >> "$LOG"
    git pull origin main -q
fi

# Rebuild base image if base-layer files changed (new deps won't appear otherwise).
if git diff --name-only "$FROM" "$REMOTE" 2>/dev/null | grep -qE 'Dockerfile\.base|requirements'; then
    echo "[$(date '+%Y-%m-%d %H:%M')] Пересборка базового образа..." >> "$LOG"
    docker build -f Dockerfile.base -t artemivanchenko/printer-log-analytics:base . >> "$LOG" 2>&1
fi

# Rebuild and restart all currently running app services (dynamic — respects active profiles).
RUNNING=$(docker compose ps --services --filter status=running 2>/dev/null | tr '\n' ' ')
SERVICES="${RUNNING:-api worker watcher scheduler}"
# shellcheck disable=SC2086
docker compose up -d --build $SERVICES >> "$LOG" 2>&1

echo "$REMOTE" > "$DEPLOYED_FILE"

NEW_COMMIT=$(git rev-parse --short HEAD)
curl -s -X POST http://localhost:8000/admin/update/notify \
  -H "Content-Type: application/json" \
  -d "{\"commit\":\"$NEW_COMMIT\",\"message\":\"Обновлено до ${REMOTE:0:8}\"}" \
  >> "$LOG" 2>&1 || true

echo "[$(date '+%Y-%m-%d %H:%M')] Готово ($NEW_COMMIT)" >> "$LOG"
