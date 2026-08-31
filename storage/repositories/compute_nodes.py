"""Registration guard for logical compute-node ownership."""

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from domain.models.jobs import ComputeNodeRegistration


class DuplicateComputeNodeError(RuntimeError):
    """A logical node id or physical instance is already registered elsewhere."""


class ComputeNodesRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def register(self, compute_node_id: str, instance_id: str) -> ComputeNodeRegistration:
        """Create or validate an immutable node↔workstation mapping.

        The mapping never expires automatically.  This is deliberate: an
        offline owner's tasks must wait for that physical PC, not become
        eligible on a different PC that happens to reuse its configured name.
        """
        if self.db.get_bind().dialect.name == "postgresql":
            digest = hashlib.sha256(
                f"compute-node:{compute_node_id}".encode("utf-8")
            ).digest()
            lock_key = int.from_bytes(digest[:8], "big", signed=True)
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": lock_key},
            )

        row = self.db.scalar(
            select(ComputeNodeRegistration)
            .where(ComputeNodeRegistration.compute_node_id == compute_node_id)
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if row is not None:
            if row.instance_id != instance_id:
                raise DuplicateComputeNodeError(
                    f"COMPUTE_NODE_ID '{compute_node_id}' is already bound to another PC"
                )
            row.last_seen_at = now
            self.db.flush()
            return row

        other_name = self.db.scalar(
            select(ComputeNodeRegistration).where(
                ComputeNodeRegistration.instance_id == instance_id
            )
        )
        if other_name is not None:
            raise DuplicateComputeNodeError(
                "This operator PC is already registered as "
                f"'{other_name.compute_node_id}', not '{compute_node_id}'"
            )

        row = ComputeNodeRegistration(
            compute_node_id=compute_node_id,
            instance_id=instance_id,
            registered_at=now,
            last_seen_at=now,
        )
        try:
            # Different logical names use different advisory keys. The unique
            # instance_id index is therefore the final race guard when one
            # physical state directory is misconfigured with two names.
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
            return row
        except IntegrityError:
            existing_node = self.db.get(ComputeNodeRegistration, compute_node_id)
            if existing_node is not None:
                raise DuplicateComputeNodeError(
                    f"COMPUTE_NODE_ID '{compute_node_id}' is already bound to another PC"
                ) from None
            existing_instance = self.db.scalar(
                select(ComputeNodeRegistration).where(
                    ComputeNodeRegistration.instance_id == instance_id
                )
            )
            if existing_instance is not None:
                raise DuplicateComputeNodeError(
                    "This operator PC is already registered as "
                    f"'{existing_instance.compute_node_id}', not '{compute_node_id}'"
                ) from None
            raise
