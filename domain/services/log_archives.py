"""Bounded, local expansion of uploaded log archives without losing provenance."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile

from core.utils.files import sha256_file

MAX_EXPANDED_BYTES = 20 * 1024**3
MAX_MEMBER_BYTES = 6 * 1024**3
MAX_MEMBERS = 10000


def validated_members(archive, *, max_bytes=MAX_EXPANDED_BYTES):
    members = archive.infolist()
    if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > max_bytes:
        raise ValueError('Архив превышает допустимый объём распаковки')
    names = set()
    for member in members:
        path = PurePosixPath(member.filename.replace('\\', '/'))
        mode = member.external_attr >> 16
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or ':' in path.parts[0] or stat.S_ISLNK(mode)):
            raise ValueError(f'Unsafe ZIP member path: {member.filename}')
        if member.file_size > MAX_MEMBER_BYTES:
            raise ValueError('Слишком большой файл внутри архива')
        if path.as_posix() in names:
            raise ValueError('Повторяющееся имя внутри ZIP')
        names.add(path.as_posix())
    return members


def expand_log_inputs(source: Path, target: Path, source_objects: dict, *, lease_check=lambda: None):
    """Return object references and member paths for a parse-only local tree.

    Only .log inputs are exposed to parsers. Models/readme/ZIP containers cannot
    become pseudo-sessions. A different file with the same basename fails closed;
    byte-identical files occurring loose and in ZIP are parsed once.
    """
    target.mkdir(parents=True, exist_ok=True)
    objects, member_paths, seen = {}, {}, {}
    expanded = 0

    def save(stream, name, size, uri, member_path):
        nonlocal expanded
        expanded += size
        if expanded > MAX_EXPANDED_BYTES or shutil.disk_usage(target).free < size + 256 * 1024**2:
            raise ValueError('Недостаточно места или превышен предел распаковки логов')
        temp = target / '.member.partial'
        written = 0
        try:
            with temp.open('xb') as out:
                while block := stream.read(1024 * 1024):
                    lease_check()
                    written += len(block)
                    if written > size:
                        raise ValueError('Размер содержимого ZIP не совпадает с заголовком')
                    out.write(block)
            if written != size:
                raise ValueError('Неполный файл в архиве')
            checksum = sha256_file(temp)
            if name in seen:
                if seen[name] != checksum:
                    raise ValueError(f'Разные логи с одинаковым именем: {name}. Разделите печати.')
                return
            seen[name] = checksum
            temp.rename(target / name)
            if uri:
                objects[name] = uri
            if member_path:
                member_paths[name] = member_path
        finally:
            temp.unlink(missing_ok=True)

    inputs = [source] if source.is_file() else sorted(source.rglob('*'))
    for path in inputs:
        lease_check()
        if path.is_symlink():
            raise ValueError('Символические ссылки в наборе импорта не разрешены')
        if not path.is_file():
            continue
        relative = path.name if source.is_file() else path.relative_to(source).as_posix()
        uri = source_objects.get(relative) or source_objects.get('__source_archive__')
        if path.suffix.lower() == '.zip':
            with zipfile.ZipFile(path) as archive:
                for member in validated_members(archive):
                    name = PurePosixPath(member.filename.replace('\\', '/')).name
                    if member.is_dir() or name.startswith('._'):
                        continue
                    if name.lower().endswith('.zip'):
                        raise ValueError('ZIP внутри ZIP не поддерживается: выберите папку одной печати')
                    if not name.lower().endswith('.log'):
                        continue
                    with archive.open(member) as stream:
                        save(stream, name, member.file_size, uri, member.filename)
        elif path.suffix.lower() == '.log' and not path.name.startswith('._'):
            with path.open('rb') as stream:
                save(stream, path.name, path.stat().st_size, uri, None)
    if not seen:
        raise ValueError('В выбранном наборе нет машинных .log файлов')
    return objects, member_paths
