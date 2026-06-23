import threading
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from api.pagination import LimitParam, PaginatedResponse, SkipParam
from domain.services.ingestion import IngestionService
from domain.services.session_grouping import group_files_into_sessions
from domain.services.session_overview import build_group_overview
from profiles.m350.profile import build_registry, get_profile
from reporting.json_report.generator import _timeline_preview, generate_session_json_report
from reporting.markdown_report.generator import generate_markdown_report
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/sessions", tags=["sessions"])

_REPORT_CACHE_MAX = 256
_report_cache: OrderedDict[tuple[str, bool], dict] = OrderedDict()
# Sync route handlers run in a threadpool, so cache access is concurrent. Guard
# every read/modify/write — an unlocked OrderedDict can corrupt or raise
# "mutated during iteration".
_cache_lock = threading.Lock()


def _invalidate_cache(session_id: str) -> None:
    with _cache_lock:
        for key in [k for k in _report_cache if k[0] == session_id]:
            _report_cache.pop(key, None)


def _cache_get(key: tuple[str, bool]) -> dict | None:
    with _cache_lock:
        if key not in _report_cache:
            return None
        _report_cache.move_to_end(key)
        return _report_cache[key]


def _cache_set(key: tuple[str, bool], value: dict) -> None:
    with _cache_lock:
        _report_cache[key] = value
        _report_cache.move_to_end(key)
        while len(_report_cache) > _REPORT_CACHE_MAX:
            _report_cache.popitem(last=False)


@router.post("/ingest")
def ingest_session(payload: dict, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    folder = Path(payload.get("folder") or payload.get("path") or "")
    registry = build_registry()
    result = IngestionService(registry, get_profile()).parse(folder)
    groups = group_files_into_sessions(result.files)
    response_groups = []
    for group in groups:
        session_id = payload.get("session_id") or group.group_id
        # Enrich the stored group with classification + dashboard features + telemetry,
        # so the persisted payload is directly renderable by the web dashboard.
        overview = build_group_overview(
            group.group_id,
            group.files,
            start_ts=group.start_ts,
            end_ts=group.end_ts,
            grouping_confidence=group.confidence,
        )
        repo.save_session_payload(
            session_id,
            # Strip parse_result (events): tiny payload; events re-read from disk
            # on demand (avoids ~96 MB/session of monitor events in the DB).
            {"files": [f.model_dump(mode="json", exclude={"parse_result"}) for f in group.files], "group": overview},
        )
        response_groups.append({"session_id": session_id, **overview})

    from domain.services.print_linking import auto_link_print_records

    links = auto_link_print_records(repo.db)
    repo.flush()
    return {"root": result.root, "groups": response_groups, "skipped": result.skipped,
            "diagnostics": result.diagnostics, "print_record_links": links}


@router.get("")
def list_sessions(
    skip: SkipParam = 0,
    limit: LimitParam = 100,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    all_sessions = list(repo.list_session_payloads())
    total = len(all_sessions)
    items = [
        {"session_id": session_id, **payload.get("group", {})}
        for session_id, payload in all_sessions[skip:skip + limit]
    ]
    return PaginatedResponse(items=items, total=total, skip=skip, limit=limit).to_dict()


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
        result.append({
            "session_id": session_id,
            "start_ts": group.get("start_ts"),
            "end_ts": group.get("end_ts"),
            "duration_min": group.get("duration_min"),
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
    return {
        "session_id": session_id,
        "start_ts": group.get("start_ts"),
        "end_ts": group.get("end_ts"),
        "duration_min": group.get("duration_min"),
        "telemetry": tel,
        "health": group.get("health") or {},
        "has_telemetry": bool(tel.get("time")),
    }


@router.get("/{session_id}")
def get_session(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    payload = repo.get_session_payload(session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, **payload.get("group", {})}


@router.post("/{session_id}/analyze")
def analyze_session(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return _generate_report(session_id, include_markdown=False, repo=repo)


@router.post("/{session_id}/reanalyze")
def reanalyze_session(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    _invalidate_cache(session_id)
    return _generate_report(session_id, include_markdown=False, repo=repo) | {"reanalyzed": True}


@router.get("/{session_id}/timeline")
def get_timeline(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _generate_report(session_id, include_markdown=False, repo=repo)
    return _timeline_preview(report["timeline"])


@router.get("/{session_id}/segments")
def get_segments(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _generate_report(session_id, include_markdown=False, repo=repo)
    return report["phase_segments"]


@router.get("/{session_id}/files")
def get_files(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _generate_report(session_id, include_markdown=False, repo=repo)
    return report["file_inventory"]


@router.get("/{session_id}/parse-diagnostics")
def get_parse_diagnostics(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _generate_report(session_id, include_markdown=False, repo=repo)
    return report["data_quality"]["parse_diagnostics"]


@router.get("/{session_id}/anomalies")
def get_session_anomalies(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _generate_report(session_id, include_markdown=False, repo=repo)
    return report.get("anomalies", [])


@router.get("/{session_id}/hypotheses")
def get_session_hypotheses(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    report = _generate_report(session_id, include_markdown=False, repo=repo)
    return report.get("hypotheses", [])


@router.get("/{session_id}/reports")
def list_session_reports(session_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return [
        {"report_id": report.get("report_id"), "session_id": report.get("session_id"), "generated_at": report.get("generated_at")}
        for report in repo.list_reports_for_session(session_id)
    ]


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


def _generate_report(session_id: str, include_markdown: bool, repo: RuntimeRepository) -> dict:
    cache_key = (session_id, include_markdown)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Report needs the events → rehydrate parse_result from disk (stored slim).
    files = repo.get_session_files(session_id, rehydrate=True)
    if files is None:
        raise HTTPException(status_code=404, detail="Session not found")
    report = generate_session_json_report(session_id, files)
    if include_markdown:
        report["markdown"] = generate_markdown_report(report)
    repo.save_report(report)
    repo.flush()

    _cache_set(cache_key, report)
    return report
