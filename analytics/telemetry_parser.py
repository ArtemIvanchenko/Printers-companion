"""Full-resolution sensors.log parser using Polars + SciPy.

Reads the complete pipe-delimited sensors log (typically 330k–400k rows)
and computes comprehensive per-signal statistics.  Called once at import time;
results are stored in session.context so the raw file is never needed again.

Dependencies already in pyproject.toml: polars>=0.20, scipy>=1.13.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from analytics.robust_stats import theil_sen_slope

logger = logging.getLogger(__name__)

# No sensor on this machine can report a magnitude anywhere near this. The
# largest physically meaningful quantity it logs is the Z position in microns
# (390 mm travel = 3.9e5), so 1e7 leaves a 25x margin over anything real while
# still catching the firmware's uninitialised-memory writes, which land at
# 1e9 and above (observed: 1.87e9, 2.58e18, 9.5e26, 4.6e28). Applied
# unconditionally — unlike the profile ranges below, it needs no per-signal
# knowledge and so cannot be wrong about a signal the profile has mis-guessed.
_ABSURD_MAGNITUDE = 1e7

# A profile range rejecting more than this fraction of a signal is describing a
# different machine (or different units) than the one that wrote the log.
_MAX_PROFILE_REJECT_FRACTION = 0.20

# ── Signal → semantic group mapping ─────────────────────────────────────────

_GROUP: dict[str, str] = {
    # Oxygen
    "SO1": "oxygen",      "SO2": "oxygen",
    # Temperatures
    "ST3": "temperature", "ST4": "temperature", "ST5": "temperature",
    "ST2": "temperature",                         # recuperator zone
    # Pressure — confirmed in sensors.log header
    "SP1": "pressure",    "SP2": "pressure",      "SP3": "pressure",
    "SP4": "pressure",    "SP5": "pressure",      "SP8": "pressure",
    "SP14": "pressure",   "SP15": "pressure",     "SP16": "pressure",
    # Pressure — SCADA visible but may appear in other sessions
    "SP9": "pressure",    "SP11": "pressure",     "SP12": "pressure",
    # Flow
    "SF1": "flow",
    # Gas flow temperature / humidity (two column-name variants in use)
    "ST1 (flow H)": "humidity",  "Flow H": "humidity",
    "ST1 (flow T)": "temp_gas",  "Flow T": "temp_gas",
    # Mechanics / counters
    "LIR": "layer_counter",
    # Binary / other signals seen on SCADA
    "BI1": "binary",
}

# Columns that are always numeric; "Time" stays as string.
_NUMERIC_COLS = set(_GROUP.keys()) | {"Raquel", "Bunker", "Filled B"}


def parse_sensors_log(path: Path) -> dict[str, np.ndarray]:
    """Stream the sensors.log line-by-line, accumulating only analytics signals.

    Uses ``array.array('f')`` (Float32) for minimal peak RAM:
    ≈ 4 bytes/value × 334k rows × 9 signals ≈ 12 MB.
    Returns a dict {signal_name: np.ndarray(float64)} ready for stats.
    """
    import array as _array

    buffers: dict[str, _array.array] = {}
    col_idx: dict[str, int] = {}

    with path.open(encoding="utf-8", errors="replace", buffering=1 << 20) as fh:
        # Header line: map stripped column names to their pipe-column index.
        header_line = fh.readline()
        parts = [p.strip() for p in header_line.split("|")]
        for i, name in enumerate(parts):
            if name in _GROUP:
                col_idx[name] = i
                buffers[name] = _array.array("f")

        if not col_idx:
            return {}

        # Data lines: parse relevant columns only.
        for line in fh:
            cells = line.split("|")
            for sig, idx in col_idx.items():
                if idx >= len(cells):
                    continue
                raw = cells[idx].strip().replace(",", ".")
                try:
                    buffers[sig].append(float(raw))
                except (ValueError, OverflowError):
                    pass  # null / header repeat / garbage line

    return {sig: np.array(buf, dtype=np.float64)
            for sig, buf in buffers.items() if buf}


def load_aligned_signals(path: Path, columns: list[str]) -> dict[str, np.ndarray]:
    """Load Time + the requested signal columns, keeping only rows where ALL
    requested columns parse cleanly.

    ``parse_sensors_log`` accumulates each signal independently, so a garbage
    cell in one column doesn't drop that row for the others — arrays can end
    up different lengths and no longer line up row-for-row. Multi-signal
    analysis (windowed features, joint anomaly detection) needs them aligned,
    hence this stricter loader.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        header = [p.strip() for p in fh.readline().split("|")]
        idx = {name: i for i, name in enumerate(header)}
        wanted = ["Time", *columns]
        missing = [c for c in wanted if c not in idx]
        if missing:
            raise ValueError(f"Columns not found in {path.name}: {missing}")

        out: dict[str, list] = {c: [] for c in wanted}
        max_idx = max(idx[c] for c in wanted)
        for raw_line in fh:
            cells = raw_line.split("|")
            if len(cells) <= max_idx:
                continue
            row: dict[str, float | str] = {}
            ok = True
            for c in wanted:
                cell = cells[idx[c]].strip()
                if c == "Time":
                    row[c] = cell
                    continue
                try:
                    row[c] = float(cell.replace(",", "."))
                except ValueError:
                    ok = False
                    break
            if ok:
                for c in wanted:
                    out[c].append(row[c])

    return {
        c: (np.array(v) if c == "Time" else np.array(v, dtype=np.float64))
        for c, v in out.items()
    }


