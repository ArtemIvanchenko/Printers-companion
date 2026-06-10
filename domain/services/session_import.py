"""Single source of truth for the lightweight session-import core.

The startup scan, the dashboard-upload rescan, the ``/sessions/ingest`` endpoint
and the confirmed-import worker each ran a near-identical
``scan → group → build overview → save slim payload`` loop. The copies drifted
(one forgot to strip ``parse_result``, another stored a bare stub), which showed
up as half-empty dashboards. Centralising the core here stops the paths from
diverging again.

Heavy imports (parsers, profiles, analytics) are kept lazy so importing this
module stays cheap.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from domain.services.session_grouping import SessionGroup

logger = logging.getLogger(__name__)


def slim_session_payload(group: "SessionGroup", overview: dict[str, Any]) -> dict[str, Any]:
    """Storable payload for a session: files with events stripped + the overview.

    ``parse_result`` (the parsed events) is excluded to keep the stored row tiny
    (~96 MB/session otherwise); consumers re-read events from disk on demand.
    """
    return {
        "files": [f.model_dump(mode="json", exclude={"parse_result"}) for f in group.files],
        "group": overview,
    }


def overview_for_group(group: "SessionGroup") -> dict[str, Any]:
    """Build the enriched dashboard overview for a grouped session."""
    from domain.services.session_overview import build_group_overview

    return build_group_overview(
        group.group_id,
        group.files,
        start_ts=group.start_ts,
        end_ts=group.end_ts,
        grouping_confidence=group.confidence,
    )


def scan_and_group(folder: Path) -> list["SessionGroup"]:
    """Parse every file under ``folder`` and group them into print sessions."""
    from domain.services.ingestion import IngestionService
    from domain.services.session_grouping import group_files_into_sessions
    from profiles.m350.profile import build_registry, get_profile

    result = IngestionService(build_registry(), get_profile()).parse(folder)
    return group_files_into_sessions(result.files)


def import_new_sessions(folder: Path, repo) -> dict[str, int]:
    """Scan, group, and persist slim payloads for sessions not already stored.

    Idempotent: a session whose deterministic ``group_id`` already exists is
    skipped, so repeated scans (startup, upload rescan) add only new prints.
    Returns ``{"found": <groups>, "imported": <new>}``.
    """
    groups = scan_and_group(folder)
    if not groups:
        return {"found": 0, "imported": 0}
    existing = {session_id for session_id, _ in repo.list_session_payloads()}
    imported = 0
    for group in groups:
        if group.group_id in existing:
            continue
        repo.save_session_payload(group.group_id, slim_session_payload(group, overview_for_group(group)))
        imported += 1
    return {"found": len(groups), "imported": imported}
