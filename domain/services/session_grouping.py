from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from domain.enums.common import SourceFileFamily
from domain.services.ingestion import IngestedFile
from parsers.common.timestamps import date_hint_from_filename


class SessionGroup(BaseModel):
    group_id: str
    files: list[IngestedFile] = Field(default_factory=list)
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)


def _normalize_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _file_temporal_anchor(file: IngestedFile) -> datetime:
    if file.parse_result:
        times = [
            _normalize_dt(event.ts)
            for event in file.parse_result.events
            if event.ts is not None
        ] + [
            _normalize_dt(transition.ts_start)
            for transition in file.parse_result.transitions
            if transition.ts_start is not None
        ]
        if times:
            return min(times)
    # No in-content timestamps (e.g. table-only burn/sensors/table_temp logs).
    # Prefer the date encoded in the filename (the printer names files by print day)
    # over mtime, which becomes unreliable once files are copied to USB/disk.
    file_name = file.classification.file_name or Path(file.relative_path).name
    hint = date_hint_from_filename(Path(file_name))
    if hint:
        # Use mtime time-of-day component when available — it disambiguates
        # multiple sessions on the same calendar day (e.g. two prints in one day).
        # mtime is unreliable for the date (USB copies change it) but the time
        # component within a known date is usually trustworthy enough for grouping.
        if file.mtime:
            mt = _normalize_dt(file.mtime)
            return datetime(hint.year, hint.month, hint.day,
                            mt.hour, mt.minute, mt.second, tzinfo=timezone.utc)
        return datetime(hint.year, hint.month, hint.day, tzinfo=timezone.utc)
    return _normalize_dt(file.mtime) if file.mtime else datetime.now(timezone.utc)


def group_files_into_sessions(
    files: list[IngestedFile],
    max_gap: timedelta = timedelta(hours=36),
) -> list[SessionGroup]:
    if not files:
        return []
    sorted_files = sorted(files, key=_file_temporal_anchor)
    first_anchor = _file_temporal_anchor(sorted_files[0])
    groups: list[SessionGroup] = []
    def _make_id(ts: datetime | None) -> str:
        date = ts.strftime("%Y%m%d") if ts else "unknown"
        return f"session_{date}_{uuid4().hex[:8]}"

    current = SessionGroup(group_id=_make_id(first_anchor), files=[sorted_files[0]], start_ts=first_anchor)
    last_anchor = first_anchor

    for file in sorted_files[1:]:
        anchor = _file_temporal_anchor(file)
        # A temporal gap larger than max_gap marks a distinct print session. Files of a
        # single print are written concurrently, so their anchors cluster; separate prints
        # are separated by the idle gap between runs. (File family must NOT override this:
        # every print produces the same family set, so a family check would merge all prints.)
        if anchor - last_anchor <= max_gap:
            current.files.append(file)
            current.reasons.append("temporal_continuity")
        else:
            current.end_ts = last_anchor
            current.confidence = _confidence(current)
            groups.append(current)
            current = SessionGroup(
                group_id=_make_id(anchor),
                files=[file],
                start_ts=anchor,
                reasons=["new_gap_exceeded"],
            )
        last_anchor = anchor
    current.end_ts = last_anchor
    current.confidence = _confidence(current)
    groups.append(current)
    return groups


def _confidence(group: SessionGroup) -> float:
    families = {file.classification.family for file in group.files}
    score = 0.35
    if SourceFileFamily.main_event_log in families:
        score += 0.15
    if SourceFileFamily.burn_log in families:
        score += 0.20
    if SourceFileFamily.stateflow_log in families:
        score += 0.20
    if SourceFileFamily.monitor100_log in families:
        score += 0.10
    return min(score, 0.95)


def manual_split(group: SessionGroup, file_paths_for_new_group: set[str]) -> tuple[SessionGroup, SessionGroup]:
    left = [file for file in group.files if file.path not in file_paths_for_new_group]
    right = [file for file in group.files if file.path in file_paths_for_new_group]
    return (
        SessionGroup(group_id=f"{group.group_id}_a", files=left, confidence=1.0, reasons=["manual_split"]),
        SessionGroup(group_id=f"{group.group_id}_b", files=right, confidence=1.0, reasons=["manual_split"]),
    )


def manual_merge(groups: list[SessionGroup]) -> SessionGroup:
    files = [file for group in groups for file in group.files]
    anchors = [_file_temporal_anchor(file) for file in files]
    return SessionGroup(
        group_id="manual_merge",
        files=files,
        start_ts=min(anchors) if anchors else None,
        end_ts=max(anchors) if anchors else None,
        confidence=1.0,
        reasons=["manual_merge"],
    )

