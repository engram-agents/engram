"""Contract cells for the daily compressed transcript archive (part 2 of #4647).

Written BEFORE the implementation (contract-first). Each cell is named test_cN_... after the
contract it pins:

  C1  replay is byte-exact           C8   a file that grows during the run
  C2  segments are contiguous        C9   a file that shrinks during the run
  C3  shrunk transcript is loud      C10  a new file starts at offset 0
  C4  an archive is never replaced   C11  replay into a non-empty dest is refused
  C5  crash safety                   C12  the CLI summary line
  C6  (gitignore + private-dir lists live in test_backup_repo_hygiene.py and
      test_bootstrap_privacy_4445.py; a module-level cell here covers engram_home_perms)
  C7  sleep runs it every time       C13  read_manifest decodes stream 1 only
                                     C14  source absent or empty
                                     C15  modes 0o600 / 0o700

stdlib + pytest only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import lzma
import os
import re
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "src" / "engram" / "tools" / "archive_transcripts.py"
SLEEP_SKILL = REPO / "src" / "engram" / "skills" / "claude" / "engram-sleep" / "SKILL.md"


def _load():
    name = "_archive_transcripts_4647"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


at = _load()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A strictly increasing fake UTC stamp, one second per call (never a stdlib patch)."""
    counter = itertools.count(1)
    monkeypatch.setattr(at, "_stamp", lambda: "20260101T%06dZ" % next(counter))


# ---------------------------------------------------------------- helpers

