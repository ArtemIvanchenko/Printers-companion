"""Drain this workstation's durable file outbox into shared PostgreSQL/MinIO.

The process performs only transport and short catalogue transactions.  Parsing,
geometry and ML remain in the existing owner-affine workers on the operator PC.
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Any

from core.compute_identity import register_compute_node
from core.config.settings import Settings, get_settings
from core.logging.config import configure_logging
from core.preflight import exit_on_failure, run_preflight
from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from storage.db.session import session_scope
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import PrintsRepository
from storage.sync.local_outbox import LocalNasOutbox, OutboxIntegrityError

logger = logging.getLogger(__name__)


class PermanentSyncError(RuntimeError):
    """The NAS is reachable, but operator intervention is required."""


def _publish_object(store: Any, item: dict[str, Any], payload: Path) -> str:
    verified = getattr(store, "put_file_verified", None)
    if callable(verified):
        return verified(
            item["bucket"],
            item["object_name"],
            payload,
            expected_sha256=item["checksum"],
            expected_size=int(item["size_bytes"]),
            content_type=item["content_type"],
        )
    # Test doubles and older compatible object-store adapters may only expose
    # put_file. The local outbox has still verified the payload before this call.
    return store.put_file(
        item["bucket"],
        item["object_name"],
        payload,
        content_type=item["content_type"],
    )


def _validate_record(item: dict[str, Any], repo: PrintsRepository) -> dict[str, Any]:
    record = repo.get_print_record(str(item["record_id"]))
    if record is None:
        raise PermanentSyncError(
            f"Карточка {item['record_id']} удалена или не существует; файл оставлен локально"
        )
    if item["file_type"] in ("stl", "stl_supports"):
        try:
            require_compute_owner(
                entity_type="print_record",
                entity_id=str(record["record_id"]),
                origin_compute_node_id=str(record["origin_compute_node_id"]),
                requested_compute_node_id=str(item["owner_node_id"]),
            )
        except ComputeAffinityError as exc:
            raise PermanentSyncError(str(exc)) from exc
    return record


def process_next(
    *,
    outbox: LocalNasOutbox | None = None,
    store: Any | None = None,
    settings: Settings | None = None,
    operation_id: str | None = None,
) -> dict[str, Any] | None:
    """Try one due operation and return its resulting public state."""
    settings = settings or get_settings()
    outbox = outbox or LocalNasOutbox.from_settings(settings)
    item = outbox.claim(operation_id)
    if item is None:
        return None
    operation_id = str(item["operation_id"])

    try:
        if item.get("kind") != "print_attachment":
            raise PermanentSyncError(f"Unknown NAS outbox operation: {item.get('kind')}")
        if item.get("owner_node_id") != settings.compute_node_id:
            raise PermanentSyncError(
                "Outbox owner does not match this physical workstation's COMPUTE_NODE_ID"
            )
        payload = outbox.verify_claimed(item)

        # Cheap database pre-check in its own short transaction. It avoids a
        # needless MinIO request for a cross-PC duplicate and never holds a NAS
        # connection while the file is uploaded.
        with session_scope() as db:
            repo = PrintsRepository(db)
            _validate_record(item, repo)
            existing = repo.find_file_by_checksum(item["record_id"], item["checksum"])
        if existing is not None:
            result = {"duplicate": True, **existing}
            outbox.complete(operation_id, result)
            return outbox.get(operation_id)

        store = store or ObjectStore(settings)
        if not store.is_available():
            raise ConnectionError("NAS object storage is unavailable")
        object_uri = _publish_object(store, item, payload)

        # Publish the catalogue pointer only after the complete immutable object
        # is visible and verified. The DB unique index resolves simultaneous
        # uploads from different operator PCs.
        with session_scope() as db:
            repo = PrintsRepository(db)
            record = _validate_record(item, repo)
            saved = repo.add_print_file({
                "file_id": item["file_id"],
                "record_id": item["record_id"],
                "object_uri": object_uri,
                "file_name": item["file_name"],
                "file_type": item["file_type"],
                "size_bytes": item["size_bytes"],
                "checksum": item["checksum"],
            })
            if not saved.get("duplicate"):
                if not record.get("printed_at"):
                    from api.routes.prints import _date_from_text

                    printed_at = _date_from_text(str(item["file_name"]))
                    if printed_at:
                        repo.update_print_record(item["record_id"], {"printed_at": printed_at})
                if item["file_type"] in ("stl", "stl_supports"):
                    from api.routes.prints import _enqueue_estimate

                    current = repo.get_print_record(item["record_id"])
                    _enqueue_estimate(repo, current)

        if saved.get("duplicate") and saved.get("object_uri") != object_uri:
            # The other PC won the unique-index race. This operation owns its
            # immutable URI, so removing only that URI cannot delete the winner.
            if not store.remove_object(item["bucket"], item["object_name"]):
                logger.warning("NAS sync could not remove duplicate object %s", object_uri)

        outbox.complete(operation_id, saved)
        logger.info(
            "NAS sync published %s (%s) for %s",
            item["file_name"],
            item["checksum"][:12],
            item["record_id"],
        )
    except (PermanentSyncError, OutboxIntegrityError, ValueError) as exc:
        outbox.fail(operation_id, str(exc))
        logger.error("NAS sync quarantined %s: %s", operation_id, exc)
    except Exception as exc:
        # PostgreSQL and MinIO outages are deliberately unbounded retries. The
        # local payload is the durable copy until both NAS publications finish.
        outbox.release(operation_id, str(exc))
        logger.warning("NAS sync postponed %s: %s", operation_id, exc)
    return outbox.get(operation_id)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    report = run_preflight(settings, component="nas-sync")
    exit_on_failure(report)
    if settings.app_env not in ("local", "test"):
        from storage.db.migrate import assert_schema_at_head

        assert_schema_at_head()
        register_compute_node(settings)

    outbox = LocalNasOutbox.from_settings(settings)
    stopped = False

    def request_stop(signum: int, frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("NAS sync started for %s at %s", settings.compute_node_id, outbox.root)
    while not stopped:
        state = process_next(outbox=outbox, settings=settings)
        if state is None:
            time.sleep(settings.nas_sync_poll_seconds)
    logger.info("NAS sync stopped")


if __name__ == "__main__":
    main()
