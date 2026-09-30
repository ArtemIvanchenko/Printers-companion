"""A SQL-free dashboard shell and bounded, on-demand historical panels."""
import html
import json
from pathlib import Path
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from domain.services.dashboard_reads import read_history, read_telemetry_page
from profiles.signal_catalog import signal_labels_ru

router = APIRouter(tags=["dashboard"])
_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _ROOT / "web_templates" / "dashboard.html"
_ASSET_ROOT = _ROOT / "web_assets"


@router.get("/assets/{asset_name:path}", include_in_schema=False)
def catalogue_script(asset_name: str):
    path = (_ASSET_ROOT / asset_name).resolve()
    if not path.is_relative_to(_ASSET_ROOT.resolve()) or not path.is_file():
        raise HTTPException(404, "Asset not found")
    media_type = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}.get(path.suffix)
    if media_type is None:
        raise HTTPException(404, "Asset not found")
    return FileResponse(path, media_type=media_type)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def _js_json(obj) -> str:
    return json.dumps(obj).replace("</", "<\\/")


def _load_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _render_template(context: dict) -> str:
    def replace(match):
        if match.group(1) not in context:
            raise ValueError(f"Missing dashboard context: {match.group(1)}")
        return str(context[match.group(1)])
    return re.sub(r"\{!(\w+)!\}", replace, _load_template())


def _quality_table_rows(quality: list) -> str:
    if not quality:
        return '<tr><td colspan="4">Нет данных о качестве</td></tr>'
    return "".join(
        "<tr>" + "".join(f"<td>{esc(value)}</td>" for value in (
            str(q.get("timestamp") or "—")[:10],
            {"accepted": "Годная", "rejected": "Брак", "unknown": "Неизвестно"}.get(q.get("result"), q.get("result") or "—"),
            q.get("defect_type") or "—", q.get("session_id") or "—",
        )) + "</tr>" for q in quality
    )


def _session_table_rows(sessions: list) -> str:
    if not sessions:
        return '<tr><td colspan="8">Нет данных о сессиях</td></tr>'
    return "".join(
        "<tr>" + "".join(f"<td>{esc(value if value is not None else '—')}</td>" for value in (
            s["id"], s["date"], s["type"],
            f"{s.get('first_time') or '—'} — {s.get('last_time') or '—'}",
            s.get("duration_min"), s.get("data_quality_score"),
            s.get("total_lines"), s.get("pause_count"),
        )) + "</tr>" for s in sessions
    )


def _gas_table_rows(events: list) -> str:
    if not events:
        return '<tr><td colspan="3">Нет данных о расходе</td></tr>'
    return "".join(
        "<tr>" + "".join(f"<td>{esc(value if value is not None else '—')}</td>" for value in (
            str(e.get("timestamp") or "—")[:10],
            e.get("value") if e.get("event_type") != "powder_consumption_recorded" else None,
            e.get("value") if e.get("event_type") == "powder_consumption_recorded" else None,
        )) + "</tr>" for e in events
    )


@router.get("/dashboard/history/{panel}")
def dashboard_history(
    panel: Literal["sessions", "timeline", "quality", "consumption"],
    skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
) -> dict:
    data = read_history(panel, skip=skip, limit=limit)
    # Rendering happens after the read-model releases its SQL connection.
    renderer = {"sessions": _session_table_rows, "timeline": _session_table_rows,
                "quality": _quality_table_rows, "consumption": _gas_table_rows}[panel]
    return {**data, "table_rows": renderer(data["items"])}


@router.get("/", response_class=HTMLResponse)
def dashboard():
    """Render the operator shell without consulting PostgreSQL or MinIO.

    Cards load through their existing paginated API; historical panels request
    their own compact read models only when the operator opens those panels.
    """
    from profiles.m350.profile import get_profile
    from profiles.thresholds import load_thresholds
    profile = get_profile()
    thresholds = load_thresholds(profile)
    machine_info = esc(profile.model_family)
    if profile.serial_number:
        machine_info += " &nbsp;·&nbsp; s/n " + esc(profile.serial_number)
    ctx = {
        "machine_info": machine_info,
        "vendor": esc(profile.vendor),
        "dashboard_bootstrap": _js_json({"signal_labels": signal_labels_ru(), "thresholds": {
            "o2": thresholds.oxygen_alarm_high, "temp": thresholds.temp_alarm_high,
            "hum": thresholds.humidity_alarm_high, "press_high": thresholds.pressure_alarm_high,
            "press_low": thresholds.pressure_alarm_low,
        }}),
    }
    return HTMLResponse(_render_template(ctx))


@router.get("/dashboard/telemetry-sessions")
def dashboard_telemetry_sessions(
    skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
) -> dict:
    return read_telemetry_page(skip=skip, limit=limit)
