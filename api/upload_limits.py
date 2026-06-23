"""Bounded reads for file uploads.

Reading an ``UploadFile`` with a bare ``await file.read()`` buffers the *entire*
body into memory before any size check runs — a 2 GB upload costs 2 GB of RAM per
request. ``read_upload_capped`` reads in chunks and aborts the moment the limit is
crossed, so memory is bounded by the limit (plus one chunk), not the client.
"""
from fastapi import HTTPException, UploadFile

_CHUNK = 1024 * 1024  # 1 MB


async def read_upload_capped(file: UploadFile, max_bytes: int, *, label: str = "Файл") -> bytes:
    """Read ``file`` in chunks, raising HTTP 413 as soon as it exceeds ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"{label} > {max_bytes // (1024 * 1024)} МБ")
        chunks.append(chunk)
    return b"".join(chunks)
