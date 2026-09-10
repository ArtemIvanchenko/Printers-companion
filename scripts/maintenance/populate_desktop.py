"""Idempotent Desktop catalogue/import preparation. Never infer a pair from date alone."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess

from sqlalchemy import select

from core.config.settings import get_settings
from core.versioning.provenance import build_provenance, stable_hash
from domain.models.prints import PrintRecord, PrintRecordFile
from domain.services.import_jobs import detect_import_candidate, mark_import_job_confirmed
from scripts.maintenance.desktop_inventory import digest
from scripts.maintenance.desktop_runtime import STATE
from storage.db.session import session_scope
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.runtime import RuntimeRepository

CONFIRMED = Path('/Users/admin/Desktop/Подтверждённые печати')
MODE = re.compile(r'\((steel|aluminum)\s+([0-9.]+)мм')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.partial')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    temporary.replace(path)


def prefix_of(smaller, larger):
    if smaller['size'] == 0:
        return False
    with Path(smaller['path']).open('rb') as a, Path(larger['path']).open('rb') as b:
        while block := a.read(1024 * 1024):
            if block != b.read(len(block)):
                return False
    return True


def source_rank(row):
    path = row['path']
    return (bool(row.get('archive')), 'сандиск' in path, len(path), path)


def upload(row, file_type, record_id):
    with session_scope() as db:
        existing = PrintsRepository(db).find_file_by_checksum(record_id, row['sha256'])
    if existing:
        return existing
    settings = get_settings()
    bucket = {'magics': settings.minio_bucket_magics, 'stl': settings.minio_bucket_stls,
              'stl_supports': settings.minio_bucket_stls, 'doc': settings.minio_bucket_docs}[file_type]
    uri = ObjectStore().put_file_verified(
        bucket, f"desktop/{row['sha256']}/{row['name']}", Path(row['path']),
        expected_sha256=row['sha256'], expected_size=row['size'],
    )
    with session_scope() as db:
        return PrintsRepository(db).add_print_file({
            'record_id': record_id, 'object_uri': uri, 'file_name': row['name'],
            'file_type': file_type, 'size_bytes': row['size'], 'checksum': row['sha256'],
        })


def as_row(path):
    return {'path': str(path), 'name': path.name, 'sha256': digest(path), 'size': path.stat().st_size}


def ensure_card(key, name, notes, *, existing_id=None, material='other', thickness=None):
    with session_scope() as db:
        repo = PrintsRepository(db)
        rows = db.scalars(select(PrintRecord)).all()
        found = next((r for r in rows if (r.metadata_json or {}).get('desktop_catalog_key') == key), None)
        if found is None and existing_id:
            found = db.get(PrintRecord, existing_id)
        if found:
            metadata = dict(found.metadata_json or {})
            metadata.update({'desktop_catalog_key': key, 'desktop_import_source': 'Desktop inventory'})
            if notes not in (found.notes or ''):
                notes = ((found.notes or '') + '\n\n' + notes).strip()
            else:
                notes = found.notes
            repo.update_print_record(found.record_id, {'metadata_json': metadata, 'notes': notes},
                                     expected_revision=found.revision)
            return found.record_id
        return repo.create_print_record({
            'name': name[:240], 'material': material, 'layer_thickness_mm': thickness,
            'status': 'draft', 'notes': notes,
            'metadata_json': {'desktop_catalog_key': key, 'desktop_import_source': 'Desktop inventory',
                              'session_link_confirmed': False,
                              'calibration_exclusions': ['scan', 'time', 'defect_risk']},
            'updated_by': 'desktop-import',
        })['record_id']


def prepare_logs(manifest):
    by_name = defaultdict(dict)
    for row in manifest['files']:
        if row['kind'] == 'log':
            by_name[row['name']].setdefault(row['sha256'], row)
    chosen, superseded, conflicts = {}, [], []
    for name, unique in sorted(by_name.items()):
        versions = sorted(unique.values(), key=lambda r: (-r['size'], source_rank(r)))
        largest = versions[0]
        if all(prefix_of(v, largest) for v in versions[1:]):
            chosen[name] = largest
            superseded.extend({'sha256': v['sha256'], 'superseded_by': largest['sha256'], 'name': name}
                              for v in versions[1:])
        elif len(versions) == 1:
            chosen[name] = largest
        else:
            conflicts.append({'name': name, 'variants': versions})
    # Reuse only existing curated associations, verifying the model checksum and
    # the recorded session start date independently. No new date-only links.
    with session_scope() as db:
        records = [(r.record_id, r.session_id) for r in db.scalars(select(PrintRecord))]
        attachments = [(f.record_id, f.checksum) for f in db.scalars(select(PrintRecordFile))]
    batches, used = [], set()
    for folder in sorted(CONFIRMED.iterdir()):
        if not folder.is_dir():
            continue
        dates = sorted({p.name[:10] for p in (folder / 'logs').glob('*.log')},
                       key=lambda d: datetime.strptime(d, '%d.%m.%Y'))
        if not dates:
            continue
        magics = next((folder / 'model').glob('*.magics'), None)
        model_hash = digest(magics) if magics else None
        date_tag = datetime.strptime(dates[0], '%d.%m.%Y').strftime('%Y%m%d')
        existing = next((rid for rid, sid in records if sid and sid.startswith('session_' + date_tag)
                         and (rid, model_hash) in attachments), None)
        note_path = folder / 'ПРИМЕЧАНИЕ.txt'
        note = note_path.read_text() if note_path.exists() else ''
        note += '\nИсточник сопоставления: ранее собранный архив «Подтверждённые печати». ' \
                'Это не отметка о годности. Неизвестные параметры и нативные поддержки не восстановлены.'
        mode = MODE.search(folder.name)
        card = ensure_card('curated:' + folder.name, folder.name, note, existing_id=existing,
                           material=mode[1] if mode else 'other', thickness=float(mode[2]) if mode else None)
        batches.append({'key': folder.name, 'dates': dates, 'record_id': card, 'curated_folder': str(folder)})
        used.update(dates)
    for date in sorted({name[:10] for name in chosen} - used,
                       key=lambda d: datetime.strptime(d, '%d.%m.%Y')):
        batches.append({'key': date, 'dates': [date], 'record_id': None})
    for batch in batches:
        selected = [r for name, r in chosen.items() if name[:10] in batch['dates']]
        fingerprint = stable_hash({r['name']: r['sha256'] for r in selected})
        root = STATE / 'raw' / fingerprint
        root.mkdir(parents=True, exist_ok=True)
        for row in selected:
            destination = root / row['name']
            if not destination.exists():
                # APFS clone: independent file, no extra physical copy until changed.
                subprocess.run(['cp', '-c', row['path'], str(destination)], check=True)
            if digest(destination) != row['sha256']:
                raise ValueError(f'Changed source: {destination}')
        batch.update({'source_path': str(root), 'fingerprint': fingerprint, 'files': selected})
        job_id = 'import_desktop_' + fingerprint[:24]
        with session_scope() as db:
            repo = RuntimeRepository(db)
            old = repo.get_import_job(job_id)
        if old is None:
            detected = detect_import_candidate(root, print_record_id=batch['record_id'])
            detected.job.import_job_id = job_id
            confirmed = mark_import_job_confirmed(detected.job, actor='operator-request-desktop-import')
            with session_scope() as db:
                RuntimeRepository(db).save_import_job(confirmed.job)
        batch['job_id'] = job_id
        print('Queued', batch['key'], len(selected), flush=True)
    result = {'batches': batches, 'superseded': superseded, 'conflicts': conflicts}
    write_json(STATE / 'import-plan.json', result)
    return result


def prepare_models(manifest, plan):
    import trimesh
    from analytics.prediction.magics_reader import read_plate

    aliases = defaultdict(list)
    for row in manifest['files']:
        if row['kind'] == 'model':
            aliases[row['sha256']].append(row)
    used, groups = set(), []
    for batch in plan['batches']:
        if not batch.get('curated_folder'):
            continue
        root = Path(batch['curated_folder']) / 'model'
        rows = [r for r in manifest['files'] if not r.get('archive') and Path(r['path']).is_relative_to(root)]
        unique = list({r['sha256']: r for r in rows}.values())
        used.update(r['sha256'] for r in unique)
        groups.append({'record_id': batch['record_id'], 'rows': unique, 'source': str(root)})
    remaining = defaultdict(list)
    for sha, variants in aliases.items():
        if sha not in used:
            representative = min(variants, key=source_rank)
            remaining[str(Path(representative['path']).parent)].append(representative)
    for folder, rows in sorted(remaining.items()):
        # Each Magics is a separate layout; never combine alternative layouts.
        magics = [r for r in rows if Path(r['path']).suffix.lower() == '.magics']
        stls = [r for r in rows if Path(r['path']).suffix.lower() != '.magics']
        for row in magics:
            key = 'magics:' + row['sha256']
            card = ensure_card(key, f"{Path(folder).name} · {Path(row['name']).stem}",
                               'Компоновка из архива. Печать и связь с логами не подтверждены.\nИсточник: ' + row['path'])
            groups.append({'record_id': card, 'rows': [row], 'source': folder})
        if stls:
            key = 'stls:' + stable_hash(sorted(r['sha256'] for r in stls))
            card = ensure_card(key, f"{Path(folder).name} · модели ({len(stls)})",
                               'Набор STL из одной папки, не подтверждённая печатная плита. '
                               'Количество экземпляров, размещение, материал и режим неизвестны.\nИсточник: ' + folder)
            groups.append({'record_id': card, 'rows': stls, 'source': folder})
    receipts = []
    for index, group in enumerate(groups):
        card = group['record_id']
        with session_scope() as db:
            meta = dict(db.get(PrintRecord, card).metadata_json or {})
        if meta.get('desktop_geometry_complete'):
            receipts.append({'record_id': card, 'status': 'already_catalogued'})
            continue
        geometries, errors, sources = [], [], []
        has_stl = any(Path(r['path']).suffix.lower() == '.stl' for r in group['rows'])
        for row in group['rows']:
            suffix = Path(row['path']).suffix.lower()
            kind = 'magics' if suffix == '.magics' else 'stl_supports' if row['name'].lower().startswith('s_') else 'stl'
            upload(row, kind, card)
            sources.append({'sha256': row['sha256'], 'aliases': aliases[row['sha256']]})
            try:
                if suffix == '.magics':
                    plate = read_plate(row['path'])
                    meshes = plate.parts
                    native_supports = plate.support_entry_count
                    if not has_stl and meshes:
                        preview = STATE / 'previews' / row['sha256'] / 'Компоновка без нативных поддержек.stl'
                        preview.parent.mkdir(parents=True, exist_ok=True)
                        if not preview.exists():
                            trimesh.util.concatenate(meshes).export(preview, file_type='stl')
                        upload(as_row(preview), 'stl', card)
                else:
                    meshes = [trimesh.load(row['path'], file_type='stl', process=False)]
                    native_supports = 0
                if not meshes:
                    raise ValueError('В проекте не найдены печатные тела поддерживаемого формата; исходник сохранён.')
                geometries.append({
                    'file': row['name'], 'sha256': row['sha256'], 'bodies': len(meshes),
                    'triangles': sum(len(m.faces) for m in meshes),
                    'bounds_mm': [min(float(m.bounds[0][i]) for m in meshes) for i in range(3)] +
                                 [max(float(m.bounds[1][i]) for m in meshes) for i in range(3)],
                    'native_support_records': native_supports,
                    'source': 'calculated',
                })
            except Exception as exc:
                errors.append({'file': row['name'], 'error': str(exc)[:500]})
        report = {'record_id': card, 'source_folder': group['source'], 'sources': sources,
                  'geometry': geometries, 'errors': errors,
                  'limitations_ru': ['Геометрия не доказывает факт печати; нативные поддержки не восстановлены.',
                                     'Прогноз времени не создавался по неподтверждённым параметрам.'],
                  'provenance': build_provenance('desktop_catalog', inputs=[r['sha256'] for r in group['rows']],
                                                 config={'geometry_only': True}, generated_by=get_settings().compute_node_id)}
        path = STATE / 'cards' / card / 'Источники и геометрия.json'
        write_json(path, report)
        upload(as_row(path), 'doc', card)
        with session_scope() as db:
            record = db.get(PrintRecord, card)
            metadata = dict(record.metadata_json or {})
            metadata.update({'desktop_geometry_complete': not errors, 'desktop_geometry_summary': {
                'files': len(geometries), 'errors': errors, 'source': 'calculated',
                'native_support_records': sum(g['native_support_records'] for g in geometries),
                'provenance': report['provenance'],
            }})
            PrintsRepository(db).update_print_record(card, {'metadata_json': metadata}, expected_revision=record.revision)
        receipts.append({'record_id': card, 'files': len(group['rows']), 'errors': errors})
        write_json(STATE / 'model-receipts.json', receipts)
        print(f'Models {index+1}/{len(groups)}: {card}, files={len(group["rows"])}, errors={len(errors)}', flush=True)
    write_json(STATE / 'model-receipts.json', receipts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['logs', 'models'])
    args = parser.parse_args()
    manifest = json.loads((STATE / 'inventory.json').read_text())
    if args.action == 'logs':
        prepare_logs(manifest)
    else:
        prepare_models(manifest, json.loads((STATE / 'import-plan.json').read_text()))


if __name__ == '__main__':
    main()
