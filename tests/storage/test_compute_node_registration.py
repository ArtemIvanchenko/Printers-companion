import pytest

from core.compute_identity import load_or_create_instance_id
from storage.db.session import SessionLocal
from storage.repositories.compute_nodes import (
    ComputeNodesRepository,
    DuplicateComputeNodeError,
)


def test_instance_id_is_persistent_on_the_operator_pc(tmp_path):
    path = tmp_path / "state" / "instance-id"
    first = load_or_create_instance_id(path)
    second = load_or_create_instance_id(path)

    assert first == second
    assert len(first) == 32
    assert path.read_text(encoding="ascii").strip() == first


def test_same_logical_node_cannot_be_registered_by_another_pc():
    with SessionLocal() as db:
        repo = ComputeNodesRepository(db)
        repo.register("operator-01", "a" * 32)
        db.commit()

    with SessionLocal() as db:
        with pytest.raises(DuplicateComputeNodeError, match="another PC"):
            ComputeNodesRepository(db).register("operator-01", "b" * 32)


def test_one_physical_pc_cannot_silently_change_its_node_name():
    with SessionLocal() as db:
        repo = ComputeNodesRepository(db)
        repo.register("operator-original", "c" * 32)
        db.commit()

    with SessionLocal() as db:
        with pytest.raises(DuplicateComputeNodeError, match="already registered"):
            ComputeNodesRepository(db).register("operator-renamed", "c" * 32)
