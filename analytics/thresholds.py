"""Shared access to per-signal thresholds and physical ranges from the profile.

Single source of truth for ``alarm_high`` / ``alarm_low`` (used by alarm-count
stats, the data-quality checks and the maintenance forecast) and for
``min_val`` / ``max_val``, the physically possible range of each sensor. Reads
``profiles/m350/signals.yaml`` and caches the result.

The two are different things and must not be conflated: an alarm threshold says
"this reading is bad news", a physical range says "no real sensor can report
this at all" — a humidity of -2.6e18 % is not a wet chamber, it is a firmware
glitch, and it has to be excluded from statistics rather than alarmed on.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any


MAX_CANDIDATE_RANGE_REJECT_FRACTION = 0.20


@lru_cache(maxsize=1)
def load_alarm_thresholds() -> dict[str, dict[str, float]]:
    """Return ``{signal: {"alarm_high": x, "alarm_low": y}}`` (entries optional).

    Best-effort: returns an empty dict if the profile file is missing or invalid.
    """
    try:
        from profiles.base.profile import load_yaml
        signals_path = Path(__file__).resolve().parents[1] / "profiles" / "m350" / "signals.yaml"
        raw = load_yaml(signals_path)
        result: dict[str, dict[str, float]] = {}
        for sig_name, sig_data in (raw.get("signals") or {}).items():
            entry: dict[str, float] = {}
            if (ah := sig_data.get("alarm_high")) is not None:
                entry["alarm_high"] = float(ah)
            if (al := sig_data.get("alarm_low")) is not None:
                entry["alarm_low"] = float(al)
            if entry:
                result[sig_name] = entry
        return result
    except Exception:
        return {}


@lru_cache(maxsize=1)
def load_valid_ranges() -> dict[str, dict[str, Any]]:
    """Return physical bounds plus their enforcement policy (entries optional).

    These are the profile's physically possible bounds — passport limits, not
    alarm levels. Readings outside them are firmware/wiring artefacts: the
    shop's real logs carry a Flow H of -2.58e18 % and a LIR (Z position, travel
    390 mm) of 1.87e9 µm, which are finite floats and so survive every nan/inf
    guard while destroying any mean or trend computed over them.

    Best-effort: returns an empty dict if the profile file is missing or invalid.
    """
    try:
        from profiles.base.profile import load_yaml
        signals_path = Path(__file__).resolve().parents[1] / "profiles" / "m350" / "signals.yaml"
        raw = load_yaml(signals_path)
        result: dict[str, dict[str, Any]] = {}
        for sig_name, sig_data in (raw.get("signals") or {}).items():
            entry: dict[str, Any] = {}
            if (lo := sig_data.get("min_val")) is not None:
                entry["min_val"] = float(lo)
            if (hi := sig_data.get("max_val")) is not None:
                entry["max_val"] = float(hi)
            if invalid_values := sig_data.get("invalid_values"):
                entry["invalid_values"] = [float(value) for value in invalid_values]
            if policy := sig_data.get("range_policy"):
                entry["range_policy"] = str(policy)
            if entry:
                result[sig_name] = entry
        return result
    except Exception:
        return {}


def value_in_valid_range(value: float, spec: dict[str, Any]) -> bool:
    """Return whether a value satisfies every bound present in ``spec``."""
    return (
        (spec.get("min_val") is None or value >= spec["min_val"])
        and (spec.get("max_val") is None or value <= spec["max_val"])
    )


def value_is_explicitly_invalid(value: float, spec: dict[str, Any]) -> bool:
    """Return whether firmware uses this exact value as a documented sentinel."""
    return value in spec.get("invalid_values", ())


def should_apply_valid_range(spec: dict[str, Any], rejected_fraction: float) -> bool:
    """Apply confirmed ranges always; distrust candidate ranges that erase reality."""
    return (
        spec.get("range_policy") == "enforced"
        or rejected_fraction <= MAX_CANDIDATE_RANGE_REJECT_FRACTION
    )


__all__ = [
    "MAX_CANDIDATE_RANGE_REJECT_FRACTION",
    "load_alarm_thresholds",
    "load_valid_ranges",
    "should_apply_valid_range",
    "value_is_explicitly_invalid",
    "value_in_valid_range",
]
