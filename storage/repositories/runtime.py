import hashlib
import json
import logging
import math
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from domain.enums.common import SourceFileFamily, VerificationStatus
from domain.models.entities import (
    BuildSession,
    CanonicalEvent,
    ConfirmedKnowledge,
    HistoricalAnalysisVerdict,
    ImportJob,
    NotificationOutbox,
    OperatorEvent,
    OperatorJournalEntry,
    PatternInsight,
    QualityOutcome,
    ReportArtifact,
    SourceFile,
)
from domain.services.import_jobs import ImportJobRecord
from domain.services.ingestion import IngestedFile
from domain.services.compute_affinity import require_compute_owner
from operator_journal.notifications import NotificationMessage

logger = logging.getLogger(__name__)


class QualityOutcomeConflict(RuntimeError):
    """A final quality verdict does not extend the card's current audit chain."""


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats with None so the payload is valid JSON.

    PostgreSQL's json/jsonb type rejects NaN and Infinity (not part of the JSON
    spec); corrupted sensor readings can produce these values.
    """
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _model_to_dict(row: Any, fields: list[str]) -> dict[str, Any]:
    """Generic model -> dict converter."""
    return jsonable_encoder({field: getattr(row, field, None) for field in fields})


def _report_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Slim DB copy of a report: full timeline replaced by a bounded preview."""
    from reporting.json_report.generator import _timeline_preview
    payload = dict(report)
    timeline = payload.get("timeline")
    if isinstance(timeline, list):
        payload["timeline"] = _timeline_preview(timeline)
    return payload


def _offload_report(report_id: str, report: dict[str, Any]) -> str | None:
    """Upload the full report JSON to object storage; return its s3 URI or None.

    Best-effort: if MinIO is unavailable the import still succeeds — the DB keeps
    the bounded payload (graceful, lossy fallback).
    """
    try:
        from core.config.settings import get_settings
        from storage.object_store.minio_client import ObjectStore
        store = ObjectStore()
        if not store.is_available():
            return None
        bucket = get_settings().minio_bucket_reports
        data = json.dumps(jsonable_encoder(report), ensure_ascii=False).encode("utf-8")
        return store.put_bytes(bucket, f"{report_id}.json", data)
    except Exception as exc:
        logger.warning("Report %s offload to object store failed: %s", report_id, exc)
        return None


def _load_offloaded_report(storage_uri: str) -> dict[str, Any] | None:
    """Fetch a full report from object storage by its s3://bucket/object URI."""
    if not storage_uri.startswith("s3://"):
        return None
    bucket, _, object_name = storage_uri[len("s3://"):].partition("/")
    try:
        from storage.object_store.minio_client import ObjectStore
        store = ObjectStore()
        if not store.is_available():
            return None
        data = store.get_bytes(bucket, object_name)
        return json.loads(data.decode("utf-8")) if data else None
    except Exception as exc:
        logger.warning("Loading offloaded report %s failed: %s", storage_uri, exc)
        return None


# Log families worth keeping as an additional session-addressable fast copy.
# The complete immutable input batch is already archived under
# ``raw-logs/imports/<owner>/<job>/...`` before parsing; this small mirror is
# not the raw retention policy.
#
# Every calibration the project has — scan time, recoat time, machine time as
# the "actual" in predicted-vs-actual — reads exactly one family: time_log. On
# this shop's 19 real prints that is 3 MB total (~160 KB per print), against
# 10 GB for the full log set, of which stateFlow alone is 9 GB (and is already
# skipped at ingest).
#
# This matters for the shared-NAS setup: without this direct copy, a consumer
# not import a print themselves gets None from every calibration path and the
# accuracy loop silently falls back to wall-clock time — which includes
# operator pauses, 18 h of 47.6 on one real build. Replicating 160 KB per print
# fixes that without downloading/extracting the complete raw batch.
_SHARED_LOG_FAMILIES = frozenset({SourceFileFamily.time_log})


def _shared_log_object_name(session_id: str, file_name: str) -> str:
    return f"{session_id}/{file_name}"


def mirror_logs_to_object_store(session_id: str, files: list[IngestedFile]) -> int:
    """Copy calibration-critical logs to a session-addressable fast path.

    Best-effort: object storage being down must never fail an import, since the
    on-disk copy is still the primary. Returns how many files were stored.
    """
    from pathlib import Path

    from storage.object_store.minio_client import ObjectStore

    candidates = [
        f for f in files
        if f.classification.family in _SHARED_LOG_FAMILIES and f.path and Path(f.path).exists()
    ]
    if not candidates:
        return 0
    try:
        store = ObjectStore()
        if not store.is_available():
            return 0
        bucket = store.settings.minio_bucket_raw
        for f in candidates:
            name = f.classification.file_name or Path(f.relative_path).name
            store.put_file(bucket, _shared_log_object_name(session_id, name), Path(f.path))
    except Exception as exc:
        logger.warning("mirror_logs_to_object_store(%s) failed: %s", session_id, exc)
        return 0
    return len(candidates)


def _fetch_shared_log(session_id: str, file_name: str) -> "Path | None":  # noqa: F821
    """Pull a mirrored log into a temp file so the parsers can read a path."""
    import tempfile
    from pathlib import Path

    from storage.object_store.minio_client import ObjectStore

    try:
        store = ObjectStore()
        data = store.get_bytes(
            store.settings.minio_bucket_raw, _shared_log_object_name(session_id, file_name)
        )
    except Exception:
        return None
    if not data:
        return None
    tmp = Path(tempfile.gettempdir()) / "pc-shared-logs" / session_id
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / file_name
    path.write_bytes(data)
    return path


