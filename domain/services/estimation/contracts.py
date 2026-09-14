"""Detached input shape and transport-neutral, process-safe estimate errors."""

from typing import Any, TypedDict


class EstimateError(Exception):
    def __init__(self, code: str, detail: str | dict):
        super().__init__(code, detail)
        self.code = code
        self.detail = detail

    def __str__(self):
        return str(self.detail)


class PreparedEstimate(TypedDict):
    record: dict[str, Any]
    platform_files: list[dict[str, Any]]
    material: str
    params: dict[str, Any]
    parameter_sources: dict[str, Any]
    printer_id: str | None
    powder_cost: float | None
