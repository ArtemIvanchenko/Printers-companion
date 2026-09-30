"""Optional experiments over a published preview; never a source of measured facts."""
from copy import deepcopy

from sqlalchemy import select

from core.versioning.provenance import build_provenance, stable_hash
from domain.models.sessions import BuildSession
from domain.services.compute_affinity import require_compute_owner
from storage.repositories.jobs_repo import JobsRepository

JOB_TYPE = "shadow_analysis"


class ShadowAnalysisError(ValueError):
    pass


def shadow_inputs(db, session_id, owner_node_id, *, lock=False):
    statement = select(BuildSession).where(BuildSession.session_id == session_id)
    if lock:
        statement = statement.with_for_update()
    session = db.scalar(statement.execution_options(populate_existing=True))
    if session is None:
        raise ShadowAnalysisError("Сессия не найдена")
    require_compute_owner(entity_type="session", entity_id=session_id,
                          origin_compute_node_id=session.origin_compute_node_id,
                          requested_compute_node_id=owner_node_id)
    group = ((session.context or {}).get("runtime_payload") or {}).get("group") or {}
    snapshot = group.get("analysis_snapshot") or {}
    if snapshot.get("schema_version") != 1 or not snapshot.get("analysis_id"):
        raise ShadowAnalysisError("Сначала нужен новый опубликованный анализ сессии")
    telemetry = deepcopy(group.get("telemetry") or {})
    return {"session_id": session_id, "analysis_id": snapshot["analysis_id"],
            "telemetry": telemetry, "input_fingerprint": stable_hash(telemetry),
            "evidence": deepcopy(snapshot.get("telemetry_evidence") or {})}


def request_shadow_analysis(db, session_id, owner_node_id):
    inputs = shadow_inputs(db, session_id, owner_node_id)
    return JobsRepository(db).enqueue(
        job_type=JOB_TYPE, owner_node_id=owner_node_id, entity_type="session",
        entity_id=session_id,
        idempotency_key=f"shadow:{owner_node_id}:{inputs['analysis_id']}:{inputs['input_fingerprint']}",
        payload={"owner_node_id": owner_node_id, "analysis_id": inputs["analysis_id"],
                 "input_fingerprint": inputs["input_fingerprint"]}, max_attempts=1,
    )


def calculate_shadow(inputs, owner_node_id):
    from analytics.process_monitoring import build_advanced_monitoring
    result = build_advanced_monitoring(inputs["telemetry"], [])
    result.update({
        "analysis_id": inputs["analysis_id"], "session_id": inputs["session_id"],
        "input_scope": "published_display_preview_and_measured_layer_times",
        "input_fingerprint": inputs["input_fingerprint"],
        "operator_action_allowed": False,
        "source_sample_count": inputs["evidence"].get("sample_count"),
        "display_sample_count": len(inputs["telemetry"].get("time") or []),
        "limitations_ru": [
            "Эксперимент над сохранённой выборкой для графика, а не полноразмерная диагностика процесса.",
            "Не изменяет измеренный анализ, оценку риска или карточку. Не доказывает качество детали.",
            "Полный поток событий не передаётся: process mining недоступен в этом режиме.",
        ],
        "provenance": build_provenance("shadow_preview_analysis", inputs=inputs["input_fingerprint"],
                                       config={"analysis_id": inputs["analysis_id"]}, generated_by=owner_node_id),
    })
    return result
