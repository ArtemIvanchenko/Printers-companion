from uuid import uuid4

from sqlalchemy import select

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