def _put(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _append(root: Path, rel: str, data: bytes) -> None:
    with open(root / rel, "ab") as f:
        f.write(data)


def _tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _archives(archive_dir: Path) -> list:
    return sorted(p for p in archive_dir.iterdir() if p.name.startswith("transcripts-"))


def _tmps(archive_dir: Path) -> list:
    return sorted(p for p in archive_dir.iterdir() if p.name.startswith(".tmp-"))


def _make_archive(path: Path, segments, payload: bytes) -> None:
    """Hand-build an archive file: manifest stream + payload stream."""
    import json
    lines = [json.dumps({"format": "transcript-archive-1"}, separators=(",", ":"))]
    for rel, start, end in segments:
        lines.append(json.dumps({"path": rel, "start": start, "end": end}, separators=(",", ":")))
    manifest = ("\n".join(lines) + "\n").encode()
    path.write_bytes(lzma.compress(manifest, preset=6) + lzma.compress(payload, preset=6))


def _seed(src: Path) -> dict:
    """The C1 source tree: nested folders, a subagent folder, an empty file, a file whose
    last line has no newline, binary-ish bytes inside a line."""
    _put(src, "proj-a/s1.jsonl", b'{"n":1}\n{"n":2}\n')
    _put(src, "proj-a/s1/subagents/agent-1.jsonl", b'{"sub":1}\n')
    _put(src, "proj-b/s2.jsonl", b'{"a":1}\n{"b":2}')  # no trailing newline
    _put(src, "proj-b/empty.jsonl", b"")
    _put(src, "proj-b/bin.jsonl", b'{"x":"\x00\xff\x80\r\n\x1b"}\n')
    _put(src, "proj-a/notes.md", b"not a transcript\n")  # not .jsonl: out of scope
    return {}


@pytest.fixture
def dirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return src, tmp_path / "archive"


# ------------------------------------------------------------- C1

def test_c1_replay_is_byte_exact(dirs, tmp_path):
    src, arc = dirs
    _seed(src)
    at.archive_run(src, arc)
    _append(src, "proj-a/s1.jsonl", b'{"n":3}\n')
    _append(src, "proj-b/s2.jsonl", b'\n{"c":3}\nmore-no-newline')
    at.archive_run(src, arc)
    _put(src, "proj-c/s3.jsonl", b'{"new":true}\n')
    _append(src, "proj-b/bin.jsonl", b"\x00\x01\x02")
    at.archive_run(src, arc)

    dest = tmp_path / "replayed"
    written = at.replay(arc, dest)

    expected = {k: v for k, v in _tree(src).items() if k.endswith(".jsonl")}
    # An empty transcript holds no bytes to lose and cannot be a segment (start < end):
    # replay leaves it out. Everything else must match byte for byte, and nothing extra.
    expected.pop("proj-b/empty.jsonl")
    assert _tree(dest) == expected
    assert set(written) == set(expected)
    for rel, n in written.items():
        assert n == (src / rel).stat().st_size


# ------------------------------------------------------------- C2

def _three_runs(src: Path, arc: Path):
    sizes = []
    _put(src, "p/a.jsonl", b"one\n" * 10)
    at.archive_run(src, arc)
    sizes.append((src / "p/a.jsonl").stat().st_size)
    _append(src, "p/a.jsonl", b"two\n" * 7)
    at.archive_run(src, arc)
    sizes.append((src / "p/a.jsonl").stat().st_size)
    _append(src, "p/a.jsonl", b"three\n" * 3)
    at.archive_run(src, arc)
    sizes.append((src / "p/a.jsonl").stat().st_size)
    return sizes


def test_c2_segments_are_contiguous(dirs):
    src, arc = dirs
    _seed(src)
    at.archive_run(src, arc)
    _append(src, "proj-a/s1.jsonl", b"x\n")
    _append(src, "proj-b/s2.jsonl", b"y\n")
    at.archive_run(src, arc)
    _append(src, "proj-a/s1.jsonl", b"z\n")
    at.archive_run(src, arc)
    ends = {}
    seen = 0
    for a in _archives(arc):
        for seg in at.read_manifest(a):
            assert seg.start == ends.get(seg.path, 0), seg
            assert seg.end > seg.start
            ends[seg.path] = seg.end
            seen += 1
    assert seen >= 6
    for rel, end in ends.items():
        assert end == (src / rel).stat().st_size


def test_c2_replay_refuses_a_gap(dirs, tmp_path):
    src, arc = dirs
    n1, n2, n3 = _three_runs(src, arc)
    middle = _archives(arc)[1]
    middle.unlink()
    with pytest.raises(at.ReplayGap) as e:
        at.replay(arc, tmp_path / "out")
    msg = str(e.value)
    assert "p/a.jsonl" in msg and str(n1) in msg and str(n2) in msg


def test_c2_replay_refuses_an_overlap(dirs, tmp_path):
    src, arc = dirs
    n1, n2, n3 = _three_runs(src, arc)
    first = _archives(arc)[0]
    (arc / "transcripts-20260101T999999Z-001.xz").write_bytes(first.read_bytes())
    with pytest.raises(at.ReplayGap) as e:
        at.replay(arc, tmp_path / "out")
    msg = str(e.value)
    assert "p/a.jsonl" in msg and str(n3) in msg and "0" in msg


def test_c2_replay_refuses_a_first_segment_above_the_previous_end(dirs, tmp_path):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"abcde\n")
    at.archive_run(src, arc)
    # a hand-built archive whose segment for p/a.jsonl starts at 99, past the previous end (6)
    _make_archive(arc / "transcripts-20260101T999999Z-001.xz", [("p/a.jsonl", 99, 103)], b"WXYZ")
    with pytest.raises(at.ReplayGap) as e:
        at.replay(arc, tmp_path / "out")
    msg = str(e.value)
    assert "p/a.jsonl" in msg and "99" in msg and "6" in msg
    # and a brand-new path whose first segment does not start at 0
    other = tmp_path / "arc2"
    other.mkdir()
    _make_archive(other / "transcripts-20260101T000001Z-001.xz", [("q.jsonl", 5, 9)], b"WXYZ")
    with pytest.raises(at.ReplayGap) as e2:
        at.replay(other, tmp_path / "out2")
    assert "q.jsonl" in str(e2.value) and "5" in str(e2.value)


# ------------------------------------------------------------- C3

