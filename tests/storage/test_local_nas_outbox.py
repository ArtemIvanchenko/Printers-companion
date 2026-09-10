import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from storage.sync.local_outbox import (
    LocalNasOutbox,
    OutboxFullError,
    OutboxIntegrityError,
)


def _queue(tmp_path, *, max_bytes=10_000) -> LocalNasOutbox:
    return LocalNasOutbox(
        tmp_path / "outbox",
        max_bytes=max_bytes,
        retry_min_seconds=1,
        retry_max_seconds=4,
        claim_seconds=60,
    )


def _enqueue(queue: LocalNasOutbox, source, *, record_id="pr_one") -> dict:
    payload = source.read_bytes()
    return queue.enqueue_attachment(
        source,
        owner_node_id="operator-01",
        record_id=record_id,
        file_name=source.name,
        file_type="doc",
        bucket="docs",
        checksum=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        content_type="application/octet-stream",
    )


def test_enqueue_is_durable_idempotent_and_checksum_verified(tmp_path):
    source = tmp_path / "drawing.bin"
    source.write_bytes(b"immutable-payload")
    queue = _queue(tmp_path)

    first = _enqueue(queue, source)
    second = _enqueue(queue, source)

    assert first["operation_id"] == second["operation_id"]
    assert queue.status()["counts"]["pending"] == 1
    claimed = queue.claim(first["operation_id"])
    assert claimed is not None
    payload = queue.verify_claimed(claimed)
    assert payload.read_bytes() == b"immutable-payload"

    queue.complete(first["operation_id"], {"file_id": "prf_done"})
    done = queue.get(first["operation_id"])
    assert done["status"] == "completed"
    assert done["result"]["file_id"] == "prf_done"
    assert not payload.exists(), "large local copy is removed only after publication"


def test_concurrent_local_producers_create_one_complete_operation(tmp_path):
    source = tmp_path / "parallel.bin"
    source.write_bytes(b"parallel-payload")
    first_queue = _queue(tmp_path)
    second_queue = _queue(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda queue: _enqueue(queue, source), [first_queue, second_queue]))

    assert results[0]["operation_id"] == results[1]["operation_id"]
    assert first_queue.status()["counts"]["pending"] == 1
    payloads = list((first_queue.root / "pending").glob("*/payload"))
    assert len(payloads) == 1
    assert payloads[0].read_bytes() == b"parallel-payload"


def test_changed_source_is_rejected_before_becoming_visible(tmp_path):
    source = tmp_path / "changed.bin"
    source.write_bytes(b"new")
    queue = _queue(tmp_path)

    with pytest.raises(OutboxIntegrityError):
        queue.enqueue_attachment(
            source,
            owner_node_id="operator-01",
            record_id="pr_one",
            file_name="changed.bin",
            file_type="doc",
            bucket="docs",
            checksum=hashlib.sha256(b"old").hexdigest(),
            size_bytes=3,
        )

    assert queue.status()["counts"]["pending"] == 0


def test_capacity_is_bounded_and_does_not_drop_first_payload(tmp_path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"123456")
    second.write_bytes(b"abcdef")
    queue = _queue(tmp_path, max_bytes=10)

    _enqueue(queue, first, record_id="pr_one")
    with pytest.raises(OutboxFullError):
        _enqueue(queue, second, record_id="pr_two")

    assert queue.status()["bytes_waiting"] == 6


def test_expired_local_claim_is_recovered_after_process_crash(tmp_path):
    source = tmp_path / "recover.bin"
    source.write_bytes(b"recover")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    claimed = queue.claim(operation["operation_id"])
    assert claimed is not None

    manifest_path = (
        queue.root / "processing" / operation["operation_id"] / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["claimed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).isoformat()
    queue._write_json(manifest_path, manifest)

    status = queue.status()
    assert status["counts"]["processing"] == 0
    assert status["counts"]["pending"] == 1


def test_tampered_queued_payload_is_detected_and_can_be_quarantined(tmp_path):
    source = tmp_path / "tamper.bin"
    source.write_bytes(b"original")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    claimed = queue.claim(operation["operation_id"])
    assert claimed is not None
    with open(claimed["payload_path"], "wb") as stream:
        stream.write(b"tampered")

    with pytest.raises(OutboxIntegrityError):
        queue.verify_claimed(claimed)
    queue.fail(operation["operation_id"], "checksum mismatch")
    assert queue.get(operation["operation_id"])["status"] == "failed"
    assert queue.retry_failed(operation["operation_id"])
    assert queue.get(operation["operation_id"])["status"] == "pending"


def test_untrusted_operation_id_cannot_escape_outbox_root(tmp_path):
    source = tmp_path / "outside"
    source.mkdir()
    marker = source / "manifest.json"
    marker.write_text('{"operation_id": "outside"}', encoding="utf-8")
    queue = _queue(tmp_path)

    assert queue.get("../../outside") is None
    assert queue.claim("../../outside") is None
    assert queue.retry_failed("../../outside") is False
    assert marker.exists()


def test_completed_receipt_reclaims_payload_after_interrupted_cleanup(tmp_path):
    source = tmp_path / "receipt.bin"
    source.write_bytes(b"receipt")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    claimed = queue.claim(operation["operation_id"])
    assert claimed is not None
    processing = queue.root / "processing" / operation["operation_id"]
    manifest = json.loads((processing / "manifest.json").read_text(encoding="utf-8"))
    manifest.update({
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "result": {"file_id": "prf_committed"},
    })
    queue._write_json(
        queue.root / "completed" / f"{operation['operation_id']}.json",
        manifest,
    )

    status = queue.status()

    assert status["counts"]["processing"] == 0
    assert status["bytes_waiting"] == 0
    assert not processing.exists()
