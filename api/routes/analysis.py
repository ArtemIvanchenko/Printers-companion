"""Cross-session intelligence analysis endpoint.

GET  /analysis/patterns          — run analysis, return structured findings
GET  /analysis/patterns/narrate  — same + LLM narration (if available)
POST /analysis/patterns/refresh  — force re-compute and cache result
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from analytics.cross_session import run_cross_session_analysis
from analytics.signal_stats import compute_signal_stats   # fallback for old sessions
from domain.models.events import OperatorEvent
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analysis", tags=["analysis"])

# Simple in-process cache so repeated dashboard loads don't re-analyze.
_CACHE: dict[str, Any] = {}
_CACHE_TTL_SEC = 300   # 5 min


def _cache_fresh() -> bool:
    ts = _CACHE.get("computed_at")
    if not ts:
        return False
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age < _CACHE_TTL_SEC


def _load_sessions(db) -> list[dict[str, Any]]:
    """Return all REAL_PRINT sessions that have telemetry data."""
    rows = db.execute(
        select(BuildSession).order_by(BuildSession.start_ts)
    ).scalars().all()

    result = []
    for row in rows:
        ctx = (row.context or {}).get("runtime_payload", {}) or {}
        group = ctx.get("group", {}) or {}
        classification = (group.get("classification") or row.classification or "")

        # Prefer full-resolution stats (computed at import from all ~330k rows).
        # Fall back to stats derived from the 150-point downsampled telemetry
        # for sessions imported before this feature was added.
        signal_stats = group.get("signal_stats") or {}
        if not signal_stats:
            telemetry = group.get("telemetry", {}) or {}
            if telemetry:
                signal_stats = compute_signal_stats(telemetry)
        if not signal_stats:
            continue

        result.append({
            "session_id":     row.session_id,
            "start_ts":       row.start_ts.isoformat() if row.start_ts else None,
            "end_ts":         row.end_ts.isoformat() if row.end_ts else None,
            "classification": classification,
            "signal_stats":   signal_stats,
            # Include health data already computed at ingest time.
            "health": {
                "readiness_score": (group.get("health") or {}).get("readiness", {}).get("score"),
                "anomaly_count":   len((group.get("health") or {}).get("anomalies", [])),
                "burn_trend":      (group.get("health") or {}).get("burn_drift", {}).get("trend"),
            },
        })
    return result


def _load_operator_events(db) -> list[dict[str, Any]]:
    """Load maintenance-relevant operator events."""
    MAINTENANCE_TYPES = {
        "seal_replaced", "filter_replaced", "optics_cleaned",
        "recoater_adjusted", "chamber_cleaned", "calibration_performed",
        "part_accepted", "part_rejected",
    }
    rows = db.execute(
        select(OperatorEvent)
        .where(OperatorEvent.event_type.in_(MAINTENANCE_TYPES))
        .order_by(OperatorEvent.timestamp)
    ).scalars().all()
    return [
        {
            "event_type": r.event_type,
            "timestamp":  r.timestamp.isoformat() if r.timestamp else None,
            "session_id": r.session_id,
            "note":       r.note,
        }
        for r in rows
    ]


def _compute() -> dict[str, Any]:
    db = SessionLocal()
    try:
        sessions = _load_sessions(db)
        events   = _load_operator_events(db)
    finally:
        db.close()

    result = run_cross_session_analysis(sessions, events)
    result["sessions_detail"] = sessions   # include per-session stats for the dashboard
    # NOTE: computed_at is set by the caller (get_patterns) as a real datetime so
    # the cache only ever holds a datetime; it is serialised to ISO on output.
    return result


@router.get("/patterns")
def get_patterns(force: bool = False) -> dict[str, Any]:
    """Return cross-session analysis findings (cached for 5 min)."""
    if force or not _cache_fresh():
        try:
            _CACHE.update(_compute())
            _CACHE["computed_at"] = datetime.now(timezone.utc)
        except Exception as exc:
            logger.exception("Analysis failed: %s", exc)
            return {"error": str(exc), "trends": [], "before_after": [], "anomalies": []}

    # Return JSON-safe copy (replace datetime object with str).
    out = dict(_CACHE)
    if isinstance(out.get("computed_at"), datetime):
        out["computed_at"] = out["computed_at"].isoformat()
    return out


@router.post("/patterns/refresh")
def refresh_patterns() -> dict[str, Any]:
    """Force re-compute and update cache."""
    return get_patterns(force=True)


def _load_session_groups(db) -> list[dict[str, Any]]:
    """Full stored ``group`` payload per session (features+health+signal_stats+
    data_quality), newest first — the input shape defect-risk expects."""
    rows = db.execute(select(BuildSession).order_by(BuildSession.start_ts.desc())).scalars().all()
    out = []
    for row in rows:
        group = ((row.context or {}).get("runtime_payload", {}) or {}).get("group", {}) or {}
        if not group:
            continue
        out.append({
            "session_id": row.session_id,
            "start_ts": row.start_ts.isoformat() if row.start_ts else None,
            "group": group,
        })
    return out


def _load_quality_labels(db) -> dict[str, int]:
    """{session_id: 1 defect / 0 good} from operator-entered QualityOutcome rows."""
    from analytics.prediction.defect_risk import outcome_to_label
    from domain.models.quality import QualityOutcome
    labels: dict[str, int] = {}
    for row in db.execute(select(QualityOutcome)).scalars().all():
        if not row.session_id:
            continue
        lbl = outcome_to_label(row.result)
        if lbl is not None:
            labels[row.session_id] = lbl  # latest wins (no ordering guarantee needed)
    return labels


@router.get("/defect-risk")
def defect_risk_all() -> dict[str, Any]:
    """Defect-risk score for every session (learned model if enough labels, else
    heuristic). Explainable: each result carries top contributing factors."""
    from analytics.prediction.defect_risk import predict_defect_risk, train_defect_model
    db = SessionLocal()
    try:
        sessions = _load_session_groups(db)
        labels = _load_quality_labels(db)
    finally:
        db.close()

    labelled = [(s["group"], labels[s["session_id"]]) for s in sessions if s["session_id"] in labels]
    model = train_defect_model(labelled)

    results = []
    for s in sessions:
        pred = predict_defect_risk(s["group"], model)
        results.append({
            "session_id": s["session_id"], "start_ts": s["start_ts"],
            "label": labels.get(s["session_id"]), **pred,
        })
    return {
        "model_trained": model is not None,
        "n_sessions": len(sessions),
        "n_labeled": len(labelled),
        "sessions": results,
    }


@router.get("/defect-risk/{session_id}")
def defect_risk_one(session_id: str) -> dict[str, Any]:
    """Defect-risk for a single session with explanation."""
    from analytics.prediction.defect_risk import predict_defect_risk, train_defect_model
    db = SessionLocal()
    try:
        sessions = _load_session_groups(db)
        labels = _load_quality_labels(db)
    finally:
        db.close()

    target = next((s for s in sessions if s["session_id"] == session_id), None)
    if target is None:
        return {"error": "session not found", "session_id": session_id}
    labelled = [(s["group"], labels[s["session_id"]]) for s in sessions if s["session_id"] in labels]
    model = train_defect_model(labelled)
    pred = predict_defect_risk(target["group"], model)
    return {"session_id": session_id, "start_ts": target["start_ts"],
            "label": labels.get(session_id), "model_trained": model is not None, **pred}


@router.get("/maintenance-forecast")
def maintenance_forecast() -> dict[str, Any]:
    """Project per-signal drift toward alarm thresholds (predictive maintenance)."""
    from analytics.prediction.maintenance import forecast_maintenance
    from analytics.thresholds import load_alarm_thresholds
    db = SessionLocal()
    try:
        sessions = _load_sessions(db)  # already chronological, carries signal_stats
    finally:
        db.close()
    forecasts = forecast_maintenance(sessions, load_alarm_thresholds())
    return {"n_sessions": len(sessions), "forecasts": forecasts}


@router.get("/patterns/narrate")
async def narrate_patterns() -> dict[str, Any]:
    """Return findings + LLM-generated narrative in Russian (if LLM available)."""
    findings = get_patterns()
    if findings.get("error"):
        return findings

    # Build compact prompt for LLM (< 300 tokens).
    compact = {
        "n_sessions": findings["n_sessions_analyzed"],
        "trends":     findings["trends"][:5],
        "anomalies":  findings["anomalies"][:5],
        "before_after": findings["before_after"][:3],
    }
    prompt = (
        "Ты аналитик металлической 3D печати. Объясни оператору на русском языке "
        "следующие статистические находки по принтеру M-350. "
        "Будь конкретен: назови сигналы, процентные изменения, вероятные причины. "
        "Дай рекомендации. Отвечай структурированным текстом, 3-8 предложений.\n\n"
        f"Данные анализа:\n{json.dumps(compact, ensure_ascii=False, indent=2)}"
    )

    narrative = None
    try:
        from reporting.llm.providers.factory import get_llm_provider
        provider = get_llm_provider()
        result = await provider.generate_markdown({"question": prompt})
        if result.success:
            narrative = result.content
    except Exception as exc:
        logger.warning("LLM narration failed: %s", exc)

    findings["narrative"] = narrative
    return findings
