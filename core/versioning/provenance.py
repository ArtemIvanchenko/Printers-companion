"""Reproducible provenance attached to every persisted analytical result."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from core.versioning.constants import ANALYSIS_VERSION, APP_VERSION


def stable_hash(value: Any) -> str:
    """SHA-256 of a JSON-compatible value with deterministic ordering."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def git_sha() -> str | None:
    """Build revision injected by Docker/release tooling, when available."""
    value = os.environ.get("GIT_COMMIT", "").strip()
    return value if value and value not in {"unknown", "dev"} else None


def build_provenance(
    component: str,
    *,
    inputs: Any | None = None,
    config: Any | None = None,
    parser_versions: dict[str, str] | None = None,
    model_versions: dict[str, str] | None = None,
    generated_by: str = "system",
) -> dict[str, Any]:
    """Build the common version block used by reports and predictions."""
    return {
        "component": component,
        "app_version": APP_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "git_sha": git_sha(),
        "input_fingerprint": stable_hash(inputs) if inputs is not None else None,
        "config_hash": stable_hash(config) if config is not None else None,
        "parser_versions": dict(sorted((parser_versions or {}).items())),
        "model_versions": dict(sorted((model_versions or {}).items())),
        "generated_by": generated_by,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["stable_hash", "git_sha", "build_provenance"]
