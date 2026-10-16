"""ZIP byte-level name interoperability without rewriting source identities."""

import hashlib
from pathlib import PurePosixPath
import struct
import zipfile
import zlib

import pytest

from domain.services.log_archives import expand_log_inputs, validated_members


class RawName(zipfile.ZipInfo):
    """Write actual local/central headers with unflagged legacy name bytes."""

    def __init__(self, name, encoding):
        super().__init__(name)
        self.encoding = encoding

    def _encodeFilenameFlags(self):
        return self.orig_filename.encode(self.encoding), self.flag_bits & ~0x800


def make_zip(path, rows):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, encoding, data in rows:
            archive.writestr(RawName(name, encoding) if encoding else name, data)


@pytest.mark.parametrize('name', [
    'Печати/оператор_17.07.2026_time.log',
    'Печати/образец_прямоугольныи\u0306.log',
    'Печати/🖨.log',
])
def test_unflagged_utf8_is_readable_with_exact_raw_archive_lineage(tmp_path, name):
    source, target = tmp_path / 'input.zip', tmp_path / 'output'
    payload = b'original machine source'
    make_zip(source, [(name, 'utf-8', payload)])
    before = source.read_bytes()
    with zipfile.ZipFile(source) as archive:
        member = archive.infolist()[0]
        raw_name = member.filename
        assert not member.flag_bits & 0x800
        assert raw_name != name
        assert raw_name.encode('cp437') == name.encode('utf-8')
        assert validated_members(archive)[0].filename == raw_name
    objects, members, checksums = expand_log_inputs(
        source, target, {'__source_archive__': 's3://raw/original.zip'},
    )
    basename = PurePosixPath(name).name
    assert [item.name for item in target.iterdir()] == [basename]
    assert (target / basename).read_bytes() == payload
    assert objects == {basename: 's3://raw/original.zip'}
    assert checksums == {basename: hashlib.sha256(payload).hexdigest()}
    assert members == {basename: raw_name}
    # The raw metadata can still address the original entry, byte-for-byte.
    with zipfile.ZipFile(source) as archive:
        assert archive.getinfo(members[basename]).filename == raw_name
        assert archive.read(members[basename]) == payload
    assert source.read_bytes() == before


def test_equivalent_flagged_and_unflagged_paths_fail_before_copy(tmp_path):
    source, target = tmp_path / 'input.zip', tmp_path / 'output'
    # Distinct encoded names have the same logical identity.
    with pytest.warns(UserWarning, match='Duplicate name'):
        make_zip(source, [('Печати/день.log', 'utf-8', b'first'), ('Печати/день.log', None, b'other')])
    with pytest.raises(ValueError, match='Повторяющееся'):
        expand_log_inputs(source, target, {})
    assert list(target.iterdir()) == []


@pytest.mark.parametrize('names', [
    ('first/день.log', 'other/день.log'),
    ('first/день.log', 'other/ДЕНЬ.LOG'),
    ('first/й.log', 'other/и\u0306.log'),
])
def test_mixed_encodings_preserve_cross_platform_collision_guard(tmp_path, names):
    source, target = tmp_path / 'input.zip', tmp_path / 'output'
    make_zip(source, [(names[0], 'utf-8', b'first'), (names[1], None, b'other')])
    with pytest.raises(ValueError, match='одинаковым именем'):
        expand_log_inputs(source, target, {})
    assert (target / PurePosixPath(names[0]).name).read_bytes() == b'first'


def test_identical_mixed_encoding_logs_keep_first_raw_source_reference(tmp_path):
    source, target = tmp_path / 'input.zip', tmp_path / 'output'
    first = 'Печати/first/день.log'
    make_zip(source, [(first, 'utf-8', b'same'), ('Печати/other/ДЕНЬ.LOG', None, b'same')])
    objects, members, checksums = expand_log_inputs(source, target, {'__source_archive__': 's3://raw/zip'})
    assert [item.name for item in target.iterdir()] == ['день.log']
    assert objects == {'день.log': 's3://raw/zip'}
    assert members == {'день.log': first.encode('utf-8').decode('cp437')}
    assert checksums == {'день.log': hashlib.sha256(b'same').hexdigest()}


