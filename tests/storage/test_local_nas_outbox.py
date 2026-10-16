import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from storage.sync.local_outbox import (
    LocalNasOutbox,
    OutboxFullError,
    OutboxIntegrityError,
    StaleOutboxClaimError,
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

    assert queue.complete(first["operation_id"], {"file_id": "prf_done"}, claim_token=claimed["claim_token"])
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
    assert queue.fail(operation["operation_id"], "checksum mismatch", claim_token=claimed["claim_token"])
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


def test_completed_receipt_reclaims_payload_after_interrupted_cleanup(tmp_path, monkeypatch):
    source = tmp_path / "receipt.bin"
    source.write_bytes(b"receipt")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    claimed = queue.claim(operation["operation_id"])
    assert claimed is not None
    processing = queue.root / "processing" / operation["operation_id"]
    with monkeypatch.context() as patch:
        def interrupted(path):
            raise OSError("power loss after receipt fsync")
        patch.setattr("storage.sync.local_outbox.shutil.rmtree", interrupted)
        with pytest.raises(OSError, match="power loss"):
            queue.complete(operation["operation_id"], {"file_id": "prf_committed"}, claim_token=claimed["claim_token"])

    status = queue.status()

    assert status["counts"]["processing"] == 0
    assert status["bytes_waiting"] == 0
    assert not processing.exists()


def test_cleanup_does_not_parse_history_without_live_payloads(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    queue._ensure_layout()
    for index in range(100):
        operation_id = f"{index:064x}"
        queue._write_json(queue.root / "completed" / f"{operation_id}.json", {
            "operation_id": operation_id, "completed_at": "2026-01-01T00:00:00Z",
            "result": {"file_id": "confirmed"},
        })
    reads = []
    original = queue._read_json

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(queue, "_read_json", read)
    assert queue.get("f" * 64) is None
    assert reads == [], "cleanup work depends on retained payloads, not total history"
    assert len(list((queue.root / "completed").glob("*.json"))) == 100


def test_completed_reopen_persists_receipt_removal_before_new_payload(tmp_path, monkeypatch):
    source = tmp_path / "reopen.bin"
    source.write_bytes(b"same logical content")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    claim = queue.claim(operation["operation_id"])
    assert queue.complete(operation["operation_id"], {"file_id": "old"}, claim_token=claim["claim_token"])
    completed = queue.root / "completed"
    calls = []
    original_sync = queue._fsync_directory
    original_copy = queue._copy_and_hash

    def sync(path):
        calls.append(("sync", path))
        return original_sync(path)

    def copy(*args):
        assert ("sync", completed) in calls
        calls.append(("copy", None))
        return original_copy(*args)

    monkeypatch.setattr(queue, "_fsync_directory", sync)
    monkeypatch.setattr(queue, "_copy_and_hash", copy)
    reopened = queue.enqueue_attachment(
        source, owner_node_id="operator-01", record_id="pr_one", file_name="photo.jpg",
        file_type="photo", bucket="photos", checksum=operation["checksum"],
        size_bytes=source.stat().st_size, reopen_completed=True,
    )
    assert reopened["status"] == "pending"
    assert calls.index(("sync", completed)) < calls.index(("copy", None))


def _reclaim(queue, operation_id):
    manifest_path = queue.root / "processing" / operation_id / "manifest.json"
    manifest = queue._read_json(manifest_path)
    manifest["claimed_at"] = "2000-01-01T00:00:00+00:00"
    queue._write_json(manifest_path, manifest)
    return queue.claim(operation_id)


@pytest.mark.parametrize("action", ["release", "fail", "complete"])
def test_replaced_claim_cannot_mutate_or_delete_current_payload(tmp_path, action):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"preserved claim")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    first = queue.claim(operation["operation_id"])
    second = _reclaim(queue, operation["operation_id"])
    assert first["claim_token"] != second["claim_token"]
    manifest_path = Path(second["payload_path"]).parent / "manifest.json"
    current_manifest = manifest_path.read_bytes()
    argument = {"file_id": "stale-result"} if action == "complete" else "late old error"

    assert getattr(queue, action)(operation["operation_id"], argument, claim_token=first["claim_token"]) is False

    assert manifest_path.read_bytes() == current_manifest
    assert queue.get(operation["operation_id"])["status"] == "processing"
    assert queue.verify_claimed(second).read_bytes() == source.read_bytes()
    assert not list((queue.root / "completed").iterdir())
    assert queue.complete(operation["operation_id"], {"file_id": "current-result"}, claim_token=second["claim_token"])
    assert queue.get(operation["operation_id"])["result"] == {"file_id": "current-result"}


def test_stale_verification_never_opens_replacement_payload(tmp_path, monkeypatch):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"preserved claim")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    first = queue.claim(operation["operation_id"])
    second = _reclaim(queue, operation["operation_id"])
    original = Path.open
    opened = []
    def tracked(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", tracked)

    with pytest.raises(StaleOutboxClaimError):
        queue.verify_claimed(first)
    assert Path(second["payload_path"]) not in opened
    with pytest.raises(StaleOutboxClaimError):
        with queue.publication_guard(first):
            pytest.fail("stale publisher entered SQL boundary")


def test_claim_replaced_during_verification_is_not_returned_for_transfer(tmp_path, monkeypatch):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"preserved claim")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    first = queue.claim(operation["operation_id"])
    digest = hashlib.sha256()
    second = None
    class Digest:
        def update(self, chunk):
            nonlocal second
            digest.update(chunk)
            second = _reclaim(queue, operation["operation_id"])
        def hexdigest(self):
            return digest.hexdigest()
    monkeypatch.setattr("storage.sync.local_outbox.hashlib.sha256", Digest)

    with pytest.raises(StaleOutboxClaimError):
        queue.verify_claimed(first)
    assert second["claim_token"] != first["claim_token"]
    assert queue.get(operation["operation_id"])["status"] == "processing"
    assert Path(second["payload_path"]).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("action", ["release", "fail", "complete"])
