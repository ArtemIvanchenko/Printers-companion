"""Versioned identity of configured scan inputs, not proof of the as-run recipe.

Never reconstruct an old training scope from today's mutable machine settings.
The complete scope travels in the prediction snapshot and the fitted artifact.
Unknown physical machine identity cannot authorise cross-workstation reuse.
"""
from __future__ import annotations

import hashlib
import json
import math

# Bump when the geometry/default scan policy changes, even if the input field
# names stay the same; an old fitted beta may absorb that policy's errors.
VERSION = "configured-scan-v1"
_NUMERIC_FIELDS = (
    "hatch_speed_mm_s", "contour_speed_mm_s", "support_speed_mm_s",
    "jump_speed_mm_s", "jump_delay_ms", "hatch_distance_mm", "laser_count",
)
_IDENTITY_FIELDS = (
    "active_preset_id", "process_strategy_id", "process_strategy_version",
    "slicer_version", "firmware_version",
)


def scan_scope(params: dict, material: str, thickness: float) -> dict | None:
    """Capture only scan-affecting inputs; costs/calibration results are excluded."""
    printer = params.get("printer_id")
    if (not isinstance(printer, str) or not printer.strip()
            or not isinstance(material, str) or not material.strip()):
        return None
    inputs = {key: params.get(key) for key in _IDENTITY_FIELDS}
    if any(value is not None and not isinstance(value, str) for value in inputs.values()):
        return None
    inputs.update(printer_id=printer, material=material)
    speeds = params.get("hatch_speeds_by_mat") or {}
    if not isinstance(speeds, dict):
        return None
    for key in (*_NUMERIC_FIELDS, "layer_thickness_mm"):
        value = thickness if key == "layer_thickness_mm" else params.get(key)
        if key == "hatch_speed_mm_s":
            value = speeds.get(material) or value
        if value is None and key in {
            "contour_speed_mm_s", "support_speed_mm_s", "jump_speed_mm_s", "jump_delay_ms",
        }:
            inputs[key] = None  # frozen default policy is covered by VERSION
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
        except OverflowError:
            return None
        if (not math.isfinite(value) or value < 0
                or (key != "jump_delay_ms" and value == 0)
                or (key == "laser_count" and int(value) != value)):
            return None
        inputs[key] = float(value)
    payload = {"version": VERSION, "inputs": inputs}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {**payload, "fingerprint": digest}


def snapshot_scan_scope(snapshot: dict, printer_id: str | None) -> dict | None:
    """Validate a captured scope against its snapshot and linked physical machine."""
    scope = snapshot.get("scan_calibration_scope")
    if not isinstance(scope, dict) or scope.get("version") != VERSION:
        return None
    inputs = scope.get("inputs")
    if not isinstance(inputs, dict) or inputs.get("printer_id") != printer_id:
        return None
    if snapshot.get("printer_id") not in (None, printer_id):
        return None
    rebuilt = scan_scope(inputs, snapshot.get("material"), snapshot.get("layer_thickness_mm"))
    if rebuilt != scope or inputs.get("laser_count") != snapshot.get("laser_count"):
        return None
    if inputs.get("hatch_distance_mm") != snapshot.get("hatch_distance_mm"):
        return None
    return rebuilt


def scan_scope_key(scope: dict) -> str:
    inputs = scope["inputs"]
    return (f"{inputs['printer_id']}|{inputs['material']}@{inputs['layer_thickness_mm']:.3f}"
            f"|lasers={int(inputs['laser_count'])}|scan={scope['fingerprint']}")