def downsample_full_series(
    path: Path,
    columns: list[str],
    time_column: str = "Time",
    max_points: int = 150,
    start_clock_seconds: float | None = None,
    end_clock_seconds: float | None = None,
    valid_ranges: dict[str, dict[str, float]] | None = None,
) -> dict[str, list]:
    """Return up to ``max_points`` rows spaced evenly across the ENTIRE file.

    The chart series must span the whole print, not just the first N rows that
    the bounded table sample (``parse_table_stream``) keeps — otherwise an
    82-hour print only shows its first ~80 minutes. Two-pass over the file:
    pass 1 counts data rows, pass 2 grabs only the evenly-spaced indices.

    Returns ``{time_column: [str|None], col: [float|None], ...}`` for the columns
    actually present in the header; ``{}`` if the file/columns can't be read.
    """
    from parsers.common.encoding import estimate_encoding

    def _clock_seconds(raw: str) -> float | None:
        try:
            parts = raw.strip().split(":")
            if len(parts) != 3:
                return None
            hour, minute, second = (float(part.replace(",", ".")) for part in parts)
            return hour * 3600 + minute * 60 + second
        except (TypeError, ValueError):
            return None

    def _in_window(line: str, time_idx: int | None) -> bool:
        if start_clock_seconds is None and end_clock_seconds is None:
            return True
        if time_idx is None:
            return False
        cells = line.split("|")
        if time_idx >= len(cells):
            return False
        seconds = _clock_seconds(cells[time_idx])
        if seconds is None:
            return False
        return (
            (start_clock_seconds is None or seconds >= start_clock_seconds)
            and (end_clock_seconds is None or seconds <= end_clock_seconds)
        )

    try:
        enc = estimate_encoding(path)
        with path.open(encoding=enc, errors="replace", buffering=1 << 20) as fh:
            header = fh.readline()
            names = [p.strip() for p in header.split("|")]
            index_of = {name: i for i, name in enumerate(names)}
            wanted = {c: index_of[c] for c in [*columns, time_column] if c in index_of}
            if not wanted:
                return {}
            time_idx = index_of.get(time_column)
            total = sum(1 for line in fh if _in_window(line, time_idx))
        if total <= 0:
            return {}

        n = min(max_points, total)
        if n <= 1:
            picks = {0}
        else:
            picks = {round(i * (total - 1) / (n - 1)) for i in range(n)}

        out: dict[str, list] = {c: [] for c in wanted}
        with path.open(encoding=enc, errors="replace", buffering=1 << 20) as fh:
            fh.readline()  # skip header
            eligible_i = -1
            for line in fh:
                if not _in_window(line, time_idx):
                    continue
                eligible_i += 1
                if eligible_i not in picks:
                    continue
                cells = line.split("|")
                for col, ci in wanted.items():
                    raw = cells[ci].strip().replace(",", ".") if ci < len(cells) else ""
                    if col == time_column:
                        out[col].append(raw or None)
                    else:
                        try:
                            val = float(raw)
                            out[col].append(
                                val if np.isfinite(val) and abs(val) <= _ABSURD_MAGNITUDE else None
                            )
                        except (ValueError, OverflowError):
                            out[col].append(None)
        if valid_ranges is None:
            from analytics.thresholds import load_valid_ranges

            valid_ranges = load_valid_ranges()
        for col, values in out.items():
            if col == time_column:
                continue
            rng = valid_ranges.get(col) or {}
            finite = [value for value in values if isinstance(value, (int, float))]
            if not finite or not rng:
                continue
            rejected = [
                value for value in finite
                if ((rng.get("min_val") is not None and value < rng["min_val"])
                    or (rng.get("max_val") is not None and value > rng["max_val"]))
            ]
            if len(rejected) / len(finite) <= _MAX_PROFILE_REJECT_FRACTION:
                out[col] = [
                    None if isinstance(value, (int, float)) and (
                        (rng.get("min_val") is not None and value < rng["min_val"])
                        or (rng.get("max_val") is not None and value > rng["max_val"])
                    ) else value
                    for value in values
                ]
        return out
    except Exception as exc:
        logger.warning("downsample_full_series failed for %s: %s", path, exc)
        return {}


