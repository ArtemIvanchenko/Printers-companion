import pytest

from parsers.base.base import ParserContext
from parsers.formats.monitor100_log import Monitor100LogParser, Monitor200LogParser


@pytest.mark.parametrize("parser_class", [Monitor100LogParser, Monitor200LogParser])
@pytest.mark.parametrize("lines,code", [
    ("12:00:02 |S|1|1|YLR1_KEY|\n12:00:01 |S|1|0|YLR1_KEY|\n",
     "monitor_out_of_order_timestamps"),
    ("x12:00:02 |S|1|1|YLR1_KEY|\n", "monitor_prefixed_records_recovered"),
])
def test_monitor_warning_is_not_reported_as_clean_quality(tmp_path, parser_class, lines, code):
    path = tmp_path / "06.04.2026_Monitor200.log"
    path.write_text(lines)
    result = parser_class().parse(path, ParserContext())

    assert [diagnostic.code for diagnostic in result.diagnostics] == [code]
    assert result.data_quality == ["partial_recovery"]
    assert len(result.events) == len(lines.splitlines())
    assert all(event.event_type == "monitor_state_change" for event in result.events)
    assert all(event.ts is not None for event in result.events)
    assert [event.source.source_line for event in result.events] == list(range(1, len(result.events) + 1))
    if code == "monitor_out_of_order_timestamps":
        assert result.events[1].ts < result.events[0].ts
        assert result.metadata["out_of_order_timestamps"] == 1
    else:
        assert result.events[0].payload["recovered_prefix"] == "x"
        assert result.metadata["prefixed_records_recovered"] == 1


@pytest.mark.parametrize("parser_class", [Monitor100LogParser, Monitor200LogParser])
def test_monitor_normal_midnight_remains_clean(tmp_path, parser_class):
    path = tmp_path / "06.04.2026_Monitor200.log"
    path.write_text("23:59:59 |S|1|1|YLR1_KEY|\n00:00:01 |S|1|0|YLR1_KEY|\n")
    result = parser_class().parse(path, ParserContext())
    assert result.data_quality == ["ok"]
    assert result.diagnostics == []
    assert result.metadata["midnight_rollovers"] == 1
    assert (result.events[1].ts - result.events[0].ts).total_seconds() == 2
