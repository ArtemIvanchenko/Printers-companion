"""WebSocket endpoint for real-time telemetry from an active print.

Tails the most recently modified sensors log in RAW_LOGS_CONTAINER_PATH.
If no live file is found, emits simulated data so the dashboard is always
demonstrable.  Clients receive JSON frames at ~2 s intervals:

    {"ts": "HH:MM:SS", "SO1": 0.12, "SO2": 0.11, "ST5": 168.3,
     "SP4": 1.002, "flow_H": 19.1, "alarm": false, "source": "live"|"sim"}
"""

import asyncio
import csv
import math
import os
import random
import re
import time
from pathlib import Path

import hmac

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core.config.settings import get_settings

from profiles.thresholds import ChartThresholds
from profiles.base.profile import load_yaml

router = APIRouter(tags=["realtime"])

# Load once at import — thresholds come from signals.yaml, not hardcoded.
_SIGNALS_PATH = Path(__file__).resolve().parent.parent.parent / "profiles" / "m350" / "signals.yaml"
_THRESHOLDS: ChartThresholds | None = None

def _get_thresholds() -> ChartThresholds:
    global _THRESHOLDS
    if _THRESHOLDS is None:
        raw = load_yaml(_SIGNALS_PATH)
        _THRESHOLDS = ChartThresholds(raw.get("signals", {}))
    return _THRESHOLDS

_LOG_DIR = Path(os.environ.get("RAW_LOGS_CONTAINER_PATH", "/mnt/raw_logs"))
_POLL_SEC = 2.0
_TAIL_LINES = 40          # read last N lines on each poll tick

# Column patterns in the pipe-delimited sensors log.
_PIPE = re.compile(r"\|")
_SENSOR_COLS = ["SO1", "SO2", "ST3", "ST4", "ST5", "SP4", "SF1",
                "ST1 (flow T)", "ST1 (flow H)", "Flow T", "Flow H", "Time"]


def _find_live_file() -> Path | None:
    """Return the sensors log modified most recently (within last 5 min)."""
    now = time.time()
    candidates = []
    for pat in ("*_sensors.log", "*.log"):
        candidates.extend(_LOG_DIR.glob(pat))
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if now - newest.stat().st_mtime < 300:      # live = touched in last 5 min
        return newest
    return None                                  # no active print → sim


def _tail(path: Path, n: int) -> list[str]:
    """Return the last n lines of a file efficiently."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            buf, pos = b"", max(0, size - n * 120)
            fh.seek(pos)
            buf = fh.read()
        lines = buf.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def _parse_sensor_line(line: str, header: list[str]) -> dict | None:
    """Parse one pipe-delimited sensor row into a dict."""
    if "|" not in line:
        return None
    try:
        cells = next(csv.reader([line], delimiter="|"))
        if len(cells) < 2:
            return None
    except Exception:
        return None
    row: dict = {}
    for i, col in enumerate(header):
        if i >= len(cells):
            break
        raw = cells[i].strip()
        try:
            row[col] = float(raw.replace(",", "."))
        except ValueError:
            row[col] = raw
    return row or None


def _extract_header(path: Path) -> list[str]:
    """Read the first non-empty line as the column header."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "|" in line:
                    cells = next(csv.reader([line.strip()], delimiter="|"))
                    if cells and cells[-1].strip() == "":
                        cells.pop()
                    return [c.strip() for c in cells]
    except OSError:
        pass
    return []


def _read_live(path: Path, header: list[str]) -> dict | None:
    """Return the latest parsed sensor row from a live log file."""
    lines = _tail(path, _TAIL_LINES)
    for line in reversed(lines):
        row = _parse_sensor_line(line, header)
        if row and any(k in row for k in ("SO1", "SO2", "ST5")):
            return row
    return None


# --- Simulation ---
_sim_phase = 0.0

def _sim_frame() -> dict:
    """Generate a realistic simulated sensor frame."""
    global _sim_phase
    _sim_phase += 0.05
    t = _sim_phase
    # Normally O2 ~0.1%, occasional spike to 1.5–3%
    o2_base = 0.10 + 0.04 * math.sin(t * 0.3)
    spike = 2.8 if (int(t * 10) % 200 == 0) else 0.0
    so1 = round(max(0.05, o2_base + spike + random.gauss(0, 0.01)), 2)
    so2 = round(max(0.05, o2_base * 1.05 + spike * 0.95 + random.gauss(0, 0.01)), 2)
    st5 = round(165 + 5 * math.sin(t * 0.07) + random.gauss(0, 0.3), 1)
    sp4 = round(1.001 + 0.002 * math.sin(t * 0.15) + random.gauss(0, 0.0005), 4)
    flow_h = round(18 + 2 * math.sin(t * 0.2) + random.gauss(0, 0.2), 1)
    now_sec = int(time.time()) % 86400
    ts = f"{now_sec//3600:02d}:{(now_sec%3600)//60:02d}:{now_sec%60:02d}"
    t = _get_thresholds()
    alarm = so1 > t.oxygen_alarm_high or so2 > t.oxygen_alarm_high or st5 > t.temp_alarm_high
    return {"ts": ts, "SO1": so1, "SO2": so2, "ST5": st5, "SP4": sp4,
            "Flow_H": flow_h, "alarm": alarm, "source": "sim"}


@router.websocket("/ws/realtime")
async def realtime_telemetry(ws: WebSocket, token: str | None = Query(default=None)):
    # Live telemetry is sensitive; require the service token (passed as a query
    # param since browser WebSocket clients cannot set request headers).
    expected = get_settings().api_service_token
    if not token or not hmac.compare_digest(token, expected):
        await ws.close(code=1008)  # 1008 = policy violation
        return
    await ws.accept()
    header: list[str] = []
    live_path: Path | None = None

    try:
        while True:
            # Re-check for live file each tick (print may start/stop)
            candidate = _find_live_file()
            if candidate and candidate != live_path:
                live_path = candidate
                header = _extract_header(live_path)

            if live_path and header:
                row = _read_live(live_path, header)
                if row:
                    t = _get_thresholds()
                    frame = {
                        "ts":     row.get("Time", ""),
                        "SO1":    row.get("SO1"),
                        "SO2":    row.get("SO2"),
                        "ST5":    row.get("ST5"),
                        "ST3":    row.get("ST3"),
                        "SP4":    row.get("SP4"),
                        "Flow_H": row.get("ST1 (flow H)") or row.get("Flow H"),
                        "source": "live",
                        "alarm":  any([
                            (row.get("SO1") or 0) > t.oxygen_alarm_high,
                            (row.get("SO2") or 0) > t.oxygen_alarm_high,
                            (row.get("ST5") or 0) > t.temp_alarm_high,
                        ]),
                    }
                    await ws.send_json(frame)
                    await asyncio.sleep(_POLL_SEC)
                    continue

            # No live file → simulation
            await ws.send_json(_sim_frame())
            await asyncio.sleep(_POLL_SEC)

    except WebSocketDisconnect:
        pass
