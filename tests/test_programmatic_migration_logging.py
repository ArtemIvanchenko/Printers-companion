"""Programmatic upgrades preserve caller logging; CLI owns its own configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

_ROOT = Path(__file__).resolve().parents[1]
_STATE_PREFIX = "MIGRATION_LOGGING_STATE "

_UPGRADE_SCRIPT = r"""
import json
import logging
import logging.config
import os
import sqlite3

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from uvicorn.config import LOGGING_CONFIG

logging.config.dictConfig(LOGGING_CONFIG)

from core.logging.config import configure_logging
from core.logging.context import request_id_var
from storage.db.migrate import upgrade_to_head

app_logger = logging.getLogger("api.main")
logging.getLogger("preflight")
configure_logging("INFO")
request_id_var.set("migration-logging-request")


def snapshot():
    root = logging.getLogger()
    return {
        "root_level": root.level,
        "root_handlers": [
            {
                "id": id(handler),
                "type": type(handler).__name__,
                "formatter": type(handler.formatter).__name__,
                "filters": [id(item) for item in handler.filters],
                "closed": handler._closed,
            }
            for handler in root.handlers
        ],
        "loggers": {
            name: {
                "disabled": logging.getLogger(name).disabled,
                "level": logging.getLogger(name).level,
                "propagate": logging.getLogger(name).propagate,
                "handlers": [id(item) for item in logging.getLogger(name).handlers],
            }
            for name in ("api.main", "preflight", "storage.db.migrate",
                         "uvicorn", "uvicorn.error", "uvicorn.access")
        },
    }


states = [snapshot()]
app_logger.info("application before migration")
for attempt in (1, 2):
    upgrade_to_head()
    states.append(snapshot())
    app_logger.info("application after upgrade %s", attempt)
    app_logger.error("application error after upgrade %s", attempt)
    logging.getLogger("uvicorn.error").info("startup complete after upgrade %s", attempt)
    logging.getLogger("uvicorn.error").error("bind error after upgrade %s", attempt)

for handler in logging.getLogger().handlers:
    handler.flush()
scripts = ScriptDirectory.from_config(Config("alembic.ini"))
connection = sqlite3.connect(make_url(os.environ["DATABASE_URL"]).database)
try:
    installed = [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
finally:
    connection.close()
print("MIGRATION_LOGGING_STATE " + json.dumps({
    "states": states,
    "heads": scripts.get_heads(),
    "bases": scripts.get_bases(),
    "installed": installed,
}))
"""


def _isolated_environment(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": "sqlite:///" + str(tmp_path / "migrations.sqlite"),
        "NAS_OUTBOX_PATH": str(tmp_path / "outbox"),
        "OPERATOR_INSTANCE_FILE": str(tmp_path / "identity" / "instance-id"),
        "LOG_DIR": str(tmp_path / "logs"),
        "COMPUTE_NODE_ID": "migration-logging-test",
        "STARTUP_IMPORT_ENABLED": "false",
        "LLM_PROVIDER": "null",
        "MINIO_ENDPOINT": "127.0.0.1:1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_programmatic_cold_and_repeat_upgrade_preserve_logging(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _UPGRADE_SCRIPT],
        cwd=_ROOT, env=_isolated_environment(tmp_path),
        text=True, capture_output=True, check=True, timeout=30,
    )
    report = json.loads(next(
        line.removeprefix(_STATE_PREFIX) for line in result.stdout.splitlines()
        if line.startswith(_STATE_PREFIX)
    ))
    before, first, repeated = report["states"]
    assert before == first == repeated
    assert before["root_level"] == 20  # logging.INFO, without configuring the parent.
    assert [item["type"] for item in before["root_handlers"]] == [
        "StreamHandler", "RotatingFileHandler",
    ]
    assert all(
        item["formatter"] == "StructuredFormatter" and not item["closed"]
        for item in before["root_handlers"]
    )
    assert all(not item["disabled"] for item in before["loggers"].values())
    assert len(report["heads"]) == len(report["bases"]) == 1
    assert report["installed"] == report["heads"]

    entries = [
        json.loads(line)
        for line in (tmp_path / "logs" / "app.log").read_text(encoding="utf-8").splitlines()
    ]
    messages = {entry["msg"]: entry for entry in entries}
    for marker in (
        "application before migration",
        "application after upgrade 1", "application error after upgrade 1",
        "application after upgrade 2", "application error after upgrade 2",
        "alembic: database upgraded to head",
    ):
        assert messages[marker]["request_id"] == "migration-logging-request"
    assert any(entry["logger"] == "alembic.runtime.migration" for entry in entries)
    for attempt in (1, 2):
        assert f"startup complete after upgrade {attempt}" in result.stderr
        assert f"bind error after upgrade {attempt}" in result.stderr


def test_cli_cold_and_repeat_upgrade_keep_generic_logging(tmp_path):
    env = _isolated_environment(tmp_path)
    upgrades = [
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_ROOT, env=env, text=True, capture_output=True, check=True, timeout=30,
        )
        for _ in range(2)
    ]
    for result in upgrades:
        assert "INFO  [alembic.runtime.migration] Context impl SQLiteImpl." in result.stderr
        assert '"logger": "alembic.runtime.migration"' not in result.stdout + result.stderr
    assert "Running upgrade  -> 0001_initial_metadata" in upgrades[0].stderr
    assert "Running upgrade" not in upgrades[1].stderr

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=_ROOT, env=env, text=True, capture_output=True, check=True, timeout=30,
    )
    heads = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1
    connection = sqlite3.connect(tmp_path / "migrations.sqlite")
    try:
        installed = [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
    finally:
        connection.close()
    assert installed == heads
