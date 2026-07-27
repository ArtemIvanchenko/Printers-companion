"""The dashboard template and its render context must stay in sync.

The two sides are joined only by string keys, so nothing used to notice when
they drifted. Seven values (the old EXPR2, EXPR5 and EXPR38…EXPR42) were being
computed on every page load after their placeholders had been deleted from the
markup, and a placeholder with no matching key renders as the literal
``{!name!}`` on the page rather than failing.
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "web_templates" / "dashboard.html"
_ROUTE = _ROOT / "api" / "routes" / "dashboard.py"

_PLACEHOLDER = re.compile(r"\{!(\w+)!\}")


def _template_keys() -> set[str]:
    return set(_PLACEHOLDER.findall(_TEMPLATE.read_text(encoding="utf-8")))


def _context_keys() -> set[str]:
    source = _ROUTE.read_text(encoding="utf-8")
    start = source.index("    ctx = {")
    end = source.index("    return HTMLResponse", start)
    return set(re.findall(r'^\s+"(\w+)":', source[start:end], re.M))


def test_every_placeholder_has_a_context_value():
    missing = _template_keys() - _context_keys()
    assert not missing, (
        f"placeholders with no context key render literally on the page: {sorted(missing)}"
    )


def test_no_context_value_is_computed_for_nothing():
    unused = _context_keys() - _template_keys()
    assert not unused, (
        f"context keys absent from the template — computed on every page load "
        f"and thrown away: {sorted(unused)}"
    )


def test_keys_are_named_not_numbered():
    """Guards the readability fix itself: EXPR0…EXPR75 with gaps meant changing
    a chart started by working out which number fed it."""
    numbered = [k for k in _template_keys() | _context_keys() if k.startswith("EXPR")]
    assert not numbered, f"positional placeholder names are back: {sorted(numbered)}"


@pytest.mark.parametrize("key", sorted(_template_keys()))
def test_placeholder_names_are_readable(key):
    assert re.fullmatch(r"[a-z][a-z0-9_]*", key), f"{key} is not snake_case"