def _rehydrate_parse_results(
    files: list[IngestedFile], session_id: str | None = None,
) -> list[IngestedFile]:
    """Re-parse files that were stored without parse_result (events stripped).

    The session payload keeps slim files for size; consumers that need events
    (report generation, every calibration) re-read them from the original
    on-disk path.

    When the file is not on this machine's disk — the normal case for a print
    imported by a different operator against a shared database — the mirrored
    copy in object storage is used instead. Files available from neither keep
    their slim form.
    """
    from pathlib import Path

    need: list[tuple[IngestedFile, Path]] = []
    for f in files:
        if f.parse_result is not None:
            continue
        if f.path and Path(f.path).exists():
            need.append((f, Path(f.path)))
        elif session_id and f.classification.family in _SHARED_LOG_FAMILIES:
            name = f.classification.file_name or Path(f.relative_path).name
            shared = _fetch_shared_log(session_id, name)
            if shared:
                need.append((f, shared))
    if not need:
        return files
    try:
        from parsers.base.base import ParserContext
        from profiles.m350.profile import build_registry, get_profile
        registry = build_registry()
        profile = get_profile()
        for f, path in need:
            ctx = ParserContext(
                profile_id=profile.profile_id,
                profile_version=profile.version,
                signal_mappings=profile.signal_mappings,
            )
            f.parse_result = registry.parse(path, f.classification.family, ctx)
    except Exception as exc:
        logger.warning("Rehydrate parse results failed: %s", exc)
    return files


