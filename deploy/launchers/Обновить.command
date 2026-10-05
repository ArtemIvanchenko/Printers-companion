#!/usr/bin/env bash
set -euo pipefail
UPDATE_LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATE_PROJECT="$(cd "$UPDATE_LAUNCHER_DIR/../.." && pwd)"
if [[ -d "$UPDATE_LAUNCHER_DIR/printers-companion" ]]; then
    UPDATE_PROJECT="$UPDATE_LAUNCHER_DIR/printers-companion"
fi
bash "$UPDATE_PROJECT/update.sh" apply "$@"
