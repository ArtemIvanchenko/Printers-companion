import csv
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from domain.schemas.parsing import ParseDiagnosticRecord, ParsedTableBatch
from parsers.common.encoding import estimate_encoding, iter_text_lines


def split_row(line: str) -> list[str]:
    stripped = line.strip()
    # Pipe is the primary delimiter for M-450-M table logs
    # (burn / sensors / table_temp / Monitor). Cells are space-padded; callers strip.
    if "|" in stripped:
        cells = next(csv.reader([stripped], delimiter="|"))
        # A trailing delimiter ("a|b|") produces a spurious empty final cell — drop it.
        if cells and cells[-1].strip() == "":
            cells.pop()
        return cells
    if ";" in line:
        return next(csv.reader([line], delimiter=";"))
    if "\t" in line:
        return next(csv.reader([line], delimiter="\t"))
    if "," in line and len(line.split(",")) > 2:
        return next(csv.reader([line], delimiter=","))
    return re.split(r"\s+", line.strip())


def coerce_value(value: str) -> Any:
    cleaned = value.strip().replace(",", ".")
    if cleaned == "":
        return None
    try:
        if re.fullmatch(r"[-+]?\d+", cleaned):
            return int(cleaned)
        return float(cleaned)
    except ValueError:
        return value.strip()


def looks_like_header(values: list[str]) -> bool:
    if not values:
        return False
    non_numeric = sum(1 for value in values if isinstance(coerce_value(value), str))
    return non_numeric >= max(1, len(values) // 2)


def build_header(values: list[str]) -> list[str]:
    """Build column names from a header row, filling blanks with positional names."""
    return [value.strip() or f"col_{index}" for index, value in enumerate(values)]


def reconcile_row_width(values: list[str], width: int) -> tuple[list[str], bool]:
    """Pad (with '') or truncate `values` to exactly `width` columns.

    Returns the adjusted values and a flag indicating whether the row was malformed
    (i.e. its width did not match). Callers own diagnostic emission so they can use
    their own codes/severity.
    """
    if len(values) == width:
        return values, False
    if len(values) < width:
        return values + [""] * (width - len(values)), True
    return values[:width], True


def parse_table_stream(
    path: Path,
    known_columns: Iterable[str] = (),
    max_rows: int = 5000,
) -> tuple[ParsedTableBatch, list[ParseDiagnosticRecord], dict[str, Any]]:
    encoding = estimate_encoding(path)
    diagnostics: list[ParseDiagnosticRecord] = []
    header: list[str] | None = None
    rows: list[dict[str, Any]] = []
    malformed = 0
    repeated_headers = 0
    total_rows = 0
    known = set(known_columns)

    for line_no, _offset, line in iter_text_lines(path, encoding):
        if not line.strip():
            continue
        # Fast path: once header is known and sample is full, just count remaining lines.
        if header is not None and len(rows) >= max_rows:
            total_rows += 1
            continue
        values = split_row(line)
        if header is None:
            if looks_like_header(values):
                header = build_header(values)
                continue
            header = [f"col_{index}" for index in range(len(values))]
        if [value.strip() for value in values] == header:
            repeated_headers += 1
            continue
        original_width = len(values)
        values, is_malformed = reconcile_row_width(values, len(header))
        if is_malformed:
            malformed += 1
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="malformed_row",
                    message=f"Expected {len(header)} columns, found {original_width}.",
                    source_line=line_no,
                    context={"raw": line[:300]},
                )
            )
        total_rows += 1
        rows.append({column: coerce_value(value) for column, value in zip(header, values, strict=True)})

    unknown_columns = [column for column in (header or []) if column not in known]
    batch = ParsedTableBatch(
        rows=rows,
        unknown_columns=unknown_columns,
        malformed_rows=malformed,
        repeated_headers=repeated_headers,
    )
    metadata = {
        "encoding": encoding,
        "total_rows": total_rows,
        "sampled_rows": len(rows),
        "streaming": total_rows > len(rows),
    }
    return batch, diagnostics, metadata

