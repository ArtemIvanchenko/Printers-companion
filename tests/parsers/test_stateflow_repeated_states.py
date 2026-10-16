"""RLE fast path preserves the existing parse contract, including NaN."""

from datetime import datetime
from hashlib import sha256
import json
import math

import pytest

from parsers.base.base import ParserContext
from parsers.formats import stateflow_log


CASES = [
    ("named_unknown", "Time|State|Mystery||\r\n"
     "23:59:58|1|armed|\r\n23:59:59|1.0|armed|\r\n23:59:59|1.0|armed|\r\n"
     "Time|State|Mystery||\r\n00:00:01|1.0|armed|\r\n00:00:03|2|held|\r\n",
     "bb25a5f74da3737bc766241771d725bba06787372e9f9dbb4709abd32983bc59"),
    ("explicit_dates", "Timestamp;State;Mystery\n"
     "2026-06-09 23:59:58;1;armed\n2026-06-09 23:59:59;1;armed\n"
     "2026-06-08 00:00:01;1;armed\n2026-02-31 00:00:01;1;armed\n"
     "2026-06-10 00:00:03;2;held\n",
     "bc170589b45d64b362f80308f66c9ddd25e7afde0822b8e6de1a8d3c5dae118a"),
    ("headerless", "23:59:59 1 armed\n23:59:59 1 armed\n00:00:01 1 armed\n",
     "eda93ab33a2863f0ddb654fc9646cfe043d3d9c290967e0a9728d19f9d2c6f5f"),
    ("nan_state", "Time;State;Mystery\n10:00:00;1;NaN\n"
     "10:00:01;1;NaN\n10:00:02;1;NaN\n",
     "0baaffdc6ec8299301183781a408ed3c442679558dfdcacf4063d73491811ad4"),
]


def parse_text(tmp_path, text):
    path = tmp_path / "08.06.2026_stateFlow.log"
    path.write_bytes(text.encode("utf-8"))
    context = ParserContext(profile_id="m350", profile_version="0.1.0", source_file_id="rle-source")
    return stateflow_log.StateFlowLogParser().parse(path, context)


@pytest.mark.parametrize("case,text,expected_sha", CASES, ids=[case[0] for case in CASES])
def test_repeated_state_full_json_matches_pre_refactor_contract(tmp_path, case, text, expected_sha):
    result = parse_text(tmp_path, text)
    # Pydantic's established JSON projection turns nonfinite values into null;
    # the separate NaN test below also checks the actual transition payloads.
    projection = json.loads(result.model_dump_json())
    # The bounded-diagnostics contract intentionally bumps the parser version.
    # The explicit header counter is tested separately; all original physical,
    # row and diagnostic JSON remains identical.
    projection["parser_version"] = "0.2.1"
    projection["metadata"].pop("repeated_headers")
    for transition in projection["transitions"]:
        transition["parser_version"] = "0.2.1"
    payload = json.dumps(projection, sort_keys=True, ensure_ascii=False)
    assert sha256(payload.encode()).hexdigest() == expected_sha


