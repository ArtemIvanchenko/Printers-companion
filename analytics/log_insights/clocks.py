"""Machine-local clock alignment, explicit pauses and conservative burn windows."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from analytics.prediction.timing_validation import calibration_timing_payloads


def field(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def seconds(value, clock_timezone="Europe/Moscow"):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(clock_timezone))
    return value.timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def pause_intervals(events, clock_timezone="Europe/Moscow"):
    points = sorted({
        (ts, field(e, "event_type")) for e in events
        if field(e, "event_type") in {"pause", "resume", "restart_attempt", "finish"}
        and (ts := seconds(field(e, "ts"), clock_timezone)) is not None
    })
    result, started = [], None
    for ts, kind in points:
        if kind == "pause" and started is None:
            started = ts
        elif kind in {"resume", "restart_attempt", "finish"} and started is not None:
            if ts > started:
                result.append({"start": started, "end": ts, "resumed": kind != "finish"})
            started = None
    if started is not None:
        result.append({"start": started, "end": None, "resumed": False})
    return result


def burn_windows(events, pauses=(), clock_timezone="Europe/Moscow"):
    """Use timestamped starts + measured durations; never infer by print progress.

    Generic main-log burn markers are approximate anchors. Multiple different
    anchors for a physical layer are ambiguous and excluded rather than joined.
    """
    timings = calibration_timing_payloads(events)
    anchors = {}
    for e in events:
        if field(e, "event_type") not in {"burn_start", "burn_event"}:
            continue
        payload = field(e, "payload", {}) or {}
        layer = field(e, "layer") or payload.get("layer")
        ts = seconds(field(e, "ts"), clock_timezone)
        if layer in timings and ts is not None:
            anchors.setdefault(layer, {})[ts] = field(e, "event_type")
    windows = []
    for layer, points in anchors.items():
        if len(points) != 1:
            continue
        start, kind = next(iter(points.items()))
        burn = timings[layer].get("burn_ms")
        if burn is None or burn <= 0:
            continue
        end = start + burn / 1000
        # A pause inside a phase means the duration cannot locate its two
        # active portions unambiguously. Do not fabricate either portion.
        if any(p["start"] < end and (p["end"] is None or p["end"] > start) for p in pauses):
            continue
        windows.append({"layer": layer, "start": start, "end": end,
                        "precision": "logged_start" if kind == "burn_start" else "approximate_burn_marker"})
    windows.sort(key=lambda row: row["start"])
    bad = set()
    for left, right in zip(windows, windows[1:]):
        if left["end"] > right["start"]:
            bad.update((left["layer"], right["layer"]))
    return [row for row in windows if row["layer"] not in bad]
