"""Query executor — runs structured query plans against the database.

Takes a QueryPlan and executes it. No knowledge of natural language — only
structured plans from the query planner.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import logging

from sqlalchemy import Float, func, select
from sqlalchemy.orm import Session

from domain.models.entities import (
    BuildSession,
    ImportJob,
    OperatorEvent,
    QualityOutcome,
)
from core.query_planner import QueryPlan

logger = logging.getLogger(__name__)

ENTITY_MODEL_MAP = {
    "sessions": BuildSession,
    "operator_events": OperatorEvent,
    "quality_outcomes": QualityOutcome,
    "import_jobs": ImportJob,
}

SESSION_CONTEXT_FIELDS = {
    "material": "context -> runtime_payload -> group -> features -> material",
    "powder_batch": "context -> runtime_payload -> group -> features -> powder_batch",
    "gas_cylinder_id": "context -> runtime_payload -> group -> features -> gas_cylinder_id",
    "duration_sec": "context -> runtime_payload -> group -> features -> duration_sec",
    "pause_count": "context -> runtime_payload -> group -> features -> pause_count",
    "restart_attempt_count": "context -> runtime_payload -> group -> features -> restart_attempt_count",
}


def _get_session_local():
    from storage.db.session import SessionLocal
    return SessionLocal


def _apply_filter(query, model, filter_spec: dict[str, Any]) -> Any:
    field_name = filter_spec["field"]
    operator = filter_spec["operator"]
    value = filter_spec["value"]

    col = _get_column(model, field_name)
    if col is None:
        return query

    if operator == "eq":
        return query.where(col == value)
    if operator == "contains":
        return query.where(col.ilike(f"%{value}%"))
    if operator == "gt":
        return query.where(col > value)
    if operator == "gte":
        return query.where(col >= value)
    if operator == "lt":
        return query.where(col < value)
    if operator == "lte":
        return query.where(col <= value)
    if operator == "in_last_n":
        if field_name == "sessions":
            cutoff = datetime.now(timezone.utc) - timedelta(days=value * 7)
            return query.where(BuildSession.start_ts >= cutoff)
        if field_name == "days":
            cutoff = datetime.now(timezone.utc) - timedelta(days=value)
            return query.where(model.__table__.c.timestamp >= cutoff)
        return query
    if operator == "between":
        return query.where(col.between(value[0], value[1]))
    if operator == "in":
        return query.where(col.in_(value))

    logger.warning("Unknown filter operator '%s' on field '%s', ignoring", operator, field_name)
    return query


def _get_column(model, field_name: str) -> Any | None:
    if hasattr(model, field_name):
        return getattr(model, field_name)
    if field_name in SESSION_CONTEXT_FIELDS:
        json_path = SESSION_CONTEXT_FIELDS[field_name].split(" -> ")
        return func.json_extract_path_text(BuildSession.context, *json_path[1:])
    logger.warning("Unknown field '%s' on model '%s'", field_name, model.__name__)
    return None


def _try_numeric(col) -> Any:
    try:
        return func.cast(col, Float)
    except Exception:
        return col


def execute_query(plan: QueryPlan, db: Session | None = None) -> dict[str, Any]:
    """Execute a query plan and return results."""
    owns_session = db is None
    db = db or _get_session_local()()

    try:
        model = ENTITY_MODEL_MAP.get(plan.entity)
        if model is None:
            return {"error": f"Unknown entity: {plan.entity}"}

        query = select(model)
        for f in plan.filters:
            query = _apply_filter(query, model, f)

        action = plan.action
        field_name = plan.field

        if action == "count":
            col = _get_column(model, field_name) if field_name else None
            if col is not None:
                result = db.scalar(select(func.count()).select_from(query.subquery()))
            else:
                result = db.scalar(select(func.count()).select_from(model).where(*[getattr(model, f["field"]) == f["value"] for f in plan.filters if f["operator"] == "eq"]))
            return {"action": "count", "result": result, "plan": plan}

        if action == "list":
            if plan.order_by:
                col = _get_column(model, plan.order_by)
                if col is not None:
                    query = query.order_by(col.desc() if plan.order_dir == "desc" else col.asc())
            if plan.limit:
                query = query.limit(plan.limit)
            rows = db.scalars(query).all()
            return {
                "action": "list",
                "result": [_serialize_row(r, plan.select_fields) for r in rows],
                "count": len(rows),
                "plan": plan,
            }

        if action in ("sum", "avg", "max", "min"):
            if not field_name:
                return {"error": f"Action '{action}' requires a field"}
            col = _get_column(model, field_name)
            if col is None:
                return {"error": f"Unknown field: {field_name}"}

            agg_func = {
                "sum": func.sum,
                "avg": func.avg,
                "max": func.max,
                "min": func.min,
            }[action]

            result = db.scalar(select(agg_func(col)).select_from(query.subquery()))
            return {"action": action, "field": field_name, "result": result, "plan": plan}

        if action == "group_by":
            if not plan.group_by:
                return {"error": "group_by action requires group_by field"}
            group_col = _get_column(model, plan.group_by)
            if group_col is None:
                return {"error": f"Unknown group_by field: {plan.group_by}"}
            query = select(group_col, func.count()).group_by(group_col).order_by(func.count().desc())
            if plan.limit:
                query = query.limit(plan.limit)
            rows = db.execute(query).all()
            return {
                "action": "group_by",
                "field": plan.group_by,
                "result": [{"value": row[0], "count": row[1]} for row in rows if row[0] is not None],
                "plan": plan,
            }

        return {"error": f"Unknown action: {action}"}

    finally:
        if owns_session:
            db.close()


def _serialize_row(row: Any, select_fields: list[str]) -> dict[str, Any]:
    result = {}
    for field_name in select_fields:
        if hasattr(row, field_name):
            val = getattr(row, field_name)
            if isinstance(val, datetime):
                val = val.isoformat()
            result[field_name] = val
        elif field_name in ("material", "duration_sec", "pause_count", "gas_cylinder_id", "powder_batch"):
            ctx = getattr(row, "context", {}) or {}
            rp = ctx.get("runtime_payload", {}) or {}
            group = rp.get("group", {}) or {}
            features = group.get("features", {}) or {}
            result[field_name] = features.get(field_name)
    if not select_fields:
        for col in row.__table__.columns.keys():
            val = getattr(row, col, None)
            if isinstance(val, datetime):
                val = val.isoformat()
            result[col] = val
    return result
