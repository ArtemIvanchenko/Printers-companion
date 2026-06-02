"""Query planner — translates natural-language questions into structured query plans.

LLM sees ONLY the question (~50 tokens) + schema description. NEVER raw logs.
Output is a JSON query plan that the executor runs against the database.
"""

import json
import logging
from dataclasses import dataclass, field as dc_field
from typing import Any

from core.config.settings import get_settings

logger = logging.getLogger(__name__)

SCHEMA_DESCRIPTION = """
Available data entities and their fields:

1. sessions — print sessions
   Fields: session_id, start_ts, end_ts, classification, classification_confidence,
           material, powder_batch, gas_cylinder_id, duration_sec, pause_count,
           restart_attempt_count

2. operator_events — operator actions and observations
   Fields: event_id, timestamp, event_type, gas_cylinder_id, gas_type, material,
           powder_batch, component, action, value, unit, note, confidence,
           session_id, layer

   Common event_types: gas_cylinder_replaced, gas_consumption_recorded,
           gas_pressure_issue, powder_batch_changed, powder_consumption_recorded,
           powder_reused, powder_sieved, powder_dried, seal_replaced,
           filter_replaced, optics_cleaned, chamber_cleaned, recoater_adjusted,
           calibration_performed, restart_attempt, part_accepted, part_rejected,
           visual_defect_found, operator_observation

3. quality_outcomes — inspection results
   Fields: outcome_id, timestamp, session_id, inspection_type, result,
           defect_type, defect_location, severity, notes

   Common results: accepted, rejected, warning, unknown
   Common defect_types: porosity, lack_of_fusion, crack, warping,
           surface_defect, delamination, oxidation, powder_inclusion

4. import_jobs — log import history
   Fields: import_job_id, source_name, status, detected_at, confirmed_at,
           session_ids, report_ids

Actions you can request:
- count: count records
- sum: sum a numeric field
- avg: average of a numeric field
- max: maximum value
- min: minimum value
- list: list records with specific fields
- group_by: group records by a field and count
- compare: compare values across groups
- trend: show values over time

Filter operators:
- eq: equals
- contains: text contains
- gt/gte: greater than / greater or equal
- lt/lte: less than / less or equal
- in_last_n: last N sessions/days
- between: between two values

Return ONLY a JSON object. No explanations. No markdown.
"""

QUERY_PLAN_PROMPT = f"""You are a query planner for a metal 3D printer analytics system.
Translate the user's natural-language question into a structured query plan.

{SCHEMA_DESCRIPTION}

Rules:
- Use the exact field names listed above.
- If the question is ambiguous, make a reasonable guess.
- For time-based questions like "last 10 prints", use filter with in_last_n.
- For "how much X", use sum or count depending on context.
- For "which was the most/longest/biggest", use max + list.
- For "show me", use list.
- For "how many times", use count.

Return ONLY valid JSON matching this schema:
{{
  "entity": "sessions" | "operator_events" | "quality_outcomes" | "import_jobs",
  "action": "count" | "sum" | "avg" | "max" | "min" | "list" | "group_by" | "compare" | "trend",
  "field": "<field name or null>",
  "filters": [{{"field": "<field>", "operator": "<op>", "value": <value>}}],
  "group_by": "<field or null>",
  "order_by": "<field or null>",
  "order_dir": "asc" | "desc",
  "limit": <int or null>,
  "select_fields": ["<field1>", "<field2>"],
  "question_summary": "<brief summary of what user asked>"
}}

User question: {{question}}
"""


@dataclass
class QueryPlan:
    entity: str
    action: str
    field: str | None = None
    filters: list[dict[str, Any]] = dc_field(default_factory=list)
    group_by: str | None = None
    order_by: str | None = None
    order_dir: str = "desc"
    limit: int | None = None
    select_fields: list[str] = dc_field(default_factory=list)
    question_summary: str = ""


def _parse_plan_json(content: str) -> dict[str, Any]:
    """Strip an optional ```json fence and parse the model's JSON reply."""
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:].strip()
    return json.loads(content)


def _plan_from_raw(raw: dict[str, Any], question: str) -> QueryPlan:
    """Build a QueryPlan from the raw LLM dict (shared by sync + async paths)."""
    return QueryPlan(
        entity=raw.get("entity", "sessions"),
        action=raw.get("action", "list"),
        field=raw.get("field"),
        filters=raw.get("filters", []),
        group_by=raw.get("group_by"),
        order_by=raw.get("order_by"),
        order_dir=raw.get("order_dir", "desc"),
        limit=raw.get("limit"),
        select_fields=raw.get("select_fields", []),
        question_summary=raw.get("question_summary", question),
    )


def _build_query_plan(question: str) -> dict[str, Any] | None:
    """Use LLM to translate question into a query plan."""
    settings = get_settings()

    if settings.llm_provider == "null":
        logger.warning("LLM disabled, cannot plan query")
        return None

    from reporting.llm.providers.factory import get_llm_provider

    provider = get_llm_provider()
    prompt = QUERY_PLAN_PROMPT.format(question=question)

    import asyncio

    try:
        try:
            asyncio.get_running_loop()
            logger.warning("Cannot call LLM from async context - use async version instead")
            return None
        except RuntimeError:
            pass

        async def _call():
            return await provider.generate_markdown({"question": prompt})

        result = asyncio.run(_call())
        if not result or not result.success or not result.content:
            return None

        return _parse_plan_json(result.content)
    except Exception as exc:
        logger.exception("Query planning failed: %s", exc)
        return None


def plan_query(question: str) -> QueryPlan | None:
    """Translate a natural-language question into a QueryPlan."""
    raw = _build_query_plan(question)
    if not raw:
        return None
    return _plan_from_raw(raw, question)


async def plan_query_async(question: str) -> QueryPlan | None:
    """Async version of plan_query for use in async endpoints."""
    settings = get_settings()

    if settings.llm_provider == "null":
        logger.warning("LLM disabled, cannot plan query")
        return None

    from reporting.llm.providers.factory import get_llm_provider

    provider = get_llm_provider()
    prompt = QUERY_PLAN_PROMPT.format(question=question)

    try:
        result = await provider.generate_markdown({"question": prompt})
        if not result or not result.success or not result.content:
            return None

        raw = _parse_plan_json(result.content)
        return _plan_from_raw(raw, question)
    except Exception as exc:
        logger.exception("Async query planning failed: %s", exc)
        return None
