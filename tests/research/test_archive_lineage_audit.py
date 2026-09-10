from pathlib import Path

from scripts.research.audit_archive_lineage import audit_lineage


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_exact_match_uses_content_not_filename(tmp_path: Path):
    confirmed = tmp_path / "confirmed"
    source = tmp_path / "source"
    payload = b"solid model\n" * 500
    _write(confirmed / "pair-1" / "model" / "renamed.stl", payload)
    _write(source / "archive" / "original.stl", payload)

    report = audit_lineage(confirmed, [source])

    row = report["targets"][0]
    assert row["status"] == "exact"
    assert row["evidence_strength"] == "strong"
    assert row["exact_sources"][0]["path"] == "archive/original.stl"


def test_detects_newer_append_only_log_snapshot(tmp_path: Path):
    confirmed = tmp_path / "confirmed"
    source = tmp_path / "source"
    prefix = b"header\n" + (b"row\n" * 1500)
    _write(confirmed / "pair-1" / "logs" / "01.01.2026_sensors.log", prefix)
    _write(source / "01.01.2026_sensors.log", prefix + b"new row\n")

    report = audit_lineage(confirmed, [source], include_all_logs=True)

    row = report["targets"][0]
    assert row["status"] == "source_extends_target"
    assert row["source_extensions"][0]["path"] == "01.01.2026_sensors.log"
    assert report["summary"]["targets_with_newer_source_snapshot"] == 1


def test_empty_log_is_not_treated_as_prefix_evidence(tmp_path: Path):
    confirmed = tmp_path / "confirmed"
    source = tmp_path / "source"
    _write(confirmed / "pair-1" / "logs" / "empty.log", b"")
    _write(source / "empty.log", b"new data")

    report = audit_lineage(confirmed, [source], include_all_logs=True)

    row = report["targets"][0]
    assert row["status"] == "empty_uninformative"
    assert row["source_extensions"] == []


def test_small_generic_log_does_not_match_a_different_filename(tmp_path: Path):
    confirmed = tmp_path / "confirmed"
    source = tmp_path / "source"
    header = b"N|pour|burn|make layer\n"
    _write(confirmed / "pair-1" / "logs" / "01.01.2026_time.log", header)
    _write(source / "02.01.2026_time.log", header)

    report = audit_lineage(confirmed, [source])

    row = report["targets"][0]
    assert row["status"] == "unmatched"
    assert row["exact_sources"] == []


def test_default_selection_excludes_non_timing_logs(tmp_path: Path):
    confirmed = tmp_path / "confirmed"
    source = tmp_path / "source"
    _write(confirmed / "pair-1" / "logs" / "01.01.2026_time.log", b"timing")
    _write(confirmed / "pair-1" / "logs" / "01.01.2026_sensors.log", b"sensor")
    _write(source / "01.01.2026_time.log", b"timing")
    _write(source / "01.01.2026_sensors.log", b"sensor")

    report = audit_lineage(confirmed, [source])

    assert [row["target"] for row in report["targets"]] == [
        "pair-1/logs/01.01.2026_time.log"
    ]
