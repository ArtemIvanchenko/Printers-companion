"""Print archive models: print records, attached files, machine parameters."""
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from storage.db.base import Base
from storage.db.session import _json_default_dict

from domain.models.sessions import _new_id, utcnow


class PrintRecord(Base):
    """One physical print: links STL/Magics/photos with the log session."""

    __tablename__ = "print_records"

    record_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("pr"))
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    material: Mapped[str] = mapped_column(String(120), default="steel")
    # Thickness this print was run at. NULL = not specified, fall back to the
    # machine default. Per-print rather than global because the shop runs
    # different thicknesses per job, and the fitted scan models are keyed
    # "material@thickness" — they do not transfer across thicknesses.
    layer_thickness_mm: Mapped[float | None] = mapped_column(Float)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="draft")
    notes: Mapped[str | None] = mapped_column(Text)
    # Actual print date: parsed from name/file names, overwritten by log session
    # start when one is linked. NULL = unknown (fall back to created_at).
    printed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Powder price snapshot at registration time — entered by the operator so
    # cost history survives later rate changes in machine_params.
    powder_cost_rub_per_kg: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PrintRecordFile(Base):
    """A file attached to a print record, stored in MinIO."""

    __tablename__ = "print_record_files"

    file_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("prf"))
    record_id: Mapped[str] = mapped_column(ForeignKey("print_records.record_id"), index=True)
    object_uri: Mapped[str] = mapped_column(String(700), nullable=False)
    file_name: Mapped[str] = mapped_column(String(300), nullable=False)
    file_type: Mapped[str] = mapped_column(String(40), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(128), index=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlateGeometryCache(Base):
    """Cached ``LayerGeometrySeries`` for one exact set of STL bodies + hatch settings.

    Content-addressed, not tied to a print record: ``cache_key`` is a hash of the
    bodies' checksums (in mesh order, part/support tagged) plus hatch_distance_mm
    and layer_thickness_mm — see ``analytics.prediction.plate_estimator._geometry_cache_key``.
    Co-hatching a real plate is minutes of CPU; changing only material (which does
    not affect the geometry at all) or re-estimating a record after a no-op edit
    used to redo that work from scratch. A second print record built from the
    same STL files (a reprint of the same layout) benefits too, since the key
    does not reference any record_id.
    """

    __tablename__ = "plate_geometry_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    series_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    body_count: Mapped[int] = mapped_column(Integer, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MachinePreset(Base):
    """Named scanning-parameter set for one material/mode (e.g. Al 60 µm).

    Presets are per-material overlays: the estimation code merges the active
    preset for the requested material on top of the global MachineParams row
    before running time/cost calculations. Fields left NULL fall back to the
    global value in machine_params.
    """

    __tablename__ = "machine_presets"

    preset_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    material: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    layer_thickness_mm: Mapped[float | None] = mapped_column(Float)
    hatch_speed_mm_s: Mapped[float | None] = mapped_column(Float)
    contour_speed_mm_s: Mapped[float | None] = mapped_column(Float)
    hatch_distance_mm: Mapped[float | None] = mapped_column(Float)
    jump_speed_mm_s: Mapped[float | None] = mapped_column(Float)
    jump_delay_ms: Mapped[float | None] = mapped_column(Float)
    laser_power_w: Mapped[float | None] = mapped_column(Float)
    # When True this preset is used by default for its material in estimations
    is_default: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MachineParams(Base):
    """Single-row table (id=1) with all machine/cost parameters, edited via UI.

    Every numeric value here is operator-configurable; nothing is hardcoded
    in calculation code. NULL means "not configured yet".
    """

    __tablename__ = "machine_params"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Scanning parameters
    hatch_speed_mm_s: Mapped[float | None] = mapped_column(Float)
    contour_speed_mm_s: Mapped[float | None] = mapped_column(Float)
    # Distance between hatch lines. When set, hatch_speed is the real laser
    # speed (mm/s); when NULL the Excel-style areal rate (mm²/s) is assumed.
    hatch_distance_mm: Mapped[float | None] = mapped_column(Float)
    # Global multiplier applied to the PySLM print time; calibrated from
    # predicted-vs-actual history (1.0 = no correction). Kept as a fallback for
    # materials that have no per-material factor yet (see time_correction_by_mat).
    time_correction_factor: Mapped[float | None] = mapped_column(Float)
    # When True the operator has pinned the correction factor(s) manually —
    # auto-calibration must not overwrite them.
    correction_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    layer_thickness_mm: Mapped[float | None] = mapped_column(Float)
    laser_count: Mapped[int | None] = mapped_column(Integer)
    recoat_time_ms: Mapped[float | None] = mapped_column(Float)
    # Per-material recoat time (ms), auto-calibrated from real per-layer
    # pour_ms readings in printer logs (see analytics.prediction.recoat_calibration).
    # Falls back to recoat_time_ms, then the hardcoded default. Same shape and
    # calibration lock as time_correction_by_mat, but this is the duration
    # itself, not a multiplier.
    recoat_time_by_mat: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    # Galvo jump (laser-off repositioning) speed and per-jump delay — used by
    # the PySLM vector estimate to account for travel between scan vectors.
    jump_speed_mm_s: Mapped[float | None] = mapped_column(Float)
    jump_delay_ms: Mapped[float | None] = mapped_column(Float)
    # Consumable rates (rub)
    powder_cost_rub_per_kg: Mapped[float | None] = mapped_column(Float)
    gas_cost_rub_per_atm: Mapped[float | None] = mapped_column(Float)
    gas_atm_per_print: Mapped[float | None] = mapped_column(Float)
    filter_cost_rub: Mapped[float | None] = mapped_column(Float)
    filter_lifetime_hours: Mapped[float | None] = mapped_column(Float)
    platform_cost_rub: Mapped[float | None] = mapped_column(Float)
    # Per-material maps: {"steel": 7.9, "aluminum": 2.7, ...}
    material_densities: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    hatch_speeds_by_mat: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    # Per-material time-correction factors: {"steel": 1.15, "aluminum": 1.08, ...}
    # Auto-calibrated from predicted-vs-actual history per material.
    time_correction_by_mat: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    # Fitted scan-time models keyed "material@thickness" ("steel@0.060"),
    # calibrated from real per-layer burn_ms logs — see
    # analytics.prediction.scan_calibration. Applied only on an exact mode match.
    scan_model_by_mat: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    build_area_cm2: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
