"""Application version — single source of truth.

Resolution order (first match wins):
  1. APP_VERSION env var   — set by ``docker build --build-arg``
  2. VERSION file in repo  — used in local development

The env var approach is standard for containers: no file I/O at runtime,
works even when the source tree is not present inside the image.
"""
from __future__ import annotations

import os
from pathlib import Path


def _read_version() -> str:
    if v := os.environ.get("APP_VERSION", "").strip():
        return v
    # Walk up from this file to find the VERSION file (repo root)
    here = Path(__file__).resolve()
    for parent in [here.parent.parent.parent, Path.cwd()]:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate.read_text().strip()
    return "dev"


APP_VERSION: str = _read_version()
