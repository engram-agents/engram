"""Uninstall removes ENGRAM's block and keeps the user's text (engram-alpha#4444 contract D1..D5, stage 1).

Contract-first: written before `uninstall-local-marketplace.sh` knows about the block.

  * D1  a pure render (nothing but whitespace outside the block): today's rule stays exactly (save the
        current file to `.pre-uninstall.<UTC>`, restore `.pre-engram`).
  * D2  text outside the block: save the current file FIRST, then remove only the block and the one blank
        line merge put before it. Round trip for an original that ends in a newline; an original with no
        final newline gains exactly one.
  * D2b text AFTER the END line, and CRLF outside lines, are kept as they were.
  * D3  ambiguous markers (B4 shapes): the file is left untouched, one line says why.
  * D3b zero markers: today's #4440 rule unchanged (save, then restore `.pre-engram`).
  * D3c the line naming the `.pre-uninstall` copy when the replaced file did not match this install's text.
  * D4  the shell's marker strings equal the Python constants.
  * D5  the block removal calls the module through the VENV python, BEFORE any `--purge-venv` step; a
        missing or failing venv python leaves the file untouched with one line (never a shell fallback).

Safety (#3519): every cell runs in the FAKE HOME of the existing uninstall rig (stub launchctl,
systemctl, uname, claude, codex, rm first on PATH). No real host CLI, no real service manager. The venv
python is a recording shim planted in the fake home that runs the repo's own interpreter with the repo's
`src/engram` on its path, so the module is found wherever stage 2 chooses to look.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest

from test_uninstall_files_4446 import FRig

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
UNINSTALL_SH = SRC / "tools" / "uninstall-local-marketplace.sh"

# Hand-built blocks use the notice sentence of contract A4 and a real hash, built here with hashlib, so the
# uninstall cells do not depend on the module for their fixtures (D1, D3b stay green on dev for that reason).
NOTICE = b"ENGRAM rewrites everything between these markers; put your own text outside them."
BEGIN_LIT = b"<!-- engram:begin sha256="
SUFFIX_LIT = b" -->"
END_LIT = b"<!-- engram:end -->"
RENDER = b"# Rules\n\nFirst rule.\n\n## Section\n\n- item one\n"
STALE = b"STALE ORIGINAL from the first install\n"


def _hand_block(render: bytes = RENDER) -> bytes:
    body = NOTICE + b"\n" + render
    h = hashlib.sha256(body).hexdigest().encode()
    return BEGIN_LIT + h + SUFFIX_LIT + b"\n" + body + END_LIT + b"\n"


@pytest.fixture
def rig(tmp_path):
    r = FRig(tmp_path)
    _venv_python(r)  # a working venv python by default; the D5 cells replace or remove it
    yield r
    r.teardown()


def _md(rig) -> Path:
    return rig.claude_home / "CLAUDE.md"


def _write_record(rig, path: Path, sha256: str) -> Path:
    """This ENGRAM home's record of the block it wrote (B12): exactly two keys, path and sha256."""
    rec = rig.eh / "claude-md-block.json"
    rec.write_text(json.dumps({"path": os.path.realpath(path), "sha256": sha256}), encoding="utf-8")
    return rec


def _setup(rig, current: bytes, pre_engram: bytes | None = STALE, record: str = "match") -> Path:
    """Plant CLAUDE.md (and the stale first copy). record="match" also writes this home's record (D6)
    naming this file and the hash in the file's first BEGIN line; record=None writes none."""
    rig.claude_home.mkdir(parents=True, exist_ok=True)
    md = _md(rig)
    md.write_bytes(current)
    if pre_engram is not None:
        (rig.claude_home / "CLAUDE.md.pre-engram").write_bytes(pre_engram)
    m = re.search(rb"engram:begin sha256=([0-9a-f]{64})", current)
    if record == "match" and m:
        _write_record(rig, md, m.group(1).decode())
    return md


def _saved(rig) -> list[Path]:
    return sorted(p for p in rig.claude_home.iterdir() if p.name.startswith("CLAUDE.md.pre-uninstall."))


