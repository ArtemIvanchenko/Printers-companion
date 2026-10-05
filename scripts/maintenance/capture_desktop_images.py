"""Build-time: capture immutable published images for this clean release SHA.

Downloads only operator images to a verified *local* Docker context. Does not
start containers, migrate a database, publish images or contact an operator NAS.
The resulting manifest can be passed to desktop resources --images PATH.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.updating.releases import Release, UpdateError, validate_sha, version_tuple
from core.updating.runtime import Updater, REQUIRED
from core.updating.state import atomic_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--context')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    updater = Updater(root)
    try:
        updater._clean_sources()
        sha = updater.commands.run(['git', 'rev-parse', 'HEAD'])
        validate_sha(sha)
        version = (root / 'VERSION').read_text().strip()
        version_tuple(version)
        updater._host({'context': args.context})
        candidate = {'services': {name: {} for name in REQUIRED}}
        release = Release(version, 'v' + version, sha, '', '', 'Published release images')
        updater._pin_images(candidate, sorted(REQUIRED), release)
        images = {name: candidate['services'][name]['image'] for name in ('api', 'worker', 'watcher')}
        for reference in images.values():
            image = updater._json_docker('image', 'inspect', reference)[0]
            if image['Config']['Labels'].get('io.printers-companion.update-protocol') != '1':
                raise UpdateError('Опубликованный релиз ещё не поддерживает безопасный протокол настольного приложения.')
        value = {'schema_version': 1, 'update_protocol': 1, 'sha': sha, 'version': version, 'platform': 'linux/amd64', 'images': images}
        if args.output.exists():
            raise UpdateError('Файл манифеста уже существует; автоматическая перезапись запрещена.')
        atomic_json(args.output, value)
        print(f'Captured {version} {sha[:12]} immutable operator images; no deployment performed.')
        return 0
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    # Never inherit a remote daemon hidden in the build machine environment.
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT'):
        os.environ.pop(key, None)
    raise SystemExit(main())
