"""MCP server for Printer Log Analytics platform.

Provides database query tools, API access, and file inspection
so the AI assistant can query real platform data during debugging.
"""

import json as _json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.config.settings import get_settings
from storage.db.session import SessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

settings = get_settings()

mcp = FastMCP(
    "Printer Log Analytics MCP",
    instructions="Direct data access for Printer Log Analytics platform",
    host="0.0.0.0",
    port=8100,
)


def get_db() -> Session:
    return SessionLocal()


def rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in rows]


def _query(sql: str, params: dict[str, Any] | None = None) -> str:
    try:
        db = get_db()
        try:
            result = db.execute(text(sql), params or {})
            if result.returns_rows:
                data = rows_to_dicts(result.fetchall())
            else:
                db.commit()
                data = [{"affected_rows": result.rowcount}]
        finally:
            db.close()
        return _json.dumps(data, default=str, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def query_database(sql: str) -> str:
    """Execute a raw SQL query against the platform database and return JSON.

    Tables: import_jobs, sessions, source_files, canonical_events,
            operator_events, operator_journal_entries, quality_outcomes,
            anomalies, reports, pattern_insights, confirmed_knowledge,
            notification_outbox, printers, printer_profiles, build_jobs,
            parts, segments, state_transitions, parse_diagnostics,
            layer_snapshots, causal_links, hypotheses
    """
    return _query(sql)


@mcp.tool()
def get_platform_stats() -> str:
    """Get overall platform statistics: counts of all major entities."""
    sql = """
    SELECT
      (SELECT COUNT(*) FROM import_jobs) AS import_jobs,
      (SELECT COUNT(*) FROM sessions) AS sessions,
      (SELECT COUNT(*) FROM source_files) AS source_files,
      (SELECT COUNT(*) FROM canonical_events) AS canonical_events,
      (SELECT COUNT(*) FROM operator_events) AS operator_events,
      (SELECT COUNT(*) FROM quality_outcomes) AS quality_outcomes,
      (SELECT COUNT(*) FROM anomalies) AS anomalies,
      (SELECT COUNT(*) FROM reports) AS reports,
      (SELECT COUNT(*) FROM pattern_insights) AS insights,
      (SELECT COUNT(*) FROM confirmed_knowledge) AS knowledge,
      (SELECT COUNT(*) FROM printers) AS printers,
      (SELECT COUNT(*) FROM build_jobs) AS build_jobs,
      (SELECT COUNT(*) FROM state_transitions) AS state_transitions,
      (SELECT COUNT(*) FROM notification_outbox) AS notifications
    """
    return _query(sql)


@mcp.tool()
def get_import_jobs(status: str | None = None, limit: int = 50, offset: int = 0) -> str:
    """List import jobs, optionally filtered by status.  
    Status: detected, awaiting_operator_confirmation, checking_stability,  
    postponed, processing, importing, analyzing, reporting, done, failed, ignored"""
    where = "WHERE j.status = :status" if status else ""
    return _query(
        f"""
        SELECT j.import_job_id, j.source_name, j.source_path, j.source_kind,
               j.status, j.detected_at, j.updated_at, j.confirmed_by,
               j.confirmed_at, j.error, j.session_ids, j.report_ids
        FROM import_jobs j {where}
        ORDER BY j.detected_at DESC LIMIT :limit OFFSET :offset
        """,
        {"status": status, "limit": limit, "offset": offset} if status else {"limit": limit, "offset": offset},
    )


@mcp.tool()
def get_import_job(import_job_id: str) -> str:
    """Get full details of a single import job by ID."""
    return _query("SELECT * FROM import_jobs WHERE import_job_id = :id", {"id": import_job_id})


@mcp.tool()
def get_sessions(limit: int = 50, offset: int = 0) -> str:
    """List all sessions with basic info."""
    return _query(
        """
        SELECT session_id, printer_id, profile_id, start_ts, end_ts,
               classification, classification_confidence, status,
               created_at, updated_at
        FROM sessions ORDER BY created_at DESC LIMIT :limit OFFSET :offset
        """,
        {"limit": limit, "offset": offset},
    )


@mcp.tool()
def get_session(session_id: str) -> str:
    """Get full session details including context (runtime_payload)."""
    return _query(
        """
        SELECT session_id, printer_id, profile_id, start_ts, end_ts,
               classification, classification_confidence, grouping_confidence,
               status, context, analysis_version, created_at, updated_at
        FROM sessions WHERE session_id = :id
        """,
        {"id": session_id},
    )


@mcp.tool()
def get_events(session_id: str, limit: int = 100, offset: int = 0) -> str:
    """Get canonical (parsed) events for a session."""
    return _query(
        """
        SELECT event_id, ts, raw_timestamp, layer, event_type, subsystem,
               phase, severity, confidence, source_file_id, source_line,
               raw_excerpt, evidence_kind
        FROM canonical_events
        WHERE session_id = :session_id
        ORDER BY ts LIMIT :limit OFFSET :offset
        """,
        {"session_id": session_id, "limit": limit, "offset": offset},
    )


@mcp.tool()
def get_source_files(session_id: str) -> str:
    """Get source (log) files for a session."""
    return _query(
        """
        SELECT source_file_id, file_name, original_path, checksum, size_bytes,
               family, role, encoding, parse_status, data_quality_status,
               first_ts, last_ts, metadata_json
        FROM source_files WHERE session_id = :session_id
        ORDER BY created_at
        """,
        {"session_id": session_id},
    )


@mcp.tool()
def get_operator_events(limit: int = 50, offset: int = 0) -> str:
    """List operator events (gas, powder, maintenance, observations)."""
    return _query(
        """
        SELECT event_id, timestamp, event_type, printer_id, session_id,
               material, powder_batch, gas_type, gas_cylinder_id,
               component, action, value, unit, note, confidence,
               verification_status, source_channel, created_by
        FROM operator_events
        ORDER BY timestamp DESC LIMIT :limit OFFSET :offset
        """,
        {"limit": limit, "offset": offset},
    )


@mcp.tool()
def get_operator_journal(limit: int = 50) -> str:
    """List operator journal entries (text notes, voice messages)."""
    return _query(
        """
        SELECT journal_entry_id, created_at, source_channel, created_by,
               entry_kind, raw_text, normalized_text, status,
               printer_id, session_id, project_id
        FROM operator_journal_entries
        ORDER BY created_at DESC LIMIT :limit
        """,
        {"limit": limit},
    )


@mcp.tool()
def get_quality_outcomes(limit: int = 50, offset: int = 0) -> str:
    """List quality inspection outcomes."""
    return _query(
        """
        SELECT outcome_id, session_id, build_id, part_id, timestamp,
               inspection_type, result, defect_type, defect_location,
               severity, notes, created_by
        FROM quality_outcomes
        ORDER BY timestamp DESC LIMIT :limit OFFSET :offset
        """,
        {"limit": limit, "offset": offset},
    )


@mcp.tool()
def get_anomalies(session_id: str | None = None, limit: int = 50) -> str:
    """List detected anomalies, optionally filtered by session."""
    if session_id:
        return _query(
            """
            SELECT anomaly_id, session_id, ts_start, ts_end,
                   layer_start, layer_end, anomaly_type, severity,
                   confidence, status
            FROM anomalies WHERE session_id = :session_id
            ORDER BY ts_start DESC LIMIT :limit
            """,
            {"session_id": session_id, "limit": limit},
        )
    return _query(
        """
        SELECT anomaly_id, session_id, ts_start, ts_end,
               layer_start, layer_end, anomaly_type, severity,
               confidence, status
        FROM anomalies ORDER BY ts_start DESC LIMIT :limit
        """,
        {"limit": limit},
    )


@mcp.tool()
def get_insights(limit: int = 50) -> str:
    """List pattern insights generated by analysis."""
    return _query(
        """
        SELECT insight_id, insight_type, title, description, status,
               confidence, sample_size, effect_size, generated_by,
               printer_id, supporting_sessions, created_at, updated_at
        FROM pattern_insights ORDER BY created_at DESC LIMIT :limit
        """,
        {"limit": limit},
    )


@mcp.tool()
def get_knowledge(limit: int = 50) -> str:
    """List confirmed knowledge entries (approved patterns/rules)."""
    return _query(
        """
        SELECT knowledge_id, title, description, status, confidence,
               confirmed_by, confirmed_at, printer_profile,
               applicable_materials, supporting_insights
        FROM confirmed_knowledge ORDER BY confirmed_at DESC LIMIT :limit
        """,
        {"limit": limit},
    )


@mcp.tool()
def get_reports(session_id: str) -> str:
    """List reports generated for a session."""
    return _query(
        """
        SELECT report_id, session_id, report_type, generated_at,
               generated_by, storage_uri
        FROM reports WHERE session_id = :session_id
        ORDER BY generated_at DESC
        """,
        {"session_id": session_id},
    )


@mcp.tool()
def get_report(report_id: str) -> str:
    """Get full report payload including timeline, segments, anomalies."""
    return _query("SELECT * FROM reports WHERE report_id = :id", {"id": report_id})


@mcp.tool()
def get_build_jobs(session_id: str | None = None, limit: int = 50) -> str:
    """List build/analysis jobs, optionally filtered by session."""
    if session_id:
        return _query(
            """
            SELECT build_id, session_id, job_name, recipe, layer_count, payload
            FROM build_jobs WHERE session_id = :session_id
            ORDER BY build_id LIMIT :limit
            """,
            {"session_id": session_id, "limit": limit},
        )
    return _query(
        """
        SELECT build_id, session_id, job_name, recipe, layer_count, payload
        FROM build_jobs ORDER BY build_id LIMIT :limit
        """,
        {"limit": limit},
    )


@mcp.tool()
def get_notifications(limit: int = 20) -> str:
    """List pending notifications in the outbox."""
    return _query(
        """
        SELECT notification_id, channel, text, status, created_at, sent_at, error
        FROM notification_outbox ORDER BY created_at DESC LIMIT :limit
        """,
        {"limit": limit},
    )


@mcp.tool()
def get_parse_diagnostics(session_id: str | None = None, limit: int = 100) -> str:
    """List parse diagnostics for debugging parser issues."""
    if session_id:
        return _query(
            """
            SELECT diagnostic_id, parser_name, parser_version, severity,
                   code, message, source_line, source_offset, created_at
            FROM parse_diagnostics WHERE session_id = :session_id
            ORDER BY created_at DESC LIMIT :limit
            """,
            {"session_id": session_id, "limit": limit},
        )
    return _query(
        """
        SELECT diagnostic_id, parser_name, parser_version, severity,
               code, message, source_line, source_offset, created_at
        FROM parse_diagnostics ORDER BY created_at DESC LIMIT :limit
        """,
        {"limit": limit},
    )


@mcp.tool()
def get_segments(session_id: str) -> str:
    """Get phase segments for a session."""
    return _query(
        """
        SELECT segment_id, phase, ts_start, ts_end, layer_start, layer_end, confidence
        FROM segments WHERE session_id = :session_id ORDER BY ts_start
        """,
        {"session_id": session_id},
    )


@mcp.tool()
def get_state_transitions(session_id: str, limit: int = 200) -> str:
    """Get state transitions for a session."""
    return _query(
        """
        SELECT transition_id, ts_start, ts_end, duration_sec,
               subsystem, changed_columns, parser_version
        FROM state_transitions WHERE session_id = :session_id
        ORDER BY ts_start LIMIT :limit
        """,
        {"session_id": session_id, "limit": limit},
    )


@mcp.tool()
def get_printers() -> str:
    """List all printers in the system."""
    return _query(
        """
        SELECT p.printer_id, p.name, p.vendor, p.model_family, p.serial_number,
               p.active, p.created_at
        FROM printers p ORDER BY p.created_at
        """
    )


@mcp.tool()
def call_api(endpoint: str, method: str = "GET", json_body: str | None = None) -> str:
    """Call a platform API endpoint.  
    Endpoints: /health, /imports, /sessions, /events/stats, etc.  
    json_body: optional JSON string for POST/PUT requests."""
    url = f"{settings.internal_api_url.rstrip('/')}{endpoint}"
    try:
        with httpx.Client(timeout=30) as client:
            body = _json.loads(json_body) if json_body else None
            resp = client.request(method, url, headers={"Content-Type": "application/json"}, json=body)
            return resp.text
    except Exception as e:
        return f"API call failed: {e}"


@mcp.tool()
def check_api_health() -> str:
    """Check if the API is healthy."""
    return call_api("/health")


def main() -> None:
    import sys
    if "--stdio" in sys.argv:
        logger.info("Starting MCP server with stdio transport")
        mcp.run(transport="stdio")
    else:
        logger.info("Starting MCP SSE server on 0.0.0.0:8100/sse")
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