def _venv_python(rig, mode: str = "ok") -> Path:
    """A recording `$HOME/.engram/venv/bin/python3`. mode: ok (run the repo's interpreter on the repo's
    src/engram) or fail (exit 3 with no output). Each call appends its argv to `venv-python.log`."""
    d = rig.venv / "bin"
    d.mkdir(parents=True, exist_ok=True)
    log = rig.state / "venv-python.log"
    if mode == "ok":
        run = f'PYTHONPATH="{SRC}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{sys.executable}" "$@"\n'
    else:
        run = "exit 3\n"
    py = d / "python3"
    py.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\n{run}')
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    return py


def _venv_log(rig) -> list[str]:
    p = rig.state / "venv-python.log"
    return p.read_text().splitlines() if p.exists() else []


def _run(rig, *args: str):
    r = rig.run(*args, extra_env={"PYTHONPATH": str(SRC)})
    assert r.returncode == 0, f"rc={r.returncode}\n--stdout--\n{r.stdout}\n--stderr--\n{r.stderr}"
    return r.stdout + r.stderr


def _blk():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        import engram_claude_block as mod
    except Exception as e:
        raise AssertionError("engram_claude_block is missing: stage 2") from e
    return mod


def _lines_naming(out: str, path: Path) -> list[str]:
    return [ln for ln in out.splitlines() if str(path) in ln]


# ------------------------------------------------------------------------------ D1

@pytest.mark.parametrize("around", [
    pytest.param((b"", b""), id="nothing_outside"),
    pytest.param((b"\n  \n", b"\n\n \t\n"), id="only_whitespace_outside"),
])
def test_D1_regression_a_pure_render_keeps_todays_rule_save_then_restore(rig, around):
    """D1 regression guard (green on dev): nothing but whitespace outside the block -> the existing rule:
    the current file is saved to `.pre-uninstall.<UTC>` and `.pre-engram` is restored over it. If the new
    block logic caught a pure render, the user's first-ever copy would not come back."""
    before, after = around
    planted = before + _hand_block() + after
    md = _setup(rig, planted, pre_engram=b"user original\n")
    out = _run(rig)
    saved = _saved(rig)
    assert len(saved) == 1 and saved[0].read_bytes() == planted
    assert md.read_bytes() == b"user original\n"
    assert str(saved[0]) in out


# ------------------------------------------------------------------------------ D2 / D2b

HAND_ORIGINALS = {
    "lf": b"my rules\nsecond line\n",
    "non_ascii": "café 中文\n".encode(),
    "tabs_and_trailing_spaces": b"a\tb  \n\tindented \n",
}


@pytest.mark.parametrize("name", list(HAND_ORIGINALS))
def test_D2_text_outside_the_block_is_kept_and_only_the_block_and_its_blank_line_go(rig, name):
    """D2: with the user's text outside the block, the current file is saved to `.pre-uninstall.<UTC>`
    and only the block and the ONE blank line merge put before it are removed: the original comes back
    byte for byte, and `.pre-engram` (a stale first copy) is NOT restored over the user's later text.
    The old rule would have restored the stale copy and lost every line the user wrote since."""
    original = HAND_ORIGINALS[name]
    merged = original + b"\n" + _hand_block()
    md = _setup(rig, merged)
    out = _run(rig)
    assert md.read_bytes() == original
    saved = _saved(rig)
    assert len(saved) == 1 and saved[0].read_bytes() == merged and str(saved[0]) in out
    assert (rig.claude_home / "CLAUDE.md.pre-engram").read_bytes() == STALE


def test_D2_an_original_with_no_final_newline_gains_exactly_one_newline(rig):
    """D2 [Q2]: 'my text' + blank line + block -> 'my text\\n'. The cell pins exactly that: the single
    newline the merge had to add is the only difference."""
    md = _setup(rig, b"my text\n\n" + _hand_block())
    _run(rig)
    assert md.read_bytes() == b"my text\n"


@pytest.mark.parametrize("name,original", [
    ("crlf", b"a\r\nb\r\n"),
    ("bom", b"\xef\xbb\xbfmy rules\n"),
    ("tabs_trailing", b"a\tb  \n\tindented \n"),
    ("non_ascii", "café 中文\n".encode()),
    ("no_final_newline", b"my rules"),
])
def test_D2_round_trip_through_the_real_merge_function(rig, name, original):
    """D2 round trip: merge (the module's own) then uninstall. For an original that ends in a newline the
    result is the original byte for byte; with no final newline it gains exactly one. If merge and
    removal disagreed about the blank line, one line would differ."""
    merged = _blk().merge(original.decode("utf-8"), RENDER.decode()).text.encode("utf-8")
    md = _setup(rig, merged)
    _venv_python(rig)
    _run(rig)
    expected = original if original.endswith(b"\n") else original + b"\n"
    assert md.read_bytes() == expected


