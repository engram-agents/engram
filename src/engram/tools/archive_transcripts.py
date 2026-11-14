"""Daily compressed transcript archive: keep Claude Code transcripts forever, replayable.

Archives every ``*.jsonl`` transcript under the source root (default ``~/.claude/projects``,
subagent transcripts included) into ``$ENGRAM_HOME/transcript-archive/``, one xz-compressed
file per run, and can replay all archives back into a folder, byte for byte.

How the cut works. Each run archives, for every transcript, the bytes after the last archived
end offset (the per-file *watermark*) up to the file's size at the moment the run stats it. No
timestamps are involved. Nothing new means no archive file is written.

Archive file: two xz streams in ONE file. Stream 1 is the manifest, one JSON object per line:
first ``{"format":"transcript-archive-1"}``, then one ``{"path":..,"start":N,"end":M}`` per
segment in data order. Stream 2 is the segment bytes concatenated in manifest order, each
exactly ``end-start`` bytes. A reader decodes ONLY stream 1 to learn the offsets of an archive
without decompressing any transcript bytes (``read_manifest``). Offsets are derived, never
recorded separately: the watermark of a path is the ``end`` of its last segment across all
archives in name order. There is no state file to drift.

Names: ``transcripts-<UTC YYYYMMDDTHHMMSSZ>-<NNN>.xz``. Lexicographic order is chronological
order is replay order. An archive is never overwritten: it is written to a temp file in the
same folder, fsynced, then published with ``os.link`` (which fails if the name exists, so the
counter NNN moves on), then the temp file is unlinked. A crash leaves a ``.tmp-*`` file that is
never read as an archive and is removed at the start of the next run.

Loud failures (exit 2, message on stderr, nothing written): a transcript now SMALLER than its
archived end offset (``TranscriptShrank``), a file that comes back short while being read, an
unreadable or malformed archive, and, on replay, a gap or overlap (``ReplayGap``).

Stated limit. The watermark proves LENGTH, not content. Replacing a transcript with a
different file of the same size or larger cannot be detected by size alone, and this tool does
not claim to: content integrity rests on the append-only assumption (Claude Code only appends
to a transcript). Only ``*.jsonl`` files are covered; other file types are not append-only and
are out of scope. A transcript deleted from the source after it was archived stays in the
archive; this tool never deletes anything and has no retention option.

Replay verifies while it goes: it checks the whole chain of manifests for gaps and overlaps
before writing a byte, then checks that every segment's bytes and the stream length match the
manifest.

Usage:
    python3 archive_transcripts.py [--source DIR] [--archive-dir DIR]
    python3 archive_transcripts.py [--archive-dir DIR] --replay DEST
"""

from __future__ import annotations

import argparse
import datetime
import json
import lzma
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# The ENGRAM home has ONE definition: tools/_owner_read.engram_home() (U14, #3862; #3863). `tools` is
# found one directory above this file in BOTH layouts (src/engram/ and the deployed plugin root).
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)
from tools import _owner_read as owner_read  # noqa: E402

FORMAT = "transcript-archive-1"
_CHUNK = 1 << 20  # 1 MiB: every read and every compression step is streamed in chunks
_PRESET = 6
_NAME_RE = re.compile(r"^transcripts-\d{8}T\d{6}Z-\d{3}\.xz$")
_TMP_PREFIX = ".tmp-"
_MAX_COUNTER = 999


class ArchiveError(Exception):
    """Base class: an archive or a run that cannot be trusted."""


class TranscriptShrank(ArchiveError):
    """A transcript is smaller than the bytes already archived from it (or came back short)."""


class ReplayGap(ArchiveError):
    """Replay found a gap or an overlap between consecutive segments of one transcript."""


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


# ------------------------------------------------------------------ seams (tests patch these)

