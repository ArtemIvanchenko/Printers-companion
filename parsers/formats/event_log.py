"""Parser for the main event log (*YYYY.MM.DD*.log).

Line format:
  HH:MM:SS <message>

Key message types (real log examples):
  "Ожидание старта прожига"                              → wait_for_burn
  "Старт прожига"                                        → burn_start
  "Старт  Ноль = -45850"                                 → session_start
  "-45878 2 Прожиг"                                      → burn_event (pos=-45878, layer=2)
  "Отсыпка слоя c опусканием, текущая позиция стола N"   → layer_pour (with_descent=True)
  "Отсыпка слоя без опускания, текущая позиция стола N"  → layer_pour (with_descent=False)
  "Перемещение ракеля"                                   → recoater_move
  "ПАУЗА"                                                → pause

Order of checks matters: most-specific patterns before broad keyword matches so
"Ожидание старта прожига" is NOT classified as "start", etc.
"""
import re
from datetime import timedelta
from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseDiagnosticRecord, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token

# "-45878 2 Прожиг" — platform position, layer number, keyword
_BURN_LINE_RE = re.compile(r"(-?\d+)\s+(\d+)\s+Прожиг", re.IGNORECASE)

# "текущая позиция стола -45880"
_TABLE_POS_RE = re.compile(r"текущая\s+позиция\s+стола\s*(-?\d+)", re.IGNORECASE)

# "Отсыпка слоя c/без опускания"
_LAYER_POUR_RE = re.compile(r"Отсыпка\s+слоя\s+(c|без)\s+опуск", re.IGNORECASE)

# "Старт  Ноль = -45850"
_SESSION_START_RE = re.compile(r"Старт\s+Ноль\s*=\s*(-?\d+)", re.IGNORECASE)

# Fallback: "layer N" / "слой N" in other contexts
_LAYER_REF_RE = re.compile(r"(?:layer|слой)\s*[:=#-]?\s*(\d+)", re.IGNORECASE)

# "LIR vertical z" position in other events
_VERTICAL_RE = re.compile(r"(?:LIR|vertical|z|позици[яи])\s*[:= ]\s*([-+]?\d+(?:[.,]\d+)?)", re.IGNORECASE)


def classify_event(text: str) -> tuple[str, str | None, dict]:
    """Return (event_type, phase, extra_payload) for a log line.

    Checks are ordered most-specific → least-specific to avoid misclassification.
    """
    lower = text.lower()

    # ── high-priority specific phrases ───────────────────────────────────────
    if "ожидание" in lower and "прожиг" in lower:
        return "wait_for_burn", None, {}

    pour_m = _LAYER_POUR_RE.search(text)
    if pour_m:
        with_descent = pour_m.group(1).lower() == "c"
        pos_m = _TABLE_POS_RE.search(text)
        extra: dict = {"with_descent": with_descent}
        if pos_m:
            extra["table_position"] = int(pos_m.group(1))
        return "layer_pour", "pour", extra

    burn_m = _BURN_LINE_RE.search(text)
    if burn_m:
        return "burn_event", "burn", {
            "table_position": int(burn_m.group(1)),
            "layer_from_text": int(burn_m.group(2)),
        }

    if "старт прожига" in lower or "start burn" in lower:
        return "burn_start", "burn", {}

    if "перемещение ракеля" in lower:
        return "recoater_move", None, {}

    sess_m = _SESSION_START_RE.search(text)
    if sess_m:
        return "session_start", "init", {"zero_position": int(sess_m.group(1))}

    # ── generic keyword checks ────────────────────────────────────────────────
    if any(t in lower for t in ("pause", "пауза", "останов")):
        return "pause", "pause", {}
    if any(t in lower for t in ("resume", "продолж", "возобнов")):
        return "resume", "restart_attempts", {}
    if any(t in lower for t in ("restart", "перезапуск", "рестарт")):
        return "restart_attempt", "restart_attempts", {}
    if any(t in lower for t in ("finish", "end", "заверш", "конец")):
        return "finish", "finish", {}
    if any(t in lower for t in ("burn", "спек", "плав", "прожиг")):
        return "burn_event", "burn", {}
    if any(t in lower for t in ("start", "старт", "начал")):
        return "start", "init", {}

    if _LAYER_REF_RE.search(text):
        return "layer_reference", None, {}

    return "log_message", None, {}


class EventLogParser(BaseParser):
    name = "main_event_log"
    version = "0.2.0"
    file_family = SourceFileFamily.main_event_log
    role = FileRole.primary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        events: list[CanonicalEventDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        missing_ts = 0
        day_shift = 0
        prev_ts = None

        for line_no, offset, line in iter_text_lines(path, encoding):
            if not line.strip():
                continue
            ts, raw_ts, uncertainty = parse_timestamp_token(line, date_hint)
            if ts is not None:
                candidate = ts + timedelta(days=day_shift)
                if prev_ts is not None and candidate + timedelta(hours=12) < prev_ts:
                    day_shift += 1
                    candidate = ts + timedelta(days=day_shift)
                ts = candidate
                prev_ts = ts

            event_type, phase, extra = classify_event(line)

            payload: dict = {"raw_text": line, **extra}

            # Layer number: try burn_line layer first, then fallback LAYER_RE
            layer: int | None = None
            if "layer_from_text" in extra:
                layer = extra.pop("layer_from_text")
                payload.pop("layer_from_text", None)
            else:
                lr = _LAYER_REF_RE.search(line)
                if lr:
                    layer = int(lr.group(1))

            # Vertical position from other patterns (non-burn lines)
            if "table_position" not in extra:
                vm = _VERTICAL_RE.search(line)
                if vm:
                    payload["vertical_position"] = float(vm.group(1).replace(",", "."))

            if ts is None:
                missing_ts += 1

            events.append(CanonicalEventDraft(
                ts=ts,
                raw_timestamp=raw_ts,
                ts_uncertainty=uncertainty,
                layer=layer,
                source=SourceLocation(
                    source_file_id=context.source_file_id,
                    source_line=line_no,
                    source_offset=offset,
                    raw_excerpt=line[:500],
                ),
                subsystem="operator_panel",
                phase=phase,
                event_type=event_type,
                confidence=0.75 if ts is None else 0.95,
                payload=payload,
            ))

        if missing_ts:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="missing_timestamps",
                message=f"{missing_ts} event-log rows had no parseable timestamp.",
                context={"count": missing_ts},
            ))

        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            diagnostics=diagnostics,
            data_quality=["partial_recovery"] if missing_ts else ["ok"],
            metadata={"encoding": encoding, "line_count": len(events)},
        )
