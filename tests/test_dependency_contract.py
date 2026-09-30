"""Dependency declarations are reviewed once; txt/Docker projections cannot drift."""
from copy import deepcopy
import subprocess
import sys
import tomllib

import pytest

from scripts.export_requirements import ROOT, render_requirements


def project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_checked_in_requirements_are_exact_projections():
    for name, rendered in render_requirements(project()).items():
        assert (ROOT / name).read_text() == rendered


def test_container_pins_do_not_restrict_native_python_install():
    metadata = project()
    assert "scipy>=1.13" in metadata["project"]["dependencies"]
    assert "scikit-learn>=1.4" in metadata["project"]["dependencies"]
    rendered = render_requirements(metadata)
    assert "scipy==1.13.1\n" in rendered["requirements.analytics.txt"]
    assert "scikit-learn==1.4.2\n" in rendered["requirements.analytics.txt"]
    assert "ruff==0.15.16\n" in rendered["requirements.dev.txt"]
    assert "mcp>=1.0.0\n" in rendered["requirements.mcp.txt"]


@pytest.mark.parametrize("mistake,match", [
    ("undeclared", "Undeclared"), ("unassigned", "without an explicit tier"),
    ("duplicate", "belongs to both"), ("incompatible_pin", "contradicts"),
    ("marker_pin", "Cannot validate"), ("stale_pin", "without a declared"),
    ("typo_tier", "Unknown dependency tiers"),
])
def test_invalid_dependency_contract_fails_closed(mistake, match):
    metadata = deepcopy(project())
    tiers = metadata["tool"]["printer-companion"]["dependencies"]
    if mistake == "undeclared":
        tiers["base"].append("nonexistent-package")
    elif mistake == "unassigned":
        metadata["project"]["dependencies"].append("new-package>=1.0")
    elif mistake == "duplicate":
        tiers["heavy"].append("scipy")
    elif mistake == "incompatible_pin":
        tiers["pins"]["scipy"] = "1.0.0"
    elif mistake == "marker_pin":
        metadata["project"]["dependencies"].remove("scipy>=1.13")
        metadata["project"]["dependencies"].append('scipy>=1.13; python_version<"3.14"')
    elif mistake == "stale_pin":
        tiers["pins"]["removed-package"] = "1.2.3"
    else:
        tiers["analtyics"] = []
    with pytest.raises(ValueError, match=match):
        render_requirements(metadata)


def test_check_mode_never_rewrites_operator_files(tmp_path):
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    sentinel = tmp_path / "requirements.base.txt"
    sentinel.write_text("operator change\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/export_requirements.py"),
                             "--root", str(tmp_path), "--check"], capture_output=True, text=True)
    assert result.returncode == 1
    assert sentinel.read_text() == "operator change\n"
    assert not (tmp_path / "requirements.heavy.txt").exists()


def test_generate_and_check_are_network_free_and_repeatable(tmp_path):
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    command = [sys.executable, str(ROOT / "scripts/export_requirements.py"), "--root", str(tmp_path)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    files = {path.name: path.read_bytes() for path in tmp_path.glob("requirements.*.txt")}
    subprocess.run([*command, "--check"], check=True, capture_output=True, text=True)
    subprocess.run(command, check=True, capture_output=True, text=True)
    assert files == {path.name: path.read_bytes() for path in tmp_path.glob("requirements.*.txt")}


def test_docker_layers_consume_declared_tiers_and_check_staleness():
    expected = {"base": "base", "api": "heavy", "worker": "analytics",
                "scheduler": "analytics", "mcp": "mcp"}
    for image, tier in expected.items():
        assert f"-r requirements.{tier}.txt" in (ROOT / f"Dockerfile.{image}").read_text()
    assert "export_requirements.py --check" in (ROOT / "Dockerfile.base").read_text()
    assert "export_requirements.py --check" in (ROOT / ".github/workflows/ci.yml").read_text()


def test_worker_entrypoints_do_not_import_http_adapters():
    import ast

    forbidden = []
    for path in (ROOT / "worker").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            if any(name == "api" or name.startswith("api.") for name in names):
                forbidden.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not forbidden, "Workers must call use-case services, not HTTP: " + ", ".join(forbidden)
