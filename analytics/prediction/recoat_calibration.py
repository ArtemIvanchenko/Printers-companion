"""Calibrate ``machine_params.recoat_time_by_mat`` from real per-layer recoat
duration ("pour", in the M-450M's own vocabulary — powder deposition) recorded
by the printer itself.

Every print's ``*_time.log`` carries, per physical layer, the machine's own
measured ``pour_ms`` alongside ``burn_ms`` (``parsers/formats/time_log.py``,
event type ``layer_timing_summary``). Nothing previously read ``pour_ms`` —
every quoted recoat time was either operator-entered or the hardcoded
``_DEFAULT_RECOAT_MS`` fallback, even though the real number was sitting in
already-parsed logs. Validated against this shop's own prints, that hardcoded
constant was off by 2-6x, and — more importantly — the true per-layer recoat
time itself varies several-fold between prints of the *same* material and
layer thickness. A single learned value cannot chase that variance away, but
it replaces a constant with the machine's own measured central tendency, which
is strictly better information.

Design mirrors ``analytics.prediction.accuracy``: linked (PrintRecord,
BuildSession) pairs, a windowed per-material median, a ``correction_locked``
gate, and sanity bounds so an implausible reading doesn't get baked in.

One difference from ``accuracy``'s reliance on the DB ``canonical_events``
table: that table is populated only by the watcher-confirmed import path
(``domain.services.import_jobs.execute_confirmed_import``) — the two other
import paths (container-startup catch-up, the dashboard's manual "rescan")
persist only the slim session payload. Rather than depend on which path
brought a session in, this module re-parses each session's ``*_time.log``
directly from its on-disk path via ``RuntimeRepository.get_session_files(...,
rehydrate=True)`` — the same rehydration report generation already relies on.
A session whose source files are no longer on disk is silently skipped (no
crash, no fabricated number) rather than treated as data.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.prediction.accuracy import (
    CALIBRATION_WINDOW,
    MIN_PAIRS_FOR_CALIBRATION,
    PRINT_CLASSIFICATIONS,
    as_utc,
    session_classification,
)
from domain.enums.common import SourceFileFamily
from domain.models.prints import MachineParams, PrintRecord
from domain.models.sessions import BuildSession

logger = logging.getLogger(__name__)

# A recoat pass outside this range is a bad reading (log corruption, a
# machine-clock glitch), not a real measurement — excluded, not averaged in.
_MIN_POUR_MS, _MAX_POUR_MS = 500.0, 120_000.0
# Same physical bounds a learned value must fall within before it is applied —
# mirrors accuracy.CORRECTION_MIN/MAX, but these are milliseconds, not a ratio.
RECOAT_MIN_MS, RECOAT_MAX_MS = 1_000.0, 60_000.0


def _pour_seconds_from_events(events: list[Any]) -> list[float]:
    """Per-layer recoat seconds from a list of parsed ``time_log`` events.

    First-wins per layer (matches ``session_overview._layer_burn_times``'s
    convention: a duplicated/rotated log must not double-count a layer).
    Accepts anything with ``event_type``/``payload`` attributes or dict keys —
    real ``CanonicalEventDraft`` objects and their ``.model_dump()`` alike, so
    the function is testable without constructing full parser output.
    """
    seen: dict[int, float] = {}
    for event in events:
        event_type = getattr(event, "event_type", None) if not isinstance(event, dict) else event.get("event_type")
        if event_type != "layer_timing_summary":
            continue
        payload = getattr(event, "payload", None) if not isinstance(event, dict) else event.get("payload")
        payload = payload or {}
        layer = payload.get("layer")
        pour_ms = payload.get("pour_ms")
        if not isinstance(layer, int) or not isinstance(pour_ms, (int, float)):
            continue
        if not (_MIN_POUR_MS <= pour_ms <= _MAX_POUR_MS):
            continue
        if layer not in seen:
            seen[layer] = pour_ms / 1000.0
    return list(seen.values())


def _machine_seconds_from_events(events: list) -> dict[int, float]:
    """{layer: burn+pour seconds} — полное машинное время слоя, без пауз.

    Использует burn_ms + pour_ms (а не make_layer_ms): make_layer_ms на части
    прошивок включает межслойные ожидания, а политика проекта — только чистое
    машинное время (см. базу знаний в plate_estimator.py, п.1).
    """
    out: dict[int, float] = {}
    for event in events:
        event_type = getattr(event, "event_type", None) if not isinstance(event, dict) else event.get("event_type")
        if event_type != "layer_timing_summary":
            continue
        payload = getattr(event, "payload", None) if not isinstance(event, dict) else event.get("payload")
        payload = payload or {}
        layer = payload.get("layer")
        burn_ms, pour_ms = payload.get("burn_ms"), payload.get("pour_ms")
        if not isinstance(layer, int):
            continue
        if not isinstance(burn_ms, (int, float)) or not isinstance(pour_ms, (int, float)):
            continue
        if burn_ms <= 0 or not (_MIN_POUR_MS <= pour_ms <= _MAX_POUR_MS):
            continue
        out.setdefault(layer, (burn_ms + pour_ms) / 1000.0)
    return out


def session_machine_seconds_by_layer(session_id: str, db: Session) -> dict[int, float] | None:
    """Полное машинное время (burn+pour) по слоям одной сессии, из time_log.

    Возвращает None, когда time_log отсутствует/не читается. Частичное
    покрытие слоёв возможно (суточная ротация логов) — вызывающий обязан
    проверять полноту, если суммирует (accuracy._machine_hours_from_logs).
    """
    from storage.repositories.runtime import RuntimeRepository

    files = RuntimeRepository(db).get_session_files(session_id, rehydrate=True)
    if not files:
        return None
    out: dict[int, float] = {}
    for f in files:
        if f.classification.family != SourceFileFamily.time_log or not f.parse_result:
            continue
        for layer, sec in _machine_seconds_from_events(f.parse_result.events).items():
            out.setdefault(layer, sec)
    return out or None


def session_recoat_seconds(session_id: str, db: Session) -> list[float] | None:
    """Per-layer recoat seconds for one session, or None if unavailable.

    Re-parses the session's time_log file(s) from disk (see module docstring)
    rather than trusting a possibly-absent canonical_events row.
    """
    from storage.repositories.runtime import RuntimeRepository

    files = RuntimeRepository(db).get_session_files(session_id, rehydrate=True)
    if not files:
        return None

    seconds: list[float] = []
    for f in files:
        if f.classification.family != SourceFileFamily.time_log or not f.parse_result:
            continue
        seconds.extend(_pour_seconds_from_events(f.parse_result.events))
    return seconds or None


def recoat_accuracy(db: Session) -> dict:
    """Real per-layer recoat durations from printer logs, by material.

    Structurally mirrors ``accuracy.prediction_accuracy``: one row per linked
    print with a measurable session, a per-material windowed median as the
    calibration candidate.
    """
    records = db.scalars(select(PrintRecord).where(PrintRecord.session_id.is_not(None))).all()
    session_ids = [r.session_id for r in records if r.session_id]
    sessions: dict[str, BuildSession] = {}
    if session_ids:
        sessions = {
            s.session_id: s
            for s in db.scalars(select(BuildSession).where(BuildSession.session_id.in_(session_ids))).all()
        }

    rows: list[dict] = []
    usable_by_mat: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    excluded: list[dict] = []

    for record in records:
        session = sessions.get(record.session_id)
        if session is None:
            continue

        pour_seconds = session_recoat_seconds(record.session_id, db)
        if not pour_seconds:
            continue  # no time_log for this session (or its files are gone) — not an error, just no data

        median_s = statistics.median(pour_seconds)
        material = record.material or "—"
        when = as_utc(session.start_ts) if session.start_ts else as_utc(record.created_at)

        skip_reason = None
        # Recoat physically happens on any run that actually printed layers,
        # unlike a whole-print duration — but a session tagged as something
        # other than a print is still not representative production behaviour.
        if session_classification(session) not in PRINT_CLASSIFICATIONS:
            skip_reason = "not_a_print"

        if skip_reason is None:
            usable_by_mat[material].append((when, median_s))
        else:
            excluded.append({"record_id": record.record_id, "session_id": record.session_id, "reason": skip_reason})

        rows.append({
            "record_id": record.record_id,
            "session_id": record.session_id,
            "material": material,
            "n_layers_measured": len(pour_seconds),
            "median_recoat_sec": round(median_s, 2),
            "used_for_calibration": skip_reason is None,
            "excluded_reason": skip_reason,
            "printed_at": when.isoformat(),
        })

    rows.sort(key=lambda r: r["printed_at"], reverse=True)

    def _windowed_median_ms(pairs: list[tuple[datetime, float]]) -> float | None:
        sample = [s for _, s in sorted(pairs, key=lambda p: p[0], reverse=True)[:CALIBRATION_WINDOW]]
        if len(sample) < MIN_PAIRS_FOR_CALIBRATION:
            return None
        return round(statistics.median(sample) * 1000.0, 1)

    by_material = {
        mat: {"n_sessions": len(pairs), "suggested_recoat_ms": _windowed_median_ms(pairs)}
        for mat, pairs in usable_by_mat.items()
    }

    return {
        "sessions": rows,
        "n_sessions": len(rows),
        "n_usable_sessions": sum(len(v) for v in usable_by_mat.values()),
        "excluded": excluded,
        "by_material": by_material,
        "min_sessions_for_calibration": MIN_PAIRS_FOR_CALIBRATION,
        "calibration_window": CALIBRATION_WINDOW,
    }


def recalibrate_recoat_and_apply(db: Session) -> dict:
    """Recompute per-material recoat_time_ms from logs and persist in-range values.

    No-op when ``correction_locked`` (shared with scan-time calibration — one
    "auto-calibration" toggle for the operator, not two). Caller commits.
    """
    report = recoat_accuracy(db)
    by_material = report["by_material"]

    row = db.get(MachineParams, 1)
    if row is None:
        return {"applied": {}, "skipped": [], "locked": False, "reason": "no machine params"}
    if row.correction_locked:
        return {"applied": {}, "skipped": [], "locked": True}

    current = dict(row.recoat_time_by_mat or {})
    applied: dict[str, float] = {}
    skipped: list[dict] = []
    for material, info in by_material.items():
        value = info["suggested_recoat_ms"]
        if value is None:
            continue
        if not (RECOAT_MIN_MS <= value <= RECOAT_MAX_MS):
            skipped.append({"material": material, "recoat_ms": value, "reason": "out_of_range"})
            logger.warning(
                "recoat calibration: %s learned %.0f ms out of [%.0f, %.0f] — not applied "
                "(check the logs / layer_timing_summary parsing for this material)",
                material, value, RECOAT_MIN_MS, RECOAT_MAX_MS,
            )
            continue
        if current.get(material) != value:
            logger.info("recoat calibration: %s %s ms → %.0f ms (%d sessions)",
                        material, current.get(material), value, info["n_sessions"])
            applied[material] = value

    if applied:
        current.update(applied)
        row.recoat_time_by_mat = current
        row.updated_at = datetime.now(timezone.utc)

    return {"applied": applied, "skipped": skipped, "locked": False}


__all__ = [
    "recoat_accuracy",
    "recalibrate_recoat_and_apply",
    "session_recoat_seconds",
    "session_machine_seconds_by_layer",
    "RECOAT_MIN_MS",
    "RECOAT_MAX_MS",
]
