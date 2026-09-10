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
    objects, members = expand_log_inputs(source, output, {'Логи.zip': 's3://raw/original.zip'})
    assert [p.name for p in output.iterdir()] == ['17.07.2026.log']
    assert objects == {'17.07.2026.log': 's3://raw/original.zip'}
    assert members == {'17.07.2026.log': 'nested/17.07.2026.log'}


def test_zip_and_loose_duplicate_is_parsed_once(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    zip_file(source / 'a.zip', [('day.log', b'log')])
    (source / 'day.log').write_bytes(b'log')
    output = tmp_path / 'output'
    expand_log_inputs(source, output, {})
    assert len(list(output.iterdir())) == 1


def test_conflicting_log_names_fail_closed(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    zip_file(source / 'a.zip', [('day.log', b'one')])
    zip_file(source / 'b.zip', [('day.log', b'two')])
    with pytest.raises(ValueError, match='одинаковым именем'):
        expand_log_inputs(source, tmp_path / 'output', {})


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


def test_folder_import_actually_passes_expanded_logs_to_ingestion(tmp_path, monkeypatch):
    from core.config.settings import Settings
    from domain.services import import_jobs
    from domain.services.ingestion import IngestionResult
    source = tmp_path / 'uploaded'
    source.mkdir()
    zip_file(source / 'logs.zip', [('17.07.2026.log', b'test')])
    job = import_jobs.ImportJobRecord(import_job_id='archive_integration', owner_node_id='test',
                                     source_path=str(source), source_name='uploaded', source_kind='folder')
    observed = []
    def parse(self, path):
        observed.extend(p.name for p in Path(path).iterdir())
        return IngestionResult(root=str(path))
    monkeypatch.setattr(import_jobs.IngestionService, 'parse', parse)
    monkeypatch.setattr(import_jobs, 'archive_raw_import', lambda *a, **kw: {'logs.zip': 's3://raw/archive.zip'})
    import_jobs.execute_confirmed_import(job, registry=object(), settings=Settings(app_env='test'))
    assert observed == ['17.07.2026.log']
