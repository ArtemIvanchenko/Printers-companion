#!/usr/bin/env bash
# Thin host wrapper: same standard-library engine as Windows, no git pull.
set -euo pipefail
UPDATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for UPDATE_PYTHON in "$UPDATE_ROOT/.venv/bin/python" python3 /Library/Developer/CommandLineTools/usr/bin/python3; do
    if "$UPDATE_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3,9))' >/dev/null 2>&1; then
        exec "$UPDATE_PYTHON" "$UPDATE_ROOT/scripts/maintenance/update_runtime.py" --root "$UPDATE_ROOT" "$@"
    fi
done
echo "Нужен Python 3.9+ для локального обновлятора. Системный Python не изменялся." >&2
exit 1