@pytest.mark.parametrize("original", [b"my rules\n\n", b"my rules\n\n\n", b"a\r\nb\r\n\r\n\r\n"], ids=["one_blank", "two_blank", "crlf_two_blank"])
def test_D2_an_original_ending_in_blank_lines_keeps_them_at_uninstall(rig, original):
    """D2 at uninstall level: only the one blank line merge added goes. The user's own trailing blank lines
    (one, two, CRLF) stay, byte for byte. A removal that stripped all blank lines before the block would
    shorten the file."""
    md = _setup(rig, original + b"\n" + _hand_block())
    _run(rig)
    assert md.read_bytes() == original


def test_D2b_text_after_the_end_line_is_joined_cleanly_to_the_text_before(rig):
    """D2b: text on BOTH sides of the block: the block lines and the one blank line before them go and the
    two sides join cleanly (before + after, nothing else). A removal from the first BEGIN to the end of the
    file (M6) would lose the tail."""
    before, after = b"top one\ntop two\n", b"tail one\ntail two\n"
    md = _setup(rig, before + b"\n" + _hand_block() + after)
    _run(rig)
    assert md.read_bytes() == before + after


def test_D2b_crlf_outside_lines_keep_their_endings(rig):
    """D2b: uninstall removes the block's lines with their own endings and leaves every outside line
    ending as it was (the block is LF inside a CRLF file). A removal that normalised the file to LF would
    change the user's bytes."""
    before, after = b"a\r\nb\r\n", b"tail\r\nmore\r\n"
    md = _setup(rig, before + b"\n" + _hand_block() + after)
    _run(rig)
    assert md.read_bytes() == before + after


# ------------------------------------------------------------------------------ D3 / D3b / D3c

def _shapes():
    B = BEGIN_LIT + b"0" * 64 + SUFFIX_LIT
    E = END_LIT
    return {
        "two_blocks": B + b"\nx\n" + E + b"\n" + B + b"\ny\n" + E + b"\n",
        "begin_without_end": b"top\n" + B + b"\nx\n",
        "end_without_begin": b"top\nx\n" + E + b"\n",
        "near_miss_indented_begin": b"top\n  " + B + b"\nx\n" + E + b"\n",
    }


@pytest.mark.parametrize("shape", list(_shapes()))
def test_D3_a_file_with_ambiguous_markers_is_left_untouched_with_one_line_saying_why(rig, shape):
    """D3: ambiguous markers (a B4 shape): the file is left untouched (no guess, and `.pre-engram` is NOT
    restored over it), exit 0, and exactly one line names the file and says why. The old rule would
    restore the stale first copy over a file ENGRAM cannot read."""
    data = _shapes()[shape]
    md = _setup(rig, data)
    _venv_python(rig)
    out = _run(rig)
    assert md.read_bytes() == data
    named = _lines_naming(out, md)
    assert len(named) == 1 and re.search(r"marker|block", named[0], re.I), named


def test_D3b_regression_zero_markers_keeps_the_4440_rule_save_then_restore(rig):
    """D3b regression guard (green on dev): a file with ZERO markers (a seat installed before this change
    holds an older unwrapped render): the current file is saved to `.pre-uninstall.<UTC>` first, then
    `.pre-engram` is restored if it exists. Narrowing it would strand every pre-change seat."""
    md = _setup(rig, b"agent's edited file\n", pre_engram=b"user original\n")
    out = _run(rig)
    saved = _saved(rig)
    assert len(saved) == 1 and saved[0].read_bytes() == b"agent's edited file\n" and str(saved[0]) in out
    assert md.read_bytes() == b"user original\n"


def test_D3c_a_replaced_file_that_did_not_match_this_installs_text_names_the_saved_copy(rig):
    """D3c: when the file uninstall is about to replace holds bytes that do not equal the render this
    install would write, ONE line says so and names the `.pre-uninstall.<UTC>` copy ('the file you are
    replacing held text that did not match this install's text; it is in <backup>'). Not a refusal: the
    restore still happens. Without the line, the user is not told their text went to a copy."""
    md = _setup(rig, b"agent's edited file\n", pre_engram=b"user original\n")
    _venv_python(rig)
    out = _run(rig)
    saved = _saved(rig)
    assert len(saved) == 1
    hits = [ln for ln in out.splitlines() if "did not match" in ln and str(saved[0]) in ln]
    assert len(hits) == 1, out
    assert md.read_bytes() == b"user original\n"


