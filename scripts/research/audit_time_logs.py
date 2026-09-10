"""Read-only, content-deduplicated audit of real time logs.

Run as ``python -m scripts.research.audit_time_logs ROOT [ROOT ...] --output report.json``.
Only the requested JSON report is written; source logs and application DB are untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from analytics.prediction.recoat_calibration import layer_seconds_from_events
from parsers.base.base import ParserContext
from parsers.formats.time_log import TimeLogParser


def audit(roots: list[Path]) -> dict:
    files: dict[str, dict] = {}
    missing = []
    file_count = 0
    for root in roots:
        if not root.is_dir():
            missing.append(str(root))
            continue
        for path in sorted(root.rglob("*_time.log")):
            file_count += 1
            with path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if checksum in files:
                files[checksum]["copies"].append(str(path))
                continue
            parsed = TimeLogParser().parse(path, ParserContext())
            # Independent count of raw summary markers catches silent row loss.
            with path.open("rb") as stream:
                raw_summaries = sum(b"OLD_STATS:" in line for line in stream)
            files[checksum] = {
                "sha256": checksum,
                "copies": [str(path)],
                "raw_summary_lines": raw_summaries,
                "unparsed_summary_lines": raw_summaries - parsed.metadata["summary_count"],
                "metadata": parsed.metadata,
                "quality": list(parsed.data_quality),
                "diagnostics": [item.model_dump(mode="json") for item in parsed.diagnostics],
                "calibration_layer_count": len(layer_seconds_from_events(parsed.events)),
            }
    totals = Counter()
    for entry in files.values():
        for key in (
            "summary_count", "detailed_count", "paired_layers", "unpaired_summaries",
            "unpaired_details", "equivalent_layer_duplicates", "conflicting_layer_duplicates",
            "impossible_cycle_count",
        ):
            totals[key] += entry["metadata"][key]
        totals["duration_mismatches"] += sum(entry["metadata"]["duration_mismatch_counts"].values())
        totals["unparsed_summary_lines"] += entry["unparsed_summary_lines"]
        totals["calibration_layers"] += entry["calibration_layer_count"]
        totals["files_without_summaries"] += entry["metadata"]["summary_count"] == 0
    return {
        "parser_version": TimeLogParser.version,
        "scope": "Individual unique files; counts are not confirmed unique print sessions.",
        "files_found": file_count,
        "unique_files": len(files),
        "missing_roots": missing,
        "totals": dict(totals),
        "files": list(files.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.roots)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
