"""Pure admission rules for geometry-dependent calibration and diagnostics.

Missing evidence is not confirmation. These gates do not discard measured
timings or reject log-only recoat/controller-cycle calibration.
"""
from __future__ import annotations

from analytics.prediction.timing_validation import finite_number

CONFIRMED_BUILD_ORIGINS = {"explicit", "confirmed_magics_plate_datum"}
INPUT_REASON_RU = {
    "incomplete_geometry": "Неполная геометрия или отсутствуют поддержки.",
    "unconfirmed_build_origin": "Не подтверждена координата начала построения.",
    "unconfirmed_session_link": "Не подтверждено соответствие модели и логов.",
    "stale_prediction": "Прогноз не соответствует текущей версии карточки; нужен перерасчёт.",
}


def geometry_input_issues(metadata: dict, snapshot: dict) -> list[str]:
    """Geometry limitations survive both old snapshots and later card reviews."""
    qualities = [metadata.get("geometry_quality"), snapshot.get("geometry_quality")]
    incomplete = (
        metadata.get("desktop_geometry_complete") is False
        or snapshot.get("estimate_quality") in {"incomplete", "lower_bound"}
        or any(isinstance(q, dict) and (
            q.get("status") in {"incomplete", "lower_bound"} or bool(q.get("missing"))
        ) for q in qualities)
    )
    issues = ["incomplete_geometry"] if incomplete else []
    if (snapshot.get("build_origin_source") not in CONFIRMED_BUILD_ORIGINS
            or not finite_number(snapshot.get("build_origin_z_mm"))):
        issues.append("unconfirmed_build_origin")
    return issues


def session_link_is_confirmed(metadata: dict, *, session_id: str | None = None) -> bool:
    if metadata.get("session_link_confirmed") is False:
        return False  # an explicit revocation also invalidates old auto-link evidence
    evidence = metadata.get("session_link_evidence") or {}
    if isinstance(evidence, dict):
        if (session_id is not None and evidence.get("session_id") is not None
                and evidence["session_id"] != session_id):
            return False  # evidence for an old link does not confirm a new one
        if evidence.get("method") == "operator_import_hint" and not evidence.get("import_job_id"):
            return False  # legacy date-only hint was not upload lineage
    return metadata.get("session_link_confirmed") is True or (
        isinstance(evidence, dict)
        and evidence.get("eligible") is True
        and evidence.get("auto_link_allowed") is True
    )


def prediction_is_current(snapshot: dict, revision: int) -> bool:
    recorded = snapshot.get("input_revision")
    # Publication itself increments the optimistic-concurrency revision once.
    return (isinstance(recorded, int) and not isinstance(recorded, bool)
            and recorded > 0 and revision in {recorded, recorded + 1})


def calibration_input_exclusion(
    metadata: dict, snapshot: dict, revision: int, *, session_id: str | None = None,
) -> str | None:
    issues = geometry_input_issues(metadata, snapshot)
    if issues:
        return issues[0]
    if not session_link_is_confirmed(metadata, session_id=session_id):
        return "unconfirmed_session_link"
    if not prediction_is_current(snapshot, revision):
        return "stale_prediction"
    return None
