from typing import Any

from profiles.base.profile import PrinterProfilePlugin


class ChartThresholds:
    """Alarm thresholds for process-telemetry charts, loaded from signals.yaml.

    Each attribute corresponds to a chart group defined in
    ``domain.services.session_overview`` (oxygen, temperatures, humidity,
    pressure).  Thresholds are read directly from the profile's
    ``signal_mappings`` so there is a single source of truth — the printer
    passport stored in ``signals.yaml``.
    """

    def __init__(self, signals: dict[str, Any]) -> None:
        self.oxygen_alarm_high = _first_of(signals, ("SO1", "SO2"), "alarm_high", 2.0)
        self.oxygen_alarm_low = _first_of(signals, ("SO1", "SO2"), "alarm_low")
        self.oxygen_max = _first_of(signals, ("SO1", "SO2"), "max_val", 20.0)
        self.oxygen_nominal = _first_of(signals, ("SO1", "SO2"), "nominal_val", 0.5)
        self.oxygen_min = _first_of(signals, ("SO1", "SO2"), "min_val", 0.0)

        self.temp_alarm_high = _first_of(signals, ("ST3", "ST4", "ST5"), "alarm_high", 200.0)
        self.temp_alarm_low = _first_of(signals, ("ST3", "ST4", "ST5"), "alarm_low")
        self.temp_max = _first_of(signals, ("ST3", "ST4", "ST5"), "max_val", 250.0)
        self.temp_min = _first_of(signals, ("ST3", "ST4", "ST5"), "min_val", 0.0)

        self.humidity_alarm_high = _first_of(
            signals, ("ST1 (flow H)", "Flow H"), "alarm_high", 40.0
        )
        self.humidity_alarm_low = _first_of(signals, ("ST1 (flow H)", "Flow H"), "alarm_low")
        self.humidity_max = _first_of(signals, ("ST1 (flow H)", "Flow H"), "max_val", 100.0)
        self.humidity_min = _first_of(signals, ("ST1 (flow H)", "Flow H"), "min_val", 0.0)

        self.pressure_alarm_high = _first_of(signals, ("SP4",), "alarm_high", 1.8)
        self.pressure_alarm_low = _first_of(signals, ("SP4",), "alarm_low", 0.85)
        self.pressure_nominal = _first_of(signals, ("SP4",), "nominal_val", 1.0)
        self.pressure_max = _first_of(signals, ("SP4",), "max_val", 1.3)
        self.pressure_min = _first_of(signals, ("SP4",), "min_val", 0.8)

    def to_dict(self) -> dict:
        """Return a plain dict suitable for JSON serialisation into JS."""
        return {
            "oxygen": {
                "alarm_high": self.oxygen_alarm_high,
                "alarm_low": self.oxygen_alarm_low,
                "nominal": self.oxygen_nominal,
                "min": self.oxygen_min,
                "max": self.oxygen_max,
            },
            "temperature": {
                "alarm_high": self.temp_alarm_high,
                "alarm_low": self.temp_alarm_low,
                "min": self.temp_min,
                "max": self.temp_max,
            },
            "humidity": {
                "alarm_high": self.humidity_alarm_high,
                "alarm_low": self.humidity_alarm_low,
                "min": self.humidity_min,
                "max": self.humidity_max,
            },
            "pressure": {
                "alarm_high": self.pressure_alarm_high,
                "alarm_low": self.pressure_alarm_low,
                "nominal": self.pressure_nominal,
                "min": self.pressure_min,
                "max": self.pressure_max,
            },
        }


def _first_of(
    signals: dict[str, Any],
    keys: tuple[str, ...],
    field: str,
    default: float | None = None,
) -> float | None:
    for k in keys:
        entry = signals.get(k, {})
        if field in entry:
            return float(entry[field])
    return default


def load_thresholds(profile: PrinterProfilePlugin) -> ChartThresholds:
    return ChartThresholds(profile.signal_mappings)