def _stamp() -> str:
    """The UTC stamp used in archive names. A seam: tests patch this, never a stdlib attribute."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_chunk(f, n: int) -> bytes:
    """One read of a source file. A seam: tests grow or shrink the file around it."""
    return f.read(n)


def _link_final(tmp: str, final: str) -> None:
    """Publish: a hard link fails with FileExistsError if `final` exists (create-exclusive)."""
    os.link(tmp, final)


def _remove_tmp(tmp: str) -> None:
    os.unlink(tmp)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------ reading archives

def _archive_files(archive_dir) -> list[Path]:
    """Regular files named like an archive, in name order. Temp files never match."""
    d = Path(archive_dir)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir(), key=lambda q: q.name):
        if _NAME_RE.match(p.name) and p.is_file() and not p.is_symlink():
            out.append(p)
    return out


def _valid_rel_path(rel: object) -> bool:
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        return False
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or rel != "/".join(parts):
        return False
    return all(p not in ("", ".", "..") for p in parts)


def _decode_stream_one(path: Path) -> tuple[bytes, bytes]:
    """Decode the FIRST xz stream of the file only. Returns (manifest bytes, unused tail bytes
    already read past the end of stream 1). Never touches stream 2 beyond the chunk that holds
    the boundary."""
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    out = bytearray()
    try:
        with open(path, "rb") as f:
            while not dec.eof:
                chunk = f.read(_CHUNK)
                if not chunk:
                    raise ArchiveError(f"{path}: archive ends inside the manifest stream")
                out += dec.decompress(chunk)
    except lzma.LZMAError as exc:
        raise ArchiveError(f"{path}: not a valid xz archive ({exc})") from exc
    except OSError as exc:
        raise ArchiveError(f"{path}: unreadable ({exc})") from exc
    return bytes(out), dec.unused_data


def _parse_manifest(path: Path, raw: bytes) -> list[Segment]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveError(f"{path}: manifest is not UTF-8 ({exc})") from exc
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise ArchiveError(f"{path}: manifest is empty, no format line")
    try:
        head = json.loads(lines[0])
    except ValueError as exc:
        raise ArchiveError(f"{path}: manifest format line is not JSON ({exc})") from exc
    if head != {"format": FORMAT}:
        raise ArchiveError(f"{path}: wrong or missing format line, expected {{\"format\":\"{FORMAT}\"}}")
    segs: list[Segment] = []
    for i, line in enumerate(lines[1:], start=2):
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ArchiveError(f"{path}: manifest line {i} is not JSON ({exc})") from exc
        if not isinstance(obj, dict) or set(obj) != {"path", "start", "end"}:
            raise ArchiveError(f"{path}: manifest line {i} must have exactly path, start, end")
        rel, start, end = obj["path"], obj["start"], obj["end"]
        if not _valid_rel_path(rel):
            raise ArchiveError(f"{path}: manifest line {i} has an unsafe or empty path {rel!r}")
        for v in (start, end):
            if isinstance(v, bool) or not isinstance(v, int):
                raise ArchiveError(f"{path}: manifest line {i} offsets must be integers ({rel})")
        if start < 0 or end < 0:
            raise ArchiveError(f"{path}: manifest line {i} has a negative offset for {rel}: {start}..{end}")
        if start >= end:
            raise ArchiveError(f"{path}: manifest line {i} has start >= end for {rel}: {start}..{end}")
        segs.append(Segment(rel, start, end))
    return segs


def read_manifest(archive_path) -> list[Segment]:
    """The segments of one archive, from stream 1 only (no transcript bytes are decompressed)."""
    p = Path(archive_path)
    raw, _unused = _decode_stream_one(p)
    return _parse_manifest(p, raw)


def watermarks(archive_dir) -> dict[str, int]:
    """path -> end of its last segment, across all archives in name order."""
    marks: dict[str, int] = {}
    for a in _archive_files(archive_dir):
        for seg in read_manifest(a):
            marks[seg.path] = seg.end
    return marks


def _archive_total(archive_dir) -> tuple[int, int]:
    files = _archive_files(archive_dir)
    return sum(p.stat().st_size for p in files), len(files)


# ------------------------------------------------------------------ writing an archive

def _cleanup_tmp(archive_dir: Path) -> None:
    if not archive_dir.is_dir():
        return
    for p in archive_dir.iterdir():
        if p.name.startswith(_TMP_PREFIX) and not p.is_dir():
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


def _candidates(source_root: Path) -> list[tuple[str, str]]:
    """(relative POSIX path, absolute path) of every regular, non-symlink *.jsonl, sorted."""
    found: list[tuple[str, str]] = []
    if not source_root.is_dir():
        return found

    def _raise(exc):
        raise exc

    for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False, onerror=_raise):
        dirnames.sort()
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(dirpath, name)
            rel = Path(full).relative_to(source_root).as_posix()
            found.append((rel, full))
    found.sort()
    return found


def _mkdirs_private(path: Path) -> None:
    """Create `path` and any missing parents, each 0o700 (os.makedirs would give parents the
    default mode, and a transcript folder must never be group- or world-readable)."""
    missing = []
    p = Path(path)
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass


def _write_all(fd_file, comp: "lzma.LZMACompressor", data: bytes) -> int:
    out = comp.compress(data)
    if out:
        fd_file.write(out)
    return len(out)


def _write_archive(tmp: str, segments: list[Segment], sources: dict[str, str]) -> None:
    """Write the two-stream archive to `tmp` (create-exclusive, 0o600), fsynced."""
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        lines = [json.dumps({"format": FORMAT}, separators=(",", ":"))]
        for s in segments:
            lines.append(json.dumps({"path": s.path, "start": s.start, "end": s.end}, separators=(",", ":")))
        manifest = ("\n".join(lines) + "\n").encode("utf-8")
        c1 = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=_PRESET)
        out.write(c1.compress(manifest))
        out.write(c1.flush())

        c2 = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=_PRESET)
        for s in segments:
            want = s.end - s.start  # fixed at stat time
            remaining = want
            try:
                f = open(sources[s.path], "rb")
            except FileNotFoundError as exc:
                raise TranscriptShrank(
                    f"{s.path}: disappeared during the run (counted {s.end} bytes at stat time)"
                ) from exc
            with f:
                f.seek(s.start)
                while remaining > 0:
                    chunk = _read_chunk(f, min(_CHUNK, remaining))
                    if not chunk:
                        got = want - remaining
                        raise TranscriptShrank(
                            f"{s.path}: shrank during the run: counted {s.start}..{s.end} "
                            f"({want} bytes) at stat time, only {got} bytes came back"
                        )
                    remaining -= len(chunk)
                    _write_all(out, c2, chunk)
        out.write(c2.flush())
        out.flush()
        os.fsync(out.fileno())


def archive_run(source_root, archive_dir) -> RunResult:
    """Archive the bytes past each transcript's watermark. One new archive file, or none."""
    src = Path(source_root)
    arc = Path(archive_dir)
    _cleanup_tmp(arc)
    marks = watermarks(arc)

    segments: list[Segment] = []
    sources: dict[str, str] = {}
    for rel, full in _candidates(src):
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            continue  # vanished between the walk and the stat: nothing counted, nothing lost
        if not stat.S_ISREG(st.st_mode):
            continue  # a symlink or other non-regular entry is out of scope
        size = st.st_size  # fixed here: the run reads exactly end-start bytes, never more
        mark = marks.get(rel, 0)
        if size < mark:
            raise TranscriptShrank(
                f"{full}: is {size} bytes but {mark} bytes are already archived (path {rel}); "
                f"refusing to archive, nothing written"
            )
        if size > mark:
            segments.append(Segment(rel, mark, size))
            sources[rel] = full
    if not segments:
        return RunResult(None, 0, 0, 0, 0)

    _mkdirs_private(arc)

    stamp = _stamp()
    tmp = os.path.join(str(arc), f"{_TMP_PREFIX}transcripts-{stamp}-{os.getpid()}.xz")
    try:
        _write_archive(tmp, segments, sources)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

    final = None
    for n in range(1, _MAX_COUNTER + 1):
        candidate = os.path.join(str(arc), f"transcripts-{stamp}-{n:03d}.xz")
        try:
            _link_final(tmp, candidate)
        except FileExistsError:
            continue
        final = candidate
        break
    if final is None:
        raise ArchiveError(f"{arc}: no free archive name for stamp {stamp} (tried {_MAX_COUNTER})")
    _remove_tmp(tmp)
    _fsync_dir(str(arc))

    bytes_in = sum(s.end - s.start for s in segments)
    return RunResult(Path(final), len(sources), len(segments), bytes_in, os.stat(final).st_size)


