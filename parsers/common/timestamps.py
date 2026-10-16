import re
from datetime import date, datetime, time, timezone
from pathlib import Path

from parsers.common.timeline import TimestampQualityTracker


TIMESTAMP_PATTERNS = [
    re.compile(r"(?<!\d)(?P<date>\d{4}[-/.]\d{2}[-/.]\d{2})[ T_]+(?P<time>\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)(?!\d)"),
    re.compile(r"(?<!\d)(?P<date>\d{2}[-/.]\d{2}[-/.]\d{4})[ T_]+(?P<time>\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)(?!\d)"),
    re.compile(r"(?<!\d)(?P<time>\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)(?!\d)"),
]


def date_hint_from_filename(path: Path) -> date | None:
    candidates = [
        re.search(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?([0-3]\d)(?!\d)", path.name),
        re.search(r"(?<!\d)([0-3]\d)[-_.](0[1-9]|1[0-2])[-_.](20\d{2})(?!\d)", path.name),
    ]
    for index, match in enumerate(candidates):
        if match:
            year, month, day = match.groups() if index == 0 else match.groups()[::-1]
            try:
                return date(int(year), int(month), int(day))
            except ValueError:
                return None
    return None


def date_hint_datetime(text: str) -> datetime | None:
    """UTC date-only hint from a card/file name, never proof of a print/log pair."""
    hint = date_hint_from_filename(Path(text))
    return datetime.combine(hint, time(), tzinfo=timezone.utc) if hint else None


def parse_timestamp_token(text: str, date_hint: date | None = None) -> tuple[datetime | None, str | None, float]:
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(0)
        time_part = match.group("time").replace(",", ".")
        date_part = match.groupdict().get("date")
        try:
            # Keep the same accepted clock range across Python versions, without
            # float rounding of fractional seconds or normalization of 24:00.
            hour, minute, seconds = time_part.split(":")
            if int(hour) > 23 or int(minute) > 59 or int(seconds.split(".")[0]) > 59:
                raise ValueError("Invalid clock fields")
            parsed_time = time.fromisoformat(time_part)
            if date_part:
                if re.match(r"\d{4}", date_part):
                    normalized = date_part.replace("/", "-").replace(".", "-")
                else:
                    day, month, year = re.split(r"[-/.]", date_part)
                    normalized = f"{year}-{month}-{day}"
                return datetime.combine(date.fromisoformat(normalized), parsed_time), raw, 0.0
            if date_hint:
                return datetime.combine(date_hint, parsed_time), raw, 86400.0
        except ValueError:
            # An explicit but invalid date is evidence of damaged time data,
            # not permission to reinterpret its clock with the filename date.
            return None, raw, 0.0
        return None, raw, 0.0
    return None, None, 0.0


def apply_midnight_rollover(
    timestamps: list[datetime | None], *, clock_only: list[bool] | None = None,
) -> list[datetime | None]:
    """Adjust only clocks whose date came from a hint; preserve explicit dates."""
    flags = [False] * len(timestamps) if clock_only is None else clock_only
    if len(flags) != len(timestamps):
        raise ValueError("Each timestamp requires one clock-only flag")
    tracker = TimestampQualityTracker()
    return [tracker.add(ts, clock_only=flag) for ts, flag in zip(timestamps, flags, strict=True)]