def test_c3_shrunk_transcript_is_loud_and_writes_nothing(dirs, capsys):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"0123456789\n" * 5)
    _put(src, "p/b.jsonl", b"bbb\n")
    at.archive_run(src, arc)
    before = _tree(arc)
    (src / "p/a.jsonl").write_bytes(b"0123\n")  # smaller than the archived end
    _put(src, "p/c.jsonl", b"new bytes that would otherwise be archived\n")
    with pytest.raises(at.TranscriptShrank) as e:
        at.archive_run(src, arc)
    assert "p/a.jsonl" in str(e.value) and "55" in str(e.value) and "5" in str(e.value)
    assert _tree(arc) == before  # existing archives untouched, nothing new, no temp file
    assert not _tmps(arc)

    rc = at.main(["--source", str(src), "--archive-dir", str(arc)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.startswith("ERROR:") and "p/a.jsonl" in captured.err
    assert _tree(arc) == before


def test_c3_limit_is_stated_in_the_module_docstring():
    doc = at.__doc__ or ""
    assert "append-only" in doc
    assert "length" in doc and "content" in doc


# ------------------------------------------------------------- C4

def test_c4_same_second_runs_never_overwrite(dirs, monkeypatch):
    src, arc = dirs
    monkeypatch.setattr(at, "_stamp", lambda: "20260202T020202Z")
    _put(src, "p/a.jsonl", b"first\n")
    r1 = at.archive_run(src, arc)
    first_hash = hashlib.sha256(r1.archive_path.read_bytes()).hexdigest()
    _append(src, "p/a.jsonl", b"second\n")
    r2 = at.archive_run(src, arc)
    assert r1.archive_path.name == "transcripts-20260202T020202Z-001.xz"
    assert r2.archive_path.name == "transcripts-20260202T020202Z-002.xz"
    assert hashlib.sha256(r1.archive_path.read_bytes()).hexdigest() == first_hash


def test_c4_preexisting_file_at_the_first_candidate_name_is_kept(dirs, tmp_path, monkeypatch):
    src, arc = dirs
    monkeypatch.setattr(at, "_stamp", lambda: "20260303T030303Z")
    other_src = tmp_path / "other"
    _put(other_src, "elsewhere/z.jsonl", b"zzz\n")
    r0 = at.archive_run(other_src, arc)  # plants a valid -001 for an unrelated path
    assert r0.archive_path.name.endswith("-001.xz")
    planted = r0.archive_path.read_bytes()
    _put(src, "p/a.jsonl", b"mine\n")
    r = at.archive_run(src, arc)
    assert r.archive_path.name == "transcripts-20260303T030303Z-002.xz"
    assert r0.archive_path.read_bytes() == planted


# ------------------------------------------------------------- C5

class _Crash(RuntimeError):
    pass


def test_c5a_crash_before_publish_loses_nothing_and_duplicates_nothing(dirs, tmp_path, monkeypatch):
    src, arc = dirs
    _seed(src)
    real = at._link_final

    def boom(tmp, final):
        raise _Crash("simulated crash after the temp file was fully written")

    monkeypatch.setattr(at, "_link_final", boom)
    with pytest.raises(_Crash):
        at.archive_run(src, arc)
    assert not _archives(arc)
    assert _tmps(arc)
    # (c) a temp file is never read as an archive
    assert at.watermarks(arc) == {}
    assert at.replay(arc, tmp_path / "x") == {}  # a temp file is no archive: nothing to replay

    monkeypatch.setattr(at, "_link_final", real)
    r = at.archive_run(src, arc)
    assert not _tmps(arc)
    assert len(_archives(arc)) == 1
    expected_in = sum(p.stat().st_size for p in src.rglob("*.jsonl"))
    assert r.bytes_in == expected_in
    dest = tmp_path / "out"
    at.replay(arc, dest)
    expected = {k: v for k, v in _tree(src).items() if k.endswith(".jsonl") and not k.endswith("empty.jsonl")}
    assert _tree(dest) == expected


def test_c5b_crash_after_link_before_unlink_is_complete_and_not_rearchived(dirs, tmp_path, monkeypatch):
    src, arc = dirs
    _seed(src)

    def boom(tmp):
        raise _Crash("simulated crash after the link, before the temp unlink")

    real = at._remove_tmp
    monkeypatch.setattr(at, "_remove_tmp", boom)
    with pytest.raises(_Crash):
        at.archive_run(src, arc)
    assert len(_archives(arc)) == 1
    assert _tmps(arc)
    marks = at.watermarks(arc)
    assert marks["proj-a/s1.jsonl"] == (src / "proj-a/s1.jsonl").stat().st_size
    monkeypatch.setattr(at, "_remove_tmp", real)

    r = at.archive_run(src, arc)  # nothing new: the watermark comes from the archive itself
    assert r.archive_path is None and r.bytes_in == 0
    assert len(_archives(arc)) == 1
    assert not _tmps(arc)
    dest = tmp_path / "out"
    at.replay(arc, dest)
    expected = {k: v for k, v in _tree(src).items() if k.endswith(".jsonl") and not k.endswith("empty.jsonl")}
    assert _tree(dest) == expected


def test_c5c_a_temp_file_is_never_read_as_an_archive(dirs, tmp_path):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"hello\n")
    at.archive_run(src, arc)
    marks = at.watermarks(arc)
    # a stray temp file that decodes as a valid archive claiming a far larger segment
    _make_archive(arc / ".tmp-transcripts-20260101T000099Z-1.xz", [("p/a.jsonl", 6, 9000)], b"x" * 8994)
    assert at.watermarks(arc) == marks
    dest = tmp_path / "out"
    at.replay(arc, dest)
    assert (dest / "p/a.jsonl").read_bytes() == b"hello\n"


# ------------------------------------------------------------- C7

def test_c7_sleep_skill_runs_the_archive_every_time_and_reports():
    text = SLEEP_SKILL.read_text(encoding="utf-8")
    assert "### Step 4.8 — Archive the day's transcripts (compressed, kept forever)" in text
    assert text.index("### Step 4.8") > text.index("### Step 4.7")
    assert text.index("### Step 4.8") < text.index("## Phase B")
    assert "tools/archive_transcripts.py" in text
    assert "✓ transcript archive OK: $transcript_archive_out" in text
    assert "⚠ transcript archive FAILED: $transcript_archive_out" in text
    assert "- transcript archive: $transcript_archive_status" in text
    assert "transcript_archive_rc" in text
    assert "--retain" in text.split("### Step 4.8")[1].split("## Phase B")[0]


# ------------------------------------------------------------- C8 / C9 / C10

def test_c8_a_file_that_grows_during_the_run_contributes_only_the_counted_bytes(dirs, tmp_path, monkeypatch):
    src, arc = dirs
    _put(src, "a/one.jsonl", b"counted-bytes\n")
    _put(src, "b/two.jsonl", b"other\n")
    counted = (src / "a/one.jsonl").stat().st_size
    counted_two = (src / "b/two.jsonl").stat().st_size
    real = at._read_chunk
    state = {"grown": False}

    def grow_then_read(f, n):
        # The first read happens while a/one is being read: grow BOTH that file (growth during
        # its own read) and b/two (growth after it was counted but before its read begins).
        if not state["grown"]:
            state["grown"] = True
            _append(src, "a/one.jsonl", b"GROWN-DURING-RUN\n")
            _append(src, "b/two.jsonl", b"ALSO-GROWN\n")
        return real(f, n)

    monkeypatch.setattr(at, "_read_chunk", grow_then_read)
    r1 = at.archive_run(src, arc)
    monkeypatch.setattr(at, "_read_chunk", real)
    assert state["grown"]
    segs = {s.path: s for s in at.read_manifest(r1.archive_path)}
    assert (segs["a/one.jsonl"].start, segs["a/one.jsonl"].end) == (0, counted)
    assert (segs["b/two.jsonl"].start, segs["b/two.jsonl"].end) == (0, counted_two)
    assert r1.bytes_in == counted + counted_two

    r2 = at.archive_run(src, arc)  # the rest goes in the next run
    seg2 = {s.path: s for s in at.read_manifest(r2.archive_path)}
    assert seg2["a/one.jsonl"].start == counted
    assert seg2["a/one.jsonl"].end == (src / "a/one.jsonl").stat().st_size
    dest = tmp_path / "out"
    at.replay(arc, dest)
    assert _tree(dest) == {k: v for k, v in _tree(src).items()}


def test_c9_a_file_that_shrinks_during_the_run_is_a_loud_failure(dirs, monkeypatch):
    src, arc = dirs
    _put(src, "a/one.jsonl", b"0123456789" * 20)
    real = at._read_chunk
    state = {"done": False}

    def shrink_then_read(f, n):
        if not state["done"]:
            state["done"] = True
            os.truncate(src / "a/one.jsonl", 10)
        return real(f, n)

    monkeypatch.setattr(at, "_read_chunk", shrink_then_read)
    with pytest.raises(at.TranscriptShrank) as e:
        at.archive_run(src, arc)
    assert "a/one.jsonl" in str(e.value) and "200" in str(e.value)
    assert not (arc.exists() and (_archives(arc) or _tmps(arc)))


def test_c10_a_new_file_between_runs_starts_at_zero(dirs):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"aaa\n")
    at.archive_run(src, arc)
    _put(src, "p/brand-new.jsonl", b"fresh\n")
    r = at.archive_run(src, arc)
    segs = at.read_manifest(r.archive_path)
    assert [(s.path, s.start, s.end) for s in segs] == [("p/brand-new.jsonl", 0, 6)]


