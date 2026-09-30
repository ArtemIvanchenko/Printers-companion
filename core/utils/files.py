import hashlib
import os
from collections.abc import Iterator
from pathlib import Path


BROWSER_UPLOAD_PREFIX = ".browser-upload-"


def iter_source_files(root: Path) -> Iterator[Path]:
    """Walk a source without discovering private browser batches or receipts.

    An explicitly selected private batch is readable by its durable import job;
    walking its ancestor never descends into it. Other hidden names retain their
    previous meaning. Symlinks are yielded, never traversed, so callers such as
    archive expansion can reject them rather than silently weakening validation.
    File-only consumers must keep their ``is_file()`` check.
    """
    if root.is_file():
        if not root.name.startswith(BROWSER_UPLOAD_PREFIX):
            yield root
        return
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories if not name.startswith(BROWSER_UPLOAD_PREFIX)
        ]
        parent = Path(current)
        for name in directories:
            child = parent / name
            if child.is_symlink():
                yield child
        for name in names:
            if not name.startswith(BROWSER_UPLOAD_PREFIX):
                yield parent / name


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name
