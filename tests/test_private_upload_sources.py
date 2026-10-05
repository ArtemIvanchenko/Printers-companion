"""Only a queued job may explicitly read a private browser upload batch."""

from types import SimpleNamespace
import zipfile

import pytest

from core.config.settings import Settings
from core.utils.files import BROWSER_UPLOAD_PREFIX, iter_source_files, sha256_file
from domain.services import import_jobs
from domain.services.ingestion import IngestionResult, IngestionService
from domain.services.log_archives import expand_log_inputs
from worker import watcher
from worker.watcher import candidate_signature, import_batch_for, is_import_candidate


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "raw"
    public = root / "public"
    private = root / f"{BROWSER_UPLOAD_PREFIX}batch"
    hidden = root / ".ordinary-hidden"
    nested_private = public / f"{BROWSER_UPLOAD_PREFIX}nested"
    for directory in (public, private, hidden, nested_private):
        directory.mkdir(parents=True)
    (public / "machine.log").write_bytes(b"public log")
    (hidden / "ordinary.log").write_bytes(b"ordinary hidden source")
    (private / "private.log").write_bytes(b"private queued source")
    (nested_private / "nested.log").write_bytes(b"nested private source")
    (root / f"{BROWSER_UPLOAD_PREFIX}batch.json").write_text("{}")
    return root, public, private, nested_private


def relative_sources(root):
    return sorted(path.relative_to(root).as_posix() for path in iter_source_files(root))


def test_private_batches_and_receipts_are_pruned_but_other_hidden_names_are_kept(source_tree):
    root, _, private, _ = source_tree
    assert relative_sources(root) == [".ordinary-hidden/ordinary.log", "public/machine.log"]
    assert relative_sources(private) == ["private.log"]
    receipt = root / f"{BROWSER_UPLOAD_PREFIX}batch.json"
    assert list(iter_source_files(receipt)) == []


def test_explicit_file_remains_a_source(source_tree):
    _, public, _, _ = source_tree
    log = public / "machine.log"
    assert list(iter_source_files(log)) == [log]
    assert set(import_jobs.snapshot_source(log)) == {"machine.log"}
    assert import_jobs.calculate_checksum_manifest(log) == {"machine.log": sha256_file(log)}
    ingested = IngestionService(registry=object()).scan(log)
    assert [item.relative_path for item in ingested.files] == ["machine.log"]
    assert ingested.files[0].checksum == import_jobs.calculate_checksum_manifest(log)["machine.log"]


@pytest.mark.parametrize("explicit_private", [False, True])
def test_snapshot_manifest_and_ingestion_share_private_source_boundary(
    source_tree, explicit_private
):
    root, _, private, _ = source_tree
    source = private if explicit_private else root
    expected = (
        {"private.log"}
        if explicit_private
        else {
            ".ordinary-hidden/ordinary.log",
            "public/machine.log",
        }
    )
    assert set(import_jobs.snapshot_source(source)) == expected
    assert set(import_jobs.calculate_checksum_manifest(source)) == expected
    result = IngestionService(registry=object()).scan(source)
    assert {item.relative_path for item in result.files} == expected


@pytest.mark.parametrize("explicit_private", [False, True])
def test_archiving_never_publishes_private_ancestor_children(
    source_tree, monkeypatch, explicit_private
):
    root, _, private, _ = source_tree
    source = private if explicit_private else root
    expected = (
        {"private.log"}
        if explicit_private
        else {
            ".ordinary-hidden/ordinary.log",
            "public/machine.log",
        }
    )
    uploaded = []

    class Store:
        settings = SimpleNamespace(minio_bucket_raw="raw")

        def is_available(self):
            return True

        def put_file_verified(self, bucket, name, path, *, expected_sha256, expected_size):
            uploaded.append(path.relative_to(source).as_posix())
            assert expected_sha256 == sha256_file(path)
            assert expected_size == path.stat().st_size
            return f"s3://{bucket}/{name}"

    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", Store)
    job = SimpleNamespace(source_kind="folder", owner_node_id="local", import_job_id="job")
    assert set(import_jobs.archive_raw_import(job, source)) == expected
    assert set(uploaded) == expected


@pytest.mark.parametrize("explicit_private", [False, True])
def test_archive_expansion_obeys_private_source_boundary(source_tree, tmp_path, explicit_private):
    root, _, private, _ = source_tree
    source = private if explicit_private else root
    destination = tmp_path / "expanded"
    _, _, checksums = expand_log_inputs(source, destination, {})
    expected = (
        {"private.log"} if explicit_private else {"ordinary.log", "machine.log"}
    )
    assert {path.name for path in destination.iterdir()} == expected
    assert checksums == {name: sha256_file(destination / name) for name in expected}


def test_watcher_ignores_private_candidates_and_changes(source_tree):
    root, public, private, nested_private = source_tree
    assert is_import_candidate(private) is False
    assert import_batch_for(private, root) is None
    assert import_batch_for(root / f"{BROWSER_UPLOAD_PREFIX}batch.json", root) is None
    before = candidate_signature(public, root)
    (nested_private / "nested.log").write_bytes(b"changed private source")
    assert candidate_signature(public, root) == before
    (public / "machine.log").write_bytes(b"changed public source")
    assert candidate_signature(public, root) != before


def test_watcher_does_not_enqueue_private_only_parent(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    incoming = root / "incoming"
    private = incoming / f"{BROWSER_UPLOAD_PREFIX}batch"
    private.mkdir(parents=True)
    (private / "private.log").write_bytes(b"private")
    (incoming / f"{BROWSER_UPLOAD_PREFIX}batch.json").write_text("{}")
    notifications = []
    monkeypatch.setattr(watcher, "notify_import_detected", notifications.append)
    watcher.scan_existing_candidates(root, watcher.CandidateTracker())
    assert notifications == []
    assert candidate_signature(incoming, root) is None


def test_private_zip_does_not_trigger_ancestor_expansion(source_tree, monkeypatch):
    root, _, private, _ = source_tree
    with zipfile.ZipFile(private / "logs.zip", "w") as archive:
        archive.writestr("only-private.log", b"private")
    seen = []

    def parse(self, path):
        seen.append(path)
        return IngestionResult(root=str(path))

    monkeypatch.setattr(import_jobs.IngestionService, "parse", parse)
    monkeypatch.setattr(import_jobs, "archive_raw_import", lambda *args, **kwargs: {})
    job = import_jobs.ImportJobRecord(
        owner_node_id="local",
        source_path=str(root),
        source_name="raw",
        source_kind="folder",
    )
    import_jobs.execute_confirmed_import(job, registry=object(), settings=Settings(app_env="test"))
    assert seen == [root]


def test_directory_symlink_is_still_rejected_by_archive_expansion(tmp_path):
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "outside.log").write_bytes(b"outside")
    (source / "public.log").write_bytes(b"public")
    link = source / "linked-directory"
    link.symlink_to(outside, target_is_directory=True)
    assert link in list(iter_source_files(source))
    assert not any(path.name == "outside.log" for path in iter_source_files(source))
    with pytest.raises(ValueError, match="Символические ссылки"):
        expand_log_inputs(source, tmp_path / "expanded", {})