def compute_full_signal_stats(
    path: Path,
    alarm_thresholds: dict[str, dict[str, float]] | None = None,
    valid_ranges: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute per-signal statistics from the complete sensors.log.

    Args:
        path: absolute path to the *_sensors.log file.
        alarm_thresholds: optional dict {signal: {"alarm_high": float,
                          "alarm_low": float}} from signals.yaml.
        valid_ranges: optional dict {signal: {"min_val": float,
                      "max_val": float}} from signals.yaml — the physically
                      possible range. Defaults to the profile's; pass ``{}``
                      to disable range filtering entirely.

    Returns:
        {signal: {mean, std, min, max, p05, p95, p99, n,
                  alarm_count, out_of_range, trend_slope, group}}

    ``trend_slope`` is the Theil-Sen slope in *signal units per row*
    (≈ per second for 1-Hz logs), positive = rising over the session.
    """
    try:
        arrays = parse_sensors_log(path)
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", path, exc)
        return {}

    thresholds = alarm_thresholds or {}
    if valid_ranges is None:
        from analytics.thresholds import load_valid_ranges
        valid_ranges = load_valid_ranges()
    result: dict[str, dict[str, Any]] = {}

    for col, vals in arrays.items():
        # Drop non-finite samples: a sensor disconnect can write "nan"/"inf"
        # cells, which float() accepts silently — a single one would poison
        # mean/std/quantile (NaN propagates) for the entire signal.
        vals = vals[np.isfinite(vals)]

        # Same reasoning, one step further: the printer also writes *finite*
        # impossible values (a Flow H of -2.58e18 %, a Z position of 1.87e9 µm
        # on a 390 mm axis), which no nan/inf guard catches. 33 such rows out
        # of 81 377 dragged this shop's real Flow H mean to 1.9e23. They are
        # excluded from the statistics but counted, so a failing sensor stays
        # visible instead of silently vanishing.
        keep = np.abs(vals) <= _ABSURD_MAGNITUDE

        # The profile's own min_val/max_val are applied on top — but only when
        # they agree with reality. Several are guesses (LIR and SF1 carry
        # confidence 0.5 / active_status "candidate"), and two of them are
        # simply wrong for this machine: LIR reads negative throughout while the
        # profile says 0..390000, and SF1 reads ~986 against a stated 0..30.
        # Trusting them blindly would discard 99.8% and 54% of real samples.
        # A range that rejects most of the signal is a bad range, not a bad
        # sensor, so it is ignored (and reported) rather than obeyed.
        rng = valid_ranges.get(col) or {}
        if rng:
            in_profile = np.ones(len(vals), dtype=bool)
            if (lo := rng.get("min_val")) is not None:
                in_profile &= vals >= lo
            if (hi := rng.get("max_val")) is not None:
                in_profile &= vals <= hi
            rejected = 1.0 - (in_profile.sum() / len(vals)) if len(vals) else 0.0
            if rejected <= _MAX_PROFILE_REJECT_FRACTION:
                keep &= in_profile
            else:
                logger.warning(
                    "%s: profile range %s rejects %.1f%% of samples — treating the "
                    "range as wrong for this machine, not the data",
                    col, rng, rejected * 100,
                )

        out_of_range = int((~keep).sum())
        if out_of_range:
            vals = vals[keep]

        n = len(vals)
        if n < 10:
            continue

        # Robust linear trend within the session. The shared helper sub-samples
        # large arrays before the O(n²) regression (334k rows would take hours).
        slope_val = theil_sen_slope(vals)

        # Alarm count (above alarm_high OR below alarm_low).
        thr = thresholds.get(col, {})
        alarm_count = 0
        if (ah := thr.get("alarm_high")) is not None:
            alarm_count += int(np.sum(vals > ah))
        if (al := thr.get("alarm_low")) is not None:
            alarm_count += int(np.sum(vals < al))

        result[col] = {
            "mean":        round(float(vals.mean()), 6),
            "std":         round(float(vals.std()),  6),
            "min":         round(float(vals.min()),  6),
            "max":         round(float(vals.max()),  6),
            "p05":         round(float(np.quantile(vals, 0.05)), 6),
            "p95":         round(float(np.quantile(vals, 0.95)), 6),
            "p99":         round(float(np.quantile(vals, 0.99)), 6),
            "n":           n,
            "alarm_count": alarm_count,
            "out_of_range": out_of_range,
            "trend_slope": round(slope_val, 8),
            "group":       _GROUP[col],
        }

    return result


def sessions_to_polars(sessions: list[dict[str, Any]]) -> pl.DataFrame:
    """Convert a list of session dicts (with signal_stats) to a wide Polars DataFrame.

    One row per session, columns: session_id, start_ts, then for each signal
    the stats suffixed: SO1_mean, SO1_std, SO1_p95, SO1_alarm_count, etc.

    Used by the cross-session analysis engine.
    """
    rows: list[dict[str, Any]] = []
    for s in sessions:
        row: dict[str, Any] = {
            "session_id": s.get("session_id", ""),
            "start_ts":   s.get("start_ts") or "",
        }
        for sig, stats in (s.get("signal_stats") or {}).items():
            for metric in ("mean", "std", "p95", "p99", "alarm_count", "trend_slope"):
                row[f"{sig}__{metric}"] = stats.get(metric)
        rows.append(row)

    if not rows:
        return pl.DataFrame()

    return pl.DataFrame(rows).sort("start_ts")
