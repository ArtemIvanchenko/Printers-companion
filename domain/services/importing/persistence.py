"""Fenced staging writes of source references and canonical-event batches.

These staging writes are not the visible analytical result. Completed timings,
card overview and reports are published by importing.publication in one transaction.
"""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from domain.services.importing.contracts import ImportPersistenceError
from domain.services.importing.fence import ImportFence, StaleImportLeaseError
from storage.db.session import SessionLocal

logger = logging.getLogger(__name__)


def persist_parse_results_to_db(
    session_id: str,
    ingested_files: list[Any],
    source_objects: dict[str, str] | None = None,
    now: datetime | None = None,
    lease_guard: Callable[[], bool] | None = None,
    fence: ImportFence | None = None,
) -> tuple[int, int]:
    """Persist parse results (source files and canonical events) to database.

    Returns:
        (files_saved_count, events_saved_count)
    """
    from storage.repositories.runtime import RuntimeRepository

    now = now or datetime.now(timezone.utc)
    files_saved = 0
    events_saved = 0
    source_objects = source_objects or {}

    with SessionLocal() as db:
        repo = RuntimeRepository(db)

        for ingested_file in ingested_files:
            if fence:
                fence.verify(db)
            else:
                _require_current_lease(lease_guard)
            # Deterministic id from session + file content hash: re-persisting the
            # same file (retry / re-detected folder) UPSERTS the same row instead
            # of inserting a duplicate. (uuid4 here doubled every file/event on
            # any re-import.)
            file_name = Path(ingested_file.path).name
            relative_name = str(getattr(ingested_file, "relative_path", file_name)).replace(
                "\\", "/"
            )
            source_file_id = "file_" + _stable_hash(session_id, ingested_file.checksum or file_name)
            try:
                repo.save_source_file(
                    source_file_id=source_file_id,
                    session_id=session_id,
                    object_uri=(
                        source_objects.get(relative_name)
                        or source_objects.get(file_name)
                        or source_objects.get("__source_archive__")
                    ),
                    file_name=file_name,
                    checksum=ingested_file.checksum,
                    original_path=ingested_file.path,
                    size_bytes=ingested_file.size_bytes,
                    family=ingested_file.classification.family,
                    role=ingested_file.classification.role,
                    encoding=ingested_file.encoding,
                    data_quality_status=ingested_file.data_quality_status,
                    metadata=ingested_file.metadata,
                )
                files_saved += 1
                logger.info("Saved source file %s for session %s", file_name, session_id)
            except Exception as exc:
                logger.error("Failed to save source file %s: %s", file_name, exc)
                raise ImportPersistenceError(
                    f"Could not persist source file {file_name}: {exc}"
                ) from exc

            # Save CanonicalEvents from parse_result (in batches)
            if ingested_file.parse_result and ingested_file.parse_result.events:
                batch: list[dict] = []
                for event_index, event_draft in enumerate(ingested_file.parse_result.events):
                    # Source location fields live on the nested `source`
                    # (SourceLocation), not on the event draft itself.
                    source = getattr(event_draft, "source", None)
                    # Deterministic event id: stable across re-parses (same file →
                    # same event order → same index), so re-import upserts instead
                    # of duplicating. The index disambiguates events that share a
                    # source line/offset (e.g. time.log emits several per line).
                    batch.append(
                        {
                            "event_id": "event_" + _stable_hash(source_file_id, str(event_index)),
                            "session_id": session_id,
                            "source_file_id": source_file_id,
                            "ts": getattr(event_draft, "ts", None),
                            "raw_timestamp": getattr(event_draft, "raw_timestamp", None),
                            "event_type": getattr(event_draft, "event_type", "unknown"),
                            "subsystem": getattr(event_draft, "subsystem", None),
                            "phase": getattr(event_draft, "phase", None),
                            "severity": getattr(event_draft, "severity", "info"),
                            "confidence": getattr(event_draft, "confidence", 1.0),
                            "layer": getattr(event_draft, "layer", None),
                            "source_line": getattr(source, "source_line", None),
                            "source_offset": getattr(source, "source_offset", None),
                            "raw_excerpt": getattr(source, "raw_excerpt", None),
                            "payload": getattr(event_draft, "payload", {}),
                            "evidence_kind": getattr(event_draft, "evidence_kind", "machine_log"),
                            "provenance": [{"source": "parser", "file": file_name}],
                        }
                    )
                    if len(batch) >= _EVENT_BATCH_SIZE:
                        events_saved += _flush_event_batch(
                            repo,
                            batch,
                            lease_guard=lease_guard,
                            fence=fence,
                        )
                        batch = []
                events_saved += _flush_event_batch(
                    repo,
                    batch,
                    lease_guard=lease_guard,
                    fence=fence,
                )

        try:
            if fence:
                fence.verify(db)
            db.commit()
        except StaleImportLeaseError:
            raise
        except Exception as exc:
            logger.error("Failed to commit parse results to database: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass
            raise ImportPersistenceError(f"Could not commit parse results: {exc}") from exc

    return files_saved, events_saved


_EVENT_BATCH_SIZE = 500


def _flush_event_batch(
    repo,
    batch: list[dict],
    *,
    lease_guard: Callable[[], bool] | None = None,
    fence: ImportFence | None = None,
) -> int:
    """Save and commit a complete canonical-event batch or raise for retry."""
    if not batch:
        return 0
    if fence:
        fence.verify(repo.db)
    else:
        _require_current_lease(lease_guard)
    try:
        saved = repo.save_canonical_event_batch(batch)
    except Exception as exc:
        repo.db.rollback()
        logger.error("Failed to save event batch: %s", exc)
        raise ImportPersistenceError(f"Could not persist canonical event batch: {exc}") from exc
    try:
        if fence:
            fence.verify(repo.db)
        repo.db.commit()
    except StaleImportLeaseError:
        repo.db.rollback()
        raise
    except Exception as exc:
        logger.error("Failed to commit event batch: %s", exc)
        try:
            repo.db.rollback()
        except Exception:
            pass
        raise ImportPersistenceError(f"Could not commit canonical event batch: {exc}") from exc
    return saved


def _require_current_lease(lease_guard: Callable[[], bool] | None) -> None:
    if lease_guard is not None and not lease_guard():
        raise StaleImportLeaseError("Import lease was lost before persistence")


def _ensure_session_record(
    session_id: str,
    grouping_confidence: float,
    *,
    origin_compute_node_id: str,
    fence: ImportFence | None = None,
) -> str:
    """Create the BuildSession row up-front so later FK inserts are satisfied."""
    from domain.models.entities import BuildSession
    from domain.services.importing.publication import session_publication_token

    try:
        with SessionLocal() as db:
            if fence:
                fence.verify(db)
            existing = db.scalar(
                select(BuildSession).where(BuildSession.session_id == session_id).with_for_update()
            )
            if existing is not None:
                # group_id is deterministic from log content but historically
                # did not include the workstation. Never let PC-2 reuse and
                # overwrite PC-1's row when identical logs are uploaded twice.
                require_compute_owner(
                    entity_type="session",
                    entity_id=session_id,
                    origin_compute_node_id=existing.origin_compute_node_id,
                    requested_compute_node_id=origin_compute_node_id,
                )
            else:
                existing = BuildSession(
                    session_id=session_id,
                    origin_compute_node_id=origin_compute_node_id,
                    status="import_processing",
                    classification="INCOMPLETE_OR_UNKNOWN",
                    classification_confidence=0.0,
                    grouping_confidence=grouping_confidence,
                )
                db.add(existing)
                db.flush()
                token = session_publication_token(existing)
                if fence:
                    fence.verify(db)
                db.commit()
                logger.info(
                    "Created session record %s for compute node %s",
                    session_id,
                    origin_compute_node_id,
                )
                return token
            return session_publication_token(existing)
    except (ComputeAffinityError, StaleImportLeaseError):
        raise
    except Exception as exc:
        raise ImportPersistenceError(f"Could not ensure session {session_id}: {exc}") from exc


def _stable_hash(*parts: str) -> str:
    """Short deterministic hex id from the given parts (for idempotent PKs)."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
