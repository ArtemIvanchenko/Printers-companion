"""Evidence-grade summaries of the M350 ``*_time.log`` layer clock.

The firmware reports three durations for every physical layer::

    OLD_STATS: layer | pour_ms | burn_ms | make_layer_ms |

``burn`` and ``pour`` are useful explanatory components.  The complete
machine-cycle fact is ``make_layer_ms``; the residual
``make_layer - burn - pour`` is controller/transition overhead and must not be
silently discarded when a quote is meant to cover machine occupancy.

This module deliberately does not turn wall-clock gaps into geometry or into
the normal print forecast.  Long pause-like residuals and repeated attempts
remain separate diagnostics of what happened during a historical build.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable

from analytics.prediction.timing_validation import valid_timing_payload


def _field(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    fraction = position - lower
    if lower + 1 == len(ordered):
        return ordered[lower]
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


@dataclass(frozen=True)
class LayerCycle:
    layer: int
    burn_ms: float
    pour_ms: float
    make_layer_ms: float

    @property
    def overhead_ms(self) -> float:
        return self.make_layer_ms - self.burn_ms - self.pour_ms


@dataclass(frozen=True)
class TimingEvidence:
    cycles: dict[int, LayerCycle]
    # Physical attempts, including a repeated layer only when its timings
    # differ materially from the first record. Equivalent boundary copies
    # created by log stitching are deliberately not machine work twice.
    attempts: tuple[LayerCycle, ...]
    duplicate_rows: int
    conflicting_duplicates: int
    equivalent_duplicate_rows: int
    repeated_attempt_rows: int
    ambiguous_layers: frozenset[int]
    invalid_rows: int

    @property
    def first_layer(self) -> int | None:
        return min(self.cycles) if self.cycles else None

    @property
    def last_layer(self) -> int | None:
        return max(self.cycles) if self.cycles else None

    @property
    def observed_layers(self) -> int:
        return len(self.cycles)

    @property
    def missing_layer_count(self) -> int:
        if not self.cycles:
            return 0
        # Layer numbering is physical and normally starts at one.  Several
        # real logs start at two because the first line was not persisted;
        # that is missing coverage, not a shorter build.
        return max(self.last_layer or 0, 0) - self.observed_layers

    @property
    def coverage_ratio(self) -> float | None:
        if not self.last_layer:
            return None
        return self.observed_layers / self.last_layer

    def component_hours(self) -> dict[str, float]:
        burn = sum(row.burn_ms for row in self.cycles.values()) / 3_600_000
        pour = sum(row.pour_ms for row in self.cycles.values()) / 3_600_000
        cycle = sum(row.make_layer_ms for row in self.cycles.values()) / 3_600_000
        attempt_burn = sum(row.burn_ms for row in self.attempts) / 3_600_000
        attempt_pour = sum(row.pour_ms for row in self.attempts) / 3_600_000
        attempt_cycle = sum(row.make_layer_ms for row in self.attempts) / 3_600_000
        pause_threshold_ms = 60_000.0
        normal_overheads = [
            row.overhead_ms for row in self.attempts
            if 0.0 <= row.overhead_ms <= pause_threshold_ms
        ]
        normal_overhead_ms = median(normal_overheads) if normal_overheads else 0.0

        def pause_excess(rows: Iterable[LayerCycle]) -> float:
            return sum(
                max(row.overhead_ms - normal_overhead_ms, 0.0)
                for row in rows
                if row.overhead_ms > pause_threshold_ms
            )

        pause_excess_ms = pause_excess(self.attempts)
        unique_pause_excess_ms = pause_excess(self.cycles.values())
        cleaned_attempt_cycle = attempt_cycle - pause_excess_ms / 3_600_000
        cleaned_unique_cycle = cycle - unique_pause_excess_ms / 3_600_000
        return {
            "laser_scan": burn,
            "powder_recoat": pour,
            "controller_overhead": cycle - burn - pour,
            "productive_components": burn + pour,
            "full_machine_cycle": cycle,
            "nominal_cycle_without_pause_like_residual": cleaned_unique_cycle,
            "all_attempts_productive": attempt_burn + attempt_pour,
            "all_attempts_full_cycle": attempt_cycle,
            "pause_like_residual": pause_excess_ms / 3_600_000,
            "all_attempts_without_pause_like_residual": cleaned_attempt_cycle,
            "repeat_attempt_overhead_without_pause": max(
                cleaned_attempt_cycle - cleaned_unique_cycle, 0.0,
            ),
        }

    def per_layer_statistics(self) -> dict[str, dict[str, float | None]]:
        series = {
            "laser_scan": [row.burn_ms / 1000 for row in self.cycles.values()],
            "powder_recoat": [row.pour_ms / 1000 for row in self.cycles.values()],
            "controller_overhead": [row.overhead_ms / 1000 for row in self.cycles.values()],
            "full_machine_cycle": [row.make_layer_ms / 1000 for row in self.cycles.values()],
        }
        return {
            name: {
                "median_seconds": median(values) if values else None,
                "p10_seconds": _percentile(values, 0.10),
                "p90_seconds": _percentile(values, 0.90),
            }
            for name, values in series.items()
        }

    def as_dict(self) -> dict[str, Any]:
        largest_overheads = sorted(
            (
                {"layer": row.layer, "seconds": row.overhead_ms / 1000}
                for row in self.cycles.values()
            ),
            key=lambda item: item["seconds"],
            reverse=True,
        )[:20]
        return {
            "first_layer": self.first_layer,
            "last_layer": self.last_layer,
            "observed_layers": self.observed_layers,
            "missing_layer_count": self.missing_layer_count,
            "coverage_ratio": self.coverage_ratio,
            "duplicate_rows": self.duplicate_rows,
            "conflicting_duplicates": self.conflicting_duplicates,
            "equivalent_duplicate_rows": self.equivalent_duplicate_rows,
            "repeated_attempt_rows": self.repeated_attempt_rows,
            "ambiguous_layer_count": len(self.ambiguous_layers),
            "ambiguous_layers": sorted(self.ambiguous_layers)[:100],
            "invalid_rows": self.invalid_rows,
            "attempt_rows": len(self.attempts),
            "pause_like_rows": sum(row.overhead_ms > 60_000 for row in self.attempts),
            "hours": self.component_hours(),
            "per_layer": self.per_layer_statistics(),
            "largest_controller_overheads": largest_overheads,
        }


def summarize_timing_events(events: Iterable[Any]) -> TimingEvidence:
    """Merge parsed timing summaries with deterministic first-wins semantics.

    A repeated boundary layer is normal when the firmware rotates files at
    midnight.  We count it once and separately report whether the repeated
    values disagree by more than 1% or 20 ms, whichever is larger.
    """
    cycles: dict[int, LayerCycle] = {}
    attempts: list[LayerCycle] = []
    duplicate_rows = conflicting_duplicates = invalid_rows = 0
    equivalent_duplicate_rows = repeated_attempt_rows = 0
    ambiguous_layers: set[int] = set()
    attempts_by_layer: dict[int, list[LayerCycle]] = {}
    for event in events:
        if _field(event, "event_type") != "layer_timing_summary":
            continue
        payload = _field(event, "payload", {}) or {}
        layer = payload.get("layer", _field(event, "layer"))
        burn = payload.get("burn_ms")
        pour = payload.get("pour_ms")
        make = payload.get("make_layer_ms")
        if (
            not valid_timing_payload(payload)
            or not isinstance(layer, int)
            or isinstance(layer, bool)
            or layer < 1
            or not isinstance(burn, (int, float))
            or not isinstance(pour, (int, float))
            or not isinstance(make, (int, float))
            or burn <= 0
            or pour < 0
            or make <= 0
        ):
            invalid_rows += 1
            continue
        burn_f, pour_f, make_f = float(burn), float(pour), float(make)
        components = burn_f + pour_f
        # OLD_STATS and NEW_STATS occasionally differ by a few milliseconds.
        # Clamp numerical noise, but reject a cycle that is physically shorter
        # than its two measured components by more than parser tolerance.
        if make_f < components:
            if components - make_f <= max(20.0, components * 0.01):
                make_f = components
            else:
                invalid_rows += 1
                continue
        candidate = LayerCycle(layer, burn_f, pour_f, make_f)
        previous = cycles.get(layer)
        if previous is None:
            cycles[layer] = candidate
            attempts.append(candidate)
            attempts_by_layer[layer] = [candidate]
            continue
        duplicate_rows += 1
        materially_different = not any(
            all(abs(a-b) <= max(20.0, min(abs(a), abs(b))*0.01) for a, b in (
                (attempt.burn_ms, candidate.burn_ms),
                (attempt.pour_ms, candidate.pour_ms),
                (attempt.make_layer_ms, candidate.make_layer_ms),
            )) for attempt in attempts_by_layer[layer]
        )
        if materially_different:
            conflicting_duplicates += 1
            repeated_attempt_rows += 1
            ambiguous_layers.add(layer)
            attempts.append(candidate)
            attempts_by_layer[layer].append(candidate)
        else:
            # Usually the boundary layer copied into both daily files. Counting
            # it twice would invent machine time that was never spent.
            equivalent_duplicate_rows += 1
    return TimingEvidence(
        cycles,
        tuple(attempts),
        duplicate_rows,
        conflicting_duplicates,
        equivalent_duplicate_rows,
        repeated_attempt_rows,
        frozenset(ambiguous_layers),
        invalid_rows,
    )


__all__ = ["LayerCycle", "TimingEvidence", "summarize_timing_events"]