@pytest.mark.parametrize('name', [
    '../Печать.log', '..\\Печать.log', '/Печать.log', 'C:/Печать.log', 'safe/../Печать.log',
])
def test_unflagged_utf8_keeps_traversal_protection(tmp_path, name):
    source, target = tmp_path / 'bad.zip', tmp_path / 'output'
    make_zip(source, [(name, 'utf-8', b'bad')])
    with pytest.raises(ValueError, match='Unsafe ZIP'):
        expand_log_inputs(source, target, {})
    assert list(target.iterdir()) == []


@pytest.mark.parametrize('name,encoding', [
    ('é.log', 'cp437'), ('Печать.log', None), ('╨ƒ╨╡╤ç╨░╤é╨╕.log', None),
])
def test_native_cp437_and_flagged_unicode_are_not_rewritten(tmp_path, name, encoding):
    source = tmp_path / 'good.zip'
    make_zip(source, [(name, encoding, b'good')])
    _, members, checksums = expand_log_inputs(source, tmp_path / 'output', {})
    assert list(checksums) == [name]
    assert members == {name: name}


@pytest.mark.parametrize('raw_name', ['raw.log', '├⌐.log'])
def test_explicit_unicode_path_decoded_by_stdlib_is_not_decoded_again(tmp_path, raw_name):
    source = tmp_path / 'unicode-extra.zip'
    unicode_name = '├⌐.log'
    member = RawName(raw_name, 'cp437')
    # Info-ZIP Unicode Path: explicit UTF-8 name bound to the raw name by CRC.
    data = struct.pack('<BL', 1, zlib.crc32(raw_name.encode('cp437'))) + unicode_name.encode('utf-8')
    member.extra = struct.pack('<HH', 0x7075, len(data)) + data
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr(member, b'good')
    with zipfile.ZipFile(source) as archive:
        parsed = archive.infolist()[0]
        assert parsed.orig_filename == raw_name
        if parsed.filename != unicode_name:
            pytest.skip('This Python zipfile does not interpret Unicode Path metadata')
        assert parsed.filename == unicode_name
    _, members, checksums = expand_log_inputs(source, tmp_path / 'output', {})
    assert list(checksums) == [unicode_name]
    assert members == {unicode_name: unicode_name}


def test_native_nul_normalization_is_not_reinterpreted(tmp_path):
    source = tmp_path / 'nul.zip'
    raw_name = '├⌐.log\0ignored'
    make_zip(source, [(raw_name, 'cp437', b'good')])
    with zipfile.ZipFile(source) as archive:
        parsed = archive.infolist()[0]
        assert parsed.orig_filename == raw_name
        assert parsed.filename == '├⌐.log'
    _, members, checksums = expand_log_inputs(source, tmp_path / 'output', {})
    assert list(checksums) == ['├⌐.log']
    assert members == {'├⌐.log': '├⌐.log'}


def test_explicit_unicode_metadata_with_invalid_crc_retains_native_name(tmp_path):
    source = tmp_path / 'unicode-extra.zip'
    member = RawName('Печать.log', 'utf-8')
    data = struct.pack('<BL', 1, 0) + 'other.log'.encode('utf-8')
    member.extra = struct.pack('<HH', 0x7075, len(data)) + data
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr(member, b'good')
    native_name = 'Печать.log'.encode('utf-8').decode('cp437')
    _, members, checksums = expand_log_inputs(source, tmp_path / 'output', {})
    assert list(checksums) == [native_name]
    assert members == {native_name: native_name}


def test_malformed_extra_metadata_still_fails_closed(tmp_path):
    source, target = tmp_path / 'bad-extra.zip', tmp_path / 'output'
    member = RawName('Печать.log', 'utf-8')
    member.extra = struct.pack('<HH', 0x7075, 20) + b'bad'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr(member, b'good')
    with pytest.raises(zipfile.BadZipFile, match='Corrupt extra field'):
        expand_log_inputs(source, target, {})
    assert list(target.iterdir()) == []