def test_claim_mutations_require_token_even_for_existing_operation(tmp_path, action):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"preserved claim")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    claim = queue.claim(operation["operation_id"])
    argument = {"file_id": "unfenced-result"} if action == "complete" else "unfenced error"
    with pytest.raises(TypeError, match="claim_token"):
        getattr(queue, action)(operation["operation_id"], argument)
    assert queue.verify_claimed(claim).read_bytes() == source.read_bytes()


def test_completed_operation_reopens_with_new_intent_and_fresh_fence(tmp_path):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"same immutable bytes")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    first = queue.claim(operation["operation_id"])
    second = _reclaim(queue, operation["operation_id"])
    assert queue.complete(operation["operation_id"], {"file_id": "committed"}, claim_token=second["claim_token"])
    reopened = queue.enqueue_attachment(
        source, owner_node_id="operator-01", record_id="pr_one",
        file_name="photo.jpg", file_type="photo", bucket="photos",
        checksum=operation["checksum"], size_bytes=source.stat().st_size,
        reopen_completed=True,
    )
    third = queue.claim(reopened["operation_id"])
    assert len({first["claim_token"], second["claim_token"], third["claim_token"]}) == 3
    assert len({first["object_name"], second["object_name"], third["object_name"]}) == 3
    assert first["file_id"] == second["file_id"]
    assert third["file_id"] != first["file_id"]
    assert queue.complete(operation["operation_id"], {"file_id": "old"}, claim_token=first["claim_token"]) is False
    assert queue.complete(operation["operation_id"], {"file_id": "previous"}, claim_token=second["claim_token"]) is False
    assert queue.get(operation["operation_id"])["file_name"] == "photo.jpg"
    assert queue.get(operation["operation_id"])["file_type"] == "photo"
    assert queue.verify_claimed(third).read_bytes() == source.read_bytes()
    assert queue.complete(operation["operation_id"], {"file_id": "reopened"}, claim_token=third["claim_token"])


def test_legacy_pending_and_receipt_without_token_remain_readable(tmp_path):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"legacy bytes")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    pending_manifest = queue.root / "pending" / operation["operation_id"] / "manifest.json"
    assert "claim_token" not in queue._read_json(pending_manifest)
    claim = queue.claim(operation["operation_id"])
    assert claim["claim_token"]
    assert queue.complete(operation["operation_id"], {"file_id": "legacy"}, claim_token=claim["claim_token"])
    receipt = queue.root / "completed" / f"{operation['operation_id']}.json"
    manifest = queue._read_json(receipt)
    manifest.pop("claim_token")
    queue._write_json(receipt, manifest)
    restarted = _queue(tmp_path)
    assert restarted.get(operation["operation_id"])["result"] == {"file_id": "legacy"}


