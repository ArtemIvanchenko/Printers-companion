from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from api.pagination import LimitParam, SkipParam
from core.config.settings import get_settings
from domain.services.compute_affinity import ComputeAffinityError
from domain.services import session_reports, session_requests
from reporting.json_report.generator import _timeline_preview
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/sessions", tags=["sessions"])

def _invalidate_cache(session_id: str) -> None:
    """Compatibility hook: published reports are no longer process-cached."""


def _session_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except session_reports.SessionReportError as exc:
        raise HTTPException(status_code={"not_found": 404, "conflict": 409,
                                         "forbidden": 403, "invalid_inputs": 422,
                                         "storage_unavailable": 503}[exc.code],
                            detail=exc.detail) from exc


@router.post("/ingest", status_code=202)
def ingest_session(payload: dict, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    """Contract v2: enqueue local import, never parse in the HTTP process."""
    return _session_call(session_requests.request_ingest, repo.db, payload, settings=get_settings())


@router.get("")
def list_sessions(
    skip: SkipParam = 0,
    limit: LimitParam = 100,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    return _session_call(session_reports.list_sessions, repo.db, skip=skip, limit=limit)


@router.get("/telemetry-list")
def list_sessions_with_telemetry(
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> list:
    """List REAL_PRINT sessions that have telemetry data, newest first."""
    result = []
    for session_id, payload in repo.list_session_payloads():
        group = payload.get("group") or {}
        if group.get("classification") != "REAL_PRINT":
            continue
        tel = group.get("telemetry") or {}
        if not tel.get("time"):
            continue
        features = group.get("features") or {}
        result.append({
            "session_id": session_id,
            "start_ts": group.get("start_ts"),
            "end_ts": group.get("end_ts"),
            # duration_min lives under features, not group top-level.
            "duration_min": features.get("duration_min"),
            "idle_min": features.get("idle_min"),
            "idle_pct": features.get("idle_pct"),
        })
    result.sort(key=lambda x: x.get("start_ts") or "", reverse=True)
    return result


@router.get("/{session_id}/telemetry")
def get_session_telemetry(
    session_id: str,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    """Return telemetry data for a specific session."""
    payload = repo.get_session_payload(session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Session not found")
    group = payload.get("group") or {}
    tel = group.get("telemetry") or {}
    features = group.get("features") or {}
    return {
        "session_id": session_id,
        "start_ts": group.get("start_ts"),
        "end_ts": group.get("end_ts"),
        "duration_min": features.get("duration_min"),
        "idle_min": features.get("idle_min"),
        "idle_pct": features.get("idle_pct"),
        "telemetry": tel,
        "health": group.get("health") or {},
        "soft_sensors": group.get("soft_sensors") or {},
        "phase_statistics": group.get("phase_statistics") or {},
        "advanced_monitoring": group.get("advanced_monitoring") or {},
        "log_insights": group.get("log_insights") or {},
        "has_telemetry": bool(tel.get("time")),
    }


@router.get("/{session_id}")
def get_session(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    payload = repo.get_session_payload(session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, **payload.get("group", {})}


@router.get("/{session_id}/operator-report")
def get_operator_report(
    session_id: str,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    """Compact current-state report; safe to read from every operator PC."""
    return _session_call(session_reports.read_operator_report, repo.db, session_id)


@router.post("/{session_id}/analyze", status_code=202)
def analyze_session(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    """Contract v2: request an owner-local, durably published analysis."""
    return _session_call(session_requests.request_analysis, repo.db, session_id,
                         compute_node_id=get_settings().compute_node_id)


@router.post("/{session_id}/reanalyze", status_code=202)
def reanalyze_session(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    """Same durable analysis path; 202 means queued, not already recalculated."""
    return analyze_session(session_id, repo=repo)


@router.get("/{session_id}/timeline")
def get_timeline(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _report_for_read(session_id, repo=repo)
    return _timeline_preview(report["timeline"])


@router.get("/{session_id}/segments")
def get_segments(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _report_for_read(session_id, repo=repo)
    return report["phase_segments"]


@router.get("/{session_id}/files")
def get_files(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _report_for_read(session_id, repo=repo)
    return report["file_inventory"]


@router.get("/{session_id}/parse-diagnostics")
def get_parse_diagnostics(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _report_for_read(session_id, repo=repo)
    return report["data_quality"]["parse_diagnostics"]


@router.get("/{session_id}/anomalies")
def get_session_anomalies(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _report_for_read(session_id, repo=repo)
    return report.get("anomalies", [])


@router.get("/{session_id}/hypotheses")
def get_session_hypotheses(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _report_for_read(session_id, repo=repo)
    return report.get("hypotheses", [])


@router.get("/{session_id}/reports")
def list_session_reports(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return _session_call(session_reports.list_reports, repo.db, session_id)


@router.post("/{session_id}/reports/generate")
def generate_report(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    report = _generate_report(session_id, include_markdown=True, repo=repo)
    return {"report_id": report["report_id"], "json": report, "markdown": report["markdown"]}


@router.post("/{session_id}/approve")
def approve_session(
    session_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    """Mark session as 'OK' and update tolerance rules."""
    from core.tolerance import learn_from_session
    from storage.db.session import session_scope

    _require_local_session(session_id, repo)
    files = repo.get_session_files(session_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Extract features from the session payload
    session_payload = repo.get_session_payload(session_id)
    features = (session_payload or {}).get("group", {}).get("features", {})

    confirmed_by = (payload or {}).get("confirmed_by", "unknown")

    with session_scope() as db:
        rules = learn_from_session(db, session_id, features, confirmed_by=confirmed_by)

    return {
        "status": "approved",
        "session_id": session_id,
        "rules_updated": len(rules),
        "features_learned": list(features.keys()),
    }


def _require_local_session(session_id: str, repo: RuntimeRepository) -> None:
    try:
        row = repo.require_session_compute_owner(
            session_id,
            requested_compute_node_id=get_settings().compute_node_id,
        )
    except ComputeAffinityError as exc:
        raise HTTPException(
            status_code=403,
            detail=(
                "Пересчёт этой сессии разрешён только на создавшем её ПК. "
                f"{exc}"
            ),
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")


def _report_for_read(session_id: str, repo: RuntimeRepository) -> dict:
    """Every PC reads the same published artifact; GET never parses or writes."""
    return _session_call(session_reports.read_report, repo.db, session_id)


def _generate_report(session_id: str, include_markdown: bool, repo: RuntimeRepository) -> dict:
    """Legacy name: render the published artifact, not a second analysis path."""
    _require_local_session(session_id, repo)
    return _session_call(session_reports.read_report, repo.db, session_id,
                         include_markdown=include_markdown)
