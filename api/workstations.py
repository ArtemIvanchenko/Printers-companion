"""Operator workstation identity shared by multi-PC API routes."""

from fastapi import Request


def workstation_id(request: Request, fallback: str | None = None) -> str | None:
    """Return a bounded, header-safe workstation label.

    The value is audit provenance, not authentication.  Authentication and
    authorization must never rely on it.
    """
    raw = (request.headers.get("X-Workstation-ID") or "").strip()
    clean = "".join(char for char in raw if char.isalnum() or char in "._- ")
    return clean[:120] or fallback
