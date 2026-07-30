"""Per-layer machine timings, extracted from the printer's logs once and stored.

The printer records, for every physical layer, how long it burned and how long
the recoat took (``*_time.log``, event ``layer_timing_summary``). Every
calibration in the project needs those two numbers and nothing else from the
raw log — which is 33 KB of text per print that was previously re-parsed from
local disk on every single calibration run.

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
from sqlalchemy.orm import Session

from domain.enums.common import SourceFileFamily
from domain.models.events import LayerSnapshot

logger = logging.getLogger(__name__)


def store_layer_timings(session_id: str, files: list, db: Session) -> int:
    """Extract per-layer burn/pour from a session's time_log into the DB.

    Idempotent: replaces whatever was stored for this session, so a re-import
    with more complete logs (the multi-day rotation case) overwrites a partial
    earlier read rather than duplicating layers.

    Returns the number of layers stored. Zero means the session has no usable
    time_log, which is normal for preparation runs and for sessions whose files
    are gone — not an error.
    """
    from analytics.prediction.recoat_calibration import _MAX_POUR_MS, _MIN_POUR_MS

    by_layer: dict[int, tuple[float, float]] = {}
    for f in files:
        if f.classification.family != SourceFileFamily.time_log or not f.parse_result:
            continue
        for event in f.parse_result.events:
            if getattr(event, "event_type", None) != "layer_timing_summary":
                continue
            payload = getattr(event, "payload", None) or {}
            layer = payload.get("layer")
            burn_ms, pour_ms = payload.get("burn_ms"), payload.get("pour_ms")
            if not isinstance(layer, int):
                continue
            if not isinstance(burn_ms, (int, float)) or not isinstance(pour_ms, (int, float)):
                continue
            # Same plausibility guards the calibrations apply, enforced once at
            # the point of storage instead of at every read.
            if burn_ms <= 0 or not (_MIN_POUR_MS <= pour_ms <= _MAX_POUR_MS):
                continue
            # First wins: a rotated/duplicated log must not overwrite a layer.
            by_layer.setdefault(layer, (float(burn_ms), float(pour_ms)))

    if not by_layer:
        return 0

    db.execute(delete(LayerSnapshot).where(LayerSnapshot.session_id == session_id))
    db.add_all([
        LayerSnapshot(
            session_id=session_id,
            layer=layer,
            features={"burn_ms": burn_ms, "pour_ms": pour_ms},
        )
        for layer, (burn_ms, pour_ms) in sorted(by_layer.items())
    ])
    db.flush()
    logger.info("layer timings: stored %d layers for %s", len(by_layer), session_id)
    return len(by_layer)


def stored_timings(session_id: str, db: Session) -> dict[int, tuple[float, float]]:
    """{layer: (burn_ms, pour_ms)} for one session — empty when nothing stored."""
    rows = db.scalars(
        select(LayerSnapshot).where(LayerSnapshot.session_id == session_id)
    ).all()
    out: dict[int, tuple[float, float]] = {}
    for row in rows:
        features = row.features or {}
        burn, pour = features.get("burn_ms"), features.get("pour_ms")
        if isinstance(burn, (int, float)) and isinstance(pour, (int, float)):
            out[row.layer] = (float(burn), float(pour))
    return out


__all__ = ["store_layer_timings", "stored_timings"]
