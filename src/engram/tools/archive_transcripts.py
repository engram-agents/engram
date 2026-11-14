"""STUB (contract-first red step): every public name exists, none is implemented."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ArchiveError(Exception):
    pass


class TranscriptShrank(ArchiveError):
    pass


class ReplayGap(ArchiveError):
    pass


@dataclass(frozen=True)
class Segment:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class RunResult:
    archive_path: Path | None
    files: int
    segments: int
    bytes_in: int
    bytes_out: int


_CHUNK = 1 << 20


def _stamp() -> str:
    raise NotImplementedError


def _read_chunk(f, n):
    raise NotImplementedError


def _link_final(tmp, final):
    raise NotImplementedError


def _remove_tmp(tmp):
    raise NotImplementedError


def archive_run(source_root, archive_dir) -> RunResult:
    raise NotImplementedError


def read_manifest(archive_path) -> list[Segment]:
    raise NotImplementedError


def watermarks(archive_dir) -> dict[str, int]:
    raise NotImplementedError


def replay(archive_dir, dest_root) -> dict[str, int]:
    raise NotImplementedError


def main(argv=None) -> int:
    raise NotImplementedError
