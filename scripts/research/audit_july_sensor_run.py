"""Sensor-only evidence for the 17–21 July run. No inferred burn intervals."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from analytics.log_insights.clocks import iso
from analytics.log_insights.environment import sensor_samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wanted = ["SO1", "SO2", "ST3", "ST4", "ST5", "Flow T", "Flow H", "SP4", "SF1", "LIR"]
    report = {"scope": "full_sensor_recording_not_burn_only", "files": [],
              "limitation_ru": "Основной журнал и time.log недоступны: фазы прожига, точные слои и паузы не восстанавливаются из одной телеметрии."}
    for date in ("17.07.2026", "18.07.2026"):
        path = args.folder / f"{date}_sensors.log"
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        source = SimpleNamespace(path=str(path), checksum=checksum, classification=SimpleNamespace(family="sensors_log"))
        diagnostics = Counter()
        arrays = {key: [] for key in wanted}
        first_valid = {}
        timestamps = []
        for ts, values in sensor_samples([source], {key: {"high": 0} for key in wanted}, diagnostics):
            timestamps.append(ts)
            for key in wanted:
                val = values.get(key)
                arrays[key].append(np.nan if val is None else val)
                if val is not None:
                    first_valid.setdefault(key, {"timestamp": iso(ts), "value": val})
        times = np.array(timestamps)
        deltas = np.diff(times)
        positive = (deltas > 0) & (deltas <= 5)
        stats = {}
        for key, vals in arrays.items():
            values = np.array(vals)
            valid = values[np.isfinite(values)]
            if not len(valid):
                continue
            stats[key] = {"valid_rows": len(valid), "invalid_rows": int(np.sum(~np.isfinite(values))),
                          "min": float(np.min(valid)), "median": float(np.median(valid)),
                          "p05": float(np.quantile(valid, 0.05)), "p95": float(np.quantile(valid, 0.95)),
                          "max": float(np.max(valid)), "last": float(valid[-1]), "first_valid": first_valid[key]}
            if key in {"SO1", "SO2"}:
                stable = values <= 0.1
                indices = np.flatnonzero(stable)
                stats[key]["first_at_or_below_0_1"] = iso(times[indices[0]]) if len(indices) else None
                stats[key]["observed_seconds_at_or_below_0_1"] = float(np.sum(deltas[positive & stable[:-1] & stable[1:]]))
                stats[key]["descriptive_threshold_note"] = "0.1 is a descriptive oxygen level, not a validated process limit."
        lir = np.array(arrays["LIR"])
        motion = np.diff(lir)
        steps = Counter(float(v) for v in motion if np.isfinite(v) and v != 0)
        report["files"].append({"path": str(path), "sha256": checksum, "rows": len(times),
                                "start_utc": iso(times[0]), "end_utc": iso(times[-1]),
                                "elapsed_hours": float((times[-1]-times[0])/3600),
                                "gaps_over_5s": int(np.sum(deltas > 5)), "max_gap_seconds": float(np.max(deltas)),
                                "timestamp_diagnostics": dict(diagnostics), "signals": stats,
                                "most_common_nonzero_lir_steps": steps.most_common(10)})
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
