"""Shared access to per-signal alarm thresholds from the printer profile.

Single source of truth for ``alarm_high`` / ``alarm_low`` (used by alarm-count
stats, the data-quality checks and the maintenance forecast). Reads
``profiles/m350/signals.yaml`` and caches the result.
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


__all__ = ["load_alarm_thresholds"]
