"""Read-only audit of stored predictions and normalized timing evidence.

No geometry calculation, imports, calibration publication or card writes.
The NAS transaction closes before local arithmetic/report generation.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path

from sqlalchemy import select, text

from analytics.prediction.accuracy import _has_full_layer_coverage, _positive_number
from analytics.prediction.input_quality import (
    INPUT_REASON_RU, calibration_input_exclusion, geometry_input_issues,
)
from analytics.prediction.timing_validation import calibration_timing_payloads
from core.config.settings import get_settings
from core.utils.files import sha256_file
from core.versioning.provenance import build_provenance
from domain.models.events import LayerSnapshot
from domain.models.prints import PrintRecord
from storage.db.session import session_scope


def load_inputs() -> tuple[list[dict], dict[str, list[dict]]]:
    with session_scope() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        records = [{
            "record_id": r.record_id, "name": r.name, "session_id": r.session_id,
            "revision": r.revision, "material": r.material,
            "layer_thickness_mm": r.layer_thickness_mm, "hatch_distance_mm": r.hatch_distance_mm,
            "metadata": deepcopy(r.metadata_json or {}),
        } for r in db.scalars(select(PrintRecord).where(PrintRecord.session_id.is_not(None)))]
        sessions = {r["session_id"] for r in records}
        events = defaultdict(list)
        for row in db.scalars(select(LayerSnapshot).where(LayerSnapshot.session_id.in_(sessions))):
            events[row.session_id].append({
                "event_type": "layer_timing_summary",
                "payload": {**deepcopy(row.features or {}), "layer": row.layer},
            })
        return records, dict(events)


def audit(records: list[dict], events: dict[str, list[dict]]) -> dict:
    rows = []
    for record in records:
        metadata = record["metadata"]
        snapshot = metadata.get("prediction") or {}
        timings = calibration_timing_payloads(events.get(record["session_id"], []))
        complete = {layer: row for layer, row in timings.items()
                    if all(_positive_number(row.get(k)) is not None
                           for k in ("burn_ms", "pour_ms", "make_layer_ms"))}
        # This subtotal is measured, not a fitted normal full-cycle reference.
        burn = sum(row["burn_ms"] for row in complete.values()) / 3_600_000
        pour = sum(row["pour_ms"] for row in complete.values()) / 3_600_000
        make = sum(row["make_layer_ms"] for row in complete.values()) / 3_600_000
        coverage = _has_full_layer_coverage(complete, snapshot.get("layer_count")) if snapshot else False
        predicted = _positive_number(snapshot.get("print_hours"))
        observed = burn + pour if complete else None
        exclusion = calibration_input_exclusion(
            metadata, snapshot, record["revision"], session_id=record["session_id"],
        ) if snapshot else None
        rows.append({
            **{k: v for k, v in record.items() if k != "metadata"},
            "source_catalog_key": metadata.get("desktop_catalog_key"),
            "has_active_prediction": bool(snapshot),
            "has_archived_prediction": bool(metadata.get("desktop_previous_prediction")),
            "stored_prediction": {key: snapshot.get(key) for key in (
                "estimated_at", "analysis_version", "input_revision", "layer_count",
                "hatch_distance_mm", "layer_thickness_mm", "n_parts", "n_supports",
                "build_origin_z_mm", "build_origin_source", "estimate_quality",
                "print_hours", "machine_cycle_hours", "scan_source",
            )} if snapshot else None,
            "geometry_input_issues": geometry_input_issues(metadata, snapshot) if snapshot else [],
            "input_exclusion": exclusion,
            "input_exclusion_ru": INPUT_REASON_RU.get(exclusion),
            "manual_exclusions": metadata.get("calibration_exclusions", []),
            "measured_layer_count": len(complete),
            "first_measured_layer": min(complete) if complete else None,
            "last_measured_layer": max(complete) if complete else None,
            "covers_prediction": coverage,
            "measured_scan_hours": burn if complete else None,
            "measured_recoat_hours": pour if complete else None,
            "measured_cycle_hours_including_residuals": make if complete else None,
            "scan_recoat_error_pct": (100 * (predicted / observed - 1)
                                      if coverage and predicted and observed else None),
        })
    return {
        "scope": "research_read_only_snapshot_audit",
        "records": sorted(rows, key=lambda r: r["session_id"]),
        "limitations_ru": [
            "Измерения взяты из сохранённых послойных данных, исходные логи здесь не перепарсены.",
            "Полный записанный цикл может содержать остановки; это не нормальное время для прогноза.",
            "Процент ошибки сравнивает только прожиг и нанесение порошка при достаточном покрытии.",
            "Значения параметров карточки не подтверждают их соответствие настройкам слайсера.",
            "Ни карточки, ни калибровочные коэффициенты не изменены.",
        ],
        "provenance": build_provenance(
            "prediction_input_audit", inputs={"records": records, "events": events},
            config={"comparison_basis": "scan_plus_recoat", "writes_database": False},
            generated_by=get_settings().compute_node_id,
        ),
    }


def audit_raw_timing_storage(records: list[dict], stored_events: dict[str, list[dict]], plan_path: Path) -> dict:
    """Reparse only manifest-bound timing files, after the NAS read is closed."""
    from analytics.log_insights.timing import time_accounting
    from analytics.prediction.timing_evidence import summarize_timing_events
    from analytics.prediction.timing_validation import timing_components_ms
    from parsers.base.base import ParserContext
    from parsers.formats.time_log import TimeLogParser

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    by_record = {record["record_id"]: record for record in records}
    results, all_sources = [], []
    parser = TimeLogParser()
    for batch in plan["batches"]:
        record = by_record.get(batch.get("record_id"))
        if record is None:
            continue
        root = Path(batch["source_path"]).resolve()
        events, sources = [], []
        for item in batch["files"]:
            name = item["name"]
            if not name.casefold().endswith("_time.log"):
                continue
            path = (root / name).resolve()
            if not path.is_relative_to(root) or sha256_file(path) != item["sha256"]:
                raise ValueError(f"Timing source is outside its batch or differs from its manifest: {name}")
            parsed = parser.parse(path, ParserContext())
            if sha256_file(path) != item["sha256"]:
                raise ValueError(f"Timing source changed while being parsed: {name}")
            events.extend(parsed.events)
            sources.append({"name": name, "sha256": item["sha256"], "parser_version": parsed.parser_version})
        all_sources.extend(sources)
        selected = calibration_timing_payloads(events)
        components = timing_components_ms(events)
        expected = {}
        for layer, (burn, pour) in components.items():
            make = selected[layer].get("make_layer_ms")
            expected[layer] = (burn, pour, make if make is not None and make >= burn + pour else None)
        stored = calibration_timing_payloads(stored_events.get(record["session_id"], []))
        missing = sorted(expected.keys() - stored.keys())
        extra = sorted(stored.keys() - expected.keys())
        different = sorted(layer for layer in expected.keys() & stored.keys()
                           if expected[layer] != tuple(stored[layer].get(key) for key in
                                                       ("burn_ms", "pour_ms", "make_layer_ms")))
        evidence = summarize_timing_events(events)
        results.append({
            "record_id": record["record_id"], "session_id": record["session_id"],
            "catalog_key": batch["key"], "sources": sources,
            "raw_observed_layers": evidence.observed_layers, "raw_invalid_rows": evidence.invalid_rows,
            "ambiguous_layers": sorted(evidence.ambiguous_layers),
            "phase_filtered_layers": sorted(selected.keys() - components.keys()),
            "expected_stored_layers": len(expected), "stored_layers": len(stored),
            "missing_count": len(missing), "extra_count": len(extra), "different_count": len(different),
            "missing_layers": missing[:100], "extra_layers": extra[:100], "different_layers": different[:100],
            "matches_storage_policy": not (missing or extra or different),
            "observed_timing_hours": evidence.component_hours(),
            "raw_time_accounting": time_accounting(events),
        })
    return {
        "scope": "fresh_raw_replay_vs_stored_calibration_rows",
        "records": results,
        "limitations_ru": [
            "Сопоставление выполнено по сохранённому манифесту, оно не доказывает правильность пары модель–лог.",
            "Число допустимых слоёв не является полным числом физических попыток.",
            "Legacy nominal_cycle_without_pause_like_residual удаляет только длинный остаток; это не эталон нормального времени при выбросе внутри фазы.",
            "Ни исходники, ни карточки, ни калибровочные настройки не изменены.",
        ],
        "provenance": build_provenance(
            "raw_timing_storage_audit", inputs={"sources": all_sources, "stored": stored_events},
            config={"writes_database": False, "verifies_sha256_before_and_after_parse": True},
            parser_versions={"time_log": all_sources[0]["parser_version"]} if all_sources else {},
            generated_by=get_settings().compute_node_id,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--import-plan", type=Path, help="Also verify and reparse timing files in this import manifest")
    args = parser.parse_args()
    records, events = load_inputs()
    report = audit(records, events)
    if args.import_plan:
        report["raw_timing_storage_audit"] = audit_raw_timing_storage(records, events, args.import_plan)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite an earlier audit or an accidentally selected source.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"output": str(args.output), "records": len(report["records"]),
                      "active_predictions": sum(r["has_active_prediction"] for r in report["records"])},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
