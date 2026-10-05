"""Read and supplement published reports without computing under a SQL transaction.

These use cases own their transactions; pass a clean session. Object-store IO,
LLM and presentation run only after the detached SQL snapshot is closed.
"""

from copy import deepcopy
import hashlib
import json
import logging

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from core.config.settings import get_settings
from core.utils.json_data import sanitize_json
from core.versioning.provenance import stable_hash
from domain.models.prints import PrintRecord
from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.reports import ReportsRepository
from storage.repositories.runtime import RuntimeRepository
from storage.repositories.session_reads import SessionReadsRepository

logger = logging.getLogger(__name__)


class SessionReportError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def list_sessions(db, *, skip: int = 0, limit: int = 100) -> dict:
    try:
        reads = SessionReadsRepository(db)
        total = reads.count_payloads()
        rows = reads.list_groups(skip=skip, limit=limit)
    finally:
        db.rollback()
    return {"items": [{"session_id": sid, **group} for sid, group in rows],
            "total": total, "skip": skip, "limit": limit, "returned": len(rows)}


def _assert_current(session: dict, report: dict) -> None:
    context = session["context"]
    group = ((context.get("runtime_payload") or {}).get("group") or {})
    snapshot = group.get("analysis_snapshot")
    saved_snapshot = report.get("analysis_snapshot")
    if snapshot != saved_snapshot:
        raise SessionReportError("conflict", "Сохранённый отчёт относится к другой версии анализа. Повторите анализ на ПК-владельце.")
    publication = group.get("timing_publication_id")
    if publication and publication != (report.get("timing_publication") or {}).get("publication_id"):
        raise SessionReportError("conflict", "Сохранённый отчёт и измерения относятся к разным поколениям импорта.")


def _read_object(uri: str, *, object_store_factory=None) -> dict | None:
    """Best-effort full payload; the SQL projection remains the readable fallback."""
    if not uri.startswith("s3://"):
        return None
    from storage.object_store.minio_client import ObjectStore

    bucket, _, name = uri[5:].partition("/")
    if not bucket or not name:
        return None
    try:
        data = (object_store_factory or ObjectStore)().get_bytes(bucket, name)
        result = json.loads(data) if data else None
        return result if isinstance(result, dict) else None
    except Exception:
        return None


def prepare_report(report: dict, *, object_store_factory=None) -> dict:
    """Serialize/upload immutable full bytes before a SQL publication begins.

    Callers decide whether an unavailable store permits a bounded fallback.
    """
    from reporting.json_report.generator import _timeline_preview
    from storage.object_store.minio_client import ObjectStore

    full = sanitize_json(jsonable_encoder(report))
    payload = dict(full)
    if isinstance(payload.get("timeline"), list):
        payload["timeline"] = _timeline_preview(payload["timeline"])
    uri = None
    try:
        store = (object_store_factory or ObjectStore)()
        if store.is_available():
            data = json.dumps(full, ensure_ascii=False).encode("utf-8")
            checksum = hashlib.sha256(data).hexdigest()
            uri = store.put_bytes_verified(
                get_settings().minio_bucket_reports, f"{report['report_id']}/{checksum}.json", data,
            )
    except Exception as exc:
        logger.warning("Report %s offload failed: %s", report["report_id"], exc)
    return {"storage_uri": uri, "payload": payload,
            "version_metadata": full.get("version_metadata", {})}


def _expand_report(artifact: dict, *, object_store_factory=None, require_full: bool = False) -> dict:
    report = artifact["payload"]
    if (report.get("report_id") != artifact["report_id"]
            or report.get("session_id") != artifact["session_id"]):
        raise SessionReportError("conflict", "Сводка отчёта не соответствует сохранённой публикации.")
    if artifact["storage_uri"]:
        expanded = _read_object(artifact["storage_uri"], object_store_factory=object_store_factory)
        if expanded is None and require_full:
            raise SessionReportError("storage_unavailable", "Полный отчёт недоступен на NAS. Дополнение не сохранено.")
        if expanded is not None:
            # Historical IDs retain their own publication, not the latest report.
            if (expanded.get("report_id") != artifact["report_id"]
                    or expanded.get("session_id") != artifact["session_id"]
                    or expanded.get("analysis_snapshot") != report.get("analysis_snapshot")
                    or expanded.get("timing_publication") != report.get("timing_publication")):
                raise SessionReportError("conflict", "Объект отчёта не соответствует сохранённой публикации.")
            return expanded
    # The downloaded JSON is already detached. Copy the SQL fallback only if
    # it is actually returned; enhancement must not mutate its fencing input.
    return deepcopy(report)


def read_report_by_id(db, report_id: str, *, object_store_factory=None) -> dict:
    try:
        artifact = ReportsRepository(db).get(report_id)
        if artifact is None:
            raise SessionReportError("not_found", "Report not found")
    finally:
        db.rollback()
    return _expand_report(artifact, object_store_factory=object_store_factory)


