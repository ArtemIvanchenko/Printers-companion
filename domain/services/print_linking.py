"""Auto-linking of log sessions to print records by print date.

Idempotent sweep: every unlinked PrintRecord is matched against sessions
whose ``start_ts`` falls within ±window of the record's print date
(``printed_at``, falling back to ``created_at``). Only unambiguous 1:1
pairs are linked — when a record matches several sessions or a session
matches several records, nothing happens until the operator resolves it
manually (PATCH /prints/{id} с session_id).

Call after any import path creates sessions; safe to run repeatedly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config.settings import get_settings
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession

logger = logging.getLogger(__name__)


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _link(record: PrintRecord, session: BuildSession, links: list[dict]) -> None:
    record.session_id = session.session_id
    record.printed_at = _as_utc(session.start_ts)  # логи — авторитетная дата печати
    record.updated_at = datetime.now(timezone.utc)
    links.append({"record_id": record.record_id, "session_id": session.session_id})
    logger.info("print_linking: linked %s ↔ %s", record.record_id, session.session_id)


def _resolve_import_hints(
    records: list[PrintRecord], sessions: list[BuildSession], links: list[dict],
) -> None:
    """Explicit operator intent: logs uploaded via a record's import-logs.

    The hint (log file date) wins over date-ambiguity with other records, but
    two same-date sessions are still ambiguous and stay unlinked.
    """
    for record in records:
        hint = (record.metadata_json or {}).get("log_import_hint")
        if not hint or record.session_id:
            continue
        matches = [
            s for s in sessions
            if s.session_id and _as_utc(s.start_ts).date().isoformat() == hint.get("date")
        ]
        if len(matches) == 1:
            _link(record, matches[0], links)
        elif len(matches) > 1:
            logger.info("print_linking: hint for %s matches %d sessions — skipped",
                        record.record_id, len(matches))
            continue
        # Hint resolved (or no session yet — keep it for the next sweep)
        if len(matches) == 1:
            meta = dict(record.metadata_json or {})
            meta.pop("log_import_hint", None)
            record.metadata_json = meta


def auto_link_print_records(db: Session, window_hours: float | None = None) -> list[dict]:
    """Link unlinked print records to sessions by date. Returns created links.

    The caller commits; this function only mutates rows.
    """
    window = timedelta(hours=window_hours or get_settings().print_link_window_hours)

    records = db.scalars(
        select(PrintRecord).where(PrintRecord.session_id.is_(None))
    ).all()
    if not records:
        return []

    taken = {
        sid for sid in db.scalars(
            select(PrintRecord.session_id).where(PrintRecord.session_id.is_not(None))
        )
    }
    sessions = [
        s for s in db.scalars(
            select(BuildSession).where(BuildSession.start_ts.is_not(None))
        ).all()
        if s.session_id not in taken
    ]
    if not sessions:
        return []

    links: list[dict] = []
    _resolve_import_hints(records, sessions, links)
    if links:
        linked_sessions = {l["session_id"] for l in links}
        sessions = [s for s in sessions if s.session_id not in linked_sessions]
        records = [r for r in records if r.session_id is None]

    # record → matching sessions and session → matching records
    record_candidates: dict[str, list[BuildSession]] = {}
    session_hits: dict[str, int] = {}
    for record in records:
        anchor = _as_utc(record.printed_at or record.created_at)
        matches = [s for s in sessions if abs(_as_utc(s.start_ts) - anchor) <= window]
        record_candidates[record.record_id] = matches
        for s in matches:
            session_hits[s.session_id] = session_hits.get(s.session_id, 0) + 1

    by_id = {r.record_id: r for r in records}
    for record_id, matches in record_candidates.items():
        if len(matches) != 1:
            if len(matches) > 1:
                logger.info("print_linking: record %s matches %d sessions — skipped (ambiguous)",
                            record_id, len(matches))
            continue
        session = matches[0]
        if session_hits.get(session.session_id, 0) != 1:
            logger.info("print_linking: session %s matches several records — skipped (ambiguous)",
                        session.session_id)
            continue
        _link(by_id[record_id], session, links)

    if links:
        # SessionLocal runs with autoflush=False — flush so repeated calls
        # within one session see the new links (idempotency).
        db.flush()
    return links


def session_candidates(db: Session, record_id: str, window_hours: float | None = None) -> list[dict]:
    """Unclaimed sessions near the record's print date — for manual confirmation.

    Unlike the auto-linker this includes ambiguous matches: the operator
    picks one in the dashboard.
    """
    record = db.get(PrintRecord, record_id)
    if not record:
        return []
    window = timedelta(hours=window_hours or get_settings().print_link_window_hours)
    anchor = _as_utc(record.printed_at or record.created_at)
    taken = {
        sid for sid in db.scalars(
            select(PrintRecord.session_id).where(PrintRecord.session_id.is_not(None))
        )
    }
    out = []
    for s in db.scalars(select(BuildSession).where(BuildSession.start_ts.is_not(None))).all():
        if s.session_id in taken:
            continue
        delta = abs(_as_utc(s.start_ts) - anchor)
        if delta <= window:
            duration_min = None
            if s.end_ts:
                duration_min = round((_as_utc(s.end_ts) - _as_utc(s.start_ts)).total_seconds() / 60, 1)
            out.append({
                "session_id": s.session_id,
                "start_ts": _as_utc(s.start_ts).isoformat(),
                "end_ts": _as_utc(s.end_ts).isoformat() if s.end_ts else None,
                "duration_min": duration_min,
                "hours_from_record_date": round(delta.total_seconds() / 3600, 1),
            })
    out.sort(key=lambda c: c["hours_from_record_date"])
    return out


__all__ = ["auto_link_print_records", "session_candidates"]
