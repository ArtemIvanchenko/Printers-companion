"""Pin print cards and parsed sessions to their originating operator PC.

Revision ID: 0019_domain_compute_affinity
Revises: 0018_import_detection_index
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_domain_compute_affinity"
down_revision = "0018_import_detection_index"
branch_labels = None
depends_on = None

_LEGACY_OWNER = "legacy-unassigned"


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _backfill_session_owners() -> None:
    """Use an import-job owner only when ownership is unambiguous."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("import_jobs"):
        return
    required = {"owner_node_id", "session_ids"}
    if not required.issubset(_columns("import_jobs")):
        return

    if bind.dialect.name == "postgresql":
        op.execute(sa.text("""
            WITH candidates AS (
                SELECT sid.value AS session_id,
                       MIN(j.owner_node_id) AS owner_node_id,
                       COUNT(DISTINCT j.owner_node_id) AS owner_count
                  FROM import_jobs AS j
                  CROSS JOIN LATERAL json_array_elements_text(
                      COALESCE(j.session_ids, '[]'::json)
                  ) AS sid(value)
                 WHERE j.owner_node_id <> 'legacy-unassigned'
                 GROUP BY sid.value
            )
            UPDATE sessions AS s
               SET origin_compute_node_id = c.owner_node_id
              FROM candidates AS c
             WHERE s.session_id = c.session_id
               AND s.origin_compute_node_id = 'legacy-unassigned'
               AND c.owner_count = 1
        """))
    elif bind.dialect.name == "sqlite":
        # JSON1 ships with supported SQLite builds. Invalid legacy values are
        # treated as an empty list instead of aborting the whole migration.
        op.execute(sa.text("""
            WITH candidates AS (
                SELECT sid.value AS session_id,
                       MIN(j.owner_node_id) AS owner_node_id,
                       COUNT(DISTINCT j.owner_node_id) AS owner_count
                  FROM import_jobs AS j
                  JOIN json_each(
                      CASE WHEN json_valid(j.session_ids) THEN j.session_ids ELSE '[]' END
                  ) AS sid
                 WHERE j.owner_node_id <> 'legacy-unassigned'
                 GROUP BY sid.value
            )
            UPDATE sessions
               SET origin_compute_node_id = (
                   SELECT c.owner_node_id
                     FROM candidates AS c
                    WHERE c.session_id = sessions.session_id
                      AND c.owner_count = 1
               )
             WHERE origin_compute_node_id = 'legacy-unassigned'
               AND EXISTS (
                   SELECT 1 FROM candidates AS c
                    WHERE c.session_id = sessions.session_id
                      AND c.owner_count = 1
               )
        """))


def _backfill_print_owners() -> None:
    """Prefer explicit import ownership, then the already-owned linked session."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("import_jobs"):
        columns = _columns("import_jobs")
        if {"owner_node_id", "print_record_id"}.issubset(columns):
            if bind.dialect.name == "postgresql":
                op.execute(sa.text("""
                    WITH candidates AS (
                        SELECT print_record_id,
                               MIN(owner_node_id) AS owner_node_id,
                               COUNT(DISTINCT owner_node_id) AS owner_count
                          FROM import_jobs
                         WHERE print_record_id IS NOT NULL
                           AND owner_node_id <> 'legacy-unassigned'
                         GROUP BY print_record_id
                    )
                    UPDATE print_records AS p
                       SET origin_compute_node_id = c.owner_node_id
                      FROM candidates AS c
                     WHERE p.record_id = c.print_record_id
                       AND p.origin_compute_node_id = 'legacy-unassigned'
                       AND c.owner_count = 1
                """))
            else:
                op.execute(sa.text("""
                    WITH candidates AS (
                        SELECT print_record_id,
                               MIN(owner_node_id) AS owner_node_id,
                               COUNT(DISTINCT owner_node_id) AS owner_count
                          FROM import_jobs
                         WHERE print_record_id IS NOT NULL
                           AND owner_node_id <> 'legacy-unassigned'
                         GROUP BY print_record_id
                    )
                    UPDATE print_records
                       SET origin_compute_node_id = (
                           SELECT c.owner_node_id FROM candidates AS c
                            WHERE c.print_record_id = print_records.record_id
                              AND c.owner_count = 1
                       )
                     WHERE origin_compute_node_id = 'legacy-unassigned'
                       AND EXISTS (
                           SELECT 1 FROM candidates AS c
                            WHERE c.print_record_id = print_records.record_id
                              AND c.owner_count = 1
                       )
                """))

    # A card and its linked log session describe the same physical print. Once
    # the session owner is known, inheriting it is deterministic.
    op.execute(sa.text("""
        UPDATE print_records
           SET origin_compute_node_id = (
               SELECT s.origin_compute_node_id
                 FROM sessions AS s
                WHERE s.session_id = print_records.session_id
           )
         WHERE origin_compute_node_id = 'legacy-unassigned'
           AND session_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM sessions AS s
                WHERE s.session_id = print_records.session_id
                  AND s.origin_compute_node_id <> 'legacy-unassigned'
           )
    """))

    # An existing estimate/reanalysis job also provides safe provenance, but
    # only if every job for that entity names the same owner.
    if inspector.has_table("background_jobs"):
        columns = _columns("background_jobs")
        if {"owner_node_id", "entity_id", "entity_type"}.issubset(columns):
            op.execute(sa.text("""
                WITH candidates AS (
                    SELECT entity_id,
                           MIN(owner_node_id) AS owner_node_id,
                           COUNT(DISTINCT owner_node_id) AS owner_count
                      FROM background_jobs
                     WHERE entity_type = 'print_record'
                       AND owner_node_id <> 'legacy-unassigned'
                     GROUP BY entity_id
                )
                UPDATE print_records
                   SET origin_compute_node_id = (
                       SELECT c.owner_node_id FROM candidates AS c
                        WHERE c.entity_id = print_records.record_id
                          AND c.owner_count = 1
                   )
                 WHERE origin_compute_node_id = 'legacy-unassigned'
                   AND EXISTS (
                       SELECT 1 FROM candidates AS c
                        WHERE c.entity_id = print_records.record_id
                          AND c.owner_count = 1
                   )
            """))


def _backfill_single_known_owner() -> None:
    """Adopt untouched legacy rows only for a provably single-PC database.

    A database with no provenance, or with more than one historical owner,
    remains fail-closed under ``legacy-unassigned`` and requires an explicit
    administrator assignment.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    owners: set[str] = set()
    for table in ("import_jobs", "background_jobs"):
        if not inspector.has_table(table) or "owner_node_id" not in _columns(table):
            continue
        owners.update(
            str(value)
            for value in bind.scalars(
                sa.text(
                    f"SELECT DISTINCT owner_node_id FROM {table} "
                    "WHERE owner_node_id IS NOT NULL "
                    "AND owner_node_id <> 'legacy-unassigned'"
                )
            )
        )
    if len(owners) != 1:
        return
    owner = next(iter(owners))
    bind.execute(
        sa.text("""
            UPDATE sessions
               SET origin_compute_node_id = :owner
             WHERE origin_compute_node_id = 'legacy-unassigned'
        """),
        {"owner": owner},
    )
    bind.execute(
        sa.text("""
            UPDATE print_records
               SET origin_compute_node_id = :owner
             WHERE origin_compute_node_id = 'legacy-unassigned'
        """),
        {"owner": owner},
    )


