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

One print can nonetheless span several prefixes: stopping and restarting it
opens a new log named by the restart date while the layer counter carries on.
Those runs are rejoined afterwards on layer continuity — see
``_merge_resumed_runs``.
"""
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

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

# A run that begins at a layer this low is starting a print, not resuming one.
# Real logs open at layer 1 or 2 depending on firmware version.
_FIRST_LAYER_OF_A_PRINT = 2

# A logger restart can lose the timing row for the layer that was in flight.
# We only bridge one such missing layer when the independently recorded table
# position proves that the physical Z sequence continued at the same step.
_MAX_MISSING_BOUNDARY_LAYERS = 1


@dataclass(frozen=True)
class _LayerBoundaryEvidence:
    first_layer: int
    last_layer: int
    first_position: float | None
    last_position: float | None
    position_step_per_layer: float | None


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


def _layer_boundary_evidence(files: list[IngestedFile]) -> _LayerBoundaryEvidence | None:
    """Layer range plus independent physical-position evidence for a run.

    ``*_time.log`` is the preferred timing source, but real M350 data can lose
    its final row during a restart even though the main event log records the
    burn and table position.  Session identity therefore uses both families;
    calibration still uses only the detailed timing rows.
    """
    layers: list[int] = []
    positions: dict[int, float] = {}
    for file in files:
        if not file.parse_result:
            continue
        for event in file.parse_result.events:
            event_type = getattr(event, "event_type", None)
            payload = getattr(event, "payload", None) or {}
            if event_type == "layer_timing_summary":
                layer = payload.get("layer", getattr(event, "layer", None))
            elif event_type == "burn_event":
                layer = getattr(event, "layer", None)
            else:
                continue
            if isinstance(layer, int):
                layers.append(layer)
                position = payload.get("table_position")
                if isinstance(position, (int, float)):
                    positions[layer] = float(position)
    if not layers:
        return None

    first_layer, last_layer = min(layers), max(layers)
    ordered_positions = sorted(positions.items())
    per_layer_steps = [
        (position_b - position_a) / (layer_b - layer_a)
        for (layer_a, position_a), (layer_b, position_b)
        in zip(ordered_positions, ordered_positions[1:])
        if 0 < layer_b - layer_a <= 3
    ]
    return _LayerBoundaryEvidence(
        first_layer=first_layer,
        last_layer=last_layer,
        first_position=positions.get(first_layer),
        last_position=positions.get(last_layer),
        position_step_per_layer=median(per_layer_steps) if per_layer_steps else None,
    )


def _is_physical_boundary_continuation(
    previous: _LayerBoundaryEvidence,
    following: _LayerBoundaryEvidence,
) -> bool:
    """True when a one-row logger hole still follows the same physical Z path."""
    layer_delta = following.first_layer - previous.last_layer
    # Firmware commonly copies the boundary layer into both files (delta 0),
    # or opens the next file at the following layer (delta 1).
    if layer_delta in (0, 1):
        return True
    if layer_delta < 0:
        return False
    missing_layers = layer_delta - 1
    if missing_layers > _MAX_MISSING_BOUNDARY_LAYERS:
        return False

    if previous.last_position is None or following.first_position is None:
        return False
    steps = [
        step for step in (
            previous.position_step_per_layer,
            following.position_step_per_layer,
        )
        if step is not None and abs(step) > 1e-9
    ]
    if not steps:
        return False
    if len(steps) == 2 and steps[0] * steps[1] <= 0:
        return False

    expected_step = median(steps)
    observed_delta = following.first_position - previous.last_position
    expected_delta = expected_step * layer_delta
    tolerance = max(5.0, abs(expected_delta) * 0.10)
    return abs(observed_delta - expected_delta) <= tolerance


def _merge_resumed_runs(
    pending: list[tuple[str | None, SessionGroup]], max_span: timedelta,
) -> list[tuple[str | None, SessionGroup]]:
    """Join runs that resume an interrupted print into the session that started it.

    Stopping and restarting a print makes the printer open a fresh log named by
    the restart date, while the layer counter carries on from where it stopped.
    Those files are two runs of ONE print. Left apart, its duration, layer count
    and per-layer timings are each split across two sessions, and a calibration
    linked to either learns from a fraction of the layers — on 27.05 that meant
    383 layers of a 949-layer print.

    A run resumes the previous one when it does not start at the beginning and
    picks up at the previous run's last layer (the same layer is usually
    reported twice, once by each side, so ``last`` and ``last + 1`` both count).
    One missing boundary layer is also accepted, but only when main-log table
    positions independently prove the expected physical Z step.
    Confirmed on every real log: 27.05 ended at 384 and 28.05 opened at 384;
    23.03 ended at 6843 and 27.03 opened at 6843; 08.06 ended at 1133 and 09.06
    opened at 1134. Reprints of the same plate open at layer 2 and so stay
    separate — 29.05 reran the 27.05 plate to the same final layer 950 and must
    not be absorbed into it.

    The date gap is deliberately not part of the rule: it was one day for
    27→28.05 but four for 23→27.03. ``max_span`` is only an outer bound.

    Chaining is safe here, unlike the time-gap clustering this module warns
    about: layer numbers must line up, with the sole position-proven one-layer
    exception above. Thus A→B→C means one print resumed twice, not two prints
    that merely happened to fall near each other.
    """
    merged: list[tuple[str | None, SessionGroup]] = []
    boundaries: list[_LayerBoundaryEvidence | None] = []
    for prefix, group in pending:
        boundary = _layer_boundary_evidence(group.files)
        previous = boundaries[-1] if boundaries else None
        if (
            boundary is not None
            and previous is not None
            and boundary.first_layer > _FIRST_LAYER_OF_A_PRINT
            and _is_physical_boundary_continuation(previous, boundary)
            and group.start_ts is not None
            and merged[-1][1].start_ts is not None
            and group.start_ts - merged[-1][1].start_ts <= max_span
        ):
            head = merged[-1][1]
            head.files.extend(group.files)
            head.end_ts = max(filter(None, (head.end_ts, group.end_ts)), default=head.end_ts)
            head.reasons.append("resumed_run")
            if boundary.first_layer == previous.last_layer + 2:
                head.reasons.append("resumed_run_position_continuity")
            head.confidence = _confidence(head)
            # The print now reaches this run's last layer.
            boundaries[-1] = _LayerBoundaryEvidence(
                first_layer=previous.first_layer,
                last_layer=boundary.last_layer,
                first_position=previous.first_position,
                last_position=boundary.last_position,
                position_step_per_layer=(
                    median([
                        step for step in (
                            previous.position_step_per_layer,
                            boundary.position_step_per_layer,
                        )
                        if step is not None
                    ])
                    if any(step is not None for step in (
                        previous.position_step_per_layer,
                        boundary.position_step_per_layer,
                    )) else None
                ),
            )
            continue
        merged.append((prefix, group))
        boundaries.append(boundary)
    return merged


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
    # Runs are joined after the split, so a resumed print ends up in the session
    # that started it — and keeps that session's id, since the id is keyed on the
    # first run's prefix.
    pending = _merge_resumed_runs(pending, max_span)

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