# ------------------------------------------------------------------ replay

def _check_chain(archives: list[Path]) -> list[tuple[Path, list[Segment]]]:
    """Read every manifest and refuse a gap or an overlap, before any byte is written."""
    ends: dict[str, int] = {}
    chain = []
    for a in archives:
        segs = read_manifest(a)
        for s in segs:
            expected = ends.get(s.path, 0)
            if s.start != expected:
                kind = "gap" if s.start > expected else "overlap"
                raise ReplayGap(
                    f"{s.path}: {kind} in {a.name}: previous segment ended at {expected} "
                    f"but this one starts at {s.start}"
                )
            ends[s.path] = s.end
        chain.append((a, segs))
    return chain


def _stream_two(path: Path):
    """Yield the decompressed bytes of stream 2 in chunks. Raises ArchiveError if stream 1 or
    stream 2 is not a complete valid xz stream."""
    d1 = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    d2 = None
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                while chunk:
                    if d2 is None:
                        d1.decompress(chunk)
                        if not d1.eof:
                            break
                        chunk = d1.unused_data
                        d2 = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
                        continue
                    if d2.eof:
                        raise ArchiveError(f"{path}: unexpected data after the second stream")
                    out = d2.decompress(chunk)
                    chunk = b""
                    if out:
                        yield out
                    if d2.eof and d2.unused_data:
                        raise ArchiveError(f"{path}: unexpected data after the second stream")
    except lzma.LZMAError as exc:
        raise ArchiveError(f"{path}: corrupt archive data ({exc})") from exc
    except OSError as exc:
        raise ArchiveError(f"{path}: unreadable ({exc})") from exc
    if d2 is None or not d2.eof:
        raise ArchiveError(f"{path}: archive is truncated, the data stream is incomplete")


