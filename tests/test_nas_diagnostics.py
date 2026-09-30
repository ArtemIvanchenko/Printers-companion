"""The browser-launched NAS audit must not execute env files or mutate storage."""
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/nas/diagnose-no-ssh.sh"


def _runner(tmp_path, *, docker_ok=True):
    project = tmp_path / "project with spaces"
    project.mkdir()
    (project / "postgres").mkdir()
    (project / "postgres/PG_VERSION").write_text("16\n")
    (project / "minio").mkdir()
    (project / "minio/keep").write_text("existing data\n")
    (project / "docker-compose.yml").write_text(
        "services:\n  minio:\n    image: minio/minio:old\n"
    )
    marker = tmp_path / "must-not-be-executed"
    (project / ".env").write_text(
        f'POSTGRES_PASSWORD=$(touch "{marker}")\n'
        'MINIO_ROOT_USER="private-admin"\n'
        'MINIO_ROOT_PASSWORD="private-secret"\n'
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    commands = tmp_path / "commands"
    scripts = {
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        "curl": '#!/bin/sh\nprintf "HTTP=200 "\n',
        "docker": (
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$DIAG_TEST_COMMANDS"\n'
            + ('exit 1\n' if not docker_ok else '''case "$1" in
version) echo 24.0 ;;
compose) case "$2" in version) echo 2.20 ;; esac ;;
ps) [ "$2" != -aq ] || echo abc123 ;;
inspect) echo 'project: status=running exit=0 oom=false health=healthy' ;;
*) exit 99 ;;
esac
''')
        ),
    }
    for name, content in scripts.items():
        path = tools / name
        path.write_text(content)
        path.chmod(0o700)

    def run():
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), str(project)],
            env=dict(os.environ, PATH=f'{tools}:{os.environ["PATH"]}',
                     DIAG_TEST_COMMANDS=str(commands)),
            text=True, capture_output=True, timeout=10,
        )
    return project, marker, commands, run


def test_report_never_executes_or_discloses_env_and_preserves_data(tmp_path):
    project, marker, commands, run = _runner(tmp_path)
    original = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    for _ in range(2):
        result = run()
        assert result.returncode == 0, result.stderr
    reports = list(project.glob("NAS-check-*"))
    assert len(reports) == 2
    assert not marker.exists()
    assert all(p.read_bytes() == content for p, content in original.items())
    for report in reports:
        text = report.read_text()
        assert "END. Not tested:" in text
        assert "Existing PostgreSQL major: 16" in text
        assert "old MinIO Docker Hub" in text
        assert "Compose validation exit=0" in text
        assert "MINIO_ROOT_PASSWORD: PRESENT (not authenticated)" in text
        assert "syntax error" not in text
        assert "private-secret" not in text
        assert "private-admin" not in text
        assert "must-not-be-executed" not in text
    calls = commands.read_text().splitlines()
    assert {call.split()[0] for call in calls} <= {"version", "compose", "ps", "inspect"}
    assert all("--format" in call for call in calls if call.startswith("inspect"))


def test_missing_env_and_docker_errors_produce_honest_report(tmp_path):
    project, marker, _, run = _runner(tmp_path, docker_ok=False)
    (project / ".env").unlink()
    result = run()
    assert result.returncode == 0
    report = next(project.glob("NAS-check-*")).read_text()
    assert ".env missing or unreadable" in report
    assert "Docker server unavailable" in report
    assert "Compose validation exit=1" in report
    assert "END. Not tested:" in report
    assert not marker.exists()


def test_duplicate_and_empty_keys_are_not_reported_as_present(tmp_path):
    project, _, _, run = _runner(tmp_path)
    (project / ".env").write_text(
        '# MINIO_ROOT_USER=commented\nPOSTGRES_PASSWORD=""\n'
        'MINIO_ROOT_PASSWORD=first\nMINIO_ROOT_PASSWORD=second\n'
    )
    assert run().returncode == 0
    report = next(project.glob("NAS-check-*")).read_text()
    assert "MINIO_ROOT_USER: MISSING/EMPTY" in report
    assert "POSTGRES_PASSWORD: MISSING/EMPTY" in report
    assert "MINIO_ROOT_PASSWORD: DUPLICATE" in report


def test_script_fits_dsm_paste_limit_and_has_valid_shell_syntax():
    assert len(SCRIPT.read_bytes()) < 4095
    subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=True)
