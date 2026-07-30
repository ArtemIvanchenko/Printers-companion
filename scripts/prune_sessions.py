#!/usr/bin/env python
"""Delete log sessions and everything hanging off them, by explicit id.

Written for a one-off cleanup, kept because the situation recurs: this DB held
five sessions imported twice (once before the grouping fix keyed ids on the run
prefix, once after) plus one built from a Finder ``.DS_Store``. The import path
no longer produces either — see the session_grouping and runtime-repo fixes —
but nothing removed what had already accumulated, and the dashboard counted all
of it as real prints.

Ids are passed explicitly rather than detected. "Which of two identical
sessions is the canonical one" is a judgement call (the answer here was: the id
that ``_deterministic_group_id`` produces today), and a script guessing it
would eventually guess wrong on data nobody is watching.

Refuses to run without --yes. Prints the full blast radius first, including
every dependent row it will take with it.

Usage (inside the api container, or locally with the right DATABASE_URL):
    python scripts/prune_sessions.py session_a session_b
    python scripts/prune_sessions.py session_a session_b --yes
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Every table with a FK to sessions.session_id. Ordered children-first so the
# deletes never trip a constraint. Verified against information_schema rather
# than written from memory — a missed table would abort the whole transaction.
_DEPENDENT_TABLES = (
    "anomalies",
    "build_jobs",
    "canonical_events",
    "hypotheses",
    "layer_snapshots",
    "operator_events",
    "operator_journal_entries",
    "parse_diagnostics",
    "production_context_snapshots",
    "quality_outcomes",
    "reports",
    "segments",
    "source_files",
    "state_transitions",
)


def prune(session_ids: list[str], apply: bool) -> int:
    from sqlalchemy import delete, func, select, text

    from domain.models.prints import PrintRecord
    from domain.models.sessions import BuildSession
    from storage.db.session import SessionLocal

    with SessionLocal() as db:
        found = set(db.scalars(
            select(BuildSession.session_id).where(BuildSession.session_id.in_(session_ids))
        ))
        missing = [s for s in session_ids if s not in found]
        for sid in missing:
            print(f"  ? {sid}: не найдена, пропускаю")
        targets = [s for s in session_ids if s in found]
        if not targets:
            print("\nНечего удалять.")
            return 0

        print(f"\nК удалению {len(targets)} сесси(й):")
        for sid in targets:
            row = db.get(BuildSession, sid)
            print(f"  • {sid}  [{row.classification}]")

        # Print records are not deleted implicitly: a card is operator-entered
        # work (name, material, files) and losing it silently would be worse
        # than a dangling link. Report them and let the operator decide.
        linked = db.scalars(
            select(PrintRecord).where(PrintRecord.session_id.in_(targets))
        ).all()
        if linked:
            print("\n  ⚠ К этим сессиям привязаны карточки печати:")
            for rec in linked:
                print(f"      {rec.record_id}  «{rec.name}»  — привязка будет снята, карточка останется")

        print("\nЗависимые строки:")
        total_deps = 0
        for table in _DEPENDENT_TABLES:
            n = db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE session_id = ANY(:ids)"),  # noqa: S608 - table names are a fixed literal tuple
                {"ids": targets},
            ).scalar_one()
            if n:
                print(f"  {table}: {n}")
                total_deps += n
        if not total_deps:
            print("  нет")

        if not apply:
            print("\n(--dry-run: ничего не записано, запустите с --yes)")
            return 0

        for rec in linked:
            rec.session_id = None
        for table in _DEPENDENT_TABLES:
            db.execute(
                text(f"DELETE FROM {table} WHERE session_id = ANY(:ids)"),  # noqa: S608 - see above
                {"ids": targets},
            )
        db.execute(delete(BuildSession).where(BuildSession.session_id.in_(targets)))
        db.commit()

        remaining = db.scalar(select(func.count()).select_from(BuildSession))
        print(f"\nУдалено сесси(й): {len(targets)}. Осталось в базе: {remaining}.")
        return len(targets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_ids", nargs="+", help="session_id для удаления")
    parser.add_argument("--yes", action="store_true", help="действительно удалить (иначе только показать)")
    args = parser.parse_args()
    prune(args.session_ids, apply=args.yes)


if __name__ == "__main__":
    main()
