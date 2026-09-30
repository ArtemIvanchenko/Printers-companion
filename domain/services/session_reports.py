"""Read published session artifacts without parsing or writing on a GET.

These use cases own their read transaction; pass a clean session. Object-store
downloads and presentation run only after the detached SQL snapshot is closed.
"""

from copy import deepcopy
import json

from sqlalchemy import select

from domain.models.prints import PrintRecord
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.runtime import RuntimeRepository
from storage.repositories.session_reads import SessionReadsRepository


class SessionReportError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def list_sessions(db, *, skip: int = 0, limit: int = 100) -> dict:
    try:
        reads = SessionReadsRepository(db)
        total = reads.count_payloads()
        rows = reads.list_payloads(skip=skip, limit=limit)
    finally:
        db.rollback()
    return {"items": [{"session_id": sid, **(payload.get("group") or {})} for sid, payload in rows],
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


def read_report(db, session_id: str, *, include_markdown: bool = False,
                object_store_factory=None) -> dict:
    try:
        reads = SessionReadsRepository(db)
        session = reads.session_snapshot(session_id)
        if session is None:
            raise SessionReportError("not_found", "Сессия не найдена")
        artifact = reads.latest_report(session_id)
        if artifact is None:
            raise SessionReportError("conflict", "Сохранённого отчёта нет. Запустите анализ на ПК-владельце; чтение не запускает пересчёт.")
        report = artifact["payload"]
        _assert_current(session, report)
    finally:
        db.rollback()
    if artifact["storage_uri"]:
        expanded = _read_object(artifact["storage_uri"], object_store_factory=object_store_factory)
        if expanded is not None:
            # A wrong object must never override the SQL publication identity.
            if (expanded.get("report_id") != artifact["report_id"]
                    or expanded.get("session_id") != session_id):
                raise SessionReportError("conflict", "Объект отчёта не соответствует сохранённой публикации.")
            _assert_current(session, expanded)
            report = expanded
    if include_markdown and not report.get("markdown"):
        from reporting.markdown_report.generator import generate_markdown_report

        report["markdown"] = generate_markdown_report(report)
    return report


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
        return SessionReadsRepository(db).report_index(session_id)
    finally:
        db.rollback()
