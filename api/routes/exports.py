"""Database export endpoint — streams selected categories as a JSON file."""
import json
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from typing import Annotated

from storage.db.session import session_scope

router = APIRouter(prefix="/export", tags=["export"])

# Logical categories → list of DB tables.
# Order within a category matters for readability of the output file.
CATEGORY_TABLES: dict[str, list[str]] = {
    "operator": [
        "print_records",
        "print_record_files",
        "operator_events",
        "operator_journal_entries",
    ],
    "sessions": [
        "sessions",
        "source_files",
        "import_jobs",
        "canonical_events",
        "state_transitions",
        "layer_snapshots",
        "segments",
    ],
    "analytics": [
        "reports",
        "llm_runs",
        "analysis_versions",
        "historical_analysis_verdicts",
        "pattern_insights",
        "confirmed_knowledge",
        "hypotheses",
        "anomalies",
        "causal_links",
    ],
    "models": [
        "parts",
        "build_jobs",
        "build_plates",
        "part_placements",
        "layer_ranges",
        "attachments",
    ],
    "maintenance": [
        "maintenance_records",
        "component_state_timeline",
        "powder_preparation_events",
        "powder_usage_cycles",
        "gas_cylinders",
        "material_batches",
        "machine_params",
        "machine_presets",
        "quality_outcomes",
        "tolerance_rules",
    ],
}

ALL_CATEGORIES = list(CATEGORY_TABLES.keys())


def _serialize(value):
    """json.dumps default: make datetimes and other non-serializables into strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@router.get("/download")
def export_database(
    categories: Annotated[list[str], Query(alias="cat")] = ALL_CATEGORIES,
) -> StreamingResponse:
    """Export selected data categories as a downloadable JSON file.

    Query param ``cat`` can be repeated: ``?cat=operator&cat=sessions``.
    Defaults to all categories.
    """
    valid = [c for c in categories if c in CATEGORY_TABLES]
    if not valid:
        valid = ALL_CATEGORIES

    result: dict = {"_meta": {"exported_at": datetime.utcnow().isoformat(), "categories": valid}}

    with session_scope() as db:
        for cat in valid:
            result[cat] = {}
            for table in CATEGORY_TABLES[cat]:
                try:
                    rows = db.execute(text(f"SELECT * FROM {table}")).mappings().all()  # noqa: S608
                    result[cat][table] = [dict(r) for r in rows]
                except Exception:
                    result[cat][table] = []

    filename = f"printers_companion_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    content = json.dumps(result, ensure_ascii=False, indent=2, default=_serialize)

    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/categories")
def list_export_categories() -> dict:
    """Return available export categories with their table lists."""
    return {
        cat: {"tables": tables, "count": len(tables)}
        for cat, tables in CATEGORY_TABLES.items()
    }