def _assert_unique_session_links() -> None:
    """Refuse to discard ambiguous historical links during an upgrade."""
    rows = op.get_bind().execute(sa.text("""
        SELECT session_id, record_id
          FROM print_records
         WHERE session_id IN (
             SELECT session_id
               FROM print_records
              WHERE session_id IS NOT NULL
              GROUP BY session_id
             HAVING COUNT(*) > 1
         )
         ORDER BY session_id, created_at, record_id
         LIMIT 50
    """)).mappings().all()
    if rows:
        examples = ", ".join(
            f"{row['session_id']}->{row['record_id']}" for row in rows
        )
        raise RuntimeError(
            "Cannot enforce one-card-per-session: duplicate historical links "
            f"must be resolved manually first (up to 50 shown): {examples}"
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sessions"):
        if "origin_compute_node_id" not in _columns("sessions"):
            op.add_column(
                "sessions",
                sa.Column(
                    "origin_compute_node_id",
                    sa.String(length=80),
                    nullable=False,
                    server_default=_LEGACY_OWNER,
                ),
            )
        _backfill_session_owners()
        if "ix_sessions_origin_compute_node_id" not in _indexes("sessions"):
            op.create_index(
                "ix_sessions_origin_compute_node_id",
                "sessions",
                ["origin_compute_node_id"],
            )

    if inspector.has_table("print_records"):
        if "origin_compute_node_id" not in _columns("print_records"):
            op.add_column(
                "print_records",
                sa.Column(
                    "origin_compute_node_id",
                    sa.String(length=80),
                    nullable=False,
                    server_default=_LEGACY_OWNER,
                ),
            )
        _backfill_print_owners()
        if inspector.has_table("sessions"):
            _backfill_single_known_owner()
        _assert_unique_session_links()
        indexes = _indexes("print_records")
        if "ix_print_records_origin_compute_node_id" not in indexes:
            op.create_index(
                "ix_print_records_origin_compute_node_id",
                "print_records",
                ["origin_compute_node_id"],
            )
        if "ux_print_records_session_id" not in indexes:
            op.create_index(
                "ux_print_records_session_id",
                "print_records",
                ["session_id"],
                unique=True,
                postgresql_where=sa.text("session_id IS NOT NULL"),
                sqlite_where=sa.text("session_id IS NOT NULL"),
            )

    # SQLite cannot drop a column default without rebuilding the table; it is
    # test-only. PostgreSQL must require explicit ownership for every new row.
    if op.get_bind().dialect.name != "sqlite":
        if inspector.has_table("sessions"):
            op.alter_column(
                "sessions",
                "origin_compute_node_id",
                existing_type=sa.String(length=80),
                server_default=None,
            )
        if inspector.has_table("print_records"):
            op.alter_column(
                "print_records",
                "origin_compute_node_id",
                existing_type=sa.String(length=80),
                server_default=None,
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("print_records"):
        indexes = _indexes("print_records")
        if "ux_print_records_session_id" in indexes:
            op.drop_index("ux_print_records_session_id", table_name="print_records")
        if "ix_print_records_origin_compute_node_id" in indexes:
            op.drop_index(
                "ix_print_records_origin_compute_node_id",
                table_name="print_records",
            )
        if "origin_compute_node_id" in _columns("print_records"):
            op.drop_column("print_records", "origin_compute_node_id")

    if inspector.has_table("sessions"):
        if "ix_sessions_origin_compute_node_id" in _indexes("sessions"):
            op.drop_index(
                "ix_sessions_origin_compute_node_id",
                table_name="sessions",
            )
        if "origin_compute_node_id" in _columns("sessions"):
            op.drop_column("sessions", "origin_compute_node_id")
