#!/usr/bin/env python
"""One-shot backfill: store per-layer burn/pour for sessions imported earlier.

New imports store these rows as they go (api/routes/sessions.py). Sessions that
predate that still carry their timings only inside the raw log, so calibration
for them keeps depending on the file being on this machine's disk — exactly the
dependency the storage was introduced to remove.

Reads each session's time_log the old way (from disk, or from the mirrored copy
in object storage) and writes the conclusions into layer_snapshots. Idempotent:
re-running replaces what a session already has.

Usage (inside the api container, or locally with the right DATABASE_URL):
    python scripts/backfill_layer_timings.py --dry-run
    python scripts/backfill_layer_timings.py
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def backfill(dry_run: bool) -> int:
    from sqlalchemy import select

    from analytics.prediction.layer_timings import store_layer_timings, stored_timings
    from domain.models.sessions import BuildSession
    from storage.db.session import SessionLocal
    from storage.repositories.runtime import RuntimeRepository

    stored_total = skipped = already = 0

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        for session_id in db.scalars(select(BuildSession.session_id)).all():
            if stored_timings(session_id, db):
                already += 1
                continue
            files = repo.get_session_files(session_id, rehydrate=True) or []
            if dry_run:
                # Count what would be written without touching the DB.
                from domain.enums.common import SourceFileFamily
                n = sum(
                    1
                    for f in files
                    if f.classification.family == SourceFileFamily.time_log and f.parse_result
                    for e in f.parse_result.events
                    if getattr(e, "event_type", None) == "layer_timing_summary"
                )
            else:
                n = store_layer_timings(session_id, files, db)
            if n:
                verb = "было бы сохранено" if dry_run else "сохранено"
                print(f"  ✓ {session_id}: {verb} слоёв — {n}")
                stored_total += n
            else:
                print(f"  — {session_id}: нет читаемого time_log")
                skipped += 1

        if dry_run:
            db.rollback()
        else:
            db.commit()

    verb = "было бы записано" if dry_run else "записано"
    print(f"\n{verb} слоёв: {stored_total} | уже было: {already} | без логов: {skipped}")
    return stored_total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="показать, ничего не писать")
    args = parser.parse_args()
    backfill(args.dry_run)


if __name__ == "__main__":
    main()
