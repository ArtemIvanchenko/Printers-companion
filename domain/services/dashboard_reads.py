"""Bounded read models for optional historical dashboard panels.

The landing page never calls these queries. JSON field projection avoids moving
whole session payloads (including sensor arrays) from the NAS for a table row.
"""
from __future__ import annotations

import math

from sqlalchemy import func, select

from domain.models.entities import OperatorEvent, QualityOutcome
from domain.models.sessions import BuildSession
from storage.db.session import session_scope


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def read_history(panel: str, *, skip: int = 0, limit: int = 50) -> dict:
    """Copy a small page and close SQL before the HTTP adapter renders it."""
    if panel not in {"sessions", "timeline", "quality", "consumption"}:
        raise ValueError("Unknown history panel")
    if skip < 0 or not 1 <= limit <= 100:
        raise ValueError("History page is out of bounds")
    with session_scope() as db:
        if panel in {"sessions", "timeline"}:
            group = BuildSession.context["runtime_payload"]["group"]
            features = group["features"]
            columns = [
                BuildSession.session_id.label("id"), BuildSession.start_ts,
                func.coalesce(group["classification"].as_string(), BuildSession.classification).label("type"),
                *[features[key].label(key) for key in (
                    "duration_min", "total_lines", "pause_count", "burn_events",
                    "first_time", "last_time",
                )],
                group["data_quality"]["score"].label("data_quality_score"),
            ]
            total = db.scalar(select(func.count()).select_from(BuildSession)) or 0
            rows = [dict(row) for row in db.execute(
                select(*columns).order_by(BuildSession.start_ts.desc(), BuildSession.session_id)
                .offset(skip).limit(limit)
            ).mappings()]
            for row in rows:
                row["date"] = row["start_ts"].date().isoformat() if row["start_ts"] else row["id"]
                for key in ("duration_min", "total_lines", "pause_count", "burn_events", "data_quality_score"):
                    row[key] = _number(row[key])
            extra = {}
        elif panel == "quality":
            total = db.scalar(select(func.count()).select_from(QualityOutcome)) or 0
            rows = [dict(row) for row in db.execute(
                select(QualityOutcome.outcome_id, QualityOutcome.timestamp, QualityOutcome.result,
                       QualityOutcome.defect_type, QualityOutcome.session_id)
                .order_by(QualityOutcome.timestamp.desc(), QualityOutcome.outcome_id)
                .offset(skip).limit(limit)
            ).mappings()]
            # These describe inspection records, not defect-risk ground truth.
            # Count in SQL; never fetch all historical notes/attachments for a pie.
            counts = dict(db.execute(select(QualityOutcome.result, func.count())
                                    .group_by(QualityOutcome.result)).all())
            defects = dict(db.execute(select(QualityOutcome.defect_type, func.count())
                                     .where(QualityOutcome.defect_type.is_not(None))
                                     .group_by(QualityOutcome.defect_type)
                                     .order_by(func.count().desc(), QualityOutcome.defect_type)
                                     .limit(20)).all())
            extra = {"result_counts": counts, "defect_counts": defects,
                     "aggregation_scope": "inspection_records", "defect_group_limit": 20}
        else:
            kinds = ("gas_consumption_recorded", "powder_consumption_recorded")
            where = OperatorEvent.event_type.in_(kinds)
            total = db.scalar(select(func.count()).select_from(OperatorEvent).where(where)) or 0
            rows = [dict(row) for row in db.execute(
                select(OperatorEvent.event_id, OperatorEvent.timestamp, OperatorEvent.event_type,
                       OperatorEvent.value, OperatorEvent.session_id)
                .where(where).order_by(OperatorEvent.timestamp.desc(), OperatorEvent.event_id)
                .offset(skip).limit(limit)
            ).mappings()]
            for row in rows:
                row["value"] = _number(row["value"])
            extra = {}
    return {"panel": panel, "items": rows, "total": total, "skip": skip,
            "limit": limit, "has_more": skip + len(rows) < total, **extra}


def read_telemetry_page(*, skip: int = 0, limit: int = 50) -> dict:
    """Compact saved summaries, not N full telemetry downloads or recalculation."""
    if skip < 0 or not 1 <= limit <= 100:
        raise ValueError("Telemetry page is out of bounds")
    group = BuildSession.context["runtime_payload"]["group"]
    has_time = group["telemetry"]["time"][0].as_string().is_not(None)
    with session_scope() as db:
        total = db.scalar(select(func.count()).select_from(BuildSession).where(has_time)) or 0
        rows = [dict(row) for row in db.execute(select(
            BuildSession.session_id, BuildSession.start_ts,
            group["features"]["duration_min"].label("duration_min"),
            group["signal_stats"].label("signal_stats"),
            group["health"]["burn_drift"]["mean_sec"].label("mean_burn_seconds"),
        ).where(has_time).order_by(BuildSession.start_ts.desc(), BuildSession.session_id)
          .offset(skip).limit(limit)).mappings()]
    for row in rows:
        raw = row["signal_stats"] or {}
        row["signal_stats"] = {
            name: {"mean": _number(stats.get("mean")), "group": stats.get("group")}
            for name, stats in raw.items() if isinstance(stats, dict)
        } if isinstance(raw, dict) else {}
        row["duration_min"] = _number(row["duration_min"])
        row["mean_burn_seconds"] = _number(row["mean_burn_seconds"])
    return {"items": rows, "total": total, "skip": skip, "limit": limit,
            "has_more": skip + len(rows) < total}
