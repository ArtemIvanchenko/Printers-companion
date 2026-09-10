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
from sqlalchemy.orm import Session

from domain.enums.common import SourceFileFamily
from domain.models.events import LayerSnapshot
from analytics.prediction.timing_validation import calibration_timing_payloads

logger = logging.getLogger(__name__)

# Longer residuals are almost certainly a stop/restart recorded inside the
# full layer cycle, not repeatable machine overhead. Keep them out of future
# estimates; wall-clock stops remain session diagnostics only.
MAX_LAYER_OVERHEAD_MS = 10_000.0


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

    by_layer: dict[int, tuple[float, float, float | None, float | None]] = {}
    timing_files = [
        f for f in files if f.classification.family == SourceFileFamily.time_log and f.parse_result
    ]
    payloads = calibration_timing_payloads(
        event for f in timing_files for event in f.parse_result.events
    )
    for layer, payload in payloads.items():
        burn_ms, pour_ms = payload.get("burn_ms"), payload.get("pour_ms")
        make_layer_ms = payload.get("make_layer_ms")
        if not isinstance(layer, int):
            continue
        if not isinstance(burn_ms, (int, float)) or not isinstance(pour_ms, (int, float)):
            continue
        # Same plausibility guards the calibrations apply, enforced once at
        # the point of storage instead of at every read.
        if burn_ms <= 0 or not (_MIN_POUR_MS <= pour_ms <= _MAX_POUR_MS):
            continue
        raw_make = None
        normal_overhead = None
        if isinstance(make_layer_ms, (int, float)):
            overhead_ms = float(make_layer_ms) - float(burn_ms) - float(pour_ms)
            if overhead_ms >= 0.0:
                raw_make = float(make_layer_ms)
                if overhead_ms <= MAX_LAYER_OVERHEAD_MS:
                    normal_overhead = overhead_ms
        by_layer[layer] = (float(burn_ms), float(pour_ms), raw_make, normal_overhead)

    if not by_layer:
        if timing_files:
            db.execute(delete(LayerSnapshot).where(LayerSnapshot.session_id == session_id))
            db.flush()
        return 0

    db.execute(delete(LayerSnapshot).where(LayerSnapshot.session_id == session_id))
    db.add_all(
        [
            LayerSnapshot(
                session_id=session_id,
                layer=layer,
                features={
                    "burn_ms": burn_ms,
                    "pour_ms": pour_ms,
                    **({"make_layer_ms": make_layer_ms} if make_layer_ms is not None else {}),
                    **(
                        {"normal_overhead_ms": normal_overhead_ms}
                        if normal_overhead_ms is not None
                        else {}
                    ),
                },
            )
            for layer, (burn_ms, pour_ms, make_layer_ms, normal_overhead_ms) in sorted(
                by_layer.items()
            )
        ]
    )
    db.flush()
    logger.info(
        "layer timings: stored %d validated layers for %s",
        len(by_layer),
        session_id,
    )
    return len(by_layer)


def stored_timings(session_id: str, db: Session) -> dict[int, tuple[float, float]]:
    """{layer: (burn_ms, pour_ms)} for one session — empty when nothing stored."""
    rows = db.scalars(select(LayerSnapshot).where(LayerSnapshot.session_id == session_id)).all()
    out: dict[int, tuple[float, float]] = {}
    for row in rows:
        features = row.features or {}
        burn, pour = features.get("burn_ms"), features.get("pour_ms")
        if isinstance(burn, (int, float)) and isinstance(pour, (int, float)):
            out[row.layer] = (float(burn), float(pour))
    return out


def stored_layer_overheads(session_id: str, db: Session) -> dict[int, float]:
    """{layer: make-burn-pour milliseconds} for validated complete cycles."""
    rows = db.scalars(select(LayerSnapshot).where(LayerSnapshot.session_id == session_id)).all()
    out: dict[int, float] = {}
    for row in rows:
        features = row.features or {}
        burn = features.get("burn_ms")
        pour = features.get("pour_ms")
        make = features.get("make_layer_ms")
        if not all(isinstance(value, (int, float)) for value in (burn, pour, make)):
            continue
        normal = features.get("normal_overhead_ms")
        overhead = (
            float(normal)
            if isinstance(normal, (int, float))
            else float(make) - float(burn) - float(pour)
        )
        if 0.0 <= overhead <= MAX_LAYER_OVERHEAD_MS:
            out[row.layer] = overhead
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
    rows = db.scalars(select(LayerSnapshot).where(LayerSnapshot.session_id == session_id)).all()
    out: dict[int, tuple[float, float, float]] = {}
    for row in rows:
        features = row.features or {}
        values = (
            features.get("burn_ms"),
            features.get("pour_ms"),
            features.get("make_layer_ms"),
        )
        if not all(isinstance(value, (int, float)) for value in values):
            continue
        burn, pour, make = (float(value) for value in values)
        if burn > 0.0 and pour >= 0.0 and make >= burn + pour:
            out[row.layer] = (burn, pour, make)
    return out


__all__ = [
    "MAX_LAYER_OVERHEAD_MS",
    "store_layer_timings",
    "stored_timings",
    "stored_layer_overheads",
    "stored_layer_cycles",
]
