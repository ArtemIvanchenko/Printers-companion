"""HTML Dashboard with all analytics charts."""
import html
import json
import re
from collections import Counter
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.entities import OperatorEvent, QualityOutcome
from domain.models.sessions import BuildSession
from storage.db.session import session_scope

router = APIRouter(tags=["dashboard"])


def esc(value) -> str:
    """HTML-escape a value before interpolating it into dashboard markup.

    User-supplied fields (defect types, session ids, notes) flow into the
    template, so every interpolated value must be escaped to prevent XSS.
    """
    return html.escape(str(value), quote=True)


_dumps = json.dumps  # raw alias, so the hardened wrapper below isn't self-rewritten


def _js_json(obj) -> str:
    """json.dumps hardened for embedding inside a <script> block.

    Plain json.dumps does not escape '/', so a user string containing
    '</script>' would close the tag and allow script injection. Escaping '</'
    keeps the output valid JSON while making breakout impossible.
    """
    return _dumps(obj).replace("</", "<\\/")


def get_sessions_paginated(db: Session, skip: int = 0, limit: int = 100):
    """Get sessions with pagination to avoid loading all rows into memory."""
    stmt = select(BuildSession).order_by(BuildSession.start_ts.desc()).offset(skip).limit(limit)
    sessions = db.execute(stmt).scalars().all()
    
    result = []
    for s in sessions:
        ctx = s.context or {}
        rp = ctx.get("runtime_payload", {}) or {}
        group = rp.get("group", {}) or {}
        features = group.get("features", {})
        
        result.append({
            'id': s.session_id,
            'date': s.session_id.replace("session_", "") if s.session_id else "-",
            'type': group.get('classification', s.classification or '-'),
            'confidence': group.get('confidence', 0),
            'first_time': features.get('first_time', '-'),
            'last_time': features.get('last_time', '-'),
            'duration_min': features.get('duration_min', 0),
            'total_lines': features.get('total_lines', 0),
            'total_events': features.get('total_events', 0),
            'layers': features.get('layers', 0),
            'burn_events': features.get('burn_events', 0),
            'file_count': features.get('file_count', 0),
            'pause_count': features.get('pause_count', 0),
            'material': features.get('material', 'unknown'),
            'start_ts': s.start_ts.isoformat() if s.start_ts else None,
            'data_quality_score': (group.get('data_quality') or {}).get('score'),
            'data_quality_grade': (group.get('data_quality') or {}).get('grade'),
            'data_quality_issues': len((group.get('data_quality') or {}).get('issues', [])),
        })
    return result



def _coerce_float(value) -> float | None:
    try:
        return float(value) if value else None
    except (ValueError, TypeError):
        return None


def _get_consumption_events(
    db: Session, event_type: str, label_key: str, label_attr: str,
    skip: int = 0, limit: int = 1000,
):
    """Shared loader for gas/powder consumption events (they differ only by the
    event_type filter and the name of one provenance field)."""
    stmt = (
        select(OperatorEvent)
        .where(OperatorEvent.event_type == event_type)
        .order_by(OperatorEvent.timestamp.asc())
        .offset(skip)
        .limit(limit)
    )
    return [
        {
            'timestamp': e.timestamp.isoformat() if e.timestamp else None,
            'value': _coerce_float(e.value),
            label_key: getattr(e, label_attr),
            'session_id': e.session_id,
        }
        for e in db.execute(stmt).scalars().all()
    ]


def get_gas_events_paginated(db: Session, skip: int = 0, limit: int = 1000):
    """Get gas consumption events with pagination."""
    return _get_consumption_events(
        db, "gas_consumption_recorded", "cylinder", "gas_cylinder_id", skip, limit
    )


def get_powder_events_paginated(db: Session, skip: int = 0, limit: int = 1000):
    """Get powder consumption events with pagination."""
    return _get_consumption_events(
        db, "powder_consumption_recorded", "batch", "powder_batch", skip, limit
    )


def get_quality_paginated(db: Session, skip: int = 0, limit: int = 1000):
    """Get quality outcomes with pagination."""
    stmt = select(QualityOutcome).order_by(QualityOutcome.timestamp.asc()).offset(skip).limit(limit)
    outcomes = db.execute(stmt).scalars().all()
    result = []
    for q in outcomes:
        result.append({
            'timestamp': q.timestamp.isoformat() if q.timestamp else None,
            'result': q.result,
            'defect_type': q.defect_type,
            'session_id': q.session_id,
        })
    return result



_MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def get_latest_print_telemetry(db: Session):
    """Return (label, telemetry, health, start_ts) for the most data-rich REAL_PRINT session."""
    stmt = select(BuildSession).order_by(BuildSession.start_ts.desc()).limit(500)
    best_label, best_tel, best_health, best_start_ts, best_score = None, {}, {}, None, -1
    for s in db.execute(stmt).scalars().all():
        group = ((s.context or {}).get("runtime_payload", {}) or {}).get("group", {}) or {}
        if group.get("classification") != "REAL_PRINT":
            continue
        tel = group.get("telemetry") or {}
        if not tel.get("time"):
            continue
        score = len(tel.get("time", []))
        if score > best_score:
            best_label = s.session_id.replace("session_", "")
            best_tel = tel
            best_health = group.get("health") or {}
            best_start_ts = s.start_ts
            best_score = score
    return best_label, best_tel, best_health, best_start_ts


# ---- Template rendering ----

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "web_templates" / "dashboard.html"

def _load_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")

def _render_template(context: dict) -> str:
    import re
    html = _load_template()
    def _replacer(m):
        key = m.group(1)
        val = context.get(key)
        if val is None:
            return m.group(0)
        return str(val)
    return re.sub(r'\{!(\w+)!\}', _replacer, html)


# ---- Table row builders (split out of the template) ----

def _quality_table_rows(quality: list) -> str:
    if not quality:
        return '<tr><td colspan="4" style="text-align:center;color:#6b7280;">Нет данных о качестве</td></tr>'
    return "".join(
        f'''<tr>
                            <td>{esc(q.get('timestamp', '')[:10] if q.get('timestamp') else '-')}</td>
                            <td><span class="type-badge {'type-real' if q.get('result')=='accepted' else 'type-unknown'}">{esc(q.get('result', '-'))}</span></td>
                            <td>{esc(q.get('defect_type', '-') or '-')}</td>
                            <td>{esc(q.get('session_id', '-')[:20])}</td>
                        </tr>'''
        for q in quality[:20]
    )

def _data_quality_badge(score, issues: int) -> str:
    """Colored data-reliability badge: green ≥85, amber ≥60, red below."""
    if score is None:
        return '<span style="color:#4a5568;">—</span>'
    color = "#10b981" if score >= 85 else "#f59e0b" if score >= 60 else "#ef4444"
    title = f"{issues} проблем(ы) данных" if issues else "проблем не найдено"
    return (f'<span title="{title}" style="color:{color};font-weight:700;">{score}</span>'
            f'<span style="color:#4a5568;font-size:11px;"> {"⚠"+str(issues) if issues else ""}</span>')


def _session_table_rows(sessions: list) -> str:
    return "".join(
        f'''<tr>
                            <td>{esc(s['id'][:25])}...</td>
                            <td>{esc(s['date'])}</td>
                            <td><span class="type-badge {'type-real' if s['type']=='REAL_PRINT' else 'type-unknown'}">{esc(s['type'])}</span></td>
                            <td>{esc(s['first_time'])} - {esc(s['last_time'])}</td>
                            <td>{s['duration_min']} мин</td>
                            <td>{_data_quality_badge(s.get('data_quality_score'), s.get('data_quality_issues', 0))}</td>
                            <td>{s['total_lines']:,}</td>
                            <td>{s['pause_count']}</td>
                        </tr>'''
        for s in sessions
    )

