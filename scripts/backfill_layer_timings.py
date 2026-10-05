#!/usr/bin/env python
"""One-shot backfill: store per-layer burn/pour for sessions imported earlier.

New imports publish these rows through the local import worker. Sessions that
predate that still carry their timings only inside the raw log, so calibration
for them keeps depending on the file being on this machine's disk — exactly the
dependency the storage was introduced to remove.

Reads owner-local legacy time_log files (disk or the session mirror) outside SQL
and writes prepared conclusions in a short, checked transaction. Any published
timing snapshot, including empty or rejected evidence, is preserved. Modern
analyses without timings are requeued through their original import job.

Usage (inside the api container, or locally with the right DATABASE_URL):
    python scripts/backfill_layer_timings.py --dry-run
    python scripts/backfill_layer_timings.py
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def backfill(dry_run: bool) -> int:
    from analytics.prediction.layer_timings import replace_layer_timings, stored_timing_events
    from analytics.prediction.timing_snapshot import prepare_layer_timings
    from core.config.settings import get_settings
    from domain.services.compute_affinity import ComputeAffinityError
    from scripts.maintenance.backfill_session_overview import (
        _lock_unchanged, _parsed_sources, _published, _queue_reanalysis,
        _read_session, _require_owner, _session_ids,
    )
    from storage.db.session import SessionLocal

    stored_total = skipped = already = queued = failed = 0
    for session_id in _session_ids():
        snapshot = _read_session(session_id)
        if snapshot is None:
            skipped += 1
            continue
        try:
            _require_owner(snapshot)
            with SessionLocal() as db:
                published = stored_timing_events(session_id, db) is not None
            if published:
                already += 1
                continue
            if _published(snapshot):
                _queue_reanalysis(snapshot, dry_run)
                queued += 1
                continue
            files = _parsed_sources(snapshot)
            prepared = prepare_layer_timings(files, owner_node_id=get_settings().compute_node_id)
            n = len(prepared.rows)
            if not dry_run:
                with SessionLocal() as db:
                    session = _lock_unchanged(db, snapshot)
                    if stored_timing_events(session_id, db) is not None:
                        raise ValueError("Снимок слоёв появился за время расчёта; он не заменён")
                    n = replace_layer_timings(session, prepared, db)
                    db.commit()
            if n:
                verb = "было бы сохранено" if dry_run else "сохранено"
                print(f"  ✓ {session_id}: {verb} слоёв — {n}")
                stored_total += n
            else:
                print(f"  — {session_id}: нет допустимых измерений time_log")
                skipped += 1
        except ComputeAffinityError as exc:
            print(f"  — {session_id}: {exc}")
            skipped += 1
        except Exception as exc:
            print(f"  ! {session_id}: {exc}")
            failed += 1

    verb = "было бы записано" if dry_run else "записано"
    print(f"\n{verb} слоёв: {stored_total} | уже опубликовано: {already} | пропущено: {skipped} "
          f"| повторный анализ: {queued} | ошибки: {failed}")
    return stored_total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="показать, ничего не писать")
    args = parser.parse_args()
    backfill(args.dry_run)


if __name__ == "__main__":
    main()