def test_exact_state_cells_are_coerced_only_when_they_change(tmp_path, monkeypatch):
    calls = []
    original = stateflow_log.coerce_value
    def counted(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(stateflow_log, "coerce_value", counted)
    result = parse_text(tmp_path, "Time;State;Mystery\n"
        "10:00:00;1;armed\n10:00:01;1;armed\n10:00:02;1;armed\n10:00:03;2;held\n")
    assert result.metadata["row_count"] == result.metadata["parsed_timestamps"] == 4
    [transition] = result.transitions
    assert transition.changed_columns == ["State", "Mystery"]
    assert transition.previous_state == {"State": 1, "Mystery": "armed"}
    assert transition.new_state == {"State": 2, "Mystery": "held"}
    assert len(calls) == 6


def test_numeric_spelling_change_does_not_move_transition_start_or_raw_excerpt(tmp_path):
    first = "10:00:00;1;armed\n"
    header = "Time;State;Mystery\n"
    result = parse_text(tmp_path, header + first +
        "10:00:01;1.0;armed\n10:00:02;1.0;armed\n10:00:03;2;held\n")
    [transition] = result.transitions
    assert transition.ts_start == datetime(2026, 6, 8, 10)
    assert transition.ts_end == datetime(2026, 6, 8, 10, 0, 3)
    assert transition.duration_sec == 3
    assert transition.source_offset_start == len(header.encode())
    assert transition.raw_excerpt_sample == first.rstrip("\n")
    assert transition.previous_state["State"] == 1


def test_unnamed_trailing_header_slot_can_carry_real_unknown_state(tmp_path):
    result = parse_text(tmp_path, "Time|State||\n"
        "10:00:00|1|opaque-a|\n10:00:01|1|opaque-a|\n10:00:02|1|opaque-b|\n")
    assert result.metadata["malformed_rows"] == 0
    assert result.metadata["row_count"] == 3
    [transition] = result.transitions
    assert transition.changed_columns == ["col_2"]
    assert transition.previous_state == {"col_2": "opaque-a"}
    assert transition.new_state == {"col_2": "opaque-b"}
    assert transition.duration_sec == 2


def test_nan_state_retains_non_reflexive_transition_behavior(tmp_path):
    result = parse_text(tmp_path, CASES[-1][1])
    assert len(result.transitions) == 2
    assert all(item.changed_columns == ["Mystery"] for item in result.transitions)
    assert all(math.isnan(item.previous_state["Mystery"]) and math.isnan(item.new_state["Mystery"])
               for item in result.transitions)
    assert [item.duration_sec for item in result.transitions] == [1, 1]


def test_repeated_state_keeps_missing_timestamp_diagnostics_and_rollover(tmp_path):
    result = parse_text(tmp_path, "Time;State\n23:59:58;1\n" +
        "invalid-clock;1\n" * 25 + "00:00:01;1\n00:00:03;2\n")
    assert result.metadata["row_count"] == 28
    assert result.metadata["missing_timestamps"] == 25
    assert result.metadata["parsed_timestamps"] == 3
    assert result.metadata["midnight_rollovers"] == 1
    assert len([item for item in result.diagnostics if item.code == "stateflow_missing_timestamp"]) == 20
    aggregate = next(item for item in result.diagnostics if item.code == "stateflow_missing_timestamps_aggregated")
    assert aggregate.context == {"missing_timestamps": 25, "examples_retained": 20}
    [transition] = result.transitions
    assert transition.duration_sec == 5
    assert transition.ts_end == datetime(2026, 6, 9, 0, 0, 3)


def test_repeated_state_still_stops_at_existing_row_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(stateflow_log.StateFlowLogParser, "MAX_ROWS", 3)
    result = parse_text(tmp_path, "Time;State\n" + "10:00:00;1\n" * 10)
    assert result.metadata["row_count"] == result.metadata["parsed_timestamps"] == 3
    assert result.metadata["truncated"] is True
    assert not result.transitions
    capped = next(item for item in result.diagnostics if item.code == "stateflow_rows_capped")
    assert capped.source_line == 5
    assert capped.context == {"max_rows": 3, "transitions_found": 0}


def test_repeated_state_does_not_change_transition_cap_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(stateflow_log.StateFlowLogParser, "MAX_TRANSITIONS", 2)
    result = parse_text(tmp_path, "Time;State\n" + "".join(
        f"10:00:0{index};{state}\n" for index, state in enumerate([1, 1, 2, 2, 3, 3, 4])
    ))
    assert result.metadata["row_count"] == 5
    assert len(result.transitions) == 2
    assert result.metadata["truncated"] is True
    assert [item.duration_sec for item in result.transitions] == [2, 2]
    capped = next(item for item in result.diagnostics if item.code == "stateflow_transitions_capped")
    assert capped.source_line == 6
