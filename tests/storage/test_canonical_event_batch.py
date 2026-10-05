from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from domain.models.events import CanonicalEvent
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


def test_batch_is_idempotent_and_preserves_creation_time():
    key = 'batch_' + uuid4().hex
    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        events = [{'event_id': key, 'event_type': 'first', 'payload': {'value': 1}},
                  {'event_id': key + '_other', 'event_type': 'second', 'layer': 3}]
        assert repo.save_canonical_event_batch(events) == 2
        first = db.get(CanonicalEvent, key)
        created_at = first.created_at
        assert first.provenance == [{'source': 'parser'}]
        assert repo.save_canonical_event_batch([{**events[0], 'payload': {'value': 2}}, events[1]]) == 2
        db.expire_all()
        assert db.get(CanonicalEvent, key).created_at == created_at
        assert db.get(CanonicalEvent, key).payload == {'value': 2}
        assert len(db.scalars(select(CanonicalEvent).where(CanonicalEvent.event_id.in_([key, key + '_other']))).all()) == 2
        db.rollback()


def test_duplicate_keys_in_one_batch_use_last_value():
    key = 'batch_' + uuid4().hex
    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        assert repo.save_canonical_event_batch([]) == 0
        repo.save_canonical_event_batch([{'event_id': key, 'event_type': 'first'},
                                        {'event_id': key, 'event_type': 'last'}])
        assert db.get(CanonicalEvent, key).event_type == 'last'
        db.rollback()


def test_parameter_batches_keep_nulls_defaults_and_bounded_calls():
    prefix = 'batch_' + uuid4().hex
    timestamp = datetime(2026, 3, 23, 14, 30, tzinfo=timezone.utc)
    rows = [
        {'event_id': f'{prefix}_{i}', 'event_type': 'measured',
         'ts': timestamp if i % 2 else None, 'raw_timestamp': None,
         'payload': {'value': i, 'label': 'слой'}, 'phase': 'burn' if i % 2 else None}
        for i in range(1001)
    ]
    calls = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO canonical_events'):
            calls.append((executemany, len(parameters), statement.count('?')))

    with SessionLocal() as db:
        engine = db.get_bind()
        event.listen(engine, 'before_cursor_execute', observe)
        try:
            assert RuntimeRepository(db).save_canonical_event_batch(rows) == 1001
            assert [size for _, size, _ in calls] == [500, 500, 19]
            assert [batch for batch, _, _ in calls] == [True, True, False]
            # The last one-row execution has 19 parameters; a parameter batch
            # never turns SQLite's per-statement 999-bind limit into 500*19.
            assert all(binds == 19 for _, _, binds in calls)
            first = db.get(CanonicalEvent, f'{prefix}_0')
            second = db.get(CanonicalEvent, f'{prefix}_1')
            assert first.ts is None and first.phase is None
            assert first.payload == {'value': 0, 'label': 'слой'}
            assert first.severity == 'info' and first.confidence == 1.0
            assert second.ts == timestamp.replace(tzinfo=None)
            assert second.phase == 'burn'
            assert first.provenance == [{'source': 'parser'}]
        finally:
            event.remove(engine, 'before_cursor_execute', observe)
            db.rollback()


def test_failed_parameter_batch_is_rolled_back_and_unknown_fields_rejected():
    prefix = 'batch_' + uuid4().hex
    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        with pytest.raises(IntegrityError):
            repo.save_canonical_event_batch([
                {'event_id': prefix, 'event_type': 'valid'},
                {'event_id': prefix + '_invalid', 'event_type': 'invalid', 'severity': None},
            ])
        db.rollback()
        assert db.get(CanonicalEvent, prefix) is None
        with pytest.raises(AttributeError, match='unknown_field'):
            repo.save_canonical_event_batch([
                {'event_id': prefix, 'event_type': 'valid', 'unknown_field': 'must not disappear'},
            ])
        db.rollback()
