#!/usr/bin/env python
"""Trace curated print files back to read-only source archives.

Exact matches are found by size and SHA-256, so renamed files and Unicode
normalisation differences are handled correctly.  For logs with the same
basename, the audit also detects append-only snapshots where one file is a
byte-for-byte prefix of another.

Example::

    python scripts/research/audit_archive_lineage.py CURATED_ROOT SOURCE_ROOT ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_MIN_PREFIX_BYTES = 4096


def _selected(path: Path, *, include_all_logs: bool) -> bool:
    suffix = path.suffix.lower()
    if suffix in {".magics", ".stl"}:
        return True
    if suffix != ".log":
        return False
    return include_all_logs or path.name.lower().endswith("_time.log")


def _kind(path: Path) -> str:
    if path.name.lower().endswith("_time.log"):
        return "time_log"
    if path.suffix.lower() == ".log":
        return "log"
    return path.suffix.lower().removeprefix(".")


def _sha256(path: Path, cache: dict[Path, str]) -> str:
    resolved = path.resolve()
    if resolved in cache:
        return cache[resolved]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    result = digest.hexdigest()
    cache[resolved] = result
    return result


def _is_prefix(shorter: Path, longer: Path) -> bool:
    """Return true only when all bytes in ``shorter`` begin ``longer``."""

    with shorter.open("rb") as left, longer.open("rb") as right:
        while chunk := left.read(CHUNK_SIZE):
            if right.read(len(chunk)) != chunk:
                return False
    return True


def _files(root: Path, *, include_all_logs: bool) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and _selected(path, include_all_logs=include_all_logs)
    )


def _source_entry(
    root: Path,
    path: Path,
    *,
    digest_cache: dict[Path, str],
) -> dict[str, Any]:
    return {
        "source_root": str(root),
        "path": str(path.relative_to(root)),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path, digest_cache),
    }


def _deduplicated_sources(
    confirmed_root: Path,
    source_roots: Iterable[Path],
    *,
    include_all_logs: bool,
) -> list[tuple[Path, Path]]:
    confirmed_resolved = confirmed_root.resolve()
    seen: set[Path] = set()
    rows: list[tuple[Path, Path]] = []
    for root in source_roots:
        for path in _files(root, include_all_logs=include_all_logs):
            resolved = path.resolve()
            if resolved == confirmed_resolved or confirmed_resolved in resolved.parents:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            rows.append((root, path))
    return rows


def audit_lineage(
    confirmed_root: Path,
    source_roots: Iterable[Path],
    *,
    include_all_logs: bool = False,
    min_prefix_bytes: int = DEFAULT_MIN_PREFIX_BYTES,
) -> dict[str, Any]:
    """Build a stable, JSON-serialisable archive-lineage report."""

    confirmed_root = confirmed_root.resolve()
    roots = [root.resolve() for root in source_roots]
    if not confirmed_root.is_dir():
        raise ValueError(f"Curated root does not exist: {confirmed_root}")
    missing = [root for root in roots if not root.is_dir()]
    if missing:
        raise ValueError(f"Source root does not exist: {missing[0]}")
    if min_prefix_bytes < 1:
        raise ValueError("min_prefix_bytes must be positive")

    targets = _files(confirmed_root, include_all_logs=include_all_logs)
    sources = _deduplicated_sources(
        confirmed_root,
        roots,
        include_all_logs=include_all_logs,
    )
    by_size: dict[int, list[tuple[Path, Path]]] = {}
    by_name: dict[str, list[tuple[Path, Path]]] = {}
    for root, path in sources:
        by_size.setdefault(path.stat().st_size, []).append((root, path))
        by_name.setdefault(path.name, []).append((root, path))

    digest_cache: dict[Path, str] = {}
    rows: list[dict[str, Any]] = []
    for target in targets:
        size = target.stat().st_size
        digest = _sha256(target, digest_cache)
        exact: list[dict[str, Any]] = []
        for root, source in by_size.get(size, []):
            # Empty/header-only logs are often identical across unrelated dates.
            # Preserve a same-name copy in the report, but do not turn a generic
            # tiny log body into cross-name lineage evidence.
            if (
                target.suffix.lower() == ".log"
                and size < min_prefix_bytes
                and source.name != target.name
            ):
                continue
            if _sha256(source, digest_cache) == digest:
                exact.append(
                    _source_entry(root, source, digest_cache=digest_cache)
                )

        source_extensions: list[dict[str, Any]] = []
        target_extensions: list[dict[str, Any]] = []
        if target.suffix.lower() == ".log":
            for root, source in by_name.get(target.name, []):
                source_size = source.stat().st_size
                prefix_size = min(size, source_size)
                if source_size == size or prefix_size < min_prefix_bytes:
                    continue
                if size < source_size and _is_prefix(target, source):
                    source_extensions.append(
                        _source_entry(root, source, digest_cache=digest_cache)
                    )
                elif source_size < size and _is_prefix(source, target):
                    target_extensions.append(
                        _source_entry(root, source, digest_cache=digest_cache)
                    )

        if size == 0:
            status = "empty_uninformative"
            evidence_strength = "none"
        elif exact:
            status = "exact"
            evidence_strength = "strong" if size >= min_prefix_bytes else "weak_small_file"
        elif source_extensions and target_extensions:
            status = "version_chain"
            evidence_strength = "strong"
        elif source_extensions:
            status = "source_extends_target"
            evidence_strength = "strong"
        elif target_extensions:
            status = "target_extends_source"
            evidence_strength = "strong"
        else:
            status = "unmatched"
            evidence_strength = "none"

        relative = target.relative_to(confirmed_root)
        rows.append(
            {
                "pair": relative.parts[0] if len(relative.parts) > 1 else None,
                "target": str(relative),
                "kind": _kind(target),
                "size_bytes": size,
                "sha256": digest,
                "status": status,
                "evidence_strength": evidence_strength,
                "exact_sources": exact,
                "source_extensions": source_extensions,
                "target_extensions": target_extensions,
            }
        )

    status_counts = Counter(row["status"] for row in rows)
    kind_counts: dict[str, dict[str, int]] = {}
    pair_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        kind_row = kind_counts.setdefault(row["kind"], {"total": 0})
        kind_row["total"] += 1
        kind_row[row["status"]] = kind_row.get(row["status"], 0) + 1
        pair = row["pair"] or "(root)"
        pair_row = pair_counts.setdefault(pair, {"total": 0})
        pair_row["total"] += 1
        pair_row[row["status"]] = pair_row.get(row["status"], 0) + 1

    return {
        "schema_version": 1,
        "confirmed_root": str(confirmed_root),
        "source_roots": [str(root) for root in roots],
        "selection": "models_and_all_logs" if include_all_logs else "models_and_time_logs",
        "min_prefix_bytes": min_prefix_bytes,
        "summary": {
            "targets": len(rows),
            "exact_targets": sum(bool(row["exact_sources"]) for row in rows),
            "strong_exact_targets": sum(
                row["status"] == "exact" and row["evidence_strength"] == "strong"
                for row in rows
            ),
            "targets_with_newer_source_snapshot": sum(
                bool(row["source_extensions"]) for row in rows
            ),
            "targets_with_older_source_snapshot": sum(
                bool(row["target_extensions"]) for row in rows
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "by_kind": dict(sorted(kind_counts.items())),
            "by_pair": dict(sorted(pair_counts.items())),
        },
        "targets": rows,
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Archive lineage audit",
        "",
        f"Targets: {summary['targets']}; exact: {summary['exact_targets']}; "
        f"newer source snapshots: {summary['targets_with_newer_source_snapshot']}.",
        "",
        "| Target | Kind | Bytes | SHA-256 | Status | Sources |",
        "|---|---|---:|---|---|---|",
    ]
    for row in report["targets"]:
        sources = [entry["path"] for entry in row["exact_sources"]]
        sources.extend(f"{entry['path']} (newer)" for entry in row["source_extensions"])
        sources.extend(f"{entry['path']} (older)" for entry in row["target_extensions"])
        cells = [
            row["target"],
            row["kind"],
            str(row["size_bytes"]),
            row["sha256"],
            row["status"],
            "; ".join(sources) or "—",
        ]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("confirmed_root", type=Path)
    parser.add_argument("source_roots", type=Path, nargs="+")
    parser.add_argument(
        "--include-all-logs",
        action="store_true",
        help="Audit every .log file instead of only *_time.log files.",
    )
    parser.add_argument(
        "--min-prefix-bytes",
        type=int,
        default=DEFAULT_MIN_PREFIX_BYTES,
        help="Reject shorter prefix evidence below this many bytes (default: 4096).",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    try:
        report = audit_lineage(
            args.confirmed_root,
            args.source_roots,
            include_all_logs=args.include_all_logs,
            min_prefix_bytes=args.min_prefix_bytes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.format == "markdown":
        print(_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
