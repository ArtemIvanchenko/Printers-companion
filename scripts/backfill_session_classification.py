#!/usr/bin/env python
"""One-shot backfill: copy each session's classification from its payload into
the indexed ``sessions.classification`` column.

Why this exists: ``save_session_payload`` never assigned the column (it was only
ever passed at row creation, and creation passed nothing), so every session in
the DB kept the ``INCOMPLETE_OR_UNKNOWN`` default while its payload said
REAL_PRINT. Readers papered over it with ``payload or column``, so nothing broke
visibly — but "only real prints" could not be expressed in SQL. The write path is
fixed now; this brings already-stored rows in line without a re-import (which
would need the raw logs still on disk).

Idempotent: a row whose column already matches its payload is left alone.

Usage (inside the api container, or locally with the right DATABASE_URL):
    python scripts/backfill_session_classification.py --dry-run
    python scripts/backfill_session_classification.py
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def backfill(dry_run: bool) -> int:
    from sqlalchemy import select

    from domain.models.sessions import BuildSession
    from storage.db.session import SessionLocal

    updated = matched = no_payload = 0

    with SessionLocal() as db:
        for row in db.scalars(select(BuildSession)).all():
            group = ((row.context or {}).get("runtime_payload", {}) or {}).get("group", {}) or {}
            wanted = group.get("classification")
            if not wanted:
                no_payload += 1
                print(f"  — {row.session_id}: no classification in payload, left as {row.classification!r}")
                continue
            if row.classification == wanted:
                matched += 1
                continue
            confidence = float(group.get("classification_confidence") or group.get("confidence") or 0.0)
            print(f"  ✓ {row.session_id}: {row.classification!r} → {wanted!r} (conf {confidence:.2f})")
            if not dry_run:
                row.classification = wanted
                row.classification_confidence = confidence
            updated += 1

        if dry_run:
            db.rollback()
        else:
            db.commit()

    verb = "would update" if dry_run else "updated"
    print(f"\n{verb}: {updated} | already correct: {matched} | no classification in payload: {no_payload}")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()
    backfill(args.dry_run)


if __name__ == "__main__":
    main()
