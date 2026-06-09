#!/usr/bin/env python
"""One-shot migration: recompute session start/end/duration for ALREADY-imported
sessions using the corrected print-span logic (monitor100 daemon excluded).

Why this is needed
------------------
``save_session_payload`` deliberately never overwrites a session's start_ts/end_ts
once set ("first import wins" — protects good data from a bad re-import). So the
duration fix in ``compute_print_span`` only affects *new* imports; sessions stored
before the fix keep their inflated times (e.g. ~99 h instead of ~82 h).

This script walks every stored session, recomputes its overview from the parse
results already embedded in the saved payload (no need to re-read raw logs), and
force-updates the times. Existing ``signal_stats`` are preserved if a recompute
can't reproduce them (raw sensors.log not on disk at migration time).

Idempotent and safe to re-run. Use --dry-run to preview without writing.

Usage:
    python recompute_session_times.py --dry-run
    python recompute_session_times.py
    DATABASE_URL=sqlite:///./printer_logs.db python recompute_session_times.py
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain.services.ingestion import IngestedFile
from domain.services.session_classification import classify_session
from domain.services.session_overview import build_group_overview, compute_print_span
from domain.models.entities import BuildSession
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
    changed = 0
    with SessionLocal() as db:
        rows = db.query(BuildSession).all()
        for row in rows:
            payload = (row.context or {}).get("runtime_payload")
            if not payload:
                continue
            files_raw = payload.get("files") or []
            if not files_raw:
                continue
            try:
                files = [IngestedFile.model_validate(f) for f in files_raw]
            except Exception as exc:
                print(f"[skip] {row.session_id}: cannot rebuild files ({exc})")
                continue

            old_group = payload.get("group", {}) or {}
            old_feats = old_group.get("features", {}) or {}
            old_dur_min = old_feats.get("duration_min")

            new_start, new_end = compute_print_span(files)
            # Fall back to the previously-persisted anchors when no usable
            # in-content timestamps exist (table-only sessions).
            anchor_start = _parse_ts(old_group.get("start_ts"))
            anchor_end = _parse_ts(old_group.get("end_ts"))

            overview = build_group_overview(
                row.session_id,
                files,
                start_ts=anchor_start,
                end_ts=anchor_end,
                grouping_confidence=float(old_group.get("confidence") or 0.0),
                classification=classify_session(files),
            )
            # Preserve expensive signal_stats if the recompute couldn't reproduce
            # them (raw sensors.log absent on this machine).
            if not overview.get("signal_stats") and old_group.get("signal_stats"):
                overview["signal_stats"] = old_group["signal_stats"]

            new_dur_min = (overview.get("features") or {}).get("duration_min")
            disp_start = _parse_ts(overview.get("start_ts"))
            disp_end = _parse_ts(overview.get("end_ts"))

            marker = "DRY" if dry_run else "FIX"
            print(
                f"[{marker}] {row.session_id}: "
                f"duration {old_dur_min}min -> {new_dur_min}min | "
                f"{_fmt(disp_start)} … {_fmt(disp_end)}"
            )

            if not dry_run:
                new_context = dict(row.context or {})
                new_payload = dict(payload)
                new_payload["group"] = overview
                new_context["runtime_payload"] = new_payload
                row.context = new_context
                # Force-update the columns the normal save path won't overwrite.
                if disp_start:
                    row.start_ts = disp_start
                if disp_end:
                    row.end_ts = disp_end
                row.updated_at = datetime.now(timezone.utc)
            changed += 1

        if not dry_run:
            db.commit()
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
