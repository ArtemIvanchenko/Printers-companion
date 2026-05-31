from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class HistoricalReanalysisPlan:
    start: datetime
    end: datetime
    max_iterations: int
    compute_budget_seconds: int = 1800


def plan_historical_reanalysis(window_days: int = 90, max_iterations: int = 10) -> HistoricalReanalysisPlan:
    end = datetime.now(timezone.utc)
    return HistoricalReanalysisPlan(
        start=end - timedelta(days=window_days),
        end=end,
        max_iterations=max(1, min(max_iterations, 10)),
    )

