#!/usr/bin/env python
"""One-shot migration: recompute session start/end/duration for ALREADY-imported
sessions using the corrected print-span logic (monitor100 daemon excluded).

Why this is needed
------------------
``save_session_payload`` deliberately never overwrites a session's start_ts/end_ts
once set ("first import wins" — protects good data from a bad re-import). So the
duration fix in ``compute_session_spans`` only affects *new* imports; sessions stored
before the fix keep their inflated times (e.g. ~99 h instead of ~82 h).

This script repairs owner-local legacy sessions from their raw sources, with SQL
closed during parsing and computation. Published analyses/reports are instead
requeued through the original import job, preserving coherent generations.
Existing ``signal_stats`` are preserved when legacy source reconstruction cannot
reproduce them.

Idempotent and safe to re-run. Use --dry-run to preview without writing.

Usage:
    python scripts/maintenance/recompute_session_times.py --dry-run
    python scripts/maintenance/recompute_session_times.py
    DATABASE_URL=sqlite:///./printer_logs.db python scripts/maintenance/recompute_session_times.py
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from domain.services.session_classification import classify_session
from domain.services.session_overview import build_group_overview
from scripts.maintenance.backfill_session_overview import (
    _lock_unchanged, _parsed_sources, _published, _queue_reanalysis,
    _read_session, _require_owner, _session_ids,
)
from storage.db.session import SessionLocal


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def recompute(dry_run: bool) -> int:
    from domain.services.compute_affinity import ComputeAffinityError

    changed = queued = skipped = failed = 0
    for sid in _session_ids():
        snapshot = _read_session(sid)
        payload = (snapshot["context"] or {}).get("runtime_payload") if snapshot else None
        if not payload:
            skipped += 1
            continue
        try:
            _require_owner(snapshot)
            if _published(snapshot):
                _queue_reanalysis(snapshot, dry_run)
                queued += 1
                continue
            files = _parsed_sources(snapshot)
            old_group = payload.get("group", {}) or {}
            old_feats = old_group.get("features", {}) or {}
            old_dur_min = old_feats.get("duration_min")
            # Fall back to the previously-persisted anchors when no usable
            # in-content timestamps exist (table-only sessions).
            anchor_start = _parse_ts(old_group.get("start_ts"))
            anchor_end = _parse_ts(old_group.get("end_ts"))

            overview = build_group_overview(
                sid,
                files,
                start_ts=anchor_start,
                end_ts=anchor_end,
                grouping_confidence=float(old_group.get("confidence") or 0.0),
                classification=classify_session(files),
            )
            # Direct legacy repair must not claim a new published generation.
            overview.pop("analysis_snapshot", None)
            # Preserve expensive signal_stats if the recompute couldn't reproduce
            # them (raw sensors.log absent on this machine).
            if not overview.get("signal_stats") and old_group.get("signal_stats"):
                overview["signal_stats"] = old_group["signal_stats"]

            new_dur_min = (overview.get("features") or {}).get("duration_min")
            disp_start = _parse_ts(overview.get("start_ts"))
            disp_end = _parse_ts(overview.get("end_ts"))

            if not dry_run:
                with SessionLocal() as db:
                    row = _lock_unchanged(db, snapshot)
                    row.context = {**(row.context or {}), "runtime_payload": {**payload, "group": overview}}
                    # Force-update the columns the normal save path won't overwrite.
                    if disp_start:
                        row.start_ts = disp_start
                    if disp_end:
                        row.end_ts = disp_end
                    row.updated_at = datetime.now(timezone.utc)
                    db.commit()
            marker = "DRY" if dry_run else "FIX"
            print(
                f"[{marker}] {sid}: "
                f"duration {old_dur_min}min -> {new_dur_min}min | "
                f"{_fmt(disp_start)} … {_fmt(disp_end)}"
            )
            changed += 1
        except ComputeAffinityError as exc:
            print(f"[skip] {sid}: {exc}")
            skipped += 1
        except Exception as exc:
            print(f"[fail] {sid}: {exc}")
            failed += 1
    print(f"Повторный анализ: {queued} | пропущено: {skipped} | ошибки: {failed}")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = ap.parse_args()
    n = recompute(args.dry_run)
    verb = "would update" if args.dry_run else "updated"
    print(f"\nDone — {verb} {n} session(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
