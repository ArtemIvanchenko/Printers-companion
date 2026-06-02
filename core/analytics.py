"""Analytics service — aggregation layer between DB and LLM/user.

Provides pre-computed statistics so LLM never sees raw logs.
All queries work on structured data already stored in the database.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.models.entities import (
    BuildSession,
    OperatorEvent,
    QualityOutcome,
    ReportArtifact,
)


def _get_session_local():
    """Lazy import to avoid DB engine creation at module level."""
    from storage.db.session import SessionLocal
    return SessionLocal


class AnalyticsService:
    """Aggregated statistics for operator questions."""

    def __init__(self, db: Session | None = None) -> None:
        self._owns_session = db is None
        self.db = db or _get_session_local()()

    def close(self) -> None:
        if self._owns_session:
            self.db.close()

    # ------------------------------------------------------------------
    # Gas statistics
    # ------------------------------------------------------------------

    def get_gas_stats(self, last_n_sessions: int = 10) -> dict[str, Any]:
        """Gas consumption summary for the last N sessions."""
        sessions = self._get_last_n_sessions(last_n_sessions)
        session_ids = [s["session_id"] for s in sessions]

        gas_events = self._get_gas_events(session_ids)
        cylinder_changes = [e for e in gas_events if e["event_type"] == "gas_cylinder_replaced"]
        consumption_records = [e for e in gas_events if e["event_type"] == "gas_consumption_recorded"]

        total_consumed_bar = 0.0
        for rec in consumption_records:
            try:
                value = float(rec.get("value") or 0)
                unit = rec.get("unit", "")
                if unit in ("bar", "бар"):
                    total_consumed_bar += value
                elif unit == "bar_remaining":
                    pass
            except (ValueError, TypeError):
                pass

        cylinders_used = sorted({e.get("gas_cylinder_id") for e in gas_events if e.get("gas_cylinder_id")})

        return {
            "total_consumed_bar": round(total_consumed_bar, 1),
            "avg_per_session_bar": round(total_consumed_bar / max(len(sessions), 1), 1),
            "cylinder_changes": len(cylinder_changes),
            "consumption_records": len(consumption_records),
            "cylinders_used": cylinders_used,
            "sessions_analyzed": len(sessions),
        }

    # ------------------------------------------------------------------
    # Powder statistics
    # ------------------------------------------------------------------

    def get_powder_stats(self, last_n_sessions: int = 10) -> dict[str, Any]:
        """Powder usage summary for the last N sessions."""
        sessions = self._get_last_n_sessions(last_n_sessions)
        session_ids = [s["session_id"] for s in sessions]

        powder_events = self._get_powder_events(session_ids)
        consumption_records = [e for e in powder_events if e["event_type"] == "powder_consumption_recorded"]
        reuse_events = [e for e in powder_events if e["event_type"] == "powder_reused"]

        total_consumed_kg = 0.0
        for rec in consumption_records:
            try:
                value = float(rec.get("value") or 0)
                unit = rec.get("unit", "")
                if unit == "kg":
                    total_consumed_kg += value
            except (ValueError, TypeError):
                pass

        batches = sorted({e.get("powder_batch") for e in powder_events if e.get("powder_batch")})
        max_reuse = max((int(e.get("value") or 0) for e in reuse_events), default=0)

        return {
            "total_consumed_kg": round(total_consumed_kg, 2),
            "avg_per_session_kg": round(total_consumed_kg / max(len(sessions), 1), 2),
            "batches_used": batches,
            "reuse_events": len(reuse_events),
            "max_reuse_cycle": max_reuse,
            "sessions_analyzed": len(sessions),
        }

    # ------------------------------------------------------------------
    # Print quality statistics
    # ------------------------------------------------------------------

    def get_print_quality_stats(self, last_n_sessions: int = 10) -> dict[str, Any]:
        """Quality outcome summary for the last N sessions."""
        sessions = self._get_last_n_sessions(last_n_sessions)
        session_ids = [s["session_id"] for s in sessions]

        outcomes = self._get_quality_outcomes(session_ids)
        total = len(outcomes)
        accepted = sum(1 for o in outcomes if o.get("result") == "accepted")
        rejected = sum(1 for o in outcomes if o.get("result") == "rejected")
        warnings = sum(1 for o in outcomes if o.get("result") == "warning")

        defect_types: dict[str, int] = {}
        for o in outcomes:
            dt = o.get("defect_type")
            if dt:
                defect_types[dt] = defect_types.get(dt, 0) + 1

        return {
            "total_outcomes": total,
            "accepted": accepted,
            "rejected": rejected,
            "warnings": warnings,
            "accepted_pct": round(accepted / max(total, 1) * 100, 1),
            "rejected_pct": round(rejected / max(total, 1) * 100, 1),
            "defect_types": defect_types,
            "sessions_analyzed": len(sessions),
        }

    # ------------------------------------------------------------------
    # Session summary
    # ------------------------------------------------------------------

    def get_session_summary(self, last_n: int = 10) -> list[dict[str, Any]]:
        """Brief summary of the last N sessions."""
        sessions = self._get_last_n_sessions(last_n)
        result = []
        for s in sessions:
            ctx = s.get("context", {}) or {}
            runtime = ctx.get("runtime_payload", {}) or {}
            group = runtime.get("group", {}) or {}
            features = group.get("features", {}) or {}

            # Check against learned tolerances
            from core.tolerance import check_session
            violations = check_session(self.db, features)

            result.append(
                {
                    "session_id": s["session_id"],
                    "start_ts": s.get("start_ts"),
                    "end_ts": s.get("end_ts"),
                    "classification": s.get("classification"),
                    "confidence": s.get("classification_confidence"),
                    "material": features.get("material"),
                    "duration_sec": features.get("duration_sec"),
                    "tolerance_violations": violations,
                    "is_within_norm": len(violations) == 0,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Question router — uses LLM query planner
    # ------------------------------------------------------------------

    def answer_question(self, question: str, last_n: int = 10) -> dict[str, Any]:
        """Answer a natural-language question using LLM query planning.

        Flow:
        1. Send ONLY the question (~50 tokens) to LLM → get query plan
        2. Execute plan against database
        3. Format results into human-readable answer
        4. Fall back to keyword matching if LLM unavailable
        """
        plan = self._try_plan_with_llm(question)
        if plan:
            result = self._execute_plan(plan)
            if result and "error" not in result:
                return {
                    "topic": plan.entity,
                    "direct_answer": self._format_query_result(result),
                    "data": result,
                    "method": "llm_planned",
                }

        return self._answer_with_keywords(question, last_n)

    def _try_plan_with_llm(self, question: str):
        """Try to create a query plan using LLM. Returns None if LLM unavailable."""
        try:
            from core.query_planner import plan_query
            return plan_query(question)
        except Exception:
            return None

    def _execute_plan(self, plan) -> dict[str, Any] | None:
        """Execute a query plan and return results."""
        try:
            from core.query_executor import execute_query
            return execute_query(plan, self.db)
        except Exception as exc:
            return {"error": str(exc)}

    def _format_query_result(self, result: dict) -> str:
        """Format query execution results into human-readable answer."""
        action = result.get("action")
        plan = result.get("plan")
        question_summary = plan.question_summary if plan else ""

        if action == "count":
            return f"📊 {question_summary}\n\n  Результат: {result.get('result', 0)}"

        _scalar_labels = {"sum": "Сумма", "avg": "Среднее", "max": "Максимум", "min": "Минимум"}
        if action in _scalar_labels:
            return f"📊 {question_summary}\n\n  {_scalar_labels[action]} ({result.get('field', '?')}): {result.get('result', 0)}"

        if action == "list":
            rows = result.get("result", [])
            if not rows:
                return f"📊 {question_summary}\n\n  Нет данных."
            lines = [f"📊 {question_summary}\n"]
            for i, row in enumerate(rows[:20], 1):
                parts = [f"{k}: {v}" for k, v in row.items() if v is not None]
                lines.append(f"  {i}. {', '.join(parts)}")
            if result.get("count", 0) > 20:
                lines.append(f"\n  ... и ещё {result['count'] - 20} записей")
            return "\n".join(lines)

        if action == "group_by":
            groups = result.get("result", [])
            if not groups:
                return f"📊 {question_summary}\n\n  Нет данных."
            lines = [f"📊 {question_summary}\n"]
            for g in groups:
                lines.append(f"  {g['value']}: {g['count']}")
            return "\n".join(lines)

        return f"📊 {question_summary}\n\n  {result}"

    # ------------------------------------------------------------------
    # Fallback: keyword-based routing (when LLM unavailable)
    # ------------------------------------------------------------------

    def _answer_with_keywords(self, question: str, last_n: int = 10) -> dict[str, Any]:
        """Fallback keyword-based routing when LLM is not available."""
        lower = question.lower()

        if any(w in lower for w in ("аргон", "argon", "газ", "gas", "баллон", "давлен")):
            data = self.get_gas_stats(last_n)
            return {
                "topic": "gas",
                "direct_answer": self._format_gas_answer(data, last_n),
                "data": data,
                "method": "keyword_fallback",
            }

        if any(w in lower for w in ("порош", "powder", "парти", "batch", "reuse", "цикл")):
            data = self.get_powder_stats(last_n)
            return {
                "topic": "powder",
                "direct_answer": self._format_powder_answer(data, last_n),
                "data": data,
                "method": "keyword_fallback",
            }

        if any(w in lower for w in ("брак", "defect", "reject", "качеств", "quality", "принят", "accepted")):
            data = self.get_print_quality_stats(last_n)
            return {
                "topic": "quality",
                "direct_answer": self._format_quality_answer(data, last_n),
                "data": data,
                "method": "keyword_fallback",
            }

        if any(w in lower for w in ("последн", "last", "сколько печат", "how many print", "summary", "сводк")):
            data = self.get_session_summary(last_n)
            return {
                "topic": "sessions",
                "direct_answer": self._format_sessions_answer(data),
                "data": data,
                "method": "keyword_fallback",
            }

        return {
            "topic": "unknown",
            "direct_answer": None,
            "data": {
                "gas": self.get_gas_stats(last_n),
                "powder": self.get_powder_stats(last_n),
                "quality": self.get_print_quality_stats(last_n),
                "sessions": self.get_session_summary(last_n),
            },
            "method": "keyword_fallback",
        }

    # ------------------------------------------------------------------
    # Formatters — direct answers without LLM
    # ------------------------------------------------------------------

    @staticmethod
    def _format_gas_answer(data: dict, last_n: int) -> str:
        lines = [f"📊 Газ за последние {last_n} печатей:"]
        lines.append(f"  Потрачено: {data['total_consumed_bar']} бар")
        lines.append(f"  Среднее на печать: {data['avg_per_session_bar']} бар")
        lines.append(f"  Замен баллонов: {data['cylinder_changes']}")
        if data["cylinders_used"]:
            lines.append(f"  Баллоны: {', '.join(data['cylinders_used'])}")
        lines.append(f"  Записей расхода: {data['consumption_records']}")
        if data["consumption_records"] == 0:
            lines.append("  ⚠️ Нет записей расхода. Пишите 'Баллон X закончился, остаток Y бар' для учёта.")
        return "\n".join(lines)

    @staticmethod
    def _format_powder_answer(data: dict, last_n: int) -> str:
        lines = [f"📊 Порошок за последние {last_n} печатей:"]
        lines.append(f"  Потрачено: {data['total_consumed_kg']} кг")
        lines.append(f"  Среднее на печать: {data['avg_per_session_kg']} кг")
        if data["batches_used"]:
            lines.append(f"  Партии: {', '.join(data['batches_used'])}")
        lines.append(f"  Макс цикл reuse: {data['max_reuse_cycle']}")
        return "\n".join(lines)

    @staticmethod
    def _format_quality_answer(data: dict, last_n: int) -> str:
        lines = [f"📊 Качество за последние {last_n} печатей:"]
        lines.append(f"  Принято: {data['accepted']} ({data['accepted_pct']}%)")
        lines.append(f"  Забраковано: {data['rejected']} ({data['rejected_pct']}%)")
        lines.append(f"  Предупреждения: {data['warnings']}")
        if data["defect_types"]:
            lines.append("  Типы дефектов:")
            for dt, count in data["defect_types"].items():
                lines.append(f"    - {dt}: {count}")
        return "\n".join(lines)

    @staticmethod
    def _format_sessions_answer(sessions: list[dict]) -> str:
        if not sessions:
            return "Нет данных о сессиях."
        lines = [f"📊 Последние {len(sessions)} печатей:"]
        for i, s in enumerate(sessions, 1):
            dur = s.get("duration_sec")
            dur_str = f"{dur / 3600:.1f}ч" if dur else "?"
            mat = s.get("material") or "?"
            cls = s.get("classification") or "?"
            lines.append(f"  {i}. {s['session_id']}: {cls}, {dur_str}, материал={mat}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_last_n_sessions(self, n: int) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(BuildSession)
            .where(BuildSession.start_ts.isnot(None))
            .order_by(BuildSession.start_ts.desc())
            .limit(n)
        ).all()
        return [
            {
                "session_id": r.session_id,
                "start_ts": r.start_ts.isoformat() if r.start_ts else None,
                "end_ts": r.end_ts.isoformat() if r.end_ts else None,
                "classification": r.classification,
                "classification_confidence": r.classification_confidence,
                "context": r.context,
            }
            for r in rows
        ]

    _GAS_EVENT_TYPES = ["gas_cylinder_replaced", "gas_consumption_recorded", "gas_pressure_issue"]
    _POWDER_EVENT_TYPES = [
        "powder_consumption_recorded", "powder_batch_changed",
        "powder_reused", "powder_sieved", "powder_dried",
    ]

    def _get_consumable_events(
        self, event_types: list[str], id_field: str, session_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Shared loader for gas/powder events — they differ only by the event-type
        filter and the name of one provenance field (``gas_cylinder_id`` / ``powder_batch``)."""
        query = select(OperatorEvent).where(OperatorEvent.event_type.in_(event_types))
        if session_ids:
            query = query.where(OperatorEvent.session_id.in_(session_ids))
        query = query.order_by(OperatorEvent.timestamp.desc())
        return [
            {
                "event_id": r.event_id,
                "event_type": r.event_type,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                id_field: getattr(r, id_field),
                "value": r.value,
                "unit": r.unit,
                "note": r.note,
                "session_id": r.session_id,
            }
            for r in self.db.scalars(query).all()
        ]

    def _get_gas_events(self, session_ids: list[str]) -> list[dict[str, Any]]:
        return self._get_consumable_events(self._GAS_EVENT_TYPES, "gas_cylinder_id", session_ids)

    def _get_powder_events(self, session_ids: list[str]) -> list[dict[str, Any]]:
        return self._get_consumable_events(self._POWDER_EVENT_TYPES, "powder_batch", session_ids)

    def _get_quality_outcomes(self, session_ids: list[str]) -> list[dict[str, Any]]:
        query = select(QualityOutcome)
        if session_ids:
            query = query.where(QualityOutcome.session_id.in_(session_ids))
        query = query.order_by(QualityOutcome.timestamp.desc())
        rows = self.db.scalars(query).all()
        return [
            {
                "outcome_id": r.outcome_id,
                "result": r.result,
                "defect_type": r.defect_type,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "session_id": r.session_id,
            }
            for r in rows
        ]