# ------------------------------------------------------------------------------ D4

def test_D4_the_shell_marker_strings_equal_the_python_constants():
    """D4: the uninstall's marker strings equal the Python constants (one test reads both). Every
    `<!-- engram:begin` literal in the script starts with BEGIN_PREFIX and every `<!-- engram:end` literal
    starts with END_LINE, and the script holds at least one (the contract says it has some). If the shell
    spelled a marker differently, uninstall would find no block that merge wrote (M7)."""
    blk = _blk()
    text = UNINSTALL_SH.read_text(encoding="utf-8")
    begins = re.findall(r"<!-- engram:begin[^\n]*", text)
    ends = re.findall(r"<!-- engram:end[^\n]*", text)
    assert begins or ends, "the uninstall script holds no marker string at all"
    assert all(b.startswith(blk.BEGIN_PREFIX) for b in begins), begins
    assert all(e.startswith(blk.END_LINE) for e in ends), ends


# ------------------------------------------------------------------------------ D5

def test_D5_the_block_is_removed_through_the_venv_python_before_purge_venv_removes_it(rig):
    """D5: the removal calls the module through the VENV python and runs BEFORE any `--purge-venv` step.
    Observable: with `--purge-venv` (and the rig set so the purge really happens) the venv is gone at the
    end, yet the block was removed, and the venv python was called. If the purge ran first, the python
    would be missing when the removal needed it and the file would be left untouched."""
    original = b"my rules\nsecond line\n"
    md = _setup(rig, original + b"\n" + _hand_block())
    _venv_python(rig)
    _run(rig, "--purge-venv")
    assert not rig.venv.exists(), "the purge did not run, so this cell would prove nothing about the order"
    assert _venv_log(rig), "the block removal did not go through the venv python"
    assert md.read_bytes() == original


@pytest.mark.parametrize("mode", ["absent", "failing"])
def test_D5_a_missing_or_failing_venv_python_leaves_the_file_untouched_with_one_line(rig, mode):
    """D5: if the venv's Python is absent or fails, D3 applies: the file is left untouched (no shell
    fallback, `.pre-engram` NOT restored) and one line says why. A shell fallback would guess at the
    block with sed and could cut the user's text."""
    data = b"my rules\n\n" + _hand_block()
    md = _setup(rig, data)
    if mode == "failing":
        _venv_python(rig, "fail")
    else:
        shutil.rmtree(rig.venv)
    out = _run(rig)
    assert md.read_bytes() == data
    named = _lines_naming(out, md)
    assert len(named) == 1 and re.search(r"python|venv", named[0], re.I), named


# ------------------------------------------------------------------------------ D6

def _d6_planted():
    original = b"my rules\nsecond line\n"
    return original, original + b"\n" + _hand_block()


def _assert_left_and_named(rig, md, planted, out):
    assert md.read_bytes() == planted, "uninstall acted on a block this home has no matching record for"
    named = _lines_naming(out, md)
    assert len(named) == 1, named
    assert re.search(r"record", named[0], re.I), named
    assert (rig.claude_home / "CLAUDE.md.pre-engram").read_bytes() == STALE, "the stale first copy was restored over it"


def test_D6_cell1_no_record_in_this_home_leaves_a_valid_block_alone_and_names_the_file(rig):
    """D6 cell 1: a scratch ENGRAM home with NO record and an account file that carries a valid block: the file
    is left untouched and ONE line names it and says no record in this home names the block. Uninstall that
    acted on markers alone would cut a block that another ENGRAM home wrote (M26)."""
    _, planted = _d6_planted()
    md = _setup(rig, planted, record=None)
    out = _run(rig)
    _assert_left_and_named(rig, md, planted, out)
    assert "no record in this ENGRAM home names this file's block" in " ".join(out.split())


def test_D6_cell2_a_record_naming_a_different_path_leaves_the_file_alone_and_names_it(rig):
    """D6 cell 2: the record names ANOTHER CLAUDE.md: left untouched, one line (M27). A record for some other
    account's file must never authorise cutting this one."""
    _, planted = _d6_planted()
    md = _setup(rig, planted, record=None)
    sha = re.search(rb"sha256=([0-9a-f]{64})", planted).group(1).decode()
    _write_record(rig, rig.tmp / "somewhere-else" / "CLAUDE.md", sha)
    _assert_left_and_named(rig, md, planted, _run(rig))