def _append_exact(dest: Path, seg: Segment, data_iter, buf: bytearray, path: Path, written: dict) -> None:
    """Write exactly seg.end-seg.start bytes taken from the stream into dest/seg.path."""
    target = dest / seg.path
    want = seg.end - seg.start
    if seg.start == 0:
        _mkdirs_private(target.parent)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ArchiveError(f"{target}: already exists, replay never overwrites") from exc
    else:
        fd = os.open(target, os.O_WRONLY | os.O_APPEND)
    try:
        size = os.fstat(fd).st_size
        if size != seg.start:
            raise ReplayGap(f"{seg.path}: replayed file holds {size} bytes but the next segment starts at {seg.start}")
        remaining = want
        while remaining > 0:
            if not buf:
                try:
                    buf += next(data_iter)
                except StopIteration:
                    raise ArchiveError(
                        f"{path}: data stream ends early inside {seg.path}: "
                        f"needed {want} bytes, {want - remaining} available"
                    ) from None
            take = bytes(buf[:remaining])
            del buf[: len(take)]
            os.write(fd, take)
            remaining -= len(take)
        os.fsync(fd)
    finally:
        os.close(fd)
    written[seg.path] = written.get(seg.path, 0) + want


def replay(archive_dir, dest_root) -> dict[str, int]:
    """Rebuild every archived transcript under dest_root. Returns relative path -> bytes written.

    Refuses a gap or an overlap (ReplayGap) before writing anything. Never overwrites a file.
    """
    dest = Path(dest_root)
    chain = _check_chain(_archive_files(archive_dir))
    written: dict[str, int] = {}
    if chain:
        _mkdirs_private(dest)
    for a, segs in chain:
        it = _stream_two(a)
        buf = bytearray()
        for seg in segs:
            _append_exact(dest, seg, it, buf, a, written)
        # the data stream must be exactly the manifest's length: nothing left over
        if buf:
            raise ArchiveError(f"{a}: data stream is longer than its manifest ({len(buf)} extra bytes)")
        for extra in it:
            if extra:
                raise ArchiveError(f"{a}: data stream is longer than its manifest")
    return written


# ------------------------------------------------------------------ CLI

def _parse_args(argv):
    default_dir = str(owner_read.engram_home() / "transcript-archive")
    p = argparse.ArgumentParser(
        prog="archive_transcripts",
        description="Archive Claude Code transcripts into a compressed, append-only, replayable archive.",
    )
    p.add_argument("--source", default="~/.claude/projects",
                   help="Transcript root (default: ~/.claude/projects).")
    p.add_argument("--archive-dir", default=default_dir,
                   help="Archive directory (default: %(default)s).")
    p.add_argument("--replay", metavar="DEST", default=None,
                   help="Rebuild every archived transcript under DEST (must be empty or absent) and exit.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    arc = os.path.expanduser(args.archive_dir)
    try:
        if args.replay is not None:
            dest = Path(os.path.expanduser(args.replay))
            if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
                raise ArchiveError(f"{dest}: replay destination is not empty, refusing to write into it")
            written = replay(arc, dest)
            total = sum(written.values())
            print(f"transcript archive: replayed {len(written)} files, {total} bytes into {dest}")
            return 0
        res = archive_run(os.path.expanduser(args.source), arc)
        total, n = _archive_total(arc)
        if res.archive_path is None:
            print(f"transcript archive: 0 new bytes; archive total {total} bytes in {n} files")
        else:
            ratio = res.bytes_in / res.bytes_out if res.bytes_out else 0.0
            print(
                f"transcript archive: {res.files} files, {res.segments} segments, "
                f"{res.bytes_in} bytes -> {res.bytes_out} bytes ({ratio:.1f}x) as "
                f"{res.archive_path.name}; archive total {total} bytes in {n} files"
            )
        return 0
    except ArchiveError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
