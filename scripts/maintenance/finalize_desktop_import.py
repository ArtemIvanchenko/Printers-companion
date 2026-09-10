"""Verify completed Desktop imports and preserve superseded sources/forecasts."""
from __future__ import annotations

import json
from urllib.parse import urlparse

from sqlalchemy import select

from domain.models import ImportJob
from domain.models.prints import PrintRecord, PrintRecordFile
from scripts.maintenance.desktop_runtime import STATE
from scripts.maintenance.populate_desktop import as_row, upload, write_json
from storage.db.session import session_scope
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import PrintsRepository


def main():
    manifest = json.loads((STATE / 'inventory.json').read_text())
    plan = json.loads((STATE / 'import-plan.json').read_text())
    store = ObjectStore()
    with session_scope() as db:
        jobs = [db.get(ImportJob, b['job_id']) for b in plan['batches']]
        if any(job is None or job.status != 'done' for job in jobs):
            raise RuntimeError('All selected imports must complete before finalization')
        receipts = [{'job_id': j.import_job_id, 'sessions': j.session_ids,
                     'reports': j.report_ids, 'source_objects': j.source_objects} for j in jobs]
    # Smaller exact-prefix logs remain evidence, but are never parsed twice.
    archived = []
    for version in plan['superseded']:
        row = next(r for r in manifest['files'] if r['sha256'] == version['sha256'])
        from pathlib import Path
        uri = store.put_file_verified(store.settings.minio_bucket_raw,
            f"desktop-prefixes/{row['sha256']}/{row['name']}", Path(row['path']),
            expected_sha256=row['sha256'], expected_size=row['size'])
        archived.append({**version, 'object_uri': uri})
    for batch in plan['batches']:
        card = batch.get('record_id')
        if not card:
            continue
        with session_scope() as db:
            row = db.get(PrintRecord, card)
            metadata = dict(row.metadata_json or {})
            prediction = metadata.get('prediction')
        if prediction:
            path = STATE / 'cards' / card / 'Прежний прогноз до импорта.json'
            write_json(path, {'prediction': prediction, 'status': 'historical_not_revalidated',
                             'reason_ru': 'Входы повторно импортированы; старый расчёт не является новой проверкой точности.'})
            upload(as_row(path), 'doc', card)
            with session_scope() as db:
                current = db.get(PrintRecord, card)
                metadata = dict(current.metadata_json or {})
                if metadata.get('prediction') != prediction:
                    raise RuntimeError('Prediction changed while its historical copy was being saved')
                metadata['desktop_previous_prediction'] = prediction
                metadata.pop('prediction')
                PrintsRepository(db).update_print_record(card, {'metadata_json': metadata}, expected_revision=current.revision)
    with session_scope() as db:
        attachments = [(f.checksum, f.object_uri, f.size_bytes) for f in db.scalars(select(PrintRecordFile))]
        counts = {'cards': len(db.scalars(select(PrintRecord)).all()),
                  'model_unique_contents': len({r['sha256'] for r in manifest['files'] if r['kind'] == 'model'}),
                  'log_unique_contents': len({r['sha256'] for r in manifest['files'] if r['kind'] == 'log'}),
                  'completed_batches': len(receipts), 'curated_pairs': sum(bool(b.get('record_id')) for b in plan['batches'])}
    model_hashes = {r['sha256'] for r in manifest['files'] if r['kind'] == 'model'}
    missing = model_hashes - {sha for sha, _, _ in attachments}
    if missing:
        raise RuntimeError(f'{len(missing)} source models have no database attachment')
    checked = set()
    for sha, uri, size in attachments:
        if sha not in model_hashes or uri in checked:
            continue
        parsed = urlparse(uri)
        stat = store.client.stat_object(parsed.netloc, parsed.path.lstrip('/'))
        if stat.size != size:
            raise RuntimeError(f'Stored size mismatch: {uri}')
        checked.add(uri)
    result = {'counts': counts, 'jobs': receipts, 'superseded_logs': archived,
              'verified_model_object_count': len(checked), 'missing_models': list(missing),
              'limitations_ru': ['Даты не использовались для новых автоматических пар.',
                '19 наборов логов оставлены без новой привязки к моделям.',
                'Два Magics сохранены без декодированных печатных тел.',
                'В июльских наборах отсутствуют основные журналы и time.log; время печати по ним не подтверждено.']}
    write_json(STATE / 'completion.json', result)
    for name in ('inventory.json', 'import-plan.json', 'completion.json'):
        row = as_row(STATE / name)
        store.put_file_verified(store.settings.minio_bucket_docs,
            f"desktop-inventory/{row['sha256']}/{name}", STATE / name,
            expected_sha256=row['sha256'], expected_size=row['size'])
    print(json.dumps({'counts': counts, 'verified_model_objects': len(checked), 'missing_models': len(missing)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