# ------------------------------------------------------------- C11 / C12

def test_c11_replay_into_a_non_empty_dest_is_refused(dirs, tmp_path, capsys):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"aaa\n")
    at.archive_run(src, arc)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "precious.txt").write_text("keep me")
    rc = at.main(["--archive-dir", str(arc), "--replay", str(dest)])
    err = capsys.readouterr().err
    assert rc == 2 and err.startswith("ERROR:") and "not empty" in err
    assert sorted(p.name for p in dest.iterdir()) == ["precious.txt"]
    # an empty or absent dest replays fine through the CLI
    fresh = tmp_path / "fresh"
    assert at.main(["--archive-dir", str(arc), "--replay", str(fresh)]) == 0
    assert (fresh / "p/a.jsonl").read_bytes() == b"aaa\n"


_SUMMARY = re.compile(
    r"^transcript archive: (\d+) files, (\d+) segments, (\d+) bytes -> (\d+) bytes \((\d+\.\d)x\) "
    r"as (transcripts-\d{8}T\d{6}Z-\d{3}\.xz); archive total (\d+) bytes in (\d+) files$"
)


def test_c12_cli_summary_lines(dirs, capsys):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"hello world\n" * 100)
    _put(src, "p/b.jsonl", b"x\n")
    assert at.main(["--source", str(src), "--archive-dir", str(arc)]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    m = _SUMMARY.match(out.rstrip("\n"))
    assert m, out
    files, segs, bin_, bout, ratio, name, total, n = m.groups()
    assert (int(files), int(segs), int(bin_)) == (2, 2, 1202)
    assert (arc / name).stat().st_size == int(bout) == int(total) and int(n) == 1
    assert float(ratio) == round(1202 / int(bout), 1)

    assert at.main(["--source", str(src), "--archive-dir", str(arc)]) == 0
    out = capsys.readouterr().out
    assert out == f"transcript archive: 0 new bytes; archive total {total} bytes in 1 files\n"

    (src / "p/a.jsonl").write_bytes(b"")  # shrank below the watermark
    assert at.main(["--source", str(src), "--archive-dir", str(arc)]) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err.startswith("ERROR: ") and "p/a.jsonl" in cap.err


def test_c12_unexpected_exception_is_a_non_zero_exit(dirs, monkeypatch):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"aaa\n")

    def explode(*a, **k):
        raise PermissionError("boom")

    monkeypatch.setattr(at, "archive_run", explode)
    with pytest.raises(PermissionError):
        at.main(["--source", str(src), "--archive-dir", str(arc)])  # propagates: exit code 1 as a script


