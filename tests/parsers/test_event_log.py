from pathlib import Path

from parsers.base.base import ParserContext
from parsers.formats.event_log import EventLogParser


def test_event_log_supports_cp1251_russian_and_layer(tmp_path: Path) -> None:
    path = tmp_path / "20260427.log"
    text = "2026-04-27 10:00:00 Старт печати\n2026-04-27 10:01:00 слой 12 LIR=0,45\n"
    path.write_bytes(text.encode("cp1251"))

    result = EventLogParser().parse(path, ParserContext(source_file_id="file_1"))

    assert len(result.events) == 2
    assert result.events[0].event_type == "start"
    assert result.events[1].layer == 12
    assert result.events[1].payload["vertical_position"] == 0.45

