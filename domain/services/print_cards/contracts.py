"""Application results and domain errors, independent of the web framework."""

from dataclasses import dataclass
from typing import Any, Iterator


class CardError(Exception):
    def __init__(self, code: str, detail: str | dict):
        super().__init__(code, detail)
        self.code = code
        self.detail = detail

    def __str__(self):
        return str(self.detail)


@dataclass(frozen=True)
class AttachmentResult:
    payload: dict[str, Any]
    queued: bool = False
    # Temporary compatibility with the synchronous test-only estimate hook.
    new_geometry: bool = False


@dataclass(frozen=True)
class DeletionResult:
    payload: dict[str, Any]
    object_uris: list[str]


@dataclass(frozen=True)
class DownloadResult:
    stream: Iterator[bytes]
    file_name: str
    content_type: str
    size_bytes: int | None