# ------------------------------------------------------------- C13

def test_c13_read_manifest_decodes_stream_one_only(tmp_path):
    arc = tmp_path / "arc"
    arc.mkdir()
    good = lzma.compress(b"payload-bytes" * 1000, preset=6)
    broken_stream2 = good[: len(good) // 2]  # a valid xz stream, chopped in half
    import json
    manifest = (
        json.dumps({"format": "transcript-archive-1"}) + "\n"
        + json.dumps({"path": "p/a.jsonl", "start": 0, "end": 13000}) + "\n"
    ).encode()
    path = arc / "transcripts-20260101T000001Z-001.xz"
    path.write_bytes(lzma.compress(manifest, preset=6) + broken_stream2)
    segs = at.read_manifest(path)
    assert [(s.path, s.start, s.end) for s in segs] == [("p/a.jsonl", 0, 13000)]
    assert at.watermarks(arc) == {"p/a.jsonl": 13000}
    with pytest.raises(at.ArchiveError):
        at.replay(arc, tmp_path / "out")


def test_c13_read_manifest_rejects_invalid_archives(tmp_path):
    def archive(manifest: bytes, name="transcripts-20260101T000001Z-001.xz"):
        p = tmp_path / name
        p.write_bytes(lzma.compress(manifest) + lzma.compress(b"abc"))
        return p

    hdr = b'{"format":"transcript-archive-1"}\n'
    with pytest.raises(at.ArchiveError):
        at.read_manifest(archive(b"", "transcripts-20260101T000002Z-001.xz"))
    cases = {
        "wrong format": b'{"format":"other"}\n',
        "no format line": b'{"path":"a.jsonl","start":0,"end":3}\n',
        "bad json": hdr + b"{not json\n",
        "start == end": hdr + b'{"path":"a.jsonl","start":3,"end":3}\n',
        "start > end": hdr + b'{"path":"a.jsonl","start":5,"end":3}\n',
        "negative": hdr + b'{"path":"a.jsonl","start":-1,"end":3}\n',
        "escaping path": hdr + b'{"path":"../a.jsonl","start":0,"end":3}\n',
        "absolute path": hdr + b'{"path":"/etc/a.jsonl","start":0,"end":3}\n',
    }
    for i, (label, body) in enumerate(cases.items()):
        with pytest.raises(at.ArchiveError):
            at.read_manifest(archive(body, "transcripts-20260101T0001%02dZ-001.xz" % i)), label
    garbage = tmp_path / "transcripts-20260101T000300Z-001.xz"
    garbage.write_bytes(b"this is not xz at all")
    with pytest.raises(at.ArchiveError):
        at.read_manifest(garbage)


# ------------------------------------------------------------- C14

def test_c14_source_absent_or_empty_is_a_clean_zero(tmp_path, capsys):
    arc = tmp_path / "arc"
    assert at.main(["--source", str(tmp_path / "nope"), "--archive-dir", str(arc)]) == 0
    assert capsys.readouterr().out == "transcript archive: 0 new bytes; archive total 0 bytes in 0 files\n"
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.md").write_text("no transcripts here")
    assert at.main(["--source", str(empty), "--archive-dir", str(arc)]) == 0
    assert capsys.readouterr().out == "transcript archive: 0 new bytes; archive total 0 bytes in 0 files\n"
    r = at.archive_run(tmp_path / "nope", arc)
    assert r.archive_path is None and (r.files, r.segments, r.bytes_in, r.bytes_out) == (0, 0, 0, 0)


# ------------------------------------------------------------- C15

@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_c15_modes_are_private(dirs):
    src, arc = dirs
    old = os.umask(0o000)  # the tool must not depend on a restrictive umask
    try:
        _put(src, "p/a.jsonl", b"aaa\n")
        r = at.archive_run(src, arc)
    finally:
        os.umask(old)
    assert stat.S_IMODE(arc.stat().st_mode) == 0o700
    assert stat.S_IMODE(r.archive_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_c15_replayed_files_are_private(dirs, tmp_path):
    src, arc = dirs
    _put(src, "p/q/a.jsonl", b"aaa\n")
    at.archive_run(src, arc)
    old = os.umask(0o000)
    try:
        at.replay(arc, tmp_path / "out")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "out/p/q/a.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "out/p/q").stat().st_mode) == 0o700


# ------------------------------------------------------------- scope cells

def test_symlinks_and_non_jsonl_are_out_of_scope(dirs):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"real\n")
    _put(src, "p/b.json", b"{}\n")
    outside = src.parent / "outside.jsonl"
    outside.write_bytes(b"outside\n")
    os.symlink(outside, src / "p/link.jsonl")
    r = at.archive_run(src, arc)
    assert [(s.path) for s in at.read_manifest(r.archive_path)] == ["p/a.jsonl"]


def test_a_transcript_deleted_after_archiving_is_not_an_error(dirs, tmp_path):
    src, arc = dirs
    _put(src, "p/a.jsonl", b"keep me\n")
    _put(src, "p/b.jsonl", b"b\n")
    at.archive_run(src, arc)
    (src / "p/a.jsonl").unlink()
    r = at.archive_run(src, arc)
    assert r.archive_path is None
    dest = tmp_path / "out"
    at.replay(arc, dest)
    assert (dest / "p/a.jsonl").read_bytes() == b"keep me\n"


def test_large_file_is_chunked(dirs, tmp_path, monkeypatch):
    src, arc = dirs
    monkeypatch.setattr(at, "_CHUNK", 64)
    blob = bytes(range(256)) * 20
    _put(src, "p/big.jsonl", blob)
    at.archive_run(src, arc)
    _append(src, "p/big.jsonl", blob[:100])
    at.archive_run(src, arc)
    dest = tmp_path / "out"
    at.replay(arc, dest)
    assert (dest / "p/big.jsonl").read_bytes() == blob + blob[:100]


def test_c6_engram_home_perms_lists_the_private_folder():
    sys.path.insert(0, str(REPO / "src" / "engram"))
    try:
        import engram_home_perms
    finally:
        sys.path.pop(0)
    assert "transcript-archive" in engram_home_perms.DATA_DIRS
