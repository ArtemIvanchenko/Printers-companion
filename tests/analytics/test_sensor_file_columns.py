from collections import Counter
from hashlib import sha256
from types import SimpleNamespace

import pytest

from analytics.log_insights.environment import sensor_samples


@pytest.mark.parametrize("apply_profile_ranges", [False, True])
def test_each_daily_source_resolves_its_own_channel_positions(tmp_path, monkeypatch, apply_profile_ranges):
    monkeypatch.setattr("analytics.log_insights.environment.load_signal_catalog", lambda: {
        "SO1": {"min_val": 0, "max_val": 1, "invalid_values": [-1]},
        "Flow H": {"min_val": 0, "max_val": 100},
    })
    sources = []
    for day, text in [
        (18, "Time|SO1|Flow H|\n23:59:58|0.1|20|\nTime|SO1|Flow H|\n23:59:59|-1||\n"),
        (19, "Time|Flow H|SO1|Other|\n00:00:00|30|2|raw|\n00:00:01|40|\n"),
    ]:
        path = tmp_path / f"{day}.07.2026_sensors.log"
        path.write_text(text)
        sources.append(SimpleNamespace(
            path=str(path), checksum=sha256(path.read_bytes()).hexdigest(),
            classification=SimpleNamespace(family="sensors_log"),
        ))
    diagnostics = Counter()
    rows = list(sensor_samples(
        sources[::-1], {"SO1": {}, "Absent": {}, "Flow H": {}}, diagnostics,
        apply_profile_ranges=apply_profile_ranges,
    ))

    assert [row[0] - rows[0][0] for row in rows] == [0, 1, 2, 3]
    assert [row[1] for row in rows] == [
        {"SO1": 0.1, "Flow H": 20},
        {"SO1": None, "Flow H": None},
        {"SO1": None if apply_profile_ranges else 2, "Flow H": 30},
        {"SO1": None, "Flow H": 40},
    ]
    assert all(list(row[1]) == ["SO1", "Flow H"] for row in rows)
    assert diagnostics["repeated_headers"] == 1
    assert diagnostics["total_rows"] == 4
    assert diagnostics["malformed_rows"] == 1
    assert diagnostics["invalid_cells:SO1"] == (3 if apply_profile_ranges else 2)
    assert diagnostics["invalid_cells:Flow H"] == 1
    columns = list(diagnostics["source_columns"].values())
    assert [column[1]["column"] for column in columns] == ["SO1", "Flow H"]
