"""Dashboard must escape user-supplied data to prevent XSS."""
from api.routes.dashboard import _gas_table_rows, _js_json, _quality_table_rows, _session_table_rows


def test_quality_rows_escape_user_fields() -> None:
    rows = _quality_table_rows([{
        "result": "<script>alert(1)</script>",
        "defect_type": "<img src=x onerror=alert(2)>",
        "session_id": "s1",
        "timestamp": "2026-01-01",
    }])
    assert "<script>" not in rows
    assert "&lt;script&gt;" in rows
    # The <img> tag is neutralized: '<' is escaped, so the onerror handler is inert text.
    assert "<img" not in rows
    assert "&lt;img" in rows


def test_session_rows_escape_user_fields() -> None:
    rows = _session_table_rows([{
        "id": "<b>x</b>", "date": "<i>d</i>", "type": "REAL_PRINT",
        "first_time": "<u>a</u>", "last_time": "b", "duration_min": 5,
        "total_lines": 10, "pause_count": 0,
    }])
    assert "<b>" not in rows and "<i>" not in rows and "<u>" not in rows
    assert "&lt;b&gt;" in rows


def test_gas_rows_escape_user_fields() -> None:
    rows = _gas_table_rows([{"timestamp": "<x>", "value": "<y>"}])
    assert "<x>" not in rows and "<y>" not in rows


def test_js_json_prevents_script_breakout() -> None:
    out = _js_json(["</script><script>alert(3)</script>"])
    assert "</script>" not in out
    assert "<\\/script>" in out
