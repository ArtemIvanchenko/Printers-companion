import pytest

from domain.models.prints import PrintRecord
from storage.db.session import SessionLocal
from storage.repositories.prints_repo import PrintRecordConflict, PrintsRepository


def _file_values(record_id: str, name: str) -> dict[str, object]:
    return {
        "record_id": record_id,
        "object_uri": f"s3://stls/{record_id}/{name}",
        "file_name": name,
        "file_type": "stl",
        "size_bytes": len(name),
        "checksum": f"checksum-{name}",
    }


def _create_record() -> tuple[str, int]:
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({"name": "Исходная карточка"})
        db.commit()
        return record["record_id"], record["revision"]


def test_stale_session_can_add_attachment_without_overwriting_card_edit():
    record_id, initial_revision = _create_record()

    # Keep the ORM object alive after releasing the read transaction.  This is
    # the state an upload request used to reach after another PC saved the card.
    with SessionLocal() as stale_db:
        stale_record = stale_db.get(PrintRecord, record_id)
        assert stale_record is not None
        assert stale_record.revision == initial_revision
        stale_db.commit()

        with SessionLocal() as winner_db:
            winner = PrintsRepository(winner_db).update_print_record(
                record_id,
                {"name": "Правка другого оператора", "status": "active"},
                expected_revision=initial_revision,
            )
            assert winner is not None
            winner_revision = winner["revision"]
            winner_db.commit()

        saved = PrintsRepository(stale_db).add_print_file(
            _file_values(record_id, "detail-a.stl")
        )
        stale_db.commit()
        assert saved["file_name"] == "detail-a.stl"
        # _touch_print_record expires the stale version attributes rather than
        # leaving a misleading in-memory revision behind.
        assert stale_record.revision == winner_revision + 1

    with SessionLocal() as verify_db:
        repo = PrintsRepository(verify_db)
        current = repo.get_print_record(record_id)
        assert current is not None
        assert current["name"] == "Правка другого оператора"
        assert current["status"] == "active"
        assert current["revision"] == winner_revision + 1
        assert [item["file_name"] for item in repo.list_print_files(record_id)] == [
            "detail-a.stl"
        ]

        # An attachment is a visible card change, so a browser holding the
        # pre-upload revision must still be rejected by normal optimistic CAS.
        with pytest.raises(PrintRecordConflict):
            repo.update_print_record(
                record_id,
                {"notes": "устаревшая правка"},
                expected_revision=winner_revision,
            )


def test_stale_session_can_delete_attachment_after_concurrent_add():
    record_id, _ = _create_record()
    with SessionLocal() as seed_db:
        repo = PrintsRepository(seed_db)
        removable = repo.add_print_file(_file_values(record_id, "remove-me.stl"))
        repo.add_print_file(_file_values(record_id, "keep-me.stl"))
        seed_db.commit()

    with SessionLocal() as stale_db:
        stale_record = stale_db.get(PrintRecord, record_id)
        assert stale_record is not None
        stale_revision = stale_record.revision
        stale_db.commit()

        with SessionLocal() as winner_db:
            PrintsRepository(winner_db).add_print_file(
                _file_values(record_id, "concurrent.stl")
            )
            winner_db.commit()

        uri = PrintsRepository(stale_db).delete_print_file(
            record_id, removable["file_id"]
        )
        stale_db.commit()
        assert uri == removable["object_uri"]
        assert stale_record.revision == stale_revision + 2

    with SessionLocal() as verify_db:
        repo = PrintsRepository(verify_db)
        current = repo.get_print_record(record_id)
        assert current is not None
        assert current["revision"] == stale_revision + 2
        assert {item["file_name"] for item in repo.list_print_files(record_id)} == {
            "keep-me.stl",
            "concurrent.stl",
        }


def test_same_content_is_stored_only_once_and_touches_card_once():
    record_id, initial_revision = _create_record()
    values = _file_values(record_id, "same-content.stl")

    with SessionLocal() as db:
        repo = PrintsRepository(db)
        first = repo.add_print_file(values)
        second = repo.add_print_file({**values, "file_name": "renamed-copy.stl"})
        db.commit()

        assert "duplicate" not in first
        assert second["duplicate"] is True
        assert second["file_id"] == first["file_id"]

    with SessionLocal() as db:
        repo = PrintsRepository(db)
        assert len(repo.list_print_files(record_id)) == 1
        assert repo.get_print_record(record_id)["revision"] == initial_revision + 1


def test_unknown_legacy_checksums_are_not_treated_as_duplicates():
    record_id, initial_revision = _create_record()
    first = {**_file_values(record_id, "legacy-a.stl"), "checksum": ""}
    second = {**_file_values(record_id, "legacy-b.stl"), "checksum": ""}

    with SessionLocal() as db:
        repo = PrintsRepository(db)
        repo.add_print_file(first)
        repo.add_print_file(second)
        db.commit()

    with SessionLocal() as db:
        repo = PrintsRepository(db)
        assert len(repo.list_print_files(record_id)) == 2
        assert repo.get_print_record(record_id)["revision"] == initial_revision + 2
