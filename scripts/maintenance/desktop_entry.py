"""Bundled, short-lived CLI child of the desktop application. No server/service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.updating.packaged import PackagedUpdater, configure_nas, finish_setup, public_status
from core.updating.releases import UpdateBusy, UpdateError
from core.updating.state import read_json


def emit(kind: str, **values):
    print(json.dumps(dict(type=kind, **values), ensure_ascii=False), flush=True)


def main() -> int:
    # PyInstaller/Windows can otherwise use the console code page for pipes.
    # Node always sends/reads UTF-8, including Russian paths and credentials.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['configure', 'finish-setup', 'adopt', 'launch', 'resume', 'stop', 'status', 'rollback', 'prepare-restart', 'cancel-restart'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'configure':
            # bounded stdin; secrets are not visible in process listings
            raw = sys.stdin.read(16385)
            if len(raw) > 16384:
                raise UpdateError('Настройки слишком велики.')
            values = json.loads(raw)
            if not isinstance(values, dict):
                raise UpdateError('Некорректный формат настроек.')
            configure_nas(args.root, args.bundle, values)
            emit('result', state={'phase': 'configured'})
            return 0
        if args.action == 'finish-setup':
            finish_setup(args.root)
            emit('result', state={'phase': 'configured'})
            return 0
        updater = PackagedUpdater(args.root, args.bundle, progress=lambda message: emit('progress', message=message))
        if args.action == 'resume':
            from core.updating.runtime import Updater
            state = Updater.launch(updater)
        else:
            state = getattr(updater, args.action.replace('-', '_'))()
        public = public_status(state)
        public['url'] = read_json(updater.settings_path).get('url', 'http://127.0.0.1:8000')
        emit('result', state=public)
        return 0
    except UpdateError as exc:
        emit('error', message=str(exc), code='busy' if isinstance(exc, UpdateBusy) else 'unconfirmed')
        return 1
    except KeyboardInterrupt:
        emit('error', message='Операция прервана. Журнал сохранён для следующего запуска.')
        return 130
    except Exception:
        # Never print an exception with a URI, resolved env or credentials.
        emit('error', message='Операция не подтверждена. Настройки и журнал сохранены; исходные данные не удалялись.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
