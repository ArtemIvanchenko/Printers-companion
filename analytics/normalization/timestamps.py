from datetime import date, datetime
from pathlib import Path

from parsers.common.timestamps import apply_midnight_rollover, date_hint_from_filename, parse_timestamp_token


def normalize_timestamps(raw_values: list[str], path: Path, fallback_date: date | None = None) -> list[datetime | None]:
    hint = fallback_date or date_hint_from_filename(path)
    parsed = [parse_timestamp_token(value, hint)[0] for value in raw_values]
    return apply_midnight_rollover(parsed)

