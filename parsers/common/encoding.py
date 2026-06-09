import re
from collections.abc import Iterator
from pathlib import Path

try:
    from charset_normalizer import from_bytes
except Exception:  # pragma: no cover
    from_bytes = None


FALLBACK_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "latin-1")


def _read_head(path: Path, sample_size: int) -> bytes:
    """Read only the first ``sample_size`` bytes — never the whole file.
    ``path.read_bytes()[:N]`` loads the entire file (80 MB sensors logs!) just
    to inspect a tiny prefix."""
    with path.open("rb") as handle:
        return handle.read(sample_size)


def estimate_encoding(path: Path, sample_size: int = 65536) -> str:
    sample = _read_head(path, sample_size)
    if not sample:
        return "utf-8"
    # BOM must be checked explicitly: decode("utf-8-sig") succeeds for ANY valid UTF-8,
    # so using it as a detection heuristic produces false positives on plain UTF-8 files.
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    # Valid UTF-8 (no BOM) — cp1251 Cyrillic bytes (0xC0-0xFF) are never valid UTF-8,
    # so a successful decode here unambiguously means the file is UTF-8.
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as exc:
        # A multibyte UTF-8 char can be split by the sample boundary. If the only
        # error is within the last 3 bytes (max char is 4 bytes), it's a truncated
        # tail, not a cp1251 file — still UTF-8. Avoids misdecoding a genuine
        # UTF-8 file as cp1251 (which would silently mojibake the whole file).
        if exc.start >= len(sample) - 3:
            return "utf-8"
    # cp1251 is the dominant single-byte encoding for Russian industrial equipment.
    try:
        cp1251_text = sample.decode("cp1251")
        if re.search(r"[А-Яа-яЁё]", cp1251_text):
            return "cp1251"
    except UnicodeDecodeError:
        pass
    if from_bytes is not None:
        match = from_bytes(sample).best()
        if match and match.encoding:
            return match.encoding
    for encoding in FALLBACK_ENCODINGS:
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin-1"


def iter_text_lines(path: Path, encoding: str | None = None) -> Iterator[tuple[int, int, str]]:
    selected = encoding or estimate_encoding(path)
    offset = 0
    with path.open("rb") as handle:
        for line_no, raw in enumerate(handle, start=1):
            start = offset
            offset += len(raw)
            try:
                text = raw.decode(selected)
            except UnicodeDecodeError:
                text = raw.decode("cp1251", errors="replace")
            yield line_no, start, text.rstrip("\r\n")


def is_probably_binary(path: Path, sample_size: int = 8192) -> bool:
    sample = _read_head(path, sample_size)
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    textish = sum(byte in b"\n\r\t\b\f" or 32 <= byte <= 126 or byte >= 128 for byte in sample)
    return (textish / max(len(sample), 1)) < 0.70
