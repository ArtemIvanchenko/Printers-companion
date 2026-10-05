"""Detached, shared calibration evidence. No raw-file fallback or ORM in a fit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.prediction.timing_validation import (
    calibration_burn_ms, calibration_cycles_ms, calibration_timing_payloads, timing_components_ms,
)
from analytics.prediction.timing_snapshot import MANIFEST_KEY, read_timing_publication
from core.versioning.provenance import stable_hash
from domain.models.events import LayerSnapshot
from domain.models.prints import MachineParams, PrintRecord
from domain.models.sessions import BuildSession


@dataclass(frozen=True)
class CalibrationRecord:
    record_id: str
    session_id: str | None
    name: str
    material: str | None
    layer_thickness_mm: float | None
    metadata_json: dict[str, Any]
    revision: int
    created_at: datetime


@dataclass(frozen=True)
class CalibrationSession:
    session_id: str
    printer_id: str | None
    start_ts: datetime | None
    end_ts: datetime | None
    classification: str
    context: dict[str, Any]


@dataclass(frozen=True)
class CalibrationInputs:
    linked: list[tuple[CalibrationRecord, CalibrationSession]]
    timing_rows: list[tuple[str, int, Any]]
    params: dict[str, Any] | None
    timing_publications: dict[str, dict | None] = field(default_factory=dict)
    timing_row_tags: list[str | None] = field(default_factory=list)

    @cached_property
    def input_fingerprint(self) -> str:
        return stable_hash({
            "records": [(asdict(record), asdict(session)) for record, session in self.linked],
            "timings": self.timing_rows,
            "publications": self.timing_publications,
            "row_tags": self.timing_row_tags,
        })

    @cached_property
    def config_fingerprint(self) -> str:
        return stable_hash(self.params)

    @cached_property
    def timings(self) -> dict[str, dict[int, dict]]:
        """One admitted timing set per session, shared by the three projections."""
        grouped: dict[str, list[tuple]] = {sid: [] for sid, manifest in self.timing_publications.items()
                                         if manifest is not None}
        for index, (sid, layer, features) in enumerate(self.timing_rows):
            tag = self.timing_row_tags[index] if index < len(self.timing_row_tags) else None
            grouped.setdefault(sid, []).append((layer, features, tag))
        return {sid: calibration_timing_payloads(
                    read_timing_publication(rows, self.timing_publications.get(sid))[1] or [])
                for sid, rows in grouped.items()}

    @cached_property
    def components(self) -> dict[str, dict[int, tuple[float, float]]]:
        return {sid: {layer: (burn / 1000, pour / 1000)
                      for layer, (burn, pour) in timing_components_ms(timings).items()}
                for sid, timings in self.timings.items()}

    @cached_property
    def burns(self) -> dict[str, dict[int, float]]:
        return {sid: {layer: burn / 1000 for layer, burn in calibration_burn_ms(timings).items()}
                for sid, timings in self.timings.items()}

    @cached_property
    def cycles(self) -> dict[str, dict[int, tuple[float, float, float]]]:
        return {sid: calibration_cycles_ms(timings) for sid, timings in self.timings.items()}


def load_calibration_inputs(db: Session, *, for_update: bool = False) -> CalibrationInputs:
    """Copy only calibration columns. Caller closes the transaction before math.

    At publication, lock params and parent rows in deterministic order. Timing
    replacement takes the same session lock. Reading never rehydrates MinIO or
    a workstation's raw files; absent compact evidence is explicitly unknown.
    """
    def read(statement):
        return db.execute(statement.with_for_update() if for_update else statement).all()

    params_rows = read(select(*MachineParams.__table__.columns).where(MachineParams.id == 1))
    params = dict(params_rows[0]._mapping) if params_rows else None
    if for_update:
        # Import finalization writes sessions before cards. Use the same lock
        # order; opposite ordering can deadlock publication against an import.
        db.execute(select(BuildSession.session_id).order_by(BuildSession.session_id)
                   .with_for_update()).all()
    # Lock unlinked cards too: linking an existing card during publication must
    # not race the validation of the selected dataset.
    record_rows = read(select(
        PrintRecord.record_id, PrintRecord.session_id, PrintRecord.name, PrintRecord.material,
        PrintRecord.layer_thickness_mm, PrintRecord.metadata_json, PrintRecord.revision,
        PrintRecord.created_at,
    ).order_by(PrintRecord.created_at, PrintRecord.record_id))
    records = [CalibrationRecord(**dict(row._mapping)) for row in record_rows if row.session_id]
    ids = sorted({record.session_id for record in records})
    if not ids:
        return CalibrationInputs([], [], params)
    sessions = {}
    timings = []
    publications = {}
    tags = []
    # Bounded bind counts work on Windows/macOS SQLite as well as PostgreSQL.
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        rows = read(select(
            BuildSession.session_id, BuildSession.printer_id, BuildSession.start_ts,
            BuildSession.end_ts, BuildSession.classification,
            BuildSession.context["runtime_payload"]["group"]["classification"].as_string().label("group_classification"),
            BuildSession.context[MANIFEST_KEY].label("timing_publication"),
        ).where(BuildSession.session_id.in_(batch)).order_by(BuildSession.session_id))
        for row in rows:
            values = dict(row._mapping)
            publications[row.session_id] = values.pop("timing_publication")
            values["context"] = {"runtime_payload": {"group": {
                "classification": values.pop("group_classification"),
            }}}
            sessions[row.session_id] = CalibrationSession(**values)
        for row in db.execute(select(
            LayerSnapshot.session_id, LayerSnapshot.layer, LayerSnapshot.features,
            LayerSnapshot.context["publication_id"].as_string(),
        ).where(LayerSnapshot.session_id.in_(batch)).order_by(
            LayerSnapshot.session_id, LayerSnapshot.layer, LayerSnapshot.layer_snapshot_id,
        )):
            timings.append(tuple(row[:3]))
            tags.append(row[3])
    return CalibrationInputs(
        [(record, sessions[record.session_id]) for record in records if record.session_id in sessions],
        timings, params, publications, tags,
    )