def read_report(db, session_id: str, *, include_markdown: bool = False,
                object_store_factory=None) -> dict:
    try:
        reads = SessionReadsRepository(db)
        session = reads.session_snapshot(session_id)
        if session is None:
            raise SessionReportError("not_found", "Сессия не найдена")
        artifact = ReportsRepository(db).latest(session_id)
        if artifact is None:
            raise SessionReportError("conflict", "Сохранённого отчёта нет. Запустите анализ на ПК-владельце; чтение не запускает пересчёт.")
        report = artifact["payload"]
        _assert_current(session, report)
    finally:
        db.rollback()
    report = _expand_report(artifact, object_store_factory=object_store_factory)
    if include_markdown and not report.get("markdown"):
        from reporting.markdown_report.generator import generate_markdown_report

        report["markdown"] = generate_markdown_report(report)
    return report


async def enhance_report(db, report_id: str, *, compute_node_id: str,
                         provider=None, object_store_factory=None) -> dict:
    """Read → release SQL → LLM/upload → lock/recheck → publish atomically."""
    from reporting.llm.evidence_package import build_evidence_package
    from reporting.llm.providers.factory import get_llm_provider

    def check_session(session: dict | None, report: dict) -> None:
        if session is None:
            raise SessionReportError("conflict", "Report session no longer exists")
        try:
            require_compute_owner(entity_type="session", entity_id=report["session_id"],
                                  origin_compute_node_id=session["owner_node_id"],
                                  requested_compute_node_id=compute_node_id)
        except ComputeAffinityError as exc:
            raise SessionReportError("forbidden", f"LLM-дополнение выполняется только на ПК-владельце сессии. {exc}") from exc
        _assert_current(session, report)

    try:
        artifact = ReportsRepository(db).get(report_id)
        if artifact is None:
            raise SessionReportError("not_found", "Report not found")
        session_id = artifact["session_id"]
        session = SessionReadsRepository(db).session_snapshot(session_id) if session_id else None
        if session_id:
            check_session(session, artifact["payload"])
    finally:
        db.rollback()
    report = _expand_report(artifact, object_store_factory=object_store_factory, require_full=True)
    evidence = build_evidence_package(report).model_dump(mode="json")
    result = await (provider or get_llm_provider()).generate_markdown(evidence)
    if result.success:
        report["llm_markdown"] = result.content
    report.setdefault("llm_runs", []).append(result.__dict__)
    prepared = prepare_report(report, object_store_factory=object_store_factory)
    if prepared["storage_uri"] is None:
        raise SessionReportError("storage_unavailable", "Полный отчёт не сохранён на NAS. Повторите дополнение.")
    with db.begin():
        # Import publication uses the same parent-before-artifact lock order.
        if session_id:
            current_session = SessionReadsRepository(db).session_snapshot(session_id, for_update=True)
            check_session(current_session, prepared["payload"])
            if stable_hash(current_session) != stable_hash(session):
                raise SessionReportError("conflict", "Сессия изменилась во время дополнения. Повторите запрос.")
        reports = ReportsRepository(db)
        current = reports.get(report_id, for_update=True)
        if current is None or stable_hash(current) != stable_hash(artifact):
            raise SessionReportError("conflict", "Отчёт изменился во время дополнения. Повторите запрос.")
        reports.save_prepared(report_id, prepared, report_type=artifact["report_type"])
    return {"report_id": report_id, "llm": result.__dict__}


def read_operator_report(db, session_id: str) -> dict:
    from domain.services.operator_report import build_operator_report

    try:
        session = SessionReadsRepository(db).session_snapshot(session_id)
        if session is None:
            raise SessionReportError("not_found", "Сессия не найдена")
        runtime = RuntimeRepository(db)
        record_id = db.scalar(select(PrintRecord.record_id).where(
            PrintRecord.session_id == session_id,
        ).limit(1))
        record = PrintsRepository(db).get_print_record(record_id) if record_id else None
        outcomes = runtime.list_quality_outcomes(session_id=session_id)
        if record:
            by_id = {row["outcome_id"]: row for row in outcomes}
            by_id.update({row["outcome_id"]: row for row in runtime.list_quality_outcomes(print_record_id=record_id)})
            outcomes = list(by_id.values())
        inputs = deepcopy({"session_id": session_id,
                           "group": ((session["context"].get("runtime_payload") or {}).get("group") or {}),
                           "quality_outcomes": outcomes, "print_record": record})
    finally:
        db.rollback()
    return build_operator_report(**inputs)


def list_reports(db, session_id: str) -> list[dict]:
    try:
        return ReportsRepository(db).index(session_id)
    finally:
        db.rollback()