def test_old_claim_without_token_recovers_but_cannot_mutate(tmp_path):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"legacy bytes")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    old = queue.claim(operation["operation_id"])
    manifest_path = Path(old["payload_path"]).parent / "manifest.json"
    manifest = queue._read_json(manifest_path)
    manifest.pop("claim_token")
    manifest["claimed_at"] = "2000-01-01T00:00:00+00:00"
    queue._write_json(manifest_path, manifest)
    current = _queue(tmp_path).claim(operation["operation_id"])
    assert current["claim_token"] != old["claim_token"]
    assert queue.fail(operation["operation_id"], "old process", claim_token=None) is False
    assert queue.verify_claimed(current).read_bytes() == source.read_bytes()


def test_current_slow_claim_and_explicit_retry_keep_existing_attempt_semantics(tmp_path):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"slow immutable upload")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    first = queue.claim(operation["operation_id"])
    manifest_path = Path(first["payload_path"]).parent / "manifest.json"
    manifest = queue._read_json(manifest_path)
    manifest["claimed_at"] = "2000-01-01T00:00:00+00:00"
    queue._write_json(manifest_path, manifest)
    # Only recovery transfers authority; time alone is not a new upload limit.
    with queue.publication_guard(first):
        pass
    assert queue.release(operation["operation_id"], "NAS unavailable", claim_token=first["claim_token"])
    pending_manifest = queue.root / "pending" / operation["operation_id"] / "manifest.json"
    manifest = queue._read_json(pending_manifest)
    assert manifest["attempts"] == 1
    assert manifest["claim_token"] is None
    manifest["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
    queue._write_json(pending_manifest, manifest)
    second = _queue(tmp_path).claim(operation["operation_id"])
    assert second["claim_token"] != first["claim_token"]
    assert second["attempts"] == 1
    assert second["file_id"] == first["file_id"]
    assert second["object_name"] != first["object_name"]
    assert queue.fail(operation["operation_id"], "operator conflict", claim_token=second["claim_token"])
    assert queue.retry_failed(operation["operation_id"])
    third = queue.claim(operation["operation_id"])
    assert third["attempts"] == 0
    assert len({first["claim_token"], second["claim_token"], third["claim_token"]}) == 3
    assert len({first["object_name"], second["object_name"], third["object_name"]}) == 3
    assert third["file_id"] == first["file_id"]
    assert queue.complete(operation["operation_id"], {"file_id": "confirmed"}, claim_token=third["claim_token"])


def test_new_claim_keeps_legacy_identity_and_completed_object_reference(tmp_path):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"legacy immutable bytes")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    manifest_path = queue.root / "pending" / operation["operation_id"] / "manifest.json"
    legacy = queue._read_json(manifest_path)
    old_uri = f"s3://{legacy['bucket']}/{legacy['object_name']}"
    claimed = _queue(tmp_path).claim(operation["operation_id"])
    assert claimed["file_id"] == legacy["file_id"]
    assert claimed["operation_id"] == legacy["operation_id"]
    assert claimed["checksum"] == legacy["checksum"]
    assert claimed["object_name"] != legacy["object_name"]
    assert claimed["object_name"].endswith(claimed["claim_token"])
    # An already-published legacy pointer remains authoritative after a retry.
    result = {"duplicate": True, "file_id": legacy["file_id"], "object_uri": old_uri}
    assert queue.complete(operation["operation_id"], result, claim_token=claimed["claim_token"])
    assert _queue(tmp_path).get(operation["operation_id"])["result"]["object_uri"] == old_uri


def test_missing_object_identity_is_quarantined_without_losing_payload(tmp_path):
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"preserved corrupt manifest bytes")
    queue = _queue(tmp_path)
    operation = _enqueue(queue, source)
    manifest_path = queue.root / "pending" / operation["operation_id"] / "manifest.json"
    manifest = queue._read_json(manifest_path)
    manifest.pop("file_id")
    queue._write_json(manifest_path, manifest)
    assert queue.claim(operation["operation_id"]) is None
    assert queue.get(operation["operation_id"])["status"] == "failed"
    assert (queue.root / "failed" / operation["operation_id"] / "payload").read_bytes() == source.read_bytes()
