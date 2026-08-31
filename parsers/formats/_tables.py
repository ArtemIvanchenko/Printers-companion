import csv
import math
import random
import re
from collections import Counter
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
    return non_numeric >= max(1, (len(values) + 1) // 2)


def build_header(values: list[str]) -> list[str]:
    """Build stable, unique column names from a possibly damaged header row."""
    names: list[str] = []
    seen: Counter[str] = Counter()
    for index, value in enumerate(values):
        base = value.strip() or f"col_{index}"
        seen[base] += 1
        names.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return names


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
    *,
    numeric_abs_limit: float | None = None,
    startup_window_rows: int = 100,
    max_malformed_diagnostics: int = 20,
) -> tuple[ParsedTableBatch, list[ParseDiagnosticRecord], dict[str, Any]]:
    """Parse a delimited log with bounded memory and whole-file quality checks.

    The retained rows combine an exact head (important for startup diagnostics)
    with deterministic reservoir sampling over the rest of the file.  This keeps
    long prints represented across their full duration without a second pass.
    Structural counters are still computed over every row.
    """
    encoding = estimate_encoding(path)
    diagnostics: list[ParseDiagnosticRecord] = []
    header: list[str] | None = None
    header_signature: list[str] | None = None
    sampled: list[tuple[int, list[str]]] = []
    reservoir: list[tuple[int, list[str]]] = []
    malformed = 0
    repeated_headers = 0
    total_rows = 0
    known = set(known_columns)
    max_rows = max(0, max_rows)
    # Reserve most of the budget for whole-file coverage.  With the production
    # 5k sample this still keeps the complete 100-row startup window; tiny test
    # or caller budgets retain a proportional head instead of starving the reservoir.
    head_capacity = min(max_rows, max(1, min(startup_window_rows, max_rows // 5)))
    reservoir_capacity = max_rows - head_capacity
    reservoir_seen = 0
    rng = random.Random(0)
    last_item: tuple[int, list[str]] | None = None

    for line_no, _offset, line in iter_text_lines(path, encoding):
        if not line.strip():
            continue
        values = split_row(line)
        if header is None:
            if looks_like_header(values):
                header_signature = [value.strip() for value in values]
                header = build_header(values)
                continue
            header = [f"col_{index}" for index in range(len(values))]
        if (
            header_signature is not None
            and [value.strip() for value in values] == header_signature
        ):
            repeated_headers += 1
            continue
        original_width = len(values)
        values, is_malformed = reconcile_row_width(values, len(header))
        if is_malformed:
            malformed += 1
            if malformed <= max_malformed_diagnostics:
                diagnostics.append(
                    ParseDiagnosticRecord(
                        severity="warning",
                        code="malformed_row",
                        message=f"Expected {len(header)} columns, found {original_width}.",
                        source_line=line_no,
                        context={"raw": line[:300]},
                    )
                )
        row_index = total_rows
        total_rows += 1
        item = (row_index, values)
        last_item = item
        if len(sampled) < head_capacity:
            sampled.append(item)
            continue
        if reservoir_capacity <= 0:
            continue
        reservoir_seen += 1
        if len(reservoir) < reservoir_capacity:
            reservoir.append(item)
            continue
        replacement = rng.randrange(reservoir_seen)
        if replacement < reservoir_capacity:
            reservoir[replacement] = item

    if last_item is not None and reservoir_capacity > 0:
        sampled_indices = {index for index, _values in reservoir}
        if last_item[0] >= head_capacity and last_item[0] not in sampled_indices:
            if len(reservoir) < reservoir_capacity:
                reservoir.append(last_item)
            else:
                reservoir[-1] = last_item

    if malformed > max_malformed_diagnostics:
        diagnostics.append(ParseDiagnosticRecord(
            severity="warning",
            code="malformed_rows_aggregated",
            message=(
                f"Suppressed {malformed - max_malformed_diagnostics} additional "
                "malformed-row diagnostics."
            ),
            context={
                "total": malformed,
                "examples_retained": max_malformed_diagnostics,
            },
        ))

    sample_items = sorted([*sampled, *reservoir], key=lambda item: item[0])
    invalid_columns: Counter[str] = Counter()
    invalid_rows: set[int] = set()
    rows: list[dict[str, Any]] = []
    for row_index, values in sample_items:
        row: dict[str, Any] = {}
        for column, raw in zip(header or [], values, strict=True):
            value = coerce_value(raw)
            if (
                numeric_abs_limit is not None
                and isinstance(value, int | float)
                and (not math.isfinite(float(value)) or abs(float(value)) > numeric_abs_limit)
            ):
                value = None
                invalid_columns[column] += 1
                invalid_rows.add(row_index)
            row[column] = value
        rows.append(row)

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
        "sample_strategy": "head_plus_deterministic_reservoir" if total_rows > len(rows) else "all",
        "sample_first_row": sample_items[0][0] if sample_items else None,
        "sample_last_row": sample_items[-1][0] if sample_items else None,
        "sample_coverage": (len(rows) / total_rows) if total_rows else 1.0,
        "malformed_rows": malformed,
        "repeated_headers": repeated_headers,
        "structural_quality_score": round(100.0 * (1.0 - malformed / total_rows), 3)
        if total_rows else 100.0,
        "sampled_invalid_numeric_cells": sum(invalid_columns.values()),
        "sampled_invalid_numeric_columns": dict(invalid_columns),
        "startup_invalid_rows": sum(row_index < startup_window_rows for row_index in invalid_rows),
    }
    return batch, diagnostics, metadata
