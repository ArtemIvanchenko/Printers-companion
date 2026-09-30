"""The dashboard template and its render context must stay in sync.

The two sides are joined only by string keys, so nothing used to notice when
they drifted. Aggregate values that belonged to the old landing-page charts
were being computed on every page load after their placeholders had been
deleted from the markup, and a placeholder with no matching key renders as the literal
``{!name!}`` on the page rather than failing.
"""
import re
import ast
from pathlib import Path

import pytest

from dashboard_test_support import dashboard_source

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "web_templates" / "dashboard.html"
_ROUTE = _ROOT / "api" / "routes" / "dashboard.py"

_PLACEHOLDER = re.compile(r"\{!(\w+)!\}")


def _template_keys() -> set[str]:
    return set(_PLACEHOLDER.findall(dashboard_source()))


def _context_keys() -> set[str]:
    module = ast.parse(_ROUTE.read_text(encoding="utf-8"))
    handler = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "dashboard")
    assignment = next(node for node in handler.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "ctx" for target in node.targets))
    return {key.value for key in assignment.value.keys}


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


def test_dashboard_script_parses():
    """Every static module parses, including their shared global bindings."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    html = _TEMPLATE.read_text(encoding="utf-8")
    names = re.findall(r'src="/assets/([^"]+\.js)"', html)
    scripts = [
        (_ROOT / "web_assets" / name).read_text(encoding="utf-8")
        for name in names if not name.startswith("vendor/")
    ]
    assert scripts, "dashboard script composition is empty"
    assert not re.search(r"<script\s*>", html), "application JS must be static"
    for index, source in enumerate([*scripts, "\n".join(scripts)]):
        result = subprocess.run(
            [node, "--check"], input=source, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"dashboard JS #{index}: {result.stderr}"


def test_dashboard_does_not_mislabel_platform_position_as_liquid_level():
    html = dashboard_source()
    assert "Уровень жидкости" not in html


def test_dashboard_protects_shared_print_cards_from_stale_edits():
    html = dashboard_source()
    assert "expected_revision: _pcRecord.revision" in html
    assert "new EventSource('/prints/events')" in html
    assert "Карточка изменена на другом ПК" in html


def _javascript_function(html: str, name: str, next_name: str) -> str:
    start = html.index(f"async function {name}(")
    end = html.index(f"async function {next_name}(", start)
    return html[start:end]


def test_general_log_selection_is_sent_as_one_multipart_batch():
    html = dashboard_source()
    source = _javascript_function(html, "uploadFiles", "rescanFolder")

    assert "files.forEach(file => fd.append('files', file))" in source
    assert source.count("fetch('/upload/logs'") == 1
    assert "for (" not in source


def test_print_card_log_selection_is_sent_as_one_multipart_batch():
    html = dashboard_source()
    source = _javascript_function(html, "uploadArchiveLogs", "_pollRecordLink")

    assert "files.forEach(file => fd.append('files', file))" in source
    assert source.count("fetch(`/prints/${recordId}/import-logs`") == 1
    assert "for (" not in source


def test_print_attachment_upload_explains_deferred_nas_sync():
    html = dashboard_source()
    source = _javascript_function(html, "uploadArchiveFile", "previewArchiveStl")

    assert "result.queued" in source
    assert "будет отправлен на NAS автоматически" in source


def test_quality_correction_sends_the_current_final_verdict_id():
    html = dashboard_source()
    start = html.index("async function savePrintQualityOutcome(")
    end = html.index("function _renderPcProcess(", start)
    source = html[start:end]

    assert "find(item => item.is_final)" in source
    assert "payload.supersedes_outcome_id = latestFinalOutcome.outcome_id" in source


def test_dashboard_uses_explicit_motion_tokens_and_reduced_motion():
    html = dashboard_source()
    assert "transition: all" not in html
    assert "--ease-out: cubic-bezier(0.23, 1, 0.32, 1)" in html
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert "animation: alarm-pulse" not in html


def test_home_prioritizes_print_cards_over_aggregate_charts():
    html = dashboard_source()
    assert 'class="home-print-grid"' in html
    assert 'id="home-recent-sessions"' in html
    assert "_renderHomeStlPreview" in html
    assert "fetch('/prints?limit=100')" in html
    assert "linesChart" not in html
    assert "Строк логов по сессиям" not in html
    assert "materialsChart" not in html
    assert "typesChart" not in html
    assert "durationChart" not in html


def test_dashboard_uses_dark_handoff_shell_and_stl_preview():
    html = dashboard_source()
    assert "--bg-page: #201e1d" in html
    assert "--color-primary: #c67139" in html
    assert 'id="nav-add"' in html
    assert "home-add-card" in html
    assert "pc-preview-host" in html
    assert "home-print-card.needs-logs" in html


def test_handoff_secondary_screens_are_api_backed_and_complete():
    html = dashboard_source()
    for page in ("add", "estimate", "anomalies", "quality", "journal", "settings"):
        assert f'id="page-design-{page}"' in html
    assert "_designAccuracyChart" in html
    assert "аномалия вызвала дефект" not in html.lower()
    assert "паузы оператора не включены" in html
    assert "Это частота, а не доказательство причины дефекта" in html
    assert "saveDesignJournalEntry" in html
    assert "correction_locked" in html


def test_print_card_actions_target_current_record_and_camera_switches_are_real():
    html = dashboard_source()
    assert "uploadArchiveLogs('${rec.record_id}',this.files)" in html
    assert "uploadArchiveFile('${rec.record_id}',this.files[0])" in html
    assert "setCamera(['45', 'top', 'front'][buttonIndex])" in html
    assert "camera.position.set(0, 0, viewDistance)" in html
    assert "camera.position.set(0, -viewDistance, 0)" in html


def test_dashboard_dialogs_and_errors_are_non_blocking_and_accessible():
    html = dashboard_source()
    assert 'role="dialog" aria-modal="true"' in html
    assert 'id="app-toast-region"' in html
    assert "function showToast(" in html
    assert "alert(" not in html
