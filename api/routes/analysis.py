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
