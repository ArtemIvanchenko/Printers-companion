"""Remove only audited exocad/DentalCAD cards from PostgreSQL; keep source files."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from sqlalchemy import func, select

from domain.models.jobs import BackgroundJob
from domain.models.prints import PrintRecord, PrintRecordFile
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession, ImportJob
from scripts.maintenance.desktop_runtime import STATE
from scripts.maintenance.populate_desktop import write_json
from storage.db.session import session_scope
from storage.repositories.prints_repo import PrintsRepository


def source_hashes(manifest):
    excluded = set()
    for row in manifest['files']:
        source = row['path'] + ' ' + json.dumps(row.get('archive') or {}, ensure_ascii=False)
        if re.search(r'exocad|dentalcad', source, re.IGNORECASE):
            excluded.add(row['sha256'])
    return excluded


def snapshot(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    excluded = source_hashes(json.loads((STATE / 'inventory.json').read_text()))
    with session_scope() as db:
        files = db.scalars(select(PrintRecordFile)).all()
        targets = {f.record_id for f in files if f.checksum in excluded}
        records = db.scalars(select(PrintRecord).where(PrintRecord.record_id.in_(targets))).all()
        # This operation was requested for an unrelated software catalogue, not
        # for real prints. Fail closed if a shared/mixed card appears.
        for record in records:
            models = [f for f in files if f.record_id == record.record_id
                      and f.file_type in {'stl', 'stl_supports', 'magics'}]
            if record.session_id or not models or any(f.checksum not in excluded for f in models):
                raise ValueError(f'Карточка содержит проектные данные: {record.record_id}')
        audit = {'records': [snapshot(r) for r in records],
                 'files': [snapshot(f) for f in files if f.record_id in targets],
                 'excluded_sha256': sorted(excluded),
                 'preserved_sessions': db.scalar(select(func.count()).select_from(BuildSession)),
                 'preserved_linked_cards': sorted(db.scalars(select(PrintRecord.record_id).where(PrintRecord.session_id.is_not(None))))}
    print(json.dumps({'cards': len(targets), 'attachments': len(audit['files']),
                      'unique_models': len(excluded), 'apply': args.apply}))
    if not args.apply or not targets:
        return
    if args.backup is None or not args.backup.is_file():
        raise ValueError('A verified PostgreSQL backup is required')
    with args.backup.open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise ValueError('Expected a PostgreSQL custom-format backup')
    audit['backup'] = str(args.backup.resolve())
    audit['requested_scope'] = 'exocad and DentalCAD database records only'
    audit['object_files_deleted'] = False
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = STATE / f'dental-removal-{stamp}.json'
    write_json(path, audit)
    path.chmod(0o600)
    with session_scope() as db:
        locked = db.scalars(select(PrintRecord).where(PrintRecord.record_id.in_(targets)).with_for_update()).all()
        old = {r['record_id']: r for r in audit['records']}
        if len(locked) != len(old) or any(snapshot(r) != old[r.record_id] for r in locked):
            raise ValueError('Карточки изменились после проверки; ничего не удалено')
        current_files = db.scalars(select(PrintRecordFile).where(PrintRecordFile.record_id.in_(targets)).with_for_update()).all()
        if sorted((snapshot(f) for f in current_files), key=lambda r: r['file_id']) != sorted(audit['files'], key=lambda r: r['file_id']):
            raise ValueError('Вложения изменились после проверки; ничего не удалено')
        for table, field in [(BackgroundJob, BackgroundJob.entity_id),
                             (ImportJob, ImportJob.print_record_id),
                             (QualityOutcome, QualityOutcome.print_record_id)]:
            if db.scalar(select(func.count()).select_from(table).where(field.in_(targets))):
                raise ValueError('У карточек появились зависимые записи; ничего не удалено')
        for record_id in sorted(targets):
            PrintsRepository(db).delete_print_record(record_id)
        if db.scalar(select(func.count()).select_from(PrintRecordFile).where(PrintRecordFile.checksum.in_(excluded))):
            raise AssertionError('Excluded file references remain')
        if db.scalar(select(func.count()).select_from(BuildSession)) != audit['preserved_sessions']:
            raise AssertionError('Session count changed')
        linked = sorted(db.scalars(select(PrintRecord.record_id).where(PrintRecord.session_id.is_not(None))))
        if linked != audit['preserved_linked_cards']:
            raise AssertionError('Linked project cards changed')
    audit['status'] = 'committed'
    write_json(path, audit)
    path.chmod(0o600)
    print(f'Deleted {len(targets)} cards and {len(audit["files"])} attachment rows. Audit: {path}')


if __name__ == '__main__':
    main()
