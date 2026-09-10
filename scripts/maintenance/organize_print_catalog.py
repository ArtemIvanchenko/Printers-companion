"""Build a verified, portable catalogue without moving any Desktop originals."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

from core.utils.files import sha256_file
from scripts.maintenance.desktop_runtime import STATE

DEFAULT_DESTINATION = Path('/Users/admin/Desktop/Каталог моделей и печатей')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.manifest-', delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        temporary = Path(stream.name)
    temporary.replace(path)


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .')[:90] or 'Без названия'


def copy_verified(source, destination, checksum):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != checksum:
            raise ValueError(f'Не перезаписываю изменённый файл: {destination}')
        return
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.copy-', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        # clonefile on APFS gives independent contents, not a symlink/hardlink.
        result = subprocess.run(['cp', '-c', str(source), str(temporary)], capture_output=True)
        if result.returncode:
            shutil.copy2(source, temporary)
        if sha256_file(temporary) != checksum:
            raise ValueError(f'Контрольная сумма источника изменилась: {source}')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def make_archive(rows, destination):
    """ZIP_DEFLATED readable by Explorer/Finder; verify every expanded SHA."""
    import hashlib
    rows = {r['name']: r for r in rows}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    if not destination.exists():
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.archive-', delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3,
                             allowZip64=True) as archive:
            for name, row in sorted(rows.items()):
                archive.write(row['path'], arcname=name)
    candidate = temporary or destination
    try:
        with zipfile.ZipFile(candidate) as archive:
            if set(archive.namelist()) != set(rows):
                raise ValueError(f'Состав архива не совпадает с манифестом: {destination}')
            for name, row in rows.items():
                with archive.open(name) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                        raise ValueError(f'Контрольная сумма в архиве не совпадает: {name}')
        if temporary:
            temporary.replace(destination)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {'sha256': sha256_file(destination), 'size': destination.stat().st_size}


def descriptor(root, path, role, checksum=None):
    return {'path': path.relative_to(root).as_posix(), 'role': role,
            'sha256': checksum or sha256_file(path), 'size': path.stat().st_size}


def build_catalog(manifest, plan, destination):
    aliases = defaultdict(list)
    for row in manifest['files']:
        aliases[row['sha256']].append(row)
    models = {sha: min(rows, key=lambda r: (bool(r.get('archive')), len(r['path'])))
              for sha, rows in aliases.items() if rows[0]['kind'] == 'model'}
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / '.printers-companion-catalog'
    if not marker.exists():
        if any(destination.iterdir()):
            raise ValueError('Целевая папка не пуста и не принадлежит каталогу')
        marker.write_text('version=1\n', encoding='utf-8')
    receipts, used, archived_hashes = [], set(), set()
    for index, batch in enumerate(plan['batches'], 1):
        paired = bool(batch.get('curated_folder'))
        category = '01 Печати с моделями и логами' if paired else '03 Логи без установленной модели'
        folder = destination / category / safe_name(batch['key'])
        archive_path = folder / 'Логи.zip'
        archive = make_archive(batch['files'], archive_path)
        archived_hashes.update(r['sha256'] for r in batch['files'])
        files = [descriptor(folder, archive_path, 'logs', archive['sha256'])]
        if paired:
            source = Path(batch['curated_folder']) / 'model'
            rows = {r['sha256']: r for r in manifest['files'] if r['kind'] == 'model'
                    and not r.get('archive') and Path(r['path']).is_relative_to(source)}
            for sha, row in rows.items():
                model_dir = folder / 'Модели' / (safe_name(Path(row['name']).stem) + '__' + sha[:10])
                model_path = model_dir / row['name']
                copy_verified(Path(row['path']), model_path, sha)
                copy_verified(archive_path, model_dir / 'Логи всей печати.zip', archive['sha256'])
                write_json(model_dir / 'print-bundle.json', {
                    'schema_version': 1, 'kind': 'model_reference', 'name': row['name'],
                    'bundle_id': 'model:' + sha, 'parent_bundle': '../../print-bundle.json',
                    'association': 'curated_archive_whole_build_not_individual_part',
                    'files': [descriptor(model_dir, model_path, 'model', sha),
                              descriptor(model_dir, model_dir / 'Логи всей печати.zip', 'logs', archive['sha256'])],
                    'sources': aliases[sha],
                    'warning_ru': 'Логи относятся ко всей печати, а не только к этой детали. '
                                  'В приложение импортируйте родительскую папку печати, чтобы сохранить состав плиты.',
                })
                files.append(descriptor(folder, model_path, 'model', sha))
                used.add(sha)
            note = source.parent / 'ПРИМЕЧАНИЕ.txt'
            if note.exists():
                copy_verified(note, folder / 'Примечание исходного архива.txt', sha256_file(note))
        bundle = {
            'schema_version': 1, 'kind': 'print' if paired else 'logs',
            'bundle_id': 'print:' + batch['fingerprint'], 'name': batch['key'], 'files': files,
            'record_id_hint': batch.get('record_id'),
            'association': 'existing_curated_archive' if paired else 'unmatched',
            'warning_ru': 'Архивное сопоставление не подтверждает качество изделия или все параметры режима.'
                          if paired else 'Связь с моделью не установлена; дата не является доказательством.',
        }
        write_json(folder / 'print-bundle.json', bundle)
        receipts.append({'folder': folder.relative_to(destination).as_posix(), **bundle})
        print(f'Архив {index}/{len(plan["batches"])}: {batch["key"]}', flush=True)
    for sha, row in sorted(models.items(), key=lambda item: (item[1]['name'], item[0])):
        if sha in used:
            continue
        folder = destination / '02 Модели без установленных логов' / (safe_name(Path(row['name']).stem) + '__' + sha[:10])
        target = folder / row['name']
        copy_verified(Path(row['path']), target, sha)
        bundle = {'schema_version': 1, 'kind': 'model', 'bundle_id': 'model:' + sha,
                  'name': row['name'], 'association': 'unmatched',
                  'files': [descriptor(folder, target, 'model', sha)],
                  'warning_ru': 'Логи этой модели не установлены. Это не подтверждение состоявшейся печати.',
                  'sources': aliases[sha]}
        write_json(folder / 'print-bundle.json', bundle)
        receipts.append({'folder': folder.relative_to(destination).as_posix(), **bundle})
        used.add(sha)
    # Keep older, shorter prefixes, but do not mix them into a parseable print.
    old_rows = []
    for old in plan['superseded']:
        row = dict(aliases[old['sha256']][0])
        row['name'] = old['sha256'][:10] + '__' + row['name']
        old_rows.append(row)
        archived_hashes.add(row['sha256'])
    if old_rows:
        make_archive(old_rows, destination / '04 Ранние версии логов' / 'Не импортировать повторно.zip')
    wanted_logs = {r['sha256'] for r in manifest['files'] if r['kind'] == 'log'}
    if set(models) != used or wanted_logs != archived_hashes:
        raise ValueError('Каталог не покрывает все исходники; оригиналы не изменены')
    counts = {'unique_models': len(used), 'unique_logs': len(archived_hashes),
              'paired_prints': sum(bool(b.get('curated_folder')) for b in plan['batches']),
              'model_only_folders': sum(r['kind'] == 'model' for r in receipts),
              'unmatched_log_batches': sum(r['kind'] == 'logs' for r in receipts)}
    write_json(destination / 'print-bundle.json', {'schema_version': 1, 'kind': 'catalog',
               'name': 'Каталог моделей и печатей', 'counts': counts, 'bundles': receipts})
    (destination / 'НАЧНИТЕ ЗДЕСЬ.txt').write_text(
        '01 — восемь архивных печатей: отдельные папки моделей и ZIP логов всей печати.\n'
        '02 — каждая оставшаяся модель отдельно; её логи не установлены.\n'
        '03 — архивы логов, для которых модель не установлена.\n'
        '04 — ранние укороченные копии: сохранены, но повторно не импортируются.\n\n'
        'В приложение выбирайте ОДНУ папку печати из 01 либо модели из 02, не весь каталог.\n'
        'Копии ZIP рядом с деталями — логи всей печати; не отдельные измерения детали.\n'
        'print-bundle.json хранит состав, SHA-256, происхождение и ограничения связи.\n'
        'Оригинальные папки рабочего стола сохранены без изменений.\n', encoding='utf-8')
    write_json(STATE / 'organized-catalog.json', {'destination': str(destination), 'counts': counts})
    print(json.dumps(counts, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=DEFAULT_DESTINATION)
    args = parser.parse_args()
    build_catalog(json.loads((STATE / 'inventory.json').read_text()),
                  json.loads((STATE / 'import-plan.json').read_text()), args.destination)
