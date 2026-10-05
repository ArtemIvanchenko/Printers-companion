"""Per-layer machine timings, extracted from the printer's logs once and stored.

The printer records, for every physical layer, how long it burned, how long
the recoat took and the complete layer cycle (``*_time.log``, event
``layer_timing_summary``). The compact conclusion keeps ``burn_ms``,
``pour_ms`` and valid ``make_layer_ms``; their residual is the separate
inter-phase machine overhead. It avoids repeatedly parsing the raw log.

Storing the conclusion instead of the source has three consequences, in order
of importance:

1. **It works against a shared database.** Re-parsing needs the file on *this*
   machine. An operator looking at a print imported by a colleague had no file,
   every calibration returned None, and the accuracy loop quietly fell back to
   wall-clock time — which includes operator pauses (18 h of 47.6 on one real
   build). Rows in the database are visible to everyone by construction.
2. It is small: ~100 bytes per layer, so a 174-layer print costs ~17 KB and the
   6842-layer one ~680 KB, against 10 GB for the full log set.
3. It is queryable. "Show me every print whose recoat drifted" is SQL now, not
   a scan of files that may not exist.

The raw logs stay on disk as the source of truth — nothing here deletes them,
and a re-import recomputes these rows from scratch.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, object_session

from domain.models.events import LayerSnapshot
from domain.models.sessions import BuildSession
from analytics.prediction.timing_validation import (
    calibration_cycles_ms, calibration_timing_payloads, timing_components_ms,
)
from analytics.prediction.timing_snapshot import (
    MANIFEST_KEY, MAX_LAYER_OVERHEAD_MS, PreparedLayerTimings,
    prepare_layer_timings, read_timing_publication,
)

logger = logging.getLogger(__name__)

# Bound for the additive-overhead summary, not a diagnosis of a pause. A long
# residual can also include a legitimate minimum-cycle wait. Raw cycles remain
# available to diagnostics and to the separate max/base/floor calibration.


def store_layer_timings(session_id: str, files: list, db: Session) -> int:
    """Legacy synchronous adapter. Durable imports prepare before opening SQL."""
    from core.config.settings import get_settings

    prepared = prepare_layer_timings(
        files, owner_node_id=get_settings().compute_node_id,
    )
    session = db.scalar(select(BuildSession).where(BuildSession.session_id == session_id)
                        .with_for_update().execution_options(populate_existing=True))
    if session is None:
        raise ValueError(f"Сессия {session_id} не найдена")
    return replace_layer_timings(session, prepared, db)


def replace_layer_timings(session: BuildSession, prepared: PreparedLayerTimings, db: Session) -> int:
    """Replace facts on the caller's locked parent; caller commits/fences.

    The import publisher and legacy repair already hold this row. Do not fetch
    its large context or acquire the same lock a second time.
    """
    from core.versioning.provenance import stable_hash

    manifest = prepared.manifest
    if manifest.get("row_count") != len(prepared.rows) or manifest.get("rows_fingerprint") != stable_hash(prepared.rows):
        raise ValueError("Снимок слоёв повреждён до публикации")
    if object_session(session) is not db:
        raise ValueError("Строка сессии должна принадлежать текущей транзакции")
    session_id = session.session_id
    db.execute(delete(LayerSnapshot).where(LayerSnapshot.session_id == session_id))
    # Bulk insert bounded batches instead of an ORM object per measured layer.
    for offset in range(0, len(prepared.rows), 100):
        db.execute(LayerSnapshot.__table__.insert(), [{
            "session_id": session_id, "layer": item["layer"], "features": item["features"],
            "context": {"publication_id": manifest["publication_id"]},
        } for item in prepared.rows[offset:offset + 100]])
    session.context = {**(session.context or {}), MANIFEST_KEY: manifest}
    db.flush()
    return len(prepared.rows)


def stored_timing_events(session_id: str, db: Session) -> list[dict] | None:
    """Adapt persisted evidence to the same contract as the raw parser.

    None means no snapshot rows exist (legacy local fallback is possible).
    Existing but invalid rows remain explicit evidence; an empty admission
    result must not silently resurrect a different on-disk copy.
    """
    manifest = db.scalar(select(BuildSession.context[MANIFEST_KEY])
                         .where(BuildSession.session_id == session_id))
    # Read the manifest once, not once per layer (megabytes over a weak NAS).
    # If these reads straddle replacement, generation/digest checks fail closed.
    rows = db.execute(select(
        LayerSnapshot.layer, LayerSnapshot.features,
        LayerSnapshot.context["publication_id"].as_string(),
    ).where(LayerSnapshot.session_id == session_id)).all()
    return read_timing_publication([tuple(row) for row in rows], manifest)[1]


def stored_timings(session_id: str, db: Session) -> dict[int, tuple[float, float]]:
    """{layer: (burn_ms, pour_ms)} admitted identically to parser events."""
    timings = calibration_timing_payloads(stored_timing_events(session_id, db) or [])
    return timing_components_ms(timings)


def legacy_session_timing_events(session_id: str, db: Session) -> list:
    """Legacy research adapter: shared facts first, owner-local raw only if absent.

    Caller retains its UoW, including uncommitted inputs; no implicit rollback
    or commit. Production fits use detached CalibrationInputs, not this adapter.
    Standalone repair tools must close SQL before reconstructing raw sources.
    """
    stored = stored_timing_events(session_id, db)
    if stored is not None:
        return stored  # Empty/invalid published evidence is not missing evidence.
    from domain.enums.common import SourceFileFamily
    from domain.services.compute_affinity import ComputeAffinityError
    from domain.services.session_sources import sources_from_snapshot, rehydrate_session_sources
    from storage.repositories.session_reads import SessionReadsRepository

    sources = sources_from_snapshot(session_id, SessionReadsRepository(db).sources_snapshot(session_id))
    if sources is None:
        return []
    try:
        files = rehydrate_session_sources(sources)
    except ComputeAffinityError:
        return []
    return [event for file in files
            if file.classification.family == SourceFileFamily.time_log and file.parse_result
            for event in file.parse_result.events]


def stored_layer_overheads(session_id: str, db: Session) -> dict[int, float]:
    """{layer: make-burn-pour milliseconds} for validated complete cycles."""
    out: dict[int, float] = {}
    for layer, (burn, pour, make) in stored_layer_cycles(session_id, db).items():
        # A cached residual can be stale relative to the three measurements.
        overhead = make - burn - pour
        if 0.0 <= overhead <= MAX_LAYER_OVERHEAD_MS:
            out[layer] = overhead
    return out


def stored_layer_cycles(
    session_id: str,
    db: Session,
) -> dict[int, tuple[float, float, float]]:
    """{layer: (burn_ms, pour_ms, make_layer_ms)} for complete raw cycles.

    ``make_layer_ms`` is intentionally returned even when its residual is too
    large to be normal controller overhead.  Consumers that build a normal
    cycle model must apply their own pause/restart filter; keeping the raw
    value is what lets diagnostics still explain an unusually long layer.
    """
    timings = calibration_timing_payloads(stored_timing_events(session_id, db) or [])
    return calibration_cycles_ms(timings)


__all__ = [
    "MAX_LAYER_OVERHEAD_MS",
    "store_layer_timings",
    "replace_layer_timings",
    "prepare_layer_timings",
    "stored_timings",
    "stored_timing_events",
    "stored_layer_overheads",
    "stored_layer_cycles",
]