def test_D6_cell3_a_record_whose_hash_differs_from_the_begin_hash_leaves_the_file_alone(rig):
    """D6 cell 3: the recorded hash is not the BEGIN line's (the block was edited or replaced since): left
    untouched, one line; uninstall never guesses and the user's edits stay (M28)."""
    _, planted = _d6_planted()
    md = _setup(rig, planted, record=None)
    _write_record(rig, md, "f" * 64)
    _assert_left_and_named(rig, md, planted, _run(rig))


def test_D6_cell4_the_matching_record_lets_the_block_go_after_the_pre_uninstall_copy(rig):
    """D6 cell 4: the record names this path and the BEGIN hash: the current file is saved to
    `.pre-uninstall.<UTC>` and then only the block and its blank line are removed."""
    original, planted = _d6_planted()
    md = _setup(rig, planted, record=None)
    sha = re.search(rb"sha256=([0-9a-f]{64})", planted).group(1).decode()
    _write_record(rig, md, sha)
    out = _run(rig)
    saved = _saved(rig)
    assert len(saved) == 1 and saved[0].read_bytes() == planted and str(saved[0]) in out
    assert md.read_bytes() == original


def test_D6_cell4b_paths_are_compared_as_real_paths(rig, tmp_path):
    """D6: 'compared as real paths'. CLAUDE_HOME is a symlink to the real folder and the record holds the
    real path: it still matches, so the block goes. A string comparison would leave it."""
    real = rig.tmp / "real-claude-home"
    real.mkdir()
    rig.claude_home.symlink_to(real)
    original, planted = _d6_planted()
    (real / "CLAUDE.md").write_bytes(planted)
    (real / "CLAUDE.md.pre-engram").write_bytes(STALE)
    sha = re.search(rb"sha256=([0-9a-f]{64})", planted).group(1).decode()
    _write_record(rig, real / "CLAUDE.md", sha)
    _run(rig)
    assert (real / "CLAUDE.md").read_bytes() == original


# ------------------------------------------------------------------------------ the "Nothing to uninstall" branch

def test_D2_the_nothing_to_uninstall_branch_removes_the_block_before_it_restores_anything(rig):
    """D2 / D5 on the early-exit branch: with the marketplace tree and version marker already gone, a merged
    file (user text, a blank line, the block) with a matching record loses only the block, the current file
    is saved to `.pre-uninstall.<UTC>` first, and the stale `.pre-engram` is NOT restored over the user's
    text. If that branch restored first (the old order), the user's text would be replaced by the stale copy
    and the block step would find nothing to do."""
    original = b"my rules\nsecond line\n"
    planted = original + b"\n" + _hand_block()
    md = _setup(rig, planted)
    rig.nothing_to_uninstall()
    out = _run(rig)
    assert "Nothing to uninstall" in out
    assert md.read_bytes() == original
    saved = _saved(rig)
    assert len(saved) == 1 and saved[0].read_bytes() == planted and str(saved[0]) in out
    assert (rig.claude_home / "CLAUDE.md.pre-engram").read_bytes() == STALE


@pytest.mark.parametrize("why", ["no_record", "record_hash_differs"])
def test_D6_the_nothing_to_uninstall_branch_leaves_the_file_alone_without_a_matching_record(rig, why):
    """D3 / D6 on the early-exit branch: no record, or a record whose hash is not the BEGIN line's (the block
    was replaced since), means the file is left untouched with one line and the `.pre-engram` copy is NOT restored over
    it, exactly as on the main path (uninstall never guesses). Restoring first would overwrite text ENGRAM
    cannot vouch for."""
    original = b"my rules\nsecond line\n"
    planted = original + b"\n" + _hand_block()
    md = _setup(rig, planted, record=None)
    if why == "record_hash_differs":
        _write_record(rig, md, "f" * 64)
    rig.nothing_to_uninstall()
    out = _run(rig)
    assert "Nothing to uninstall" in out
    assert md.read_bytes() == planted
    named = _lines_naming(out, md)
    assert len(named) == 1 and re.search(r"record", named[0], re.I), named
    assert (rig.claude_home / "CLAUDE.md.pre-engram").read_bytes() == STALE
