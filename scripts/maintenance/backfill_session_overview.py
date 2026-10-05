#!/usr/bin/env python
"""One-shot backfill: re-run build_group_overview for sessions that were imported
via the watcher path before the enrichment fix.

Such sessions have a bare group stub (no features / telemetry / classification),
which causes the dashboard to show empty graphs and INCOMPLETE_OR_UNKNOWN status.

The script is idempotent — sessions that already have features are skipped.
Only owner-local legacy sessions are repaired directly. Published analyses and
reports are requeued through their original import job, so their projections
remain coherent. Source reconstruction and overview computation run outside SQL.

Usage (inside the api container, or locally with the right DATABASE_URL):
    python scripts/maintenance/backfill_session_overview.py --dry-run
    python scripts/maintenance/backfill_session_overview.py
"""
import argparse
from copy import deepcopy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _session_ids() -> list[str]:
    from sqlalchemy import select
    from domain.models.sessions import BuildSession
    from storage.db.session import SessionLocal

    with SessionLocal() as db:
        return list(db.scalars(select(BuildSession.session_id).order_by(BuildSession.session_id)))


def _snapshot(row) -> dict:
    return deepcopy({column.key: getattr(row, column.key) for column in row.__table__.columns})


def _read_session(session_id: str) -> dict | None:
    from sqlalchemy import select
    from domain.models.sessions import BuildSession, ReportArtifact
    from storage.db.session import SessionLocal

    with SessionLocal() as db:
        row = db.get(BuildSession, session_id)
        if row is None:
            return None
        result = _snapshot(row)
        result["has_reports"] = db.scalar(select(ReportArtifact.report_id).where(
            ReportArtifact.session_id == session_id,
        ).limit(1)) is not None
        return result


def _require_owner(snapshot: dict) -> None:
    from core.config.settings import get_settings
    from domain.services.compute_affinity import require_compute_owner

    require_compute_owner(
        entity_type="session", entity_id=snapshot["session_id"],
        origin_compute_node_id=snapshot["origin_compute_node_id"],
        requested_compute_node_id=get_settings().compute_node_id,
    )


def _published(snapshot: dict) -> bool:
    context = snapshot["context"] or {}
    group = ((context.get("runtime_payload") or {}).get("group") or {})
    timing = context.get("timing_publication") or {}
    # Legacy layer backfill publishes its own authoritative compact measurements
    # with source={}; it does not create a coherent session/report generation.
    # Keep those timings untouched while allowing the other legacy repairs.
    original_source = not isinstance(timing, dict) or bool(timing.get("source"))
    return (original_source or "analysis_snapshot" in group
            or bool(snapshot["analysis_version"]) or snapshot["has_reports"])


def _lock_unchanged(db, snapshot: dict):
    from sqlalchemy import select
    from core.versioning.provenance import stable_hash
    from domain.models.sessions import BuildSession, ReportArtifact

    row = db.scalar(select(BuildSession).where(
        BuildSession.session_id == snapshot["session_id"],
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise ValueError("Сессия удалена за время расчёта")
    current = _snapshot(row)
    _require_owner(current)
    if stable_hash(current) != stable_hash({key: value for key, value in snapshot.items()
                                          if key != "has_reports"}):
        raise ValueError("Сессия изменилась за время расчёта; устаревший результат не записан")
    has_reports = db.scalar(select(ReportArtifact.report_id).where(
        ReportArtifact.session_id == snapshot["session_id"],
    ).limit(1)) is not None
    if has_reports != snapshot["has_reports"]:
        raise ValueError("Отчёты сессии изменились за время расчёта; требуется повторный анализ")
    return row


def _queue_reanalysis(snapshot: dict, dry_run: bool) -> dict | None:
    """Existing publication is repaired by its worker, never by partial JSON edits."""
    from core.config.settings import get_settings
    from domain.services.session_requests import request_analysis
    from storage.db.session import SessionLocal

    _require_owner(snapshot)
    if dry_run:
        print(f"[DRY queue] {snapshot['session_id']}: требуется повторный анализ исходного задания")
        return None
    with SessionLocal() as db:
        _lock_unchanged(db, snapshot)
        result = request_analysis(db, snapshot["session_id"],
                                  compute_node_id=get_settings().compute_node_id,
                                  actor="maintenance")
    print(f"[JOB {result['job_status']}] {snapshot['session_id']}: {result['job_id']}; "
          "не пересчитано этим скриптом")
    return result


def _parsed_sources(snapshot: dict) -> list:
    """The read session has already closed before this boundary opens any file."""
    from domain.services.ingestion import IngestedFile
    from domain.services.session_sources import SessionSources, rehydrate_session_sources

    payload = (snapshot["context"] or {}).get("runtime_payload") or {}
    sources = SessionSources(snapshot["session_id"], snapshot["origin_compute_node_id"],
                             [IngestedFile.model_validate(item) for item in payload.get("files", [])])
    files = rehydrate_session_sources(sources)
    if not any(file.parse_result is not None for file in files):
        raise ValueError("Нет читаемых исходных логов; сохранённые данные не изменены")
    return files


def _needs_backfill(group: dict) -> bool:
    if not group:
        return True
    features = group.get("features") or {}
    return not features


def backfill(dry_run: bool, force: bool = False) -> None:
    from domain.services.compute_affinity import ComputeAffinityError
    from domain.services.session_overview import build_group_overview
    from storage.db.session import SessionLocal

    ok = queued = skipped = failed = 0
    for sid in _session_ids():
        snapshot = _read_session(sid)
        if snapshot is None:
            skipped += 1
            continue
        payload = (snapshot["context"] or {}).get("runtime_payload")
        group = (payload or {}).get("group") or {}
        if not payload or (not force and not _needs_backfill(group)):
            print(f"[skip] {sid}: нет payload или признаки уже рассчитаны")
            skipped += 1
            continue
        try:
            _require_owner(snapshot)
            if _published(snapshot):
                _queue_reanalysis(snapshot, dry_run)
                queued += 1
                continue
            files = _parsed_sources(snapshot)
            overview = build_group_overview(sid, files, start_ts=snapshot["start_ts"],
                                            end_ts=snapshot["end_ts"],
                                            grouping_confidence=group.get("grouping_confidence", 0.0))
            # This is a legacy projection, not a coherent report publication.
            # Never introduce a modern marker without its matching artifacts.
            overview.pop("analysis_snapshot", None)
            if not dry_run:
                with SessionLocal() as db:
                    row = _lock_unchanged(db, snapshot)
                    row.context = {**(row.context or {}), "runtime_payload": {
                        **payload, "group": overview,
                        "files": [file.model_dump(mode="json", exclude={"parse_result"}) for file in files],
                    }}
                    db.commit()
            print(f"[{'DRY' if dry_run else 'OK'}] {sid}: {overview.get('classification', '?')}, "
                  f"layers={(overview.get('features') or {}).get('layers', '?')}")
            ok += 1
        except ComputeAffinityError as exc:
            print(f"[skip] {sid}: {exc}")
            skipped += 1
        except Exception as exc:
            print(f"[fail] {sid}: {exc}")
            failed += 1

    print(f"\nDone. legacy_backfilled={ok}  reanalysis_requested={queued}  skipped={skipped}  failed={failed}")
    if dry_run:
        print("(dry-run — nothing was written)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill session overview data.")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument(
        "--force", action="store_true",
        help="recompute sessions that already have features — needed after the "
             "overview logic itself changes (e.g. the signal range filter), "
             "since stored stats are otherwise kept as-is",
    )
    args = parser.parse_args()
    backfill(dry_run=args.dry_run, force=args.force)