def _gas_table_rows(gas_events: list) -> str:
    if not gas_events:
        return '<tr><td colspan="3" style="text-align:center;color:#6b7280;">Нет данных о расходе</td></tr>'
    return "".join(
        f'''<tr>
                            <td>{esc(e.get('timestamp', '')[:10] if e.get('timestamp') else '-')}</td>
                            <td>{esc(e.get('value', '-'))}</td>
                            <td>-</td>
                        </tr>'''
        for e in gas_events[:20]
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    """Dashboard endpoint that loads limited data for rendering."""
    from profiles.m350.profile import get_profile as _get_profile
    from profiles.thresholds import load_thresholds
    _profile = _get_profile()
    _thresholds = load_thresholds(_profile)
    _thr_js = _js_json(_thresholds.to_dict())
    machine_info = (
        f"{_profile.model_family} &nbsp;·&nbsp; "
        f"s/n {_profile.serial_number}" if _profile.serial_number else _profile.model_family
    )

    with session_scope() as db:
        # Load a limited amount of data for dashboard display (e.g., last 500 sessions)
        sessions = get_sessions_paginated(db, skip=0, limit=10_000)
        gas_events = get_gas_events_paginated(db, skip=0, limit=10_000)
        powder_events = get_powder_events_paginated(db, skip=0, limit=10_000)
        quality = get_quality_paginated(db, skip=0, limit=10_000)
        tel_label, telemetry, health, tel_start_ts = get_latest_print_telemetry(db)
    
    # Stats
    total = len(sessions)
    prints = len([s for s in sessions if s['type'] == 'REAL_PRINT'])
    hours = sum(s['duration_min'] for s in sessions) // 60
    lines = sum(s['total_lines'] for s in sessions)
    gas_total = sum(e['value'] for e in gas_events if e['value'])
    powder_total = sum(e['value'] for e in powder_events if e['value'])
    
    # Counts by category (Counter keeps first-seen order, like the old dicts)
    types = Counter(s['type'] for s in sessions)
    materials = Counter(s.get('material') or 'unknown' for s in sessions)
    quality_stats = Counter(q['result'] for q in quality)
    defects = Counter(q['defect_type'] for q in quality if q.get('defect_type'))

    # Duration per session
    durations = [s['duration_min'] for s in sessions if s['type'] == 'REAL_PRINT']
    dates_labels = [s['date'] for s in sessions if s['type'] == 'REAL_PRINT']

    # Pauses
    pauses = [s.get('pause_count', 0) for s in sessions]
    pause_labels = [s['date'] for s in sessions]
    
    # Pre-compute JS-safe color arrays (avoids undefined-variable ReferenceError in browser)
    duration_colors = _js_json(
        ["#10b981" if d > 500 else "#f59e0b" if d > 100 else "#60a5fa" for d in durations]
    )
    pause_colors = _js_json(
        ["#f59e0b" if p > 0 else "#60a5fa" for p in pauses]
    )

    # --- Process telemetry (decoded sensor series) for the latest real print ---
    tel_time = _js_json(telemetry.get("time", []))
    tel_oxygen = telemetry.get("oxygen", {})
    tel_temps = telemetry.get("temperatures", {})
    tel_humidity = telemetry.get("humidity", {})
    tel_pressure = telemetry.get("pressure", {})
    tel_burn = telemetry.get("layer_burn_times", [])
    tel_burn_labels = _js_json([b["layer"] for b in tel_burn])
    tel_burn_data = _js_json([b["duration_sec"] for b in tel_burn])
    has_telemetry = bool(telemetry.get("time"))
    if tel_label and tel_start_ts:
        _d = tel_start_ts.day
        _m = _MONTHS_RU[tel_start_ts.month - 1]
        _y = tel_start_ts.year
        _hm = tel_start_ts.strftime("%H:%M")
        tel_subtitle = f"{_d} {_m} {_y} · {_hm}"
    elif tel_label:
        tel_subtitle = tel_label
    else:
        tel_subtitle = "нет данных"
    tel_session_id = tel_label or ""

    # Build labelled datasets (canonical names from the signal dictionary).
    _sig_titles = {
        "SO1": "O₂ канал 1", "SO2": "O₂ канал 2",
        "ST3": "Камера (низ)", "ST4": "Камера (верх)", "ST5": "Стол",
        "SP4": "Давление камеры", "ST1 (flow H)": "Влажность", "Flow H": "Влажность",
    }
    _o2_colors = {"SO1": "#ef4444", "SO2": "#f59e0b"}
    _temp_colors = {"ST3": "#60a5fa", "ST4": "#8b5cf6", "ST5": "#10b981"}

    def _datasets(series, colors, fallback="#06b6d4"):
        out = []
        palette = ["#60a5fa", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4"]
        for i, (col, values) in enumerate(series.items()):
            out.append({
                "label": _sig_titles.get(col, col),
                "data": values,
                "borderColor": colors.get(col, palette[i % len(palette)]) if colors else fallback,
                "backgroundColor": "transparent",
                "borderWidth": 2,
                "pointRadius": 0,
                "tension": 0.3,
            })
        return out

    # Alarm threshold lines — injected as a flat dashed dataset per chart.
    # Values from M350 passport (САЦН.681749.002ПС) and TSF-400VAD sieve manual.
    def _alarm_dataset(label: str, value: float, color: str = "#ef4444") -> dict:
        return {
            "label": label,
            "data": [value] * max(len(telemetry.get("time", [])), 1),
            "borderColor": color,
            "borderWidth": 1.5,
            "borderDash": [6, 4],
            "pointRadius": 0,
            "backgroundColor": "transparent",
            "tension": 0,
            "fill": False,
        }

    _o2_alarms = [_alarm_dataset(f"Предел O₂ {_thresholds.oxygen_alarm_high}%", _thresholds.oxygen_alarm_high, "#ef4444")]
    _temp_alarms = [_alarm_dataset(f"Макс. платформа {_thresholds.temp_alarm_high:.0f}°C", _thresholds.temp_alarm_high, "#ef4444")]
    _hum_alarms = [_alarm_dataset(f"Порог влажности {_thresholds.humidity_alarm_high:.0f}%", _thresholds.humidity_alarm_high, "#f59e0b")]
    _press_alarms = [
        _alarm_dataset(f"Макс. {_thresholds.pressure_alarm_high} бар", _thresholds.pressure_alarm_high, "#ef4444"),
        _alarm_dataset(f"Норма {_thresholds.pressure_nominal} бар", _thresholds.pressure_nominal, "#10b981"),
    ]

    oxygen_datasets = _js_json(_datasets(tel_oxygen, _o2_colors) + _o2_alarms)
    temp_datasets = _js_json(_datasets(tel_temps, _temp_colors) + _temp_alarms)
    humidity_datasets = _js_json(_datasets(tel_humidity, {}, "#06b6d4") + _hum_alarms)
    pressure_datasets = _js_json(_datasets(tel_pressure, {}, "#a78bfa") + _press_alarms)

    # --- Alarm detection: check last N points of each series against thresholds ---
    # Returns True if ANY of the tail values exceeds the alarm threshold.
    _CHECK_TAIL = 10  # last 10 downsampled points (~last ~7% of session)

    def _series_in_alarm(series: dict, alarm_high: float | None = None,
                         alarm_low: float | None = None) -> bool:
        for values in series.values():
            tail = [v for v in values[-_CHECK_TAIL:] if isinstance(v, (int, float))]
            if not tail:
                continue
            if alarm_high is not None and max(tail) > alarm_high:
                return True
            if alarm_low is not None and min(tail) < alarm_low:
                return True
        return False

    alarm_o2   = _series_in_alarm(tel_oxygen,   alarm_high=_thresholds.oxygen_alarm_high)
    alarm_temp = _series_in_alarm(tel_temps,     alarm_high=_thresholds.temp_alarm_high)
    alarm_hum  = _series_in_alarm(tel_humidity,  alarm_high=_thresholds.humidity_alarm_high)
    alarm_press = _series_in_alarm(tel_pressure, alarm_high=_thresholds.pressure_alarm_high, alarm_low=_thresholds.pressure_alarm_low)

    # CSS class injected into chart-container divs
    _ac = lambda flag: ' alarm-active' if flag else ''
    _ex = lambda flag: '<span class="alarm-badge">!</span>' if flag else ''

    # --- Process-health panel (readiness score, anomalies, layer burn-time drift) ---
    readiness = (health or {}).get("readiness") or {}
    anomalies = (health or {}).get("anomalies") or []
    burn_drift = (health or {}).get("burn_drift") or {}
    score = readiness.get("score")
    grade = readiness.get("grade", "unknown")
    grade_color = {"good": "#10b981", "fair": "#f59e0b", "poor": "#ef4444"}.get(grade, "#6b7280")
    trend = burn_drift.get("trend", "—")
    trend_ru = {"rising": "↑ растёт", "falling": "↓ снижается", "stable": "→ стабильно",
                "insufficient_data": "нет данных"}.get(trend, trend)
    trend_color = {"rising": "#ef4444", "falling": "#10b981", "stable": "#60a5fa"}.get(trend, "#6b7280")
    sev_color = {"high": "#ef4444", "medium": "#f59e0b", "low": "#60a5fa"}
    if anomalies:
        anomaly_rows = "".join(
            f'<div style="padding:8px 12px;background:#2d3748;border-left:3px solid '
            f'{sev_color.get(a.get("severity"), "#6b7280")};border-radius:6px;margin-bottom:6px;font-size:13px;">'
            f'⚠ {a.get("detail", a.get("signal"))}</div>'
            for a in anomalies[:12]
        )
    else:
        anomaly_rows = '<div style="color:#10b981;font-size:13px;">✓ Аномалий процесса не обнаружено</div>'
    score_txt = f"{score:.0f}" if isinstance(score, (int, float)) else "—"
    health_panel = f"""
            <div class="stats" style="margin-bottom:20px;">
                <div class="stat-card">
                    <div class="value" style="color:{grade_color};">{score_txt}</div>
                    <div class="label">Готовность атмосферы (0–100)</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:{sev_color.get('high') if anomalies else '#10b981'};">{len(anomalies)}</div>
                    <div class="label">Аномалий процесса</div>
                </div>
                <div class="stat-card">
                    <div class="value" style="color:{trend_color};font-size:22px;">{trend_ru}</div>
                    <div class="label">Тренд времени прожига</div>
                </div>
            </div>
            <div class="section" style="margin-bottom:20px;">
                <h2>⚠️ Аномалии процесса</h2>
                <div style="margin-top:12px;">{anomaly_rows}</div>
            </div>"""

    # --- Template rendering ---
    ctx = {
        "EXPR0": machine_info,
        "EXPR1": _profile.vendor,
        "EXPR2": total,
        "EXPR3": prints,
        "EXPR4": hours,
        "EXPR5": f"{lines:,}",
        "EXPR6": f"{gas_total:.0f}",
        "EXPR7": f"{powder_total:.1f}",
        "EXPR8": tel_subtitle,
        "EXPR9": "" if has_telemetry else '<div class="section" style="text-align:center;color:#6b7280;">Нет данных телеметрии. Импортируйте логи реальной печати (burn/sensors).</div>',
        "EXPR10": health_panel if has_telemetry else "",
        "EXPR11": _ac(alarm_o2),
        "EXPR12": _ex(alarm_o2),
        "EXPR13": _ac(alarm_temp),
        "EXPR14": _ex(alarm_temp),
        "EXPR15": _ac(alarm_hum),
        "EXPR16": _ex(alarm_hum),
        "EXPR17": _ac(alarm_press),
        "EXPR18": _ex(alarm_press),
        "EXPR25": len(sessions),
        "EXPR38": _thresholds.oxygen_alarm_high,
        "EXPR39": f"{_thresholds.temp_alarm_high:.0f}",
        "EXPR40": _thresholds.pressure_nominal,
        "EXPR41": f"{_thresholds.humidity_alarm_high:.0f}",
        "EXPR42": _thr_js,
        "EXPR43": _js_json(list(types.keys())),
        "EXPR44": _js_json(list(types.values())),
        "EXPR45": _js_json(list(materials.keys())),
        "EXPR46": _js_json(list(materials.values())),
        "EXPR47": _js_json(dates_labels),
        "EXPR48": _js_json(durations),
        "EXPR49": duration_colors,
        "EXPR50": _js_json([s['date'] for s in sessions]),
        "EXPR51": _js_json([s['total_lines'] for s in sessions]),
        "EXPR52": _js_json(dates_labels),
        "EXPR53": _js_json([d/60 for d in durations]),
        "EXPR54": _js_json(pause_labels),
        "EXPR55": _js_json(pauses),
        "EXPR56": pause_colors,
        "EXPR57": _js_json([s['date'] for s in sessions]),
        "EXPR58": _js_json([s.get('burn_events', 0) for s in sessions]),
        "EXPR59": _js_json([s['date'] for s in sessions]),
        "EXPR60": _js_json([s['total_lines'] for s in sessions]),
        "EXPR61": _js_json(list(quality_stats.keys())),
        "EXPR62": _js_json(list(quality_stats.values())),
        "EXPR63": _js_json(list(defects.keys())),
        "EXPR64": _js_json(list(defects.values())),
        "EXPR65": _js_json([e.get('timestamp', '')[:10] if e.get('timestamp') else '-' for e in gas_events[:15]]),
        "EXPR66": _js_json([e.get('value', 0) for e in gas_events[:15]]),
        "EXPR67": _js_json([e.get('timestamp', '')[:10] if e.get('timestamp') else '-' for e in powder_events[:15]]),
        "EXPR68": _js_json([e.get('value', 0) for e in powder_events[:15]]),
        "EXPR69": tel_time,
        "EXPR70": oxygen_datasets,
        "EXPR71": temp_datasets,
        "EXPR72": humidity_datasets,
        "EXPR73": pressure_datasets,
        "EXPR74": tel_burn_labels,
        "EXPR75": tel_burn_data,
        "EXPR_QUALITY_ROWS": _quality_table_rows(quality),
        "EXPR_SESSION_ROWS": _session_table_rows(sessions),
        "EXPR_GAS_ROWS": _gas_table_rows(gas_events),
        "EXPR_TEL_SESSION_ID": _js_json(tel_session_id),
    }

    return HTMLResponse(_render_template(ctx))