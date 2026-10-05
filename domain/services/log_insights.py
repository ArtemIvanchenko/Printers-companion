"""Short NAS reads followed by pure, local diagnostic calculations."""
from copy import deepcopy

from sqlalchemy import select

from analytics.log_insights.geometry import compare_repeats, geometry_residuals, inspection_map
from analytics.log_insights.timing import time_accounting
from analytics.prediction.timing_validation import calibration_timing_payloads, finite_number
from analytics.prediction.timing_snapshot import MANIFEST_KEY, read_timing_publication
from analytics.prediction.input_quality import (
    geometry_input_issues, prediction_is_current, session_link_is_confirmed,
)
from core.versioning.constants import LOG_INSIGHTS_VERSION
from core.config.settings import get_settings
from core.versioning.provenance import build_provenance, stable_hash
from domain.models.events import LayerSnapshot
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from storage.db.session import session_scope


def _record(row):
    return {"record_id": row.record_id, "session_id": row.session_id, "revision": row.revision,
            "metadata": deepcopy(row.metadata_json or {})}


def context_key(record):
    metadata = record["metadata"]
    snap = metadata.get("prediction") or {}
    confirmed = session_link_is_confirmed(metadata, session_id=record.get("session_id"))
    fields = ("geometry_fingerprint", "printer_id", "material", "layer_thickness_mm",
              "hatch_distance_mm", "laser_count", "process_profile_fingerprint", "build_origin_source")
    if not confirmed or any(snap.get(key) in (None, "", "unknown") for key in fields):
        return None
    if geometry_input_issues(metadata, snap):
        return None
    if (not finite_number(snap.get("build_origin_z_mm"))
            or any(not finite_number(snap.get(k)) or snap[k] <= 0
                   for k in ("layer_thickness_mm", "hatch_distance_mm", "laser_count"))):
        return None
    return stable_hash({**{key: snap[key] for key in fields}, "build_origin_z_mm": snap.get("build_origin_z_mm")})


def _current(record):
    return prediction_is_current(record["metadata"].get("prediction") or {}, record["revision"])


def print_log_insights(record_id):
    # Candidate retrieval is bounded. No raw files are parsed and no geometry
    # is sliced while holding a database connection (or by another PC).
    with session_scope() as db:
        row = db.get(PrintRecord, record_id)
        if row is None:
            return None
        target = _record(row)
        key = context_key(target) if _current(target) else None
        refs = []
        if key:
            candidates = [_record(r) for r in db.scalars(
                select(PrintRecord).where(PrintRecord.session_id.is_not(None))
                .order_by(PrintRecord.printed_at.desc(), PrintRecord.record_id).limit(100)
            )]
            refs = [r for r in candidates if r["record_id"] != record_id
                    and _current(r) and context_key(r) == key][:10]
        session_ids = {r["session_id"] for r in [target, *refs] if r["session_id"]}
        groups, publications = {}, {}
        rows = {sid: [] for sid in session_ids}
        if session_ids:
            for sid, group, manifest in db.execute(select(
                BuildSession.session_id, BuildSession.context["runtime_payload"]["group"],
                BuildSession.context[MANIFEST_KEY],
            ).where(BuildSession.session_id.in_(session_ids))):
                groups[sid] = deepcopy(group or {})
                publications[sid] = manifest
            for sid, layer, features, tag in db.execute(select(
                LayerSnapshot.session_id, LayerSnapshot.layer, LayerSnapshot.features,
                LayerSnapshot.context["publication_id"].as_string(),
            ).where(LayerSnapshot.session_id.in_(session_ids))):
                rows[sid].append((layer, features, tag))
    states, raw = {}, {}
    for sid, items in rows.items():
        states[sid], events = read_timing_publication(items, publications.get(sid))
        raw[sid] = events or []
    timings_by_session = {sid: calibration_timing_payloads(events) for sid, events in raw.items()}
    group = groups.get(target["session_id"], {})
    base = deepcopy(group.get("log_insights") or {})
    snapshot = target["metadata"].get("prediction") or {}
    events = raw.get(target["session_id"], [])
    timings = timings_by_session.get(target["session_id"], {})
    valid_context = key is not None
    residuals = geometry_residuals(timings, snapshot, target["session_id"], record_id) if valid_context else {
        "status": "unconfirmed_context", "source": "calculated", "sample_count": 0, "items": [],
        "reason_ru": "Нужны актуальный прогноз, подтверждённая связь с логом, машина и режим печати.",
    }

    def comparison(r):
        insights = (groups.get(r["session_id"], {}).get("log_insights") or {}).get("environment") or {}
        return {"session_id": r["session_id"], "comparison_key": context_key(r),
                "timings": timings_by_session.get(r["session_id"], {}),
                "environment": insights.get("metrics", [])}

    target_comparison = comparison(target)
    target_comparison["comparison_key"] = key
    reference_comparisons = [comparison(r) for r in refs]
    return {
        **base, "record_id": record_id, "session_id": target["session_id"],
        "status": "needs_reanalysis" if states.get(target["session_id"]) == "invalid" else "ok" if base or timings else "needs_reanalysis",
        "timing_publication_status": states.get(target["session_id"], "absent"),
        "reason_ru": ("Слои и сводка не соответствуют одной версии импорта. Обновите карточку; если ошибка остаётся, повторите анализ на ПК владельца."
                      if states.get(target["session_id"]) == "invalid" else None if base
                      else "Послойная среда и восстановление появятся после повторного анализа логов на ПК владельца."),
        "geometry_residuals": residuals,
        "repeatability": compare_repeats(target_comparison, reference_comparisons),
        "inspection_map": inspection_map(base.get("environment") or {}, residuals, snapshot if valid_context else {}),
        "normal_time_reference": {**time_accounting(events, cycle_model=snapshot if valid_context else None),
                                  "scope": "validated_unique_layers_only"},
        "provenance": build_provenance("print_log_insights", inputs={"record": target, "timings": timings,
                                         "source_provenance": base.get("provenance"),
                                         "references": reference_comparisons},
                                       config={"method": LOG_INSIGHTS_VERSION, "max_candidates": 100},
                                       generated_by=get_settings().compute_node_id),
    }
