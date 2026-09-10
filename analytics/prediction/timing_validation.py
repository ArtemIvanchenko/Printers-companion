"""Shared admission rules for timing calibration, on disk and in storage."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

TIMING_FIELDS = ("burn_ms", "pour_ms", "make_layer_ms")


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_timing_payload(payload: dict) -> bool:
    """Allow old partial summaries, but never known contradictions or NaN."""
    if payload.get("timing_valid") is False:
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
