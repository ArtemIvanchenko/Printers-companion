"""Pure preparation and verification of one compact timing publication."""

from __future__ import annotations

from pydantic import BaseModel, Field

from analytics.prediction.timing_validation import (
    MIN_POUR_MS,
    MAX_POUR_MS,
    calibration_timing_payloads,
)
from core.versioning.provenance import build_provenance, stable_hash
from domain.enums.common import SourceFileFamily

SNAPSHOT_SCHEMA = 1
MAX_LAYER_OVERHEAD_MS = 10_000.0
MANIFEST_KEY = "timing_publication"


class PreparedLayerTimings(BaseModel):
    """Local-only artifact. Large row lists never go into the job's JSON."""

    rows: list[dict] = Field(default_factory=list)
    manifest: dict = Field(default_factory=dict)


def prepare_layer_timings(
    files: list, *, owner_node_id: str = "system", source: dict | None = None
) -> PreparedLayerTimings:
    timing_files = [f for f in files if f.classification.family == SourceFileFamily.time_log]
    parsed = [f for f in timing_files if f.parse_result is not None]
    payloads = calibration_timing_payloads(event for f in parsed for event in f.parse_result.events)
    rows = []
    for layer, payload in sorted(payloads.items()):
        burn, pour, make = (
            payload.get("burn_ms"),
            payload.get("pour_ms"),
            payload.get("make_layer_ms"),
        )
        if (
            not isinstance(layer, int)
            or not isinstance(burn, (int, float))
            or not isinstance(pour, (int, float))
        ):
            continue
        if burn <= 0 or not (MIN_POUR_MS <= pour <= MAX_POUR_MS):
            continue
        features = {"burn_ms": float(burn), "pour_ms": float(pour)}
        if isinstance(make, (int, float)) and make >= burn + pour:
            features["make_layer_ms"] = float(make)
            overhead = float(make - burn - pour)
            if overhead <= MAX_LAYER_OVERHEAD_MS:
                features["normal_overhead_ms"] = overhead
        rows.append({"layer": layer, "features": features})

    # A failed daily file must not publish a plausible-looking partial replacement.
    if len(parsed) != len(timing_files):
        raise ValueError("Не все файлы времени разобраны; прежние измерения должны быть сохранены.")
    hashes = sorted((str(f.relative_path), f.checksum) for f in timing_files)
    provenance = build_provenance(
        "layer-timing-publication",
        inputs=hashes,
        config={"schema": SNAPSHOT_SCHEMA, "pour_min_ms": MIN_POUR_MS, "pour_max_ms": MAX_POUR_MS},
        parser_versions={f.parse_result.parser_name: f.parse_result.parser_version for f in parsed},
        generated_by=owner_node_id,
    )
    manifest = {
        "schema": SNAPSHOT_SCHEMA,
        "status": "complete" if rows else "empty" if timing_files else "no_time_log",
        "row_count": len(rows),
        "timing_file_count": len(timing_files),
        "rows_fingerprint": stable_hash(rows),
        "provenance": provenance,
        "source": source or {},
    }
    manifest["publication_id"] = stable_hash(manifest)
    return PreparedLayerTimings(rows=rows, manifest=manifest)


def read_timing_publication(
    rows: list[tuple], manifest: dict | None,
) -> tuple[str, list[dict] | None]:
    """Validate once and return the publication status together with its events.

    None = legacy absent. [] = known empty/unverifiable, NEVER raw fallback.

    Each row is (layer, features, publication_id). Count, generation and digest
    must agree with the committed manifest before any consumer uses these facts.
    Physical timing admission remains in calibration_timing_payloads.
    This is a pure projection over detached rows, without SQL or raw-file IO.
    """
    status = _timing_publication_status(rows, manifest)
    if status == "invalid":
        return status, []
    if not rows:
        return status, [] if manifest is not None else None
    return status, [
        {
            "event_type": "layer_timing_summary",
            "payload": {
                **(features if isinstance(features, dict) else {"timing_valid": False}),
                "layer": layer,
            },
        }
        for layer, features, _ in rows
    ]


def _timing_publication_status(rows: list[tuple], manifest: dict | None) -> str:
    """Shared integrity rule for legacy, empty and versioned publications."""
    if manifest is None and any(tag is not None for _, _, tag in rows):
        return "invalid"  # Versioned rows without their manifest are not legacy.
    if manifest is None:
        return "legacy" if rows else "absent"
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != SNAPSHOT_SCHEMA
        or manifest.get("row_count") != len(rows)
        or not manifest.get("publication_id")
        or any(tag != manifest["publication_id"] for _, _, tag in rows)
        or any(
            type(layer) is not int or layer < 0 or not isinstance(features, dict)
            for layer, features, _ in rows
        )
        or len({layer for layer, _, _ in rows}) != len(rows)
        or stable_hash(
            [
                {"layer": layer, "features": features}
                for layer, features, _ in sorted(rows, key=lambda row: row[0])
            ]
        )
        != manifest.get("rows_fingerprint")
    ):
        return "invalid"
    status = manifest.get("status")
    if status not in ({"complete"} if rows else {"empty", "no_time_log"}):
        return "invalid"
    return str(manifest["status"])
