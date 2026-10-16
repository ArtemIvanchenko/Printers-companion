from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from core.config.settings import get_settings


def test_execution_counter_migration_preserves_existing_import_state(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'import-migration.sqlite'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    previous = "0021_layer_cycle_models"
    head = "0022_import_execution_attempts"
    assert ScriptDirectory.from_config(config).get_revision(head).down_revision == previous
    command.upgrade(config, previous)

    engine = sa.create_engine(url)
    try:
        legacy = sa.Table("import_jobs", sa.MetaData(), autoload_with=engine)
        assert "execution_attempts" not in legacy.c
        now = datetime.now(timezone.utc)
        with engine.begin() as db:
            db.execute(legacy.insert().values(
                import_job_id="existing-import",
                owner_node_id="operator-01",
                source_path="/local/logs",
                source_name="logs",
                source_kind="folder",
                status="postponed",
                detected_at=now,
                updated_at=now,
                confirmed_by="operator",
                lease_generation=9,
                stability_check_attempts=4,
                file_snapshot={"job.log": {"size": 4}},
                checksum_manifest={"job.log": "a" * 64},
                source_objects={},
                session_ids=[],
                report_ids=[],
                missing_context_questions=[],
                notification_log=[],
                audit_trail=[{"action": "existing"}],
            ))
            before = dict(db.execute(sa.select(legacy)).mappings().one())

        command.upgrade(config, head)
        migrated = sa.Table("import_jobs", sa.MetaData(), autoload_with=engine)
        column = next(c for c in sa.inspect(engine).get_columns("import_jobs") if c["name"] == "execution_attempts")
        assert not column["nullable"]
        with engine.begin() as db:
            after = dict(db.execute(sa.select(migrated)).mappings().one())
            assert after.pop("execution_attempts") == 0
            assert after == before
            db.execute(migrated.update().values(execution_attempts=6))

        # A database that has already applied the revision keeps its budget.
        command.upgrade(config, head)
        with engine.connect() as db:
            assert db.scalar(sa.select(migrated.c.execution_attempts)) == 6
            assert db.scalar(sa.text("SELECT version_num FROM alembic_version")) == head

        command.downgrade(config, previous)
        assert "execution_attempts" not in {c["name"] for c in sa.inspect(engine).get_columns("import_jobs")}
        with engine.connect() as db:
            assert dict(db.execute(sa.select(legacy)).mappings().one()) == before
        command.upgrade(config, head)
        with engine.connect() as db:
            assert db.scalar(sa.select(migrated.c.execution_attempts)) == 0
    finally:
        engine.dispose()
