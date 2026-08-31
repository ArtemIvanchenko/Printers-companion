"""Persistent physical-workstation identity for owner-affine calculations."""

from __future__ import annotations

import hashlib
import os
import socket
import time
import uuid
from pathlib import Path

from core.config.settings import Settings


def process_lease_owner(compute_node_id: str) -> str:
    """Return a process token that always fits BackgroundJob VARCHAR(120)."""
    host = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]
    return f"{compute_node_id}:{host}:{os.getpid()}"


def load_or_create_instance_id(path: str | Path) -> str:
    """Atomically create the UUID shared by this PC's local containers."""
    instance_file = Path(path)
    instance_file.parent.mkdir(parents=True, exist_ok=True)
    candidate = uuid.uuid4().hex
    try:
        descriptor = os.open(
            instance_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        try:
            os.write(descriptor, (candidate + "\n").encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return candidate

    # Another local container may have won O_EXCL but not completed its tiny
    # write yet. Wait briefly for a valid value instead of inventing a second
    # physical identity.
    for _ in range(40):
        try:
            value = instance_file.read_text(encoding="ascii").strip()
            return uuid.UUID(value).hex
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
    raise RuntimeError(f"Operator instance-id file is missing or corrupt: {instance_file}")


def register_compute_node(settings: Settings) -> str:
    """Validate this physical PC's immutable mapping in the shared NAS DB."""
    from storage.db.session import session_scope
    from storage.repositories.compute_nodes import ComputeNodesRepository

    instance_id = load_or_create_instance_id(settings.operator_instance_file)
    with session_scope() as db:
        ComputeNodesRepository(db).register(settings.compute_node_id, instance_id)
    return instance_id