class RuntimeRepository:
    """Persistence boundary for API/runtime workflows.

    Heavy parser outputs are kept as JSON payloads on the session/report rows until
    the deeper normalized repositories are wired end-to-end.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def flush(self) -> None:
        """Flush pending changes within the unit of work; the boundary commits."""
        self.db.flush()

    def _upsert(self, entity_class, entity_id: str, id_field: str, values: dict[str, Any]) -> Any:
        """Helper for save-or-update pattern - reduces boilerplate."""
        existing = self.db.get(entity_class, entity_id)
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            return existing
        else:
            entity = entity_class(**{id_field: entity_id, **values})
            self.db.add(entity)
            return entity

    def save_import_job(self, job: ImportJobRecord) -> None:
        data = job.model_dump()
        values = {
            "owner_node_id": job.owner_node_id,
            "print_record_id": job.print_record_id,
            "source_path": job.source_path,
            "source_name": job.source_name,
            "source_kind": job.source_kind,
            "status": job.status.value,
            "detected_at": job.detected_at,
            "updated_at": job.updated_at,
            "confirmation_deadline": job.confirmation_deadline,
            "confirmed_by": job.confirmed_by,
            "confirmed_at": job.confirmed_at,
            "postponed_until": job.postponed_until,
            "lease_owner": job.lease_owner,
            "lease_until": job.lease_until,
            "lease_generation": job.lease_generation,
            "ignored_by": job.ignored_by,
            "ignored_at": job.ignored_at,
            "last_stability_check_at": job.last_stability_check_at,
            "stability_check_attempts": job.stability_check_attempts,
            "file_snapshot": jsonable_encoder(data["file_snapshot"]),
            "checksum_manifest": jsonable_encoder(data["checksum_manifest"]),
            "source_objects": jsonable_encoder(data["source_objects"]),
            "session_ids": jsonable_encoder(data["session_ids"]),
            "report_ids": jsonable_encoder(data["report_ids"]),
            "missing_context_questions": jsonable_encoder(data["missing_context_questions"]),
            "notification_log": jsonable_encoder(data["notification_log"]),
            "error": job.error,
            "audit_trail": jsonable_encoder(data["audit_trail"]),
        }
        self._upsert(ImportJob, job.import_job_id, "import_job_id", values)

    def get_import_job(self, import_job_id: str) -> ImportJobRecord | None:
        row = self.db.get(ImportJob, import_job_id)
        return _import_job_record_from_row(row) if row else None

    def get_import_job_for_update(self, import_job_id: str) -> ImportJobRecord | None:
        """Read and lock an import row for an operator state transition."""
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.import_job_id == import_job_id)
            .with_for_update()
        )
        return _import_job_record_from_row(row) if row else None

    def lock_import_candidate(self, owner_node_id: str, source_path: str) -> None:
        """Serialize detection of the same local batch on PostgreSQL.

        Watcher and browser upload can report one freshly-created directory at
        the same time. A transaction-scoped advisory lock closes the list→add
        race without imposing a global unique constraint that would prevent a
        legitimately changed file from being re-imported at the same path.
        SQLite tests are single-process and need no equivalent.
        """
        if self.db.get_bind().dialect.name != "postgresql":
            return
        digest = hashlib.sha256(
            f"{owner_node_id}\0{source_path}".encode("utf-8")
        ).digest()
        lock_key = int.from_bytes(digest[:8], "big", signed=True)
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    def list_import_jobs(
        self,
        owner_node_id: str | None = None,
        *,
        skip: int = 0,
        limit: int | None = None,
    ) -> list[ImportJobRecord]:
        query = select(ImportJob)
        if owner_node_id is not None:
            query = query.where(ImportJob.owner_node_id == owner_node_id)
        query = query.order_by(ImportJob.detected_at.desc()).offset(max(0, skip))
        if limit is not None:
            query = query.limit(max(0, limit))
        rows = self.db.scalars(query).all()
        return [_import_job_record_from_row(row) for row in rows]

    def list_import_jobs_by_source_path(
        self,
        *,
        owner_node_id: str,
        source_path: str,
    ) -> list[ImportJobRecord]:
        """Only versions of one local path, newest first."""
        rows = self.db.scalars(
            select(ImportJob)
            .where(
                ImportJob.owner_node_id == owner_node_id,
                ImportJob.source_path == source_path,
            )
            .order_by(ImportJob.detected_at.desc())
        ).all()
        return [_import_job_record_from_row(row) for row in rows]

    def has_terminal_import_job_by_name(
        self,
        *,
        owner_node_id: str,
        source_name: str,
    ) -> bool:
        """Cheap hint used before any potentially long local hashing."""
        found = self.db.scalar(
            select(ImportJob.import_job_id)
            .where(
                ImportJob.owner_node_id == owner_node_id,
                ImportJob.source_name == source_name,
                ImportJob.status.in_(("done", "needs_operator_context")),
            )
            .limit(1)
        )
        return found is not None

    def list_terminal_import_jobs_by_name(
        self,
        *,
        owner_node_id: str,
        source_name: str,
    ) -> list[ImportJobRecord]:
        """Completed imports that may have the same content at another path."""
        rows = self.db.scalars(
            select(ImportJob)
            .where(
                ImportJob.owner_node_id == owner_node_id,
                ImportJob.source_name == source_name,
                ImportJob.status.in_(("done", "needs_operator_context")),
            )
            .order_by(ImportJob.detected_at.desc())
        ).all()
        return [_import_job_record_from_row(row) for row in rows]

    def count_import_jobs(self, owner_node_id: str | None = None) -> int:
        query = select(func.count()).select_from(ImportJob)
        if owner_node_id is not None:
            query = query.where(ImportJob.owner_node_id == owner_node_id)
        return int(self.db.scalar(query) or 0)

    def latest_import_job(self, owner_node_id: str) -> ImportJobRecord | None:
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.owner_node_id == owner_node_id)
            .order_by(ImportJob.updated_at.desc())
            .limit(1)
        )
        return _import_job_record_from_row(row) if row else None

    def claim_next_import_job(
        self,
        *,
        owner_node_id: str,
        lease_owner: str,
        now: datetime | None = None,
        lease_seconds: int = 900,
    ) -> ImportJobRecord | None:
        """Atomically lease one due import owned by this operator PC.

        ``owner_node_id`` is stable across container restarts; ``lease_owner``
        identifies this particular worker process.  Other PCs cannot see the
        row through this claim even though the database itself is shared.
        """
        now = now or datetime.now(timezone.utc)
        due = or_(
            ImportJob.status == "checking_stability",
            and_(
                ImportJob.status == "postponed",
                ImportJob.confirmed_by.is_not(None),
                ImportJob.postponed_until.is_not(None),
                ImportJob.postponed_until <= now,
            ),
        )
        lease_available = or_(
            ImportJob.lease_until.is_(None),
            ImportJob.lease_until < now,
        )
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.owner_node_id == owner_node_id, due, lease_available)
            .order_by(ImportJob.updated_at, ImportJob.detected_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.lease_owner = lease_owner
        row.lease_until = now + timedelta(seconds=max(60, lease_seconds))
        row.lease_generation += 1
        row.updated_at = now
        self.db.flush()
        return _import_job_record_from_row(row)

    def renew_import_job_lease(
        self,
        import_job_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> bool:
        """Keep a long local parse leased without holding a NAS transaction.

        Renewal is fenced by both the worker-process identity and generation.
        It never revives an expired lease, which could already have been
        reclaimed by a restarted worker on the same operator PC.
        """
        now = now or datetime.now(timezone.utc)
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.import_job_id == import_job_id)
            .with_for_update()
        )
        if row is None or row.lease_owner != lease_owner:
            return False
        if row.lease_generation != lease_generation or row.lease_until is None:
            return False
        lease_until = row.lease_until
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if lease_until <= now:
            return False
        row.lease_until = now + timedelta(seconds=max(60, lease_seconds))
        row.updated_at = now
        self.db.flush()
        return True

    def save_notifications(self, notifications: Iterable[NotificationMessage]) -> None:
        for notification in notifications:
            existing = self.db.get(NotificationOutbox, notification.notification_id)
            values = notification.model_dump(mode="json")
            if existing:
                existing.owner_node_id = values["owner_node_id"]
                existing.channel = values["channel"]
                existing.text = values["text"]
                existing.buttons = values["buttons"]
                existing.metadata_json = values["metadata"]
            else:
                self.db.add(
                    NotificationOutbox(
                        notification_id=values["notification_id"],
                        owner_node_id=values["owner_node_id"],
                        channel=values["channel"],
                        text=values["text"],
                        buttons=values["buttons"],
                        metadata_json=values["metadata"],
                        created_at=notification.created_at,
                    )
                )

    def list_pending_notifications(
        self,
        *,
        owner_node_id: str,
        channel: str = "telegram",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.owner_node_id == owner_node_id,
                NotificationOutbox.channel == channel,
                NotificationOutbox.status == "pending",
            )
            .order_by(NotificationOutbox.created_at.asc())
            .limit(limit)
        ).all()
        return [
            jsonable_encoder(
                {
                    "notification_id": row.notification_id,
                    "owner_node_id": row.owner_node_id,
                    "channel": row.channel,
                    "text": row.text,
                    "buttons": row.buttons or [],
                    "metadata": row.metadata_json or {},
                    "created_at": row.created_at,
                    "status": row.status,
                }
            )
            for row in rows
        ]

    def mark_notification_sent(
        self,
        notification_id: str,
        *,
        owner_node_id: str,
        status_value: str = "sent",
        error: str | None = None,
    ) -> bool:
        row = self.db.scalar(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.notification_id == notification_id,
                NotificationOutbox.owner_node_id == owner_node_id,
            )
            .with_for_update()
        )
        if not row:
            return False
        row.status = status_value
        row.sent_at = datetime.now(timezone.utc) if status_value == "sent" else row.sent_at
        row.error = error
        return True

    def save_session_payload(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        origin_compute_node_id: str | None = None,
    ) -> None:
        from core.versioning.constants import ANALYSIS_VERSION

        if origin_compute_node_id is None:
            from core.config.settings import get_settings

            origin_compute_node_id = get_settings().compute_node_id

        existing = self.db.get(BuildSession, session_id)
        context = {"runtime_payload": _sanitize_for_json(jsonable_encoder(payload))}
        group = payload.get("group", {}) or {}

        def _parse_ts(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                from datetime import timezone as _tz
                dt = datetime.fromisoformat(value)
                return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
            except Exception:
                return None

        start_ts = _parse_ts(group.get("start_ts"))
        end_ts   = _parse_ts(group.get("end_ts"))
        confidence = float(group.get("confidence") or 0.0)
        # The classification lives in the payload, but the column is what SQL can
        # filter and index on. It used to be written only at row creation (i.e.
        # never — creation passes no classification), so every session in the DB
        # kept the "INCOMPLETE_OR_UNKNOWN" default forever while the payload said
        # REAL_PRINT. Readers all worked around it with `payload or column`, which
        # hid the drift and made "only real prints" impossible to express in SQL.
        classification = group.get("classification") or None
        classification_confidence = float(group.get("classification_confidence") or confidence or 0.0)

        if existing:
            require_compute_owner(
                entity_type="session",
                entity_id=session_id,
                origin_compute_node_id=existing.origin_compute_node_id,
                requested_compute_node_id=origin_compute_node_id,
            )
            existing.context = context
            existing.analysis_version = ANALYSIS_VERSION
            existing.updated_at = datetime.now(timezone.utc)
            # Overwrite, don't fill-if-empty: a re-import re-derives the print
            # span from the full file set, and that recomputed value is the more
            # accurate one. Keeping the first-ever value froze a span computed
            # from a partial file set (or from a mis-grouped session) forever —
            # and that span is what predicted-vs-actual calibration divides by.
            if start_ts:
                existing.start_ts = start_ts
            if end_ts:
                existing.end_ts = end_ts
            if confidence:
                existing.grouping_confidence = confidence
            if classification:
                existing.classification = classification
                existing.classification_confidence = classification_confidence
        else:
            self.db.add(
                BuildSession(
                    session_id=session_id,
                    origin_compute_node_id=origin_compute_node_id,
                    status="runtime_payload",
                    context=context,
                    grouping_confidence=confidence,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    analysis_version=ANALYSIS_VERSION,
                    **({"classification": classification,
                        "classification_confidence": classification_confidence}
                       if classification else {}),
                )
            )
        self.flush()

    def save_sessions(
        self,
        sessions: dict[str, dict[str, Any]],
        *,
        origin_compute_node_id: str | None = None,
    ) -> None:
        for session_id, payload in sessions.items():
            self.save_session_payload(
                session_id,
                payload,
                origin_compute_node_id=origin_compute_node_id,
            )

    def get_session_payload(self, session_id: str) -> dict[str, Any] | None:
        row = self.db.get(BuildSession, session_id)
        if not row:
            return None
        return (row.context or {}).get("runtime_payload")

    def get_session_origin_compute_node_id(self, session_id: str) -> str | None:
        return self.db.scalar(
            select(BuildSession.origin_compute_node_id).where(
                BuildSession.session_id == session_id
            )
        )

    def require_session_compute_owner(
        self,
        session_id: str,
        *,
        requested_compute_node_id: str | None = None,
    ) -> BuildSession | None:
        row = self.db.get(BuildSession, session_id)
        if row is None:
            return None
        if requested_compute_node_id is None:
            from core.config.settings import get_settings

            requested_compute_node_id = get_settings().compute_node_id
        require_compute_owner(
            entity_type="session",
            entity_id=session_id,
            origin_compute_node_id=row.origin_compute_node_id,
            requested_compute_node_id=requested_compute_node_id,
        )
        return row

    def list_session_ids(self) -> set[str]:
        """Every known session id, without loading the payloads.

        Callers that only need to test membership (the re-import guard) must use
        this: list_session_payloads() deserialises every session's full JSON
        context, which is orders of magnitude more work and memory.
        """
        return set(self.db.scalars(select(BuildSession.session_id)).all())

    def list_session_payloads(
        self,
        *,
        origin_compute_node_id: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        stmt = select(BuildSession).order_by(BuildSession.created_at.desc())
        if origin_compute_node_id is not None:
            stmt = stmt.where(BuildSession.origin_compute_node_id == origin_compute_node_id)
        rows = self.db.scalars(stmt).all()
        payloads: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            payload = (row.context or {}).get("runtime_payload")
            if payload:
                payloads.append((row.session_id, payload))
        return payloads

    def get_session_files(self, session_id: str, rehydrate: bool = False) -> list[IngestedFile] | None:
        """Reconstruct the session's IngestedFile list from its stored payload.

        Stored files are slim (parse_result/events stripped to keep the payload
        tiny). When ``rehydrate=True`` (e.g. report generation needs the events),
        re-parse each file from its on-disk path; files whose source is gone are
        returned as-is (slim).
        """
        payload = self.get_session_payload(session_id)
        if not payload:
            return None
        files = [IngestedFile.model_validate(item) for item in payload.get("files", [])]
        if rehydrate:
            self.require_session_compute_owner(session_id)
            # session_id lets the mirrored copy stand in when the file is not on
            # this machine — the normal case against a shared database.
            files = _rehydrate_parse_results(files, session_id)
        return files

    def save_report(self, report: dict[str, Any], report_type: str = "session") -> None:
        report_id = report["report_id"]
        # Offload the full report (incl. complete timeline) to object storage so a
        # huge timeline can't blow the PostgreSQL 1 GB per-value limit. Postgres
        # keeps only a slim copy (bounded timeline preview) + the storage pointer.
        storage_uri = _offload_report(report_id, report)
        values = {
            "session_id": report.get("session_id"),
            "report_type": report_type,
            "storage_uri": storage_uri,
            "payload": jsonable_encoder(_report_payload(report)),
            "version_metadata": jsonable_encoder(report.get("version_metadata", {})),
        }
        self._upsert(ReportArtifact, report_id, "report_id", values)
        self.flush()

    def save_reports(self, reports: dict[str, dict[str, Any]]) -> None:
        for report in reports.values():
            self.save_report(report)

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        row = self.db.get(ReportArtifact, report_id)
        if not row:
            return None
        if row.storage_uri:
            full = _load_offloaded_report(row.storage_uri)
            if full is not None:
                return full
        return row.payload

    def list_reports_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(ReportArtifact).where(ReportArtifact.session_id == session_id).order_by(ReportArtifact.generated_at.desc())
        ).all()
        return [row.payload for row in rows]

    def get_latest_report_for_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the newest saved report, expanding its MinIO payload if present."""
        row = self.db.scalar(
            select(ReportArtifact)
            .where(ReportArtifact.session_id == session_id)
            .order_by(ReportArtifact.generated_at.desc(), ReportArtifact.report_id.desc())
            .limit(1)
        )
        if row is None:
            return None
        if row.storage_uri:
            full = _load_offloaded_report(row.storage_uri)
            if full is not None:
                return full
        return row.payload

    def save_operator_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event = jsonable_encoder(event)
        event_id = event["event_id"]
        timestamp = _parse_datetime(event.get("timestamp")) or datetime.now(timezone.utc)
        values = {
            "timestamp": timestamp,
            "created_by": event.get("created_by", "unknown"),
            "source_channel": event.get("source_channel", "api"),
            "event_type": event.get("event_type", "operator_observation"),
            "printer_id": event.get("printer_id"),
            "session_id": event.get("session_id"),
            "build_id": event.get("build_id"),
            "layer": event.get("layer"),
            "material": event.get("material"),
            "powder_batch": event.get("powder_batch"),
            "gas_type": event.get("gas_type"),
            "gas_cylinder_id": event.get("gas_cylinder_id"),
            "component": event.get("component"),
            "action": event.get("action"),
            "value": event.get("value"),
            "unit": event.get("unit"),
            "note": event.get("note"),
            "attachments": event.get("attachments", []),
            "confidence": float(event.get("confidence", 0.5)),
            "verification_status": event.get("verification_status", VerificationStatus.unverified.value),
            "linked_machine_events": event.get("linked_machine_events", []),
            "audit_trail": event.get("audit_trail", []),
        }
        self._upsert(OperatorEvent, event_id, "event_id", values)
        self.flush()
        return event

    def get_operator_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.get(OperatorEvent, event_id)
        return _operator_event_to_dict(row) if row else None

    def list_operator_events(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(OperatorEvent).order_by(OperatorEvent.timestamp.desc())).all()
        return [_operator_event_to_dict(row) for row in rows]

    def save_operator_journal_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry = jsonable_encoder(entry)
        entry_id = entry["journal_entry_id"]
        values = {
            "created_at": _parse_datetime(entry.get("created_at")) or datetime.now(timezone.utc),
            "source_channel": entry.get("source_channel", "telegram"),
            "created_by": entry.get("created_by", "unknown"),
            "printer_id": entry.get("printer_id"),
            "session_id": entry.get("session_id"),
            "project_id": entry.get("project_id"),
            "platform_id": entry.get("platform_id"),
            "duplication_group_id": entry.get("duplication_group_id"),
            "entry_kind": entry.get("entry_kind", "operator_input"),
            "raw_text": entry.get("raw_text"),
            "normalized_text": entry.get("normalized_text"),
            "voice_attachment": entry.get("voice_attachment"),
            "transcription": entry.get("transcription", {}),
            "operator_event_id": entry.get("operator_event_id"),
            "status": entry.get("status", "draft"),
            "duplicate_targets": entry.get("duplicate_targets", []),
            "export_payload": entry.get("export_payload", {}),
            "audit_trail": entry.get("audit_trail", []),
        }
        self._upsert(OperatorJournalEntry, entry_id, "journal_entry_id", values)
        return entry

    def get_operator_journal_entry(self, journal_entry_id: str) -> dict[str, Any] | None:
        row = self.db.get(OperatorJournalEntry, journal_entry_id)
        return _operator_journal_entry_to_dict(row) if row else None

    def list_operator_journal_entries(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(OperatorJournalEntry).order_by(OperatorJournalEntry.created_at.desc())).all()
        return [_operator_journal_entry_to_dict(row) for row in rows]

    @staticmethod
    def _quality_outcome_values(outcome: dict[str, Any]) -> dict[str, Any]:
        """Map an API payload to the persisted quality-outcome columns."""
        outcome = jsonable_encoder(outcome)
        return {
            "print_record_id": outcome.get("print_record_id"),
            "session_id": outcome.get("session_id"),
            "build_id": outcome.get("build_id"),
            "part_id": outcome.get("part_id"),
            "timestamp": _parse_datetime(outcome.get("timestamp")) or datetime.now(timezone.utc),
            "inspection_type": outcome.get("inspection_type", "visual"),
            "result": outcome.get("result", "unknown"),
            "is_final": bool(outcome.get("is_final", False)),
            "supersedes_outcome_id": outcome.get("supersedes_outcome_id"),
            "inspection_result": outcome.get("inspection_result"),
            "defect_type": outcome.get("defect_type"),
            "defect_location": outcome.get("defect_location"),
            "layer_range": outcome.get("layer_range"),
            "severity": outcome.get("severity"),
            "notes": outcome.get("notes"),
            "attachments": outcome.get("attachments", []),
            "created_by": outcome.get("created_by", "unknown"),
            "evidence_links": outcome.get("evidence_links", []),
        }

    def create_quality_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        """Insert an inspection row without ever updating an existing verdict."""
        outcome = jsonable_encoder(outcome)
        outcome_id = str(outcome["outcome_id"])
        if self.db.get(QualityOutcome, outcome_id) is not None:
            raise ValueError(f"Quality outcome '{outcome_id}' already exists")

        supersedes_id = outcome.get("supersedes_outcome_id")
        if bool(outcome.get("is_final")):
            print_record_id = outcome.get("print_record_id")
            if not print_record_id:
                raise ValueError("A final quality verdict must belong to a print card")

            # All final verdicts for one card form one append-only chain. Locking
            # the card serialises two operator PCs before either reads the head;
            # the second request therefore sees the first one's committed head
            # and receives a conflict instead of creating a competing branch.
            from domain.models.prints import PrintRecord

            card = self.db.scalar(
                select(PrintRecord)
                .where(PrintRecord.record_id == str(print_record_id))
                .with_for_update()
            )
            if card is None:
                raise QualityOutcomeConflict("Карточка печати больше не существует")
            current = self.db.scalar(
                select(QualityOutcome)
                .where(
                    QualityOutcome.print_record_id == str(print_record_id),
                    QualityOutcome.is_final.is_(True),
                )
                .order_by(QualityOutcome.timestamp.desc(), QualityOutcome.outcome_id.desc())
                .with_for_update()
                .limit(1)
            )
            if current is None:
                if supersedes_id:
                    raise QualityOutcomeConflict(
                        "Первый итог контроля не должен ссылаться на исправляемую запись"
                    )
            elif str(supersedes_id or "") != current.outcome_id:
                raise QualityOutcomeConflict(
                    "Итог контроля уже изменён; обновите карточку и повторите исправление"
                )
        elif supersedes_id:
            raise ValueError("Only a final verdict can supersede another final verdict")

        row = QualityOutcome(
            outcome_id=outcome_id,
            **self._quality_outcome_values(outcome),
        )
        self.db.add(row)
        self.flush()
        return outcome

    def save_quality_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible name for the create-only persistence operation."""
        return self.create_quality_outcome(outcome)

    def link_quality_outcome_session(self, outcome_id: str, session_id: str) -> dict[str, Any]:
        """Link an interim observation without exposing a generic row update."""
        row = self.db.get(QualityOutcome, outcome_id)
        if row is None:
            raise ValueError(f"Quality outcome '{outcome_id}' does not exist")
        if row.is_final:
            raise ValueError("Final quality outcomes are immutable")
        row.session_id = session_id
        self.flush()
        return _quality_outcome_to_dict(row)

    def get_quality_outcome(self, outcome_id: str) -> dict[str, Any] | None:
        row = self.db.get(QualityOutcome, outcome_id)
        return _quality_outcome_to_dict(row) if row else None

    def list_quality_outcomes(
        self,
        *,
        session_id: str | None = None,
        print_record_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(QualityOutcome)
        if session_id is not None:
            stmt = stmt.where(QualityOutcome.session_id == session_id)
        if print_record_id is not None:
            stmt = stmt.where(QualityOutcome.print_record_id == print_record_id)
        rows = self.db.scalars(
            stmt.order_by(QualityOutcome.timestamp.desc(), QualityOutcome.outcome_id.desc())
        ).all()
        return [_quality_outcome_to_dict(row) for row in rows]

    def save_historical_verdict(self, verdict: dict[str, Any]) -> None:
        verdict = jsonable_encoder(verdict)
        verdict_id = verdict["verdict_id"]
        values = {
            "created_at": _parse_datetime(verdict.get("created_at")) or datetime.now(timezone.utc),
            "analysis_window": verdict.get("analysis_window", {}),
            "max_iterations": verdict.get("max_iterations", 10),
            "completed_iterations": verdict.get("completed_iterations", 0),
            "status": verdict.get("status", "completed"),
            "verdict": verdict.get("verdict", "no_new_pattern"),
            "confidence": verdict.get("confidence", 0.0),
            "summary": verdict.get("summary", ""),
            "new_insights": verdict.get("new_insights", []),
            "updated_insights": verdict.get("updated_insights", []),
            "dismissed_candidates": verdict.get("dismissed_candidates", []),
            "counterexamples": verdict.get("counterexamples", []),
            "missing_data": verdict.get("missing_data", []),
            "recommended_actions": verdict.get("recommended_actions", []),
            "affected_sessions": verdict.get("affected_sessions", []),
            "analysis_version": verdict.get("analysis_version", "0.1.0"),
            "evidence_links": verdict.get("evidence_links", []),
        }
        self._upsert(HistoricalAnalysisVerdict, verdict_id, "verdict_id", values)

    def list_historical_verdicts(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(HistoricalAnalysisVerdict).order_by(HistoricalAnalysisVerdict.created_at.desc())).all()
        return [_historical_verdict_to_dict(row) for row in rows]

    def get_historical_verdict(self, verdict_id: str) -> dict[str, Any] | None:
        row = self.db.get(HistoricalAnalysisVerdict, verdict_id)
        return _historical_verdict_to_dict(row) if row else None

    def save_insight(self, insight: dict[str, Any]) -> dict[str, Any]:
        insight = jsonable_encoder(insight)
        insight_id = insight["insight_id"]
        values = _pattern_insight_values(insight)
        self._upsert(PatternInsight, insight_id, "insight_id", values)
        return insight

    def get_insight(self, insight_id: str) -> dict[str, Any] | None:
        row = self.db.get(PatternInsight, insight_id)
        return _pattern_insight_to_dict(row) if row else None

    def list_insights(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(PatternInsight).order_by(PatternInsight.created_at.desc())).all()
        return [_pattern_insight_to_dict(row) for row in rows]

    def save_knowledge(self, item: dict[str, Any]) -> dict[str, Any]:
        item = jsonable_encoder(item)
        knowledge_id = item["knowledge_id"]
        values = _knowledge_values(item)
        self._upsert(ConfirmedKnowledge, knowledge_id, "knowledge_id", values)
        return item

    def get_knowledge(self, knowledge_id: str) -> dict[str, Any] | None:
        row = self.db.get(ConfirmedKnowledge, knowledge_id)
        return _knowledge_to_dict(row) if row else None

    def list_knowledge(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(ConfirmedKnowledge).order_by(ConfirmedKnowledge.confirmed_at.desc())).all()
        return [_knowledge_to_dict(row) for row in rows]

    def save_source_file(self, source_file_id: str, session_id: str | None,
                         object_uri: str | None,
                         file_name: str, checksum: str, original_path: str,
                         size_bytes: int, family: str, role: str, 
                         encoding: str | None = None, data_quality_status: str = "ok",
                         first_ts: datetime | None = None, last_ts: datetime | None = None,
                         metadata: dict[str, Any] | None = None) -> SourceFile:
        """Save a source file record to the database."""
        values = {
            "session_id": session_id,
            "object_uri": object_uri,
            "original_path": original_path,
            "file_name": file_name,
            "checksum": checksum,
            "size_bytes": size_bytes,
            "family": family,
            "role": role,
            "encoding": encoding,
            "data_quality_status": data_quality_status,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "parse_status": "parsed",
            "metadata_json": metadata or {},
        }
        result = self._upsert(SourceFile, source_file_id, "source_file_id", values)
        self.db.flush()
        return result

    def save_canonical_event(self, event_id: str, session_id: str | None,
                            source_file_id: str | None, ts: datetime | None,
                            raw_timestamp: str | None, event_type: str,
                            subsystem: str | None = None, phase: str | None = None,
                            severity: str = "info", confidence: float = 1.0,
                            layer: int | None = None, source_line: int | None = None,
                            source_offset: int | None = None, raw_excerpt: str | None = None,
                            payload: dict[str, Any] | None = None,
                            evidence_kind: str = "machine_log",
                            provenance: list[dict[str, Any]] | None = None) -> CanonicalEvent:
        """Save a canonical event to the database."""
        values = {
            "session_id": session_id,
            "source_file_id": source_file_id,
            "ts": ts,
            "raw_timestamp": raw_timestamp,
            "ts_uncertainty": 0.0,
            "layer": layer,
            "source_line": source_line,
            "source_offset": source_offset,
            "raw_excerpt": raw_excerpt,
            "subsystem": subsystem,
            "phase": phase,
            "event_type": event_type,
            "severity": severity,
            "confidence": confidence,
            "payload": payload or {},
            "evidence_kind": evidence_kind,
            "provenance": provenance or [{"source": "parser"}],
        }
        return self._upsert(CanonicalEvent, event_id, "event_id", values)

    def save_canonical_event_batch(self, events: list[dict[str, Any]]) -> int:
        """Idempotent bounded writes; avoid a NAS SELECT per machine event.

        The caller owns commit/rollback and lease checks. Preserve the original
        created_at on retries, updating the same fields as save_canonical_event.
        """
        if not events:
            return 0
        dialect = self.db.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            for event in events:
                self.save_canonical_event(**event)
            return len(events)
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        rows = {}
        for event in events:
            row = {key: None for key in (
                "session_id", "source_file_id", "ts", "raw_timestamp", "layer",
                "source_line", "source_offset", "raw_excerpt", "subsystem", "phase",
            )}
            row.update({"severity": "info", "confidence": 1.0, "evidence_kind": "machine_log"})
            row.update(event)
            row["ts_uncertainty"] = 0.0
            row["payload"] = row.get("payload") or {}
            row["provenance"] = row.get("provenance") or [{"source": "parser"}]
            rows[row["event_id"]] = row
        values = list(rows.values())
        self.db.flush()
        # 25 rows also fit older SQLite's 999-bind limit.
        for offset in range(0, len(values), 25):
            statement = insert(CanonicalEvent).values(values[offset:offset + 25])
            updates = {key: getattr(statement.excluded, key) for key in values[0] if key != "event_id"}
            self.db.execute(statement.on_conflict_do_update(index_elements=["event_id"], set_=updates))
        return len(events)

    def list_canonical_events_by_session(self, session_id: str) -> list[CanonicalEvent]:
        """Get all canonical events for a session."""
        return self.db.scalars(
            select(CanonicalEvent)
            .where(CanonicalEvent.session_id == session_id)
            .order_by(CanonicalEvent.ts)
        ).all()

    def list_source_files_by_session(self, session_id: str) -> list[SourceFile]:
        """Get all source files for a session."""
        return self.db.scalars(
            select(SourceFile)
            .where(SourceFile.session_id == session_id)
            .order_by(SourceFile.created_at)
        ).all()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


_IMPORT_JOB_SCALARS = (
    "import_job_id", "owner_node_id", "print_record_id", "source_path", "source_name", "source_kind", "status",
    "detected_at", "updated_at", "confirmation_deadline", "confirmed_by",
    "confirmed_at", "postponed_until", "lease_owner", "lease_until", "lease_generation",
    "ignored_by", "ignored_at",
    "last_stability_check_at", "stability_check_attempts", "error",
)
_IMPORT_JOB_DICTS = ("file_snapshot", "checksum_manifest", "source_objects")
_IMPORT_JOB_LISTS = (
    "session_ids", "report_ids", "missing_context_questions",
    "notification_log", "audit_trail",
)


def _import_job_record_from_row(row: ImportJob) -> ImportJobRecord:
    """Rebuild an ImportJobRecord from its ORM row (shared by get + list)."""
    data: dict[str, Any] = {f: getattr(row, f) for f in _IMPORT_JOB_SCALARS}
    data.update({f: getattr(row, f) or {} for f in _IMPORT_JOB_DICTS})
    data.update({f: getattr(row, f) or [] for f in _IMPORT_JOB_LISTS})
    return ImportJobRecord.model_validate(data)


def _operator_event_to_dict(row: OperatorEvent) -> dict[str, Any]:
    return _model_to_dict(row, [
        "event_id", "timestamp", "created_at", "created_by", "source_channel",
        "event_type", "printer_id", "session_id", "build_id", "layer",
        "material", "powder_batch", "gas_type", "gas_cylinder_id",
        "component", "action", "value", "unit", "note", "attachments",
        "confidence", "verification_status", "linked_machine_events", "audit_trail"
    ])


def _operator_journal_entry_to_dict(row: OperatorJournalEntry) -> dict[str, Any]:
    return _model_to_dict(row, [
        "journal_entry_id", "created_at", "updated_at", "source_channel",
        "created_by", "printer_id", "session_id", "project_id", "platform_id",
        "duplication_group_id", "entry_kind", "raw_text", "normalized_text",
        "voice_attachment", "transcription", "operator_event_id", "status",
        "duplicate_targets", "export_payload", "audit_trail"
    ])


def _quality_outcome_to_dict(row: QualityOutcome) -> dict[str, Any]:
    return _model_to_dict(row, [
        "outcome_id", "print_record_id", "session_id", "build_id", "part_id", "timestamp",
        "inspection_type", "result", "is_final", "supersedes_outcome_id",
        "inspection_result", "defect_type", "defect_location",
        "layer_range", "severity", "notes", "attachments", "created_by", "evidence_links"
    ])


def _historical_verdict_to_dict(row: HistoricalAnalysisVerdict) -> dict[str, Any]:
    return _model_to_dict(row, [
        "verdict_id", "created_at", "analysis_window", "max_iterations",
        "completed_iterations", "status", "verdict", "confidence", "summary",
        "new_insights", "updated_insights", "dismissed_candidates",
        "counterexamples", "missing_data", "recommended_actions",
        "affected_sessions", "analysis_version", "evidence_links"
    ])


def _pattern_insight_values(insight: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": _parse_datetime(insight.get("created_at")) or datetime.now(timezone.utc),
        "updated_at": _parse_datetime(insight.get("updated_at")) or datetime.now(timezone.utc),
        "analysis_window": insight.get("analysis_window", {}),
        "printer_id": insight.get("printer_id"),
        "scope_filters": insight.get("scope_filters", {}),
        "insight_type": insight.get("insight_type", "manual"),
        "title": insight.get("title", "Untitled insight"),
        "description": insight.get("description", ""),
        "supporting_sessions": insight.get("supporting_sessions", []),
        "supporting_events": insight.get("supporting_events", []),
        "counterexamples": insight.get("counterexamples", []),
        "sample_size": insight.get("sample_size", 0),
        "effect_size": insight.get("effect_size"),
        "confidence": insight.get("confidence", 0.0),
        "causal_data_quality": insight.get("causal_data_quality", {}),
        "status": insight.get("status", "draft"),
        "generated_by": insight.get("generated_by", "system"),
        "analysis_version": insight.get("analysis_version", "0.1.0"),
        "recommended_action": insight.get("recommended_action"),
        "audit_trail": insight.get("audit_trail", []),
    }


def _pattern_insight_to_dict(row: PatternInsight) -> dict[str, Any]:
    return _model_to_dict(row, [
        "insight_id", "created_at", "updated_at", "analysis_window",
        "printer_id", "scope_filters", "insight_type", "title", "description",
        "supporting_sessions", "supporting_events", "counterexamples",
        "sample_size", "effect_size", "confidence", "causal_data_quality",
        "status", "generated_by", "analysis_version", "recommended_action", "audit_trail"
    ])


def _knowledge_values(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title", "Untitled knowledge"),
        "description": item.get("description", ""),
        "scope": item.get("scope", {}),
        "printer_profile": item.get("printer_profile"),
        "applicable_materials": item.get("applicable_materials", []),
        "applicable_conditions": item.get("applicable_conditions", {}),
        "supporting_insights": item.get("supporting_insights", []),
        "confirmed_by": item.get("confirmed_by", "system"),
        "confirmed_at": _parse_datetime(item.get("confirmed_at")) or datetime.now(timezone.utc),
        "confidence": item.get("confidence", 0.0),
        "status": item.get("status", "active"),
        "rule_implications": item.get("rule_implications", {}),
        "report_implications": item.get("report_implications", {}),
        "audit_trail": item.get("audit_trail", []),
    }


def _knowledge_to_dict(row: ConfirmedKnowledge) -> dict[str, Any]:
    return _model_to_dict(row, [
        "knowledge_id", "title", "description", "scope", "printer_profile",
        "applicable_materials", "applicable_conditions", "supporting_insights",
        "confirmed_by", "confirmed_at", "confidence", "status",
        "rule_implications", "report_implications", "audit_trail"
    ])
