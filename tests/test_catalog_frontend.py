from pathlib import Path
import shutil
import subprocess

import pytest


def test_catalog_and_import_frontend_modules():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not available')
    result = subprocess.run([node, '--test', str(Path(__file__).parent / 'web/catalog-import.test.cjs')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_estimate_feedback_is_not_hidden_in_legacy_card_form():
    html = (Path(__file__).parents[1] / 'web_templates/dashboard.html').read_text()
    assert 'id="estimate-status" role="status"' in html
    source = html.split('function _setEstimateStatus(text)', 1)[1].split('async function', 1)[0]
    assert "getElementById('estimate-status')" in source
    assert 'textContent' in source
    assert 'pc-save-result' not in source


def test_frontend_modules_are_served_and_initialization_waits_for_state():
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as client:
        for name in ('catalog-pager.js', 'folder-import.js'):
            response = client.get('/assets/' + name)
            assert response.status_code == 200
            assert 'javascript' in response.headers['content-type']
        assert client.get('/assets/unknown.js').status_code == 404
    html = (Path(__file__).parents[1] / 'web_templates/dashboard.html').read_text()
    assert "document.addEventListener('DOMContentLoaded', () => loadHomeStats())" in html
