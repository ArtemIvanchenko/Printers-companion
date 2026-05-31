import re
from datetime import date, datetime, timedelta
from pathlib import Path


TIMESTAMP_PATTERNS = [
    re.compile(r"(?P<date>\d{4}[-/.]\d{2}[-/.]\d{2})[ T_]+(?P<time>\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    re.compile(r"(?P<date>\d{2}[-/.]\d{2}[-/.]\d{4})[ T_]+(?P<time>\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
    re.compile(r"(?P<time>\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
]


def date_hint_from_filename(path: Path) -> date | None:
    candidates = [
        re.search(r"(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?([0-3]\d)", path.name),
        re.search(r"([0-3]\d)[-_.](0[1-9]|1[0-2])[-_.](20\d{2})", path.name),
    ]
    if candidates[0]:
        year, month, day = candidates[0].groups()
        return date(int(year), int(month), int(day))
    if candidates[1]:
        day, month, year = candidates[1].groups()
        return date(int(year), int(month), int(day))
    return None


def parse_timestamp_token(text: str, date_hint: date | None = None) -> tuple[datetime | None, str | None, float]:
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(0)
        time_part = match.group("time").replace(",", ".")
        date_part = match.groupdict().get("date")
        try:
            if date_part:
                if re.match(r"\d{4}", date_part):
                    normalized = f"{date_part.replace('/', '-').replace('.', '-')} {time_part}"
                    return datetime.fromisoformat(normalized), raw, 0.0
                day, month, year = re.split(r"[-/.]", date_part)
                return datetime.fromisoformat(f"{year}-{month}-{day} {time_part}"), raw, 0.0
            if date_hint:
                hour, minute, sec = time_part.split(":")
                seconds = float(sec)
                whole_seconds = int(seconds)
                microseconds = int((seconds - whole_seconds) * 1_000_000)
                return (
                    datetime(
                        date_hint.year,
                        date_hint.month,
                        date_hint.day,
                        int(hour),
                        int(minute),
                        whole_seconds,
                        microseconds,
                    ),
                    raw,
                    86400.0,
                )
        except ValueError:
            continue
    return None, None, 0.0


def apply_midnight_rollover(timestamps: list[datetime | None]) -> list[datetime | None]:
    adjusted: list[datetime | None] = []
    day_shift = 0
    previous: datetime | None = None
    for ts in timestamps:
        if ts is None:
            adjusted.append(None)
            continue
        candidate = ts + timedelta(days=day_shift)
        if previous and candidate + timedelta(hours=12) < previous:
            day_shift += 1
            candidate = ts + timedelta(days=day_shift)
        adjusted.append(candidate)
        previous = candidate
    return adjusted

