"""Grouping raw log files into print sessions.

The M-450-M writes every log of one run with a **common file-name prefix** and
appends a per-family suffix to it::

    23.03.2026.log              ← main event log   (prefix "23.03.2026")
    23.03.2026_sensors.log      ← sensors          (prefix "23.03.2026")
    23.03.2026_Monitor100.log   ← monitor daemon   (prefix "23.03.2026")

That prefix — not a timestamp heuristic — is the authoritative "same run"
signal, so it is what grouping keys on.

Why not time gaps: an earlier version clustered files by the gap between
consecutive file anchors. Files of ONE print anchor at wildly different times
(a sensors log carries in-content timestamps and anchors at the real print
start; a table-only log has none and anchors at its filename date + mtime), so
small thresholds tore a single print into several sessions. The threshold was
raised to 36 h to stop that — which turned the rule into single-linkage
clustering: because each file was compared with the *previous* file rather than
with the group start, any chain of files spaced under 36 h merged without limit,
so a stretch of separate prints collapsed into one session. The prefix has
neither failure mode.

Time is still used, but only as a guard: two runs that genuinely reuse the same
prefix (an operator retyping a job name months later) are split when a file
falls further than ``max_span`` from the group start. That comparison is against
the group start, never the previous file, so it cannot chain.
"""
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from domain.enums.common import SourceFileFamily
from domain.services.ingestion import IngestedFile
from parsers.common.timestamps import date_hint_from_filename

# Suffixes the printer appends to the run prefix. Longest-first so that
# "_stateFlowData.log" is never mistaken for "_stateFlow.log" + "Data".
_FAMILY_SUFFIXES: tuple[str, ...] = (
    "_stateflowdata.log",
    "_monitor100.log",
    "_monitor200.log",
    "_stateflow.log",
    "_sensors.log",
    "_error.log",
    "_burn.log",
    "_time.log",
)

# Machine-global logs: not tied to one run, so they carry no run prefix.
_PREFIXLESS_FILES = frozenset({"table_temp.log"})

# A single print never legitimately spans this long. Only ever compared against
# the group start, so it cannot chain groups together (see module docstring).
MAX_SESSION_SPAN = timedelta(days=14)

# Fallback for files whose name yields no run prefix (non-standard names).
# Same non-chaining rule, tighter bound.
PREFIXLESS_MAX_SPAN = timedelta(hours=36)


class SessionGroup(BaseModel):
    group_id: str
    files: list[IngestedFile] = Field(default_factory=list)
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)


def run_prefix(file_name: str) -> str | None:
    """The run prefix shared by every log of one print, or None.

    ``"23.03.2026_sensors.log" -> "23.03.2026"``; the main event log has no
    family suffix, so ``"23.03.2026.log" -> "23.03.2026"`` too.
    """
    lowered = file_name.lower()
    if lowered in _PREFIXLESS_FILES:
        return None
    for suffix in _FAMILY_SUFFIXES:
        if lowered.endswith(suffix):
            return file_name[: -len(suffix)] or None
    if lowered.endswith(".log"):
        return file_name[: -len(".log")] or None
    return None


def _file_name(file: IngestedFile) -> str:
    return file.classification.file_name or Path(file.relative_path).name


def _normalize_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _file_temporal_anchor(file: IngestedFile) -> datetime:
    """Best available "when did this file's data start" timestamp.

    Only used for ordering and for the span guard — never to decide whether two
    files belong to the same run (the prefix decides that), because anchors of
    files from ONE run legitimately differ by hours.
    """
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
    hint = date_hint_from_filename(Path(_file_name(file)))
    if hint:
        # Use mtime time-of-day component when available — it disambiguates
        # multiple sessions on the same calendar day (e.g. two prints in one day).
        # mtime is unreliable for the date (USB copies change it) but the time
        # component within a known date is usually trustworthy enough.
        if file.mtime:
            mt = _normalize_dt(file.mtime)
            return datetime(hint.year, hint.month, hint.day,
                            mt.hour, mt.minute, mt.second, tzinfo=timezone.utc)
        return datetime(hint.year, hint.month, hint.day, tzinfo=timezone.utc)
    return _normalize_dt(file.mtime) if file.mtime else datetime.now(timezone.utc)


def _group_date(prefix: str | None, files: list[IngestedFile], start_ts: datetime | None) -> str:
    """Date component of the group id.

    Taken from the run prefix when it encodes one (``23.03.2026`` → ``20260323``),
    because that is immutable for the run: deriving it from ``start_ts`` would
    change the id if a later import discovers a file with an earlier anchor.
    """
    hint: date | None = None
    if prefix:
        hint = date_hint_from_filename(Path(prefix))
    if hint is None:
        for file in sorted(files, key=_file_name):
            hint = date_hint_from_filename(Path(_file_name(file)))
            if hint:
                break
    if hint:
        return hint.strftime("%Y%m%d")
    return start_ts.strftime("%Y%m%d") if start_ts else "unknown"


