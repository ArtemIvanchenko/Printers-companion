"""Inventory print sources without changing Desktop originals; extract ZIP members safely."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import zipfile

from scripts.maintenance.desktop_runtime import STATE

DESKTOP = Path('/Users/admin/Desktop')
LOG = re.compile(r'^\d{2}\.\d{2}\.\d{4}(?:_(?:time|sensors|error|burn|Monitor\d+|stateFlow|stateFlowData))?\.log$', re.I)


def kind(name):
    name = Path(name).name
    if name.startswith('._'):
        return None
    if Path(name).suffix.lower() in {'.stl', '.magics', '.3mf'}:
        return 'model'
    if LOG.match(name):
        return 'log'
    return None


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['rg', '--files', '--hidden', '-g', '!.git', '-g', '!node_modules',
                             '-g', '!.venv', '-g', '!.operator-state',
                             '-g', '!Каталог моделей и печатей', str(DESKTOP)],
                            text=True, capture_output=True)
    paths = [Path(p) for p in result.stdout.splitlines()]
    rows, archives, errors = [], [], [result.stderr] if result.stderr else []
    sources = [(p, None) for p in paths if kind(p.name)]
    for p in paths:
        if p.suffix.lower() not in {'.zip', '.rar', '.7z'}:
            continue
        try:
            if p.suffix.lower() != '.zip':
                listing = subprocess.run(['7z', 'l', '-slt', str(p)], capture_output=True, text=True, timeout=30)
                members = [line[7:] for line in listing.stdout.splitlines() if line.startswith('Path = ')][1:]
                relevant = [name for name in members if kind(name)]
                archives.append({'path': str(p), 'relevant_members': relevant, 'status': 'listed_not_extracted'})
                continue
            with zipfile.ZipFile(p) as archive:
                infos = [info for info in archive.infolist() if kind(info.filename) and not info.is_dir()]
                archives.append({'path': str(p), 'relevant_members': len(infos), 'status': 'checked'})
                if not infos:
                    continue
                sha = digest(p)
                root = STATE / 'extracted' / sha
                for info in infos:
                    relative = PurePosixPath(info.filename.replace('\\', '/'))
                    if relative.is_absolute() or '..' in relative.parts or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError('unsafe archive member')
                    if info.file_size > 5 * 1024**3:
                        raise ValueError('archive member exceeds 5 GiB')
                    target = root.joinpath(*relative.parts)
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as src, target.open('xb') as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                    sources.append((target, {'archive': str(p), 'member': info.filename, 'archive_sha256': sha}))
        except Exception as exc:
            errors.append({'path': str(p), 'error': str(exc)})
    for index, (path, archive) in enumerate(sources):
        try:
            rows.append({'path': str(path), 'name': path.name, 'kind': kind(path.name),
                         'size': path.stat().st_size, 'sha256': digest(path), 'archive': archive})
        except OSError as exc:
            errors.append({'path': str(path), 'error': str(exc)})
        if index % 100 == 0:
            print(f'Inventoried {index}/{len(sources)}', flush=True)
    manifest = {'files': rows, 'archives': archives, 'errors': errors,
                'desktop_file_count': len(paths),
                'counts': dict(Counter(row['kind'] for row in rows)),
                'unique_contents': len({r['sha256'] for r in rows})}
    (STATE / 'inventory.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in manifest.items() if key not in {'files', 'archives'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
