#!/usr/bin/env python
"""Import a folder of printer logs into the database (no API/Docker required).

Runs the same pipeline as POST /sessions/ingest — parse -> group into sessions ->
enrich with classification/features/telemetry -> persist — so the imported sessions
render directly in the web dashboard.

Database-agnostic: it uses whatever DATABASE_URL is configured (PostgreSQL inside
Docker, or local SQLite). Schema is ensured via create_all() (idempotent).

Usage:
    python import_logs.py <folder>
    python import_logs.py "C:\\PrinterLogs\\incoming"
    RAW_LOGS_DIR=/mnt/raw_logs python import_logs.py        # folder from env

Examples (local SQLite):
    DATABASE_URL=sqlite:///./printer_logs.db python import_logs.py ./logs
"""
import argparse
import os
import sys
from pathlib import Path

# Ensure the project root is importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain.services.ingestion import IngestionService
from domain.services.session_grouping import group_files_into_sessions
from domain.services.session_overview import build_group_overview
from profiles.m350.profile import build_registry, get_profile
from storage.db.init_db import create_all
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


def import_folder(folder: Path) -> int:
    if not folder.exists():
        print(f"[ERROR] Folder not found: {folder}", file=sys.stderr)
        return 1

    print("[INFO] Ensuring database schema ...")
    create_all()

    print(f"[INFO] Parsing logs from: {folder}")
    service = IngestionService(build_registry(), get_profile())
    result = service.parse(folder)
    groups = group_files_into_sessions(result.files)
    print(f"[INFO] {len(result.files)} files -> {len(groups)} session(s)")

    db = SessionLocal()
    repo = RuntimeRepository(db)
    saved = 0
    try:
        for group in groups:
            overview = build_group_overview(
                group.group_id,
                group.files,
                start_ts=group.start_ts,
                end_ts=group.end_ts,
                grouping_confidence=group.confidence,
            )
            repo.save_session_payload(
                group.group_id,
                {"files": [f.model_dump(mode="json") for f in group.files], "group": overview},
            )
            feats = overview["features"]
            print(
                f"  [OK] {group.group_id}: {overview['classification']} "
                f"(files={feats['file_count']}, events={feats['total_events']}, "
                f"lines={feats['total_lines']}, duration={feats['duration_min']}min)"
            )
            saved += 1
    except Exception as exc:  # noqa: BLE001 - surface any import failure to the operator
        db.rollback()
        print(f"[ERROR] Import failed: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()

    print(f"[SUCCESS] {saved} session(s) imported.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import printer logs into the database.")
    parser.add_argument(
        "folder",
        nargs="?",
        default=os.environ.get("RAW_LOGS_DIR"),
        help="Path to the folder containing the printer log files.",
    )
    args = parser.parse_args()
    if not args.folder:
        parser.error("provide a folder argument or set RAW_LOGS_DIR")
    return import_folder(Path(args.folder))


if __name__ == "__main__":
    raise SystemExit(main())
