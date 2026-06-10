#!/usr/bin/env python
"""One-shot backfill: re-run build_group_overview for sessions that were imported
via the watcher path before the enrichment fix.

Such sessions have a bare group stub (no features / telemetry / classification),
which causes the dashboard to show empty graphs and INCOMPLETE_OR_UNKNOWN status.

The script is idempotent — sessions that already have features are skipped.
Raw log files must still exist on disk (the path stored in IngestedFile.relative_path
is resolved relative to RAW_LOGS_CONTAINER_PATH from the environment).

Usage (inside the api container, or locally with the right DATABASE_URL):
    python backfill_session_overview.py --dry-run
    python backfill_session_overview.py
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _needs_backfill(group: dict) -> bool:
    if not group:
        return True
    features = group.get("features") or {}
    return not features


def backfill(dry_run: bool) -> None:
    from domain.services.ingestion import IngestedFile
    from domain.services.session_overview import build_group_overview
    from domain.models.entities import BuildSession
    from storage.db.session import SessionLocal
    from storage.repositories.runtime import _rehydrate_parse_results

    ok = skipped = failed = 0

    with SessionLocal() as db:
        rows = db.query(BuildSession).all()
        for row in rows:
            sid = row.session_id
            payload = (row.context or {}).get("runtime_payload")
            if not payload:
                print(f"[skip] {sid}: no runtime_payload")
                skipped += 1
                continue

            group = payload.get("group") or {}
            if not _needs_backfill(group):
                print(f"[skip] {sid}: already has features")
                skipped += 1
                continue

            files_raw = payload.get("files") or []
            if not files_raw:
                print(f"[skip] {sid}: no files in payload")
                skipped += 1
                continue

            try:
                files = [IngestedFile.model_validate(f) for f in files_raw]
            except Exception as exc:
                print(f"[fail] {sid}: cannot rebuild IngestedFile list: {exc}")
                failed += 1
                continue

            # Re-populate parse results from disk so build_group_overview has
            # real events/telemetry to work with.
            files = _rehydrate_parse_results(files)

            try:
                overview = build_group_overview(
                    sid,
                    files,
                    start_ts=row.start_ts,
                    end_ts=row.end_ts,
                    grouping_confidence=group.get("grouping_confidence", 0.0),
                )
            except Exception as exc:
                print(f"[fail] {sid}: build_group_overview error: {exc}")
                failed += 1
                continue

            classification = overview.get("classification", "?")
            layers = (overview.get("features") or {}).get("layers", "?")
            print(f"[{'DRY' if dry_run else 'OK '}] {sid}: {classification}, layers={layers}")

            if not dry_run:
                stripped_files = [
                    f.model_dump(mode="json", exclude={"parse_result"}) for f in files
                ]
                if row.context is None:
                    row.context = {}
                row.context = {
                    **row.context,
                    "runtime_payload": {**payload, "files": stripped_files, "group": overview},
                }
                # SQLAlchemy won't detect mutation of nested JSON; mark as modified.
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(row, "context")
                db.commit()

            ok += 1

    print(f"\nDone. backfilled={ok}  skipped={skipped}  failed={failed}")
    if dry_run:
        print("(dry-run — nothing was written)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill session overview data.")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
