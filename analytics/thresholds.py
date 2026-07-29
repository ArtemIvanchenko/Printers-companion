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
def load_valid_ranges() -> dict[str, dict[str, float]]:
    """Return ``{signal: {"min_val": x, "max_val": y}}`` (entries optional).

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
        result: dict[str, dict[str, float]] = {}
        for sig_name, sig_data in (raw.get("signals") or {}).items():
            entry: dict[str, float] = {}
            if (lo := sig_data.get("min_val")) is not None:
                entry["min_val"] = float(lo)
            if (hi := sig_data.get("max_val")) is not None:
                entry["max_val"] = float(hi)
            if entry:
                result[sig_name] = entry
        return result
    except Exception:
        return {}


__all__ = ["load_alarm_thresholds", "load_valid_ranges"]