def _deterministic_group_id(
    prefix: str | None, files: list[IngestedFile], start_ts: datetime | None,
) -> str:
    """Stable id for a session group: ``session_<date>_<hash>``.

    The hash is derived from the **run prefix**, so it does not change while a
    print is still being written: a session discovered with three files keeps
    its id when the remaining logs land, and the re-import guard (skip a
    group_id that already exists) actually deduplicates instead of inserting a
    second row for the same print on every scan.

    Files with no run prefix fall back to hashing their names — such a group has
    no run identity to key on, so its id necessarily depends on its membership.
    """
    date_part = _group_date(prefix, files, start_ts)
    if prefix:
        key = f"prefix:{prefix}"
    else:
        key = "names:" + "|".join(sorted(_file_name(f) for f in files))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"session_{date_part}_{digest}"


def _drop_logless_files(files: list[IngestedFile]) -> list[IngestedFile]:
    """Drop files that share no run prefix with any recognised printer log.

    A session is a print, and a print is evidenced by the printer's own logs.
    Without this, any stray file in a scanned folder became its own "session":
    the live DB held sessions built from a screenshot (``Безымянный.png``) and
    from a Finder ``.DS_Store``, which then showed up on the dashboard as
    prints and were counted by every consumer that groups by session.

    The filter works per run prefix rather than per file, so an unrecognised
    file is kept when its bucket holds at least one real log — a print whose
    main log has a non-standard name lands in the prefixless bucket together
    with such files, and must not lose them.
    """
    by_prefix: dict[str | None, list[IngestedFile]] = {}
    for file in files:
        by_prefix.setdefault(run_prefix(_file_name(file)), []).append(file)

    kept: list[IngestedFile] = []
    for bucket in by_prefix.values():
        if any(f.classification.family != SourceFileFamily.unsupported for f in bucket):
            kept.extend(bucket)
    return kept


def _split_by_span(
    files: list[IngestedFile], max_span: timedelta, split_reason: str,
) -> list[SessionGroup]:
    """Order files by anchor and cut whenever one falls past ``max_span`` from
    the CURRENT GROUP START. Comparing against the start (not the previous file)
    is what stops groups from chaining indefinitely."""
    ordered = sorted(files, key=_file_temporal_anchor)
    groups: list[SessionGroup] = []
    current = SessionGroup(group_id="", files=[ordered[0]], start_ts=_file_temporal_anchor(ordered[0]))
    last_anchor = current.start_ts

    for file in ordered[1:]:
        anchor = _file_temporal_anchor(file)
        if anchor - current.start_ts <= max_span:
            current.files.append(file)
            last_anchor = max(last_anchor, anchor)
        else:
            current.end_ts = last_anchor
            groups.append(current)
            current = SessionGroup(
                group_id="", files=[file], start_ts=anchor, reasons=[split_reason],
            )
            last_anchor = anchor
    current.end_ts = last_anchor
    groups.append(current)
    return groups


def group_files_into_sessions(
    files: list[IngestedFile],
    max_span: timedelta = MAX_SESSION_SPAN,
) -> list[SessionGroup]:
    """Group ingested files into print sessions, keyed on the run prefix.

    Returns groups ordered by start time.
    """
    if not files:
        return []

    files = _drop_logless_files(files)
    if not files:
        return []

    buckets: dict[str | None, list[IngestedFile]] = {}
    for file in files:
        buckets.setdefault(run_prefix(_file_name(file)), []).append(file)

    # (run prefix, group) pairs — the prefix is needed for the id, and is not
    # stored on SessionGroup itself.
    pending: list[tuple[str | None, SessionGroup]] = []
    for prefix, bucket in buckets.items():
        if prefix is None:
            # No run identity — fall back to a (non-chaining) temporal split.
            parts = _split_by_span(bucket, PREFIXLESS_MAX_SPAN, "new_gap_exceeded")
        else:
            parts = _split_by_span(bucket, max_span, "run_prefix_reused")
        for part in parts:
            part.reasons.insert(0, "same_run_prefix" if prefix else "no_run_prefix")
            part.confidence = _confidence(part)
            pending.append((prefix, part))

    _LAST = datetime.max.replace(tzinfo=timezone.utc)
    pending.sort(key=lambda item: item[1].start_ts or _LAST)

    # Assign stable ids; disambiguate the rare case of a reused run prefix
    # producing two groups by appending an index.
    seen: dict[str, int] = {}
    groups: list[SessionGroup] = []
    for prefix, group in pending:
        base = _deterministic_group_id(prefix, group.files, group.start_ts)
        n = seen.get(base, 0)
        seen[base] = n + 1
        group.group_id = base if n == 0 else f"{base}_{n}"
        groups.append(group)
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
