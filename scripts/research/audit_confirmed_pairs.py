#!/usr/bin/env python
"""Audit curated log/model pairs without changing the source archive.

Usage::

    python scripts/research/audit_confirmed_pairs.py \
      "/Users/admin/Desktop/Подтверждённые печати" --format json

The JSON is intentionally stable enough to diff between parser/estimator
versions.  It is evidence, not an automatic declaration that a pair is true.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from analytics.prediction.magics_reader import read_plate
from analytics.prediction.timing_evidence import summarize_timing_events
from parsers.base.base import ParserContext
from parsers.formats.event_log import EventLogParser
from parsers.formats.time_log import TimeLogParser

_MODE_RE = re.compile(r"\((steel|aluminum)\s+([0-9]+(?:\.[0-9]+)?)\s*мм", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _date_key(path: Path) -> tuple[datetime, str]:
    match = _DATE_RE.search(path.name)
    if match:
        day, month, year = (int(value) for value in match.groups())
        return datetime(year, month, day), path.name
    return datetime.max, path.name


def _mode(folder_name: str) -> tuple[str | None, float | None]:
    match = _MODE_RE.search(folder_name)
    if not match:
        return None, None
    return match.group(1).lower(), float(match.group(2))


def _geometry(model_dir: Path, last_layer: int | None) -> dict[str, Any]:
    magics_files = sorted(model_dir.glob("*.magics"))
    stls = sorted((*model_dir.glob("*.stl"), *model_dir.glob("*.STL")))
    result: dict[str, Any] = {
        "magics_files": len(magics_files),
        "stl_files": len(stls),
        "magics": [],
    }
    for path in magics_files:
        plate = read_plate(path)
        bounds = [mesh.bounds for mesh in plate.parts]
        z_min = min((float(value[0][2]) for value in bounds), default=None)
        z_max = max((float(value[1][2]) for value in bounds), default=None)
        span = (z_max - z_min) if z_min is not None and z_max is not None else None
        # ``z_max`` alone is not a proven build height.  A raised part can have
        # native supports whose geometry is not decoded by the current reader.
        # Keep both hypotheses visible instead of silently assuming origin Z=0.
        zero_origin_height = max(0.0, z_max) if z_max is not None else None
        result["magics"].append({
            "file": path.name,
            "sha256": _sha256(path),
            "printable_bodies": len(plate.parts),
            "marker_bodies": len(plate.markers),
            "native_support_records": plate.support_entry_count,
            "printable_volume_cm3": sum(abs(float(mesh.volume)) for mesh in plate.parts) / 1000,
            "z_min_mm": z_min,
            "z_max_mm": z_max,
            "body_span_mm": span,
            "zero_origin_height_hypothesis_mm": zero_origin_height,
            "span_layer_thickness_hypothesis_mm": (
                span / last_layer if span is not None and last_layer else None
            ),
            "zero_origin_layer_thickness_hypothesis_mm": (
                zero_origin_height / last_layer
                if zero_origin_height is not None and last_layer else None
            ),
            "warnings": plate.warnings,
        })
    hashes: dict[str, list[str]] = {}
    stl_bounds: list[tuple[float, float]] = []
    for path in stls:
        hashes.setdefault(_sha256(path), []).append(path.name)
        try:
            import trimesh

            mesh = trimesh.load(str(path), file_type="stl", process=False)
            if not mesh.is_empty:
                stl_bounds.append((float(mesh.bounds[0][2]), float(mesh.bounds[1][2])))
        except Exception:
            # The audit remains useful when one ancillary export is malformed;
            # exact estimator validation will reject that file separately.
            continue
    result["duplicate_stl_groups"] = [names for names in hashes.values() if len(names) > 1]
    result["stl_z_min_mm"] = min((row[0] for row in stl_bounds), default=None)
    result["stl_z_max_mm"] = max((row[1] for row in stl_bounds), default=None)
    magics_bounds = [
        (row["z_min_mm"], row["z_max_mm"])
        for row in result["magics"]
        if row["z_min_mm"] is not None and row["z_max_mm"] is not None
    ]
    all_bounds = magics_bounds + stl_bounds
    result["combined_z_min_mm"] = min((row[0] for row in all_bounds), default=None)
    result["combined_z_max_mm"] = max((row[1] for row in all_bounds), default=None)
    result["combined_span_mm"] = (
        result["combined_z_max_mm"] - result["combined_z_min_mm"]
        if result["combined_z_min_mm"] is not None else None
    )
    return result


def _wall_clock(log_dir: Path) -> dict[str, Any]:
    files = sorted(
        (path for path in log_dir.glob("*.log") if "_" not in path.name),
        key=_date_key,
    )
    parser = EventLogParser()
    events = [
        event
        for path in files
        for event in parser.parse(path, ParserContext()).events
        if event.ts is not None
    ]
    print_events = [
        event for event in events
        if event.layer is not None or event.phase == "burn" or "burn" in event.event_type
    ]
    timestamps = [event.ts for event in print_events]
    first = min(timestamps) if timestamps else None
    last = max(timestamps) if timestamps else None
    return {
        "event_logs": [path.name for path in files],
        "first_print_event": first.isoformat() if first else None,
        "last_print_event": last.isoformat() if last else None,
        "print_event_span_hours": (
            (last - first).total_seconds() / 3600 if first and last and last > first else None
        ),
        "pause_markers": sum(event.event_type == "pause" for event in events),
        "resume_markers": sum(event.event_type in {"resume", "restart_attempt"} for event in events),
    }


def audit_pair(folder: Path) -> dict[str, Any]:
    time_logs = sorted((folder / "logs").glob("*_time.log"), key=_date_key)
    parser = TimeLogParser()
    parsed = [parser.parse(path, ParserContext()) for path in time_logs]
    evidence = summarize_timing_events(
        event for result in parsed for event in result.events
    )
    material, declared_thickness = _mode(folder.name)
    timing = evidence.as_dict()
    geometry = _geometry(folder / "model", evidence.last_layer)
    inferred = [
        row["span_layer_thickness_hypothesis_mm"]
        for row in geometry["magics"]
        if row["span_layer_thickness_hypothesis_mm"] is not None
    ]
    thickness_error_pct = None
    if inferred and declared_thickness:
        thickness_error_pct = (inferred[0] - declared_thickness) / declared_thickness * 100
    note_path = folder / "ПРИМЕЧАНИЕ.txt"
    return {
        "pair": folder.name,
        "material": material,
        "declared_layer_thickness_mm": declared_thickness,
        "time_logs": [path.name for path in time_logs],
        "timing": timing,
        "wall_clock": _wall_clock(folder / "logs"),
        "geometry": geometry,
        "thickness_error_pct": thickness_error_pct,
        "note": note_path.read_text(encoding="utf-8").strip() if note_path.exists() else None,
    }


def _markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Пара | Логи | Слои факт | Z деталей, мм | span/слой, мм | Нормальная печать, ч | Повторы, ч | Паузы (диагностика), ч |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        timing = row["timing"]
        hours = timing["hours"]
        magics = row["geometry"]["magics"]
        z_min = magics[0]["z_min_mm"] if magics else None
        z_max = magics[0]["z_max_mm"] if magics else None
        inferred = magics[0]["span_layer_thickness_hypothesis_mm"] if magics else None
        z_range = f"{z_min:.3f}–{z_max:.3f}" if z_min is not None and z_max is not None else "—"
        lines.append(
            "| {pair} | {logs} | {observed}/{last} | {z_range} | {inferred} | {cycle:.3f} | {repeat:.3f} | {pause:.3f} |".format(
                pair=row["pair"].replace("|", "\\|"),
                logs=len(row["time_logs"]),
                observed=timing["observed_layers"],
                last=timing["last_layer"] or "—",
                z_range=z_range,
                inferred=f"{inferred:.5f}" if inferred is not None else "—",
                cycle=hours["nominal_cycle_without_pause_like_residual"],
                repeat=hours["repeat_attempt_overhead_without_pause"],
                pause=hours["pause_like_residual"],
            )
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    rows = [audit_pair(path) for path in sorted(args.root.iterdir()) if path.is_dir()]
    if args.format == "json":
        print(json.dumps({"schema_version": 2, "pairs": rows}, ensure_ascii=False, indent=2))
    else:
        print(_markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
