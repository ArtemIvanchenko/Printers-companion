"""Drain this workstation's durable file outbox into shared PostgreSQL/MinIO.

The process performs only transport and short catalogue transactions.  Parsing,
geometry and ML remain in the existing owner-affine workers on the operator PC.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Any

from core.compute_identity import register_compute_node
from core.config.settings import Settings, get_settings
from core.logging.config import configure_logging
from core.preflight import exit_on_failure, run_preflight
from domain.services.estimation.contracts import EstimateError
from domain.services.print_cards.contracts import CardError
from domain.services.print_cards.attachments import (
    publish_attachment, publish_object, validate_attachment_record,
)
from storage.db.session import session_scope
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import PrintsRepository
from storage.sync.local_outbox import LocalNasOutbox, OutboxIntegrityError

logger = logging.getLogger(__name__)


class PermanentSyncError(RuntimeError):
    """The NAS is reachable, but operator intervention is required."""


_publish_object = publish_object


def _validate_record(item: dict[str, Any], repo: PrintsRepository) -> dict[str, Any]:
    try:
        return validate_attachment_record(item, repo)
    except CardError as exc:
        if exc.code == "not_found":
            raise PermanentSyncError(
                f"Карточка {item['record_id']} удалена или не существует; файл оставлен локально"
            ) from exc
        raise PermanentSyncError(str(exc)) from exc
    except EstimateError as exc:
        raise PermanentSyncError(str(exc)) from exc


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
            saved = publish_attachment(repo, item, object_uri)

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
    except (PermanentSyncError, CardError, EstimateError, OutboxIntegrityError, ValueError) as exc:
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
