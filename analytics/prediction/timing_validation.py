"""Shared admission rules for timing calibration, on disk and in storage."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def has_complete_layer_coverage(per_layer, expected_layers: int | None) -> bool:
    """A full-print total requires exactly the independently expected 1..N.

    Unknown N, partial coverage and extra layers stay diagnostic. Do not
    allocate a range up to an untrusted N or infer N from the observed maximum.
    """
    return (
        type(expected_layers) is int and expected_layers > 0
        and len(per_layer) == expected_layers
        and all(type(layer) is int and 1 <= layer <= expected_layers for layer in per_layer)
        and len(set(per_layer)) == expected_layers
    )

TIMING_FIELDS = ("burn_ms", "pour_ms", "make_layer_ms")
# Admission bounds for normal calibration, not proof of a corrupt counter or
# an operator pause. Keep the measured phase in historical diagnostics.
MIN_BURN_MS, MAX_BURN_MS = 100.0, 3_600_000.0
MIN_POUR_MS, MAX_POUR_MS = 500.0, 120_000.0


def finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def valid_timing_payload(payload: dict) -> bool:
    """Allow old partial summaries, but never known contradictions or NaN."""
    if not isinstance(payload, dict) or payload.get("timing_valid") is False:
        return False
    for key in TIMING_FIELDS:
        value = payload.get(key)
        if value is not None and (not finite_number(value) or value < 0):
            return False
    burn, pour, make = (payload.get(key) for key in TIMING_FIELDS)
    if all(value is not None for value in (burn, pour, make)):
        components = burn + pour
        if make + max(20.0, components * 0.01) < components:
            return False
    return True


def calibration_timing_payloads(events: Iterable[Any]) -> dict[int, dict]:
    """Exclude a whole physical layer if any attempt conflicts or is invalid.

    Compare all available timings before individual consumer bounds. Otherwise
    an out-of-range retry disappears and its earlier attempt becomes a false
    normal-cycle target. Call once across every daily file in a session.
    """
    selected: dict[int, dict] = {}
    rejected: set[int] = set()
    for event in events:
        kind = event.get("event_type") if isinstance(event, dict) else event.event_type
        if kind != "layer_timing_summary":
            continue
        payload = (event.get("payload") if isinstance(event, dict) else event.payload) or {}
        layer = payload.get("layer")
        if not isinstance(layer, int) or isinstance(layer, bool) or layer < 1:
            continue
        if layer in rejected:
            continue
        previous = selected.get(layer)
        conflict = previous is not None and any(
            finite_number(previous.get(key))
            and finite_number(payload.get(key))
            and abs(previous[key] - payload[key])
            > max(
                20.0,
                min(abs(previous[key]), abs(payload[key])) * 0.01,
            )
            for key in TIMING_FIELDS
        )
        if not valid_timing_payload(payload) or conflict:
            rejected.add(layer)
            selected.pop(layer, None)
        elif previous is None:
            selected[layer] = dict(payload)
        else:
            for key in TIMING_FIELDS:
                if previous.get(key) is None and payload.get(key) is not None:
                    previous[key] = payload[key]
            if not valid_timing_payload(previous):
                rejected.add(layer)
                selected.pop(layer, None)
    return selected


def normal_phase_exclusions(payload: dict) -> list[str]:
    """Explain why measured phases cannot represent a normal machine layer."""
    reasons = []
    for name, low, high, reason in (
        ("burn_ms", MIN_BURN_MS, MAX_BURN_MS, "burn_out_of_range"),
        ("pour_ms", MIN_POUR_MS, MAX_POUR_MS, "pour_out_of_range"),
    ):
        value = payload.get(name)
        if not finite_number(value) or not low <= value <= high:
            reasons.append(reason)
    return reasons


def timing_components_ms(timings: dict[int, dict]) -> dict[int, tuple[float, float]]:
    """Burn+pour pairs from calibration_timing_payloads, with their own bounds."""
    out = {}
    for layer, payload in timings.items():
        burn, pour = payload.get("burn_ms"), payload.get("pour_ms")
        if (finite_number(burn) and burn > 0 and finite_number(pour)
                and MIN_POUR_MS <= pour <= MAX_POUR_MS):
            out[layer] = (float(burn), float(pour))
    return out


def calibration_burn_ms(timings: dict[int, dict]) -> dict[int, float]:
    """Independent burn projection of calibration_timing_payloads, even partial."""
    return {
        layer: float(payload["burn_ms"])
        for layer, payload in timings.items()
        if finite_number(payload.get("burn_ms"))
        and MIN_BURN_MS <= payload["burn_ms"] <= MAX_BURN_MS
    }


def calibration_cycles_ms(timings: dict[int, dict]) -> dict[int, tuple[float, float, float]]:
    """Complete cycles from calibration_timing_payloads; long residuals remain."""
    out = {}
    for layer, payload in timings.items():
        if normal_phase_exclusions(payload):
            continue
        make = payload.get("make_layer_ms")
        burn, pour = payload["burn_ms"], payload["pour_ms"]
        if finite_number(make) and make >= burn + pour:
            out[layer] = (float(burn), float(pour), float(make))
    return out
