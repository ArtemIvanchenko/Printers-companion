"""Strict ownership of work that must execute on its originating workstation."""

from __future__ import annotations


LEGACY_UNASSIGNED_NODE_ID = "legacy-unassigned"


class ComputeAffinityError(RuntimeError):
    """A workstation attempted to calculate an entity owned by another PC."""

    def __init__(
        self,
        *,
        entity_type: str,
        entity_id: str,
        origin_compute_node_id: str,
        requested_compute_node_id: str,
    ) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.origin_compute_node_id = origin_compute_node_id
        self.requested_compute_node_id = requested_compute_node_id
        if origin_compute_node_id == LEGACY_UNASSIGNED_NODE_ID:
            message = (
                f"{entity_type} '{entity_id}' has no trusted compute owner "
                "(legacy-unassigned); an administrator must explicitly assign "
                "it before any raw-data processing or recalculation"
            )
        else:
            message = (
                f"{entity_type} '{entity_id}' belongs to compute node "
                f"'{origin_compute_node_id}', not '{requested_compute_node_id}'"
            )
        super().__init__(message)


def require_compute_owner(
    *,
    entity_type: str,
    entity_id: str,
    origin_compute_node_id: str,
    requested_compute_node_id: str,
) -> None:
    """Reject calculation outside the immutable originating-PC boundary."""
    if origin_compute_node_id != requested_compute_node_id:
        raise ComputeAffinityError(
            entity_type=entity_type,
            entity_id=entity_id,
            origin_compute_node_id=origin_compute_node_id,
            requested_compute_node_id=requested_compute_node_id,
        )
