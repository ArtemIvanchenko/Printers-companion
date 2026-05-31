from statistics import median


def median_absolute_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    med = median(values)
    return median([abs(value - med) for value in values])


def robust_z_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    med = median(values)
    mad = median_absolute_deviation(values)
    if mad == 0:
        return [0.0 for _ in values]
    return [0.6745 * (value - med) / mad for value in values]


def rolling_baseline(values: list[float], window: int = 7) -> list[float | None]:
    baselines: list[float | None] = []
    for index, _value in enumerate(values):
        start = max(0, index - window)
        previous = values[start:index]
        baselines.append(median(previous) if previous else None)
    return baselines

