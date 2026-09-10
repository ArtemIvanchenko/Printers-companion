"""Replay a daily main/time/sensor set without modifying the application DB."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from analytics.log_insights.pipeline import build_log_insights
from parsers.base.base import ParserContext
from parsers.formats.event_log import EventLogParser
from parsers.formats.time_log import TimeLogParser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("date", help="DD.MM.YYYY")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    files, events = [], []
    for suffix, family, reader in ((".log", "main_event_log", EventLogParser()),
                                    ("_time.log", "time_log", TimeLogParser()),
                                    ("_sensors.log", "sensors_log", None)):
        path = args.folder / (args.date + suffix)
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        result = reader.parse(path, ParserContext(source_file_id=checksum)) if reader else None
        files.append(SimpleNamespace(path=str(path), relative_path=path.name, checksum=checksum,
                                     classification=SimpleNamespace(family=family), parse_result=result))
        if result:
            events.extend(result.events)
    report = build_log_insights(files, events)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(files), "events": len(events),
                      "sample_count": report["environment"]["sample_count"],
                      "burn_windows": report["environment"]["burn_window_count"],
                      "metrics": len(report["environment"]["metrics"]),
                      "affected_layers": report["environment"]["affected_layer_count"],
                      "resumes": report["recovery"]["count"],
                      "attempts": report["time_accounting"]["sample_count"]}))


if __name__ == "__main__":
    main()
