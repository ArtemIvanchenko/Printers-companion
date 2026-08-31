from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class TimestampQualityTracker:
    """Normalize midnight rollover and summarize timestamp integrity."""

    rollover_days: int = 0
    parsed_count: int = 0
    missing_count: int = 0
    out_of_order_count: int = 0
    max_gap_seconds: float = 0.0
    previous: datetime | None = None

    def add(self, timestamp: datetime | None) -> datetime | None:
        if timestamp is None:
            self.missing_count += 1
            return None
        candidate = timestamp + timedelta(days=self.rollover_days)
        if self.previous is not None and candidate + timedelta(hours=12) < self.previous:
            self.rollover_days += 1
            candidate = timestamp + timedelta(days=self.rollover_days)
        if self.previous is not None:
            delta = (candidate - self.previous).total_seconds()
            if delta < 0:
                self.out_of_order_count += 1
            else:
                self.max_gap_seconds = max(self.max_gap_seconds, delta)
        self.previous = candidate
        self.parsed_count += 1
        return candidate

    def metadata(self) -> dict[str, int | float]:
        return {
            "parsed_timestamps": self.parsed_count,
            "missing_timestamps": self.missing_count,
            "midnight_rollovers": self.rollover_days,
            "out_of_order_timestamps": self.out_of_order_count,
            "max_timestamp_gap_seconds": round(self.max_gap_seconds, 3),
        }
