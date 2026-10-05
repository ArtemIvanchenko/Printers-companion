import hashlib
from pathlib import Path
import zipfile

import pytest

from domain.services.log_archives import expand_log_inputs, validated_members
from scripts.maintenance.organize_print_catalog import copy_verified, make_archive


def zip_file(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)


def test_folder_zip_is_expanded_and_raw_uri_preserved(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    zip_file(source / 'Логи.zip', [('nested/17.07.2026.log', b'log'), ('model.stl', b'model')])
    output = tmp_path / 'output'
    objects, members, checksums = expand_log_inputs(source, output, {'Логи.zip': 's3://raw/original.zip'})
    assert [p.name for p in output.iterdir()] == ['17.07.2026.log']
    assert objects == {'17.07.2026.log': 's3://raw/original.zip'}
    assert members == {'17.07.2026.log': 'nested/17.07.2026.log'}
    assert checksums == {'17.07.2026.log': hashlib.sha256(b'log').hexdigest()}


def test_zip_and_loose_duplicate_is_parsed_once(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    zip_file(source / 'a.zip', [('day.log', b'log')])
    (source / 'day.log').write_bytes(b'log')
    output = tmp_path / 'output'
    _, _, checksums = expand_log_inputs(source, output, {})
    assert len(list(output.iterdir())) == 1
    assert checksums == {'day.log': hashlib.sha256(b'log').hexdigest()}


def test_conflicting_log_names_fail_closed(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    zip_file(source / 'a.zip', [('day.log', b'one')])
    zip_file(source / 'b.zip', [('day.log', b'two')])
    with pytest.raises(ValueError, match='одинаковым именем'):
        expand_log_inputs(source, tmp_path / 'output', {})


@pytest.mark.parametrize('names', [
    ('day.log', 'DAY.LOG'),
    ('й.log', 'и\u0306.log'),
])
def test_cross_platform_filename_collisions_never_overwrite_logs(tmp_path, names):
    source = tmp_path / 'input.zip'
    zip_file(source, [(names[0], b'first'), (names[1], b'second')])
    with pytest.raises(ValueError, match='одинаковым именем'):
        expand_log_inputs(source, tmp_path / 'output', {})
    assert (tmp_path / 'output' / names[0]).read_bytes() == b'first'


def test_equivalent_cross_platform_names_keep_first_source_uri(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    zip_file(source / 'a.zip', [('day.log', b'same')])
    zip_file(source / 'b.zip', [('DAY.LOG', b'same')])
    output = tmp_path / 'output'
    objects, _, checksums = expand_log_inputs(source, output, {'a.zip': 's3://raw/a.zip', 'b.zip': 's3://raw/b.zip'})
    assert [p.name for p in output.iterdir()] == ['day.log']
    assert objects == {'day.log': 's3://raw/a.zip'}
    assert checksums == {'day.log': hashlib.sha256(b'same').hexdigest()}


def test_manifest_is_hashed_while_copying_without_reading_temporary_file(tmp_path, monkeypatch):
    source = tmp_path / 'input'
    source.mkdir()
    payload = b'machine data' * 150_000  # More than one copy block.
    zip_file(source / 'a.zip', [('nested/day.log', payload)])
    (source / 'loose.log').write_bytes(b'loose')
    (source / 'empty.log').write_bytes(b'')
    original_open = Path.open

    def open_without_temp_read(path, mode='r', *args, **kwargs):
        if path.name == '.member.partial' and 'r' in mode:
            raise AssertionError('Expansion must hash copied blocks, not reread the temporary file')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', open_without_temp_read)
    output = tmp_path / 'output'
    objects, members, checksums = expand_log_inputs(source, output, {'a.zip': 's3://raw/a.zip'})
    assert checksums == {
        'day.log': hashlib.sha256(payload).hexdigest(),
        'loose.log': hashlib.sha256(b'loose').hexdigest(),
        'empty.log': hashlib.sha256(b'').hexdigest(),
    }
    assert objects == {'day.log': 's3://raw/a.zip'}
    assert members == {'day.log': 'nested/day.log'}
    assert (output / 'day.log').read_bytes() == payload
    assert not (output / '.member.partial').exists()


def test_crc_failure_does_not_publish_file_or_manifest(tmp_path):
    source = tmp_path / 'bad.zip'
    zip_file(source, [('day.log', b'original log')])
    with zipfile.ZipFile(source) as archive:
        member = archive.getinfo('day.log')
        data_offset = member.header_offset + 30 + len(member.filename) + len(member.extra)
    damaged = bytearray(source.read_bytes())
    damaged[data_offset] ^= 1
    source.write_bytes(damaged)
    output = tmp_path / 'output'
    with pytest.raises(zipfile.BadZipFile, match='CRC'):
        expand_log_inputs(source, output, {})
    assert list(output.iterdir()) == []


def test_lease_expiry_during_copy_removes_partial_file(tmp_path):
    source = tmp_path / 'input.zip'
    zip_file(source, [('day.log', b'x' * (1024 * 1024 + 1))])
    checks = 0

    def require_lease():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError('lease expired')

    output = tmp_path / 'output'
    with pytest.raises(RuntimeError, match='lease expired'):
        expand_log_inputs(source, output, {}, lease_check=require_lease)
    assert checks == 3
    assert list(output.iterdir()) == []


def test_nonempty_output_is_never_overwritten(tmp_path):
    source = tmp_path / 'input.zip'
    zip_file(source, [('day.log', b'new')])
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'day.log').write_bytes(b'existing')
    with pytest.raises(ValueError, match='пуст'):
        expand_log_inputs(source, output, {})
    assert (output / 'day.log').read_bytes() == b'existing'


@pytest.mark.parametrize('name', ['../escape.log', '/absolute.log', '..\\escape.log', 'C:/file.log'])
def test_archive_traversal_rejected(tmp_path, name):
    path = tmp_path / 'bad.zip'
    zip_file(path, [(name, b'bad')])
    with pytest.raises(ValueError, match='Unsafe ZIP'):
        expand_log_inputs(path, tmp_path / 'output', {})


def test_symlink_and_size_budget_rejected(tmp_path):
    path = tmp_path / 'link.zip'
    info = zipfile.ZipInfo('link.log')
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(info, '../outside')
    with zipfile.ZipFile(path) as archive, pytest.raises(ValueError):
        validated_members(archive)
    zip_file(path, [('x.log', b'large')])
    with zipfile.ZipFile(path) as archive, pytest.raises(ValueError, match='объём'):
        validated_members(archive, max_bytes=1)


def test_nested_archive_is_not_silently_ignored(tmp_path):
    path = tmp_path / 'nested.zip'
    zip_file(path, [('inside.zip', b'zip')])
    with pytest.raises(ValueError, match='ZIP внутри ZIP'):
        expand_log_inputs(path, tmp_path / 'output', {})


def test_catalog_archives_verify_contents_and_preserve_changed_targets(tmp_path):
    source = tmp_path / 'source.log'
    source.write_bytes(b'raw log')
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    row = {'name': source.name, 'path': str(source), 'sha256': checksum}
    result = make_archive([row], tmp_path / 'logs.zip')
    assert result == make_archive([row], tmp_path / 'logs.zip')
    copied = tmp_path / 'copy.log'
    copy_verified(source, copied, checksum)
    copied.write_bytes(b'operator changed this copy')
    with pytest.raises(ValueError, match='перезаписываю'):
        copy_verified(source, copied, checksum)
    assert source.read_bytes() == b'raw log'


@pytest.mark.parametrize('source_kind', ['folder', 'zip'])
def test_import_uses_expansion_manifest_and_passes_logs_to_ingestion(tmp_path, monkeypatch, source_kind):
    from core.config.settings import Settings
    from domain.services import import_jobs
    source = tmp_path / ('uploaded' if source_kind == 'folder' else 'logs.zip')
    if source_kind == 'folder':
        source.mkdir()
    archive_path = source / 'logs.zip' if source_kind == 'folder' else source
    zip_file(archive_path, [('17.07.2026.log', b'test')])
    job = import_jobs.ImportJobRecord(import_job_id='archive_integration', owner_node_id='test',
                                     source_path=str(source), source_name=source.name, source_kind=source_kind)
    observed = []
    archived = []
    manifest_reads = []
    original_parse = import_jobs.IngestionService.parse
    original_manifest = import_jobs.calculate_checksum_manifest

    def manifest(path):
        assert path == source  # Never rescan the prepared temporary tree.
        manifest_reads.append(path)
        return original_manifest(path)

    def archive(*args, **kwargs):
        archived.append(source)
        return {'logs.zip' if source_kind == 'folder' else '__source_archive__': 's3://raw/archive.zip'}

    def parse(self, path):
        assert archived == [source]
        observed.extend(p.name for p in Path(path).iterdir())
        return original_parse(self, path)  # Independent ingestion SHA-256 remains active.

    monkeypatch.setattr(import_jobs.IngestionService, 'parse', parse)
    monkeypatch.setattr(import_jobs, 'archive_raw_import', archive)
    monkeypatch.setattr(import_jobs, 'calculate_checksum_manifest', manifest)
    monkeypatch.setattr(import_jobs, 'group_files_into_sessions', lambda files: [])
    import_jobs.execute_confirmed_import(job, registry=object(), settings=Settings(app_env='test'))
    assert observed == ['17.07.2026.log']
    assert manifest_reads == ([source] if source_kind == 'folder' else [])
    assert job.checksum_manifest == {'17.07.2026.log': hashlib.sha256(b'test').hexdigest()}
    assert job.source_objects['17.07.2026.log'] == 's3://raw/archive.zip'


@pytest.mark.parametrize('source_kind', ['folder', 'zip'])
def test_ingestion_rejects_source_changed_after_expansion(tmp_path, monkeypatch, source_kind):
    from core.config.settings import Settings
    from domain.services import import_jobs

    source = tmp_path / ('uploaded' if source_kind == 'folder' else 'logs.zip')
    if source_kind == 'folder':
        source.mkdir()
    archive_path = source / 'logs.zip' if source_kind == 'folder' else source
    zip_file(archive_path, [('day.log', b'original')])
    job = import_jobs.ImportJobRecord(owner_node_id='test', source_path=str(source),
                                     source_name=source.name, source_kind=source_kind)
    original_parse = import_jobs.IngestionService.parse

    def changed_parse(self, path):
        (Path(path) / 'day.log').write_bytes(b'changed')
        return original_parse(self, path)

    monkeypatch.setattr(import_jobs.IngestionService, 'parse', changed_parse)
    monkeypatch.setattr(import_jobs, 'archive_raw_import', lambda *args, **kwargs: {})
    with pytest.raises(import_jobs.RetryableImportError, match='изменился между архивированием и разбором'):
        import_jobs.execute_confirmed_import(job, registry=object(), settings=Settings(app_env='test'))
    assert job.checksum_manifest == {'day.log': hashlib.sha256(b'original').hexdigest()}
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read('day.log') == b'original'


@pytest.mark.parametrize('analysis_fails', [False, True])
def test_prepared_sources_live_through_analysis_and_cleanup_only_owned_tree(tmp_path, monkeypatch, analysis_fails):
    from contextlib import nullcontext
    from datetime import datetime, timezone
    from core.config.settings import Settings
    from domain.services import import_jobs

    source = tmp_path / 'logs.zip'
    zip_file(source, [('day.log', b'original')])
    original_bytes = source.read_bytes()
    job = import_jobs.ImportJobRecord(owner_node_id='test', source_path=str(source),
                                     source_name=source.name, source_kind='zip')
    monkeypatch.setattr(import_jobs, 'archive_raw_import', lambda *a, **k: {'__source_archive__': 's3://raw/zip'})
    outcome = pytest.raises(RuntimeError, match='analysis failed') if analysis_fails else nullcontext()
    with outcome:
        with import_jobs._prepared_import_sources(
            job, settings=Settings(app_env='test'), now=datetime.now(timezone.utc), lease_guard=None,
        ) as (root, members):
            assert root != tmp_path and root.exists()
            assert (root / 'day.log').read_bytes() == b'original'
            assert members == {'day.log': 'day.log'}
            if analysis_fails:
                raise RuntimeError('analysis failed')
    assert not root.exists()
    assert source.read_bytes() == original_bytes
