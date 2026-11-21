"""`bootstrap.py --merge` keeps the user's own CLAUDE.md text (engram-alpha#4444, stage 1: the cells).

Contract-first: written BEFORE the feature exists. Every cell runs the REAL `bootstrap.py` as a
subprocess against a tmp HOME (the shape of tests/test_bootstrap_no_clobber_4440.py, copied, not
imported) and compares BYTES, directory trees and the printed lines. Nothing here monkeypatches
the guard or bootstrap.

Cells are named with their contract id (C1..C5, B1..B11, E1..E9, F1/F2, A1/A2/A5/A6, G). Every cell
that depends on the feature asserts first that the flag is KNOWN (so on dev the failure reads
"--merge is not implemented", never a bare returncode). The profile dimension (E3) is parametrized
on every cell that runs a real install: `default` and `local` (`--claude-md-profile local`).

Rules every refusal cell follows: exit non-zero, exactly ONE message line (plus diff lines for B9(b)
only), no traceback, the message names the file, the file's bytes unchanged, and the whole tmp tree
unchanged (no backup, no temp file, nothing seeded). No cell asserts an OS errno text (E6).

Speed: ENGRAM_NO_EMBEDDINGS=1 is set for the child (CI sets it for the whole run); a successful run
is then about a second.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
BOOTSTRAP = SRC / "bootstrap.py"
INSTALL_SH = SRC / "tools" / "install.sh"
DRIFT_TOOL = SRC / "tools" / "check-claude-md-drift.py"
UPGRADE_SKILL = SRC / "skills" / "claude" / "engram-upgrade" / "SKILL.md"

RUNTIME_FILES = [
    "server.py", "engram_client.py", "engram_confidence.py",
    "engram_filter.py", "engram_idf.py", "engram_ids.py",
    "engram_log_emitter.py", "engram_log_indexer.py",
]

BEGIN_LIT = b"<!-- engram:begin sha256="
SUFFIX_LIT = b" -->"
END_LIT = b"<!-- engram:end -->"
BOM = b"\xef\xbb\xbf"
SHA0 = b"0" * 64
FLAG = "--overwrite-edited-block"
OWN_TEXT_RE = re.compile(r"(your|user'?s?) own", re.I)  # the one place the B9(b) wording is encoded
EXTRA_SECTION = "\n## Added by a later version\n\nThree more lines here.\n"
EXTRA_LINES = 4  # "", the heading, "", the sentence: a pure insertion

PROFILES = [pytest.param([], id="default"), pytest.param(["--claude-md-profile", "local"], id="local")]


# ------------------------------------------------------------------------------ harness

def _home(tmp_path: Path) -> Path:
    engram_home = tmp_path / ".engram"
    engram_home.mkdir(parents=True, exist_ok=True)
    for fname in RUNTIME_FILES:
        if (SRC / fname).exists():
            shutil.copy2(SRC / fname, engram_home / fname)
    return tmp_path


def _run(tmp_path: Path, args=(), target: str = "claude", templates: Path | None = None):
    _home(tmp_path)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(templates or (SRC / "templates")),
        "ENGRAM_HOME": str(tmp_path / ".engram"),
        "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "PYTHON_BIN": sys.executable,
        "TARGET": target,
        "ENGRAM_NO_EMBEDDINGS": "1",
    })
    for k in ("ENGRAM_BOOTSTRAP", "FORCE", "FORCE_RESEED_EMPTY"):
        env.pop(k, None)
    return subprocess.run([sys.executable, str(BOOTSTRAP), *args],
                          env=env, capture_output=True, text=True, timeout=120)


def _out(r) -> str:
    return r.stderr + r.stdout


def _lines(r) -> list[str]:
    return [ln for ln in _out(r).splitlines() if ln.strip()]


def _md(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "CLAUDE.md"


def _plant(tmp_path: Path, data: bytes) -> Path:
    md = _md(tmp_path)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_bytes(data)
    return md


def _stamped(parent: Path, name: str = "CLAUDE.md") -> list[Path]:
    return sorted(p for p in parent.iterdir() if p.name.startswith(name + ".engram-backup."))


def _tree(root: Path) -> dict:
    snap = {}
    for dp, dns, fns in os.walk(root, followlinks=False):
        for n in dns + fns:
            p = Path(dp) / n
            rel = str(p.relative_to(root))
            if p.is_symlink():
                snap[rel] = ("L", os.readlink(p))
            elif p.is_dir():
                snap[rel] = ("D",)
            else:
                snap[rel] = ("F", hashlib.sha256(p.read_bytes()).hexdigest())
    return snap


def _fresh_data_home(tmp_path: Path) -> None:
    """Set the ENGRAM data home aside so the next run seeds again (a reinstall); CLAUDE_HOME stays."""
    shutil.rmtree(tmp_path / ".engram")


def _ok(r) -> str:
    assert "unknown argument" not in _out(r), f"--merge/--overwrite-edited-block is not implemented yet: {_out(r)}"
    assert r.returncode == 0, f"rc={r.returncode}\n{_out(r)}"
    return _out(r)


def _assert_refused_untouched(tmp_path: Path, r, md: Path, original: bytes, before: dict, *, one_line: bool = True) -> str:
    out = _out(r)
    assert "unknown argument" not in out, f"--merge/--overwrite-edited-block is not implemented yet: {out}"
    assert r.returncode != 0, f"expected a refusal, got rc=0:\n{out}"
    assert "Traceback" not in out, out
    assert "REFUSED" in out, out
    if one_line:
        assert len(_lines(r)) == 1, f"ONE message line expected, got {len(_lines(r))}:\n{out}"
    assert str(md) in out, f"the message must name the file {md}:\n{out}"
    assert md.read_bytes() == original, "the file's bytes changed"
    assert _tree(tmp_path) == before, "the tmp tree changed (a backup, a temp file, or something seeded)"
    return out


# ------------------------------------------------------------------------------ block reading (no module)

def _spans(data: bytes):
    parts = data.split(b"\n")
    pos, out = 0, []
    for i, part in enumerate(parts):
        end = pos + len(part) + (1 if i < len(parts) - 1 else 0)
        if not (i == len(parts) - 1 and part == b""):
            out.append((pos, end, part))
        pos = end
    return out


def _core(part: bytes, first: bool) -> bytes:
    if first and part.startswith(BOM):
        part = part[len(BOM):]
    return part.rstrip(b" \t\r")


def _split_block(data: bytes):
    """(before, block, after, render) of a file holding exactly one clean block whose hash verifies."""
    spans = _spans(data)
    begins = [i for i, (_, _, p) in enumerate(spans)
              if _core(p, i == 0).startswith(BEGIN_LIT) and _core(p, i == 0).endswith(SUFFIX_LIT)]
    ends = [i for i, (_, _, p) in enumerate(spans) if _core(p, i == 0) == END_LIT]
    assert len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0], \
        f"no ENGRAM block in the file (stage 2 not built, or not one clean block): begins={begins} ends={ends}"
    b, e = begins[0], ends[0]
    begin_core = _core(spans[b][2], b == 0)
    m = re.fullmatch(re.escape(BEGIN_LIT) + rb"([0-9a-f]{64})" + re.escape(SUFFIX_LIT), begin_core)
    assert m, f"BEGIN line without a 64-lower-hex hash: {begin_core!r}"
    begin_start = spans[b][0] + (len(BOM) if b == 0 and data.startswith(BOM) else 0)  # a leading BOM is kept outside the block
    body = data[spans[b][1]:spans[e][0]]
    assert m.group(1).decode() == hashlib.sha256(body.replace(b"\r\n", b"\n")).hexdigest(), \
        "the hash in the BEGIN line is not the SHA-256 of the block body"
    render = data[spans[b + 1][1]:spans[e][0]]  # body minus the one notice line
    return data[:begin_start], data[begin_start:spans[e][1]], data[spans[e][1]:], render


def _install(tmp_path: Path, prof, *, templates: Path | None = None, extra_args=()):
    r = _run(tmp_path, [*prof, *extra_args], templates=templates)
    _ok(r)
    return r


def _baseline(tmp_path: Path, prof, templates: Path | None = None) -> bytes:
    """Install on an absent CLAUDE.md, return what it wrote, then reset so the next step starts clean."""
    _install(tmp_path, prof, templates=templates)
    md = _md(tmp_path)
    data = md.read_bytes()
    md.unlink()
    _fresh_data_home(tmp_path)
    return data


def _templates_with_extra(tmp_path: Path) -> Path:
    """A copy of the templates directory whose two CLAUDE.md templates gain one more `##` section."""
    dst = tmp_path / "templates-later"
    shutil.copytree(SRC / "templates", dst)
    for name in ("template.CLAUDE.md", "template.CLAUDE.local.md"):
        with open(dst / name, "a", encoding="utf-8") as f:
            f.write(EXTRA_SECTION)
    return dst


def _edit_inside(block: bytes, extra: bytes) -> bytes:
    first, second, rest = block.split(b"\n", 2)  # BEGIN, notice, the rest
    return first + b"\n" + second + b"\n" + extra + rest


def _with_begin(block: bytes, begin_line: bytes) -> bytes:
    return begin_line + b"\n" + block.split(b"\n", 1)[1]


def _begin_line(block: bytes) -> bytes:
    return block.split(b"\n", 1)[0]


# ------------------------------------------------------------------------------ B1 / F1 / E3

@pytest.mark.parametrize("merge", [pytest.param([], id="no_flag"), pytest.param(["--merge"], id="merge")])
@pytest.mark.parametrize("prof", PROFILES)
def test_B1_F1_an_absent_file_gets_the_pure_wrapped_render_and_no_backup(tmp_path, prof, merge):
    """B1 + F1 + E3: with no CLAUDE.md the run writes the pure wrapped render: one BEGIN as the first
    line with a hash that verifies, one END as the last non-empty line, one final newline, nothing else
    in the file, and NO backup. If the local profile were left unwrapped (M8) or the flag changed the
    absent case, the first-line or hash check would differ."""
    r = _run(tmp_path, [*prof, *merge])
    _ok(r)
    md = _md(tmp_path)
    data = md.read_bytes()
    before, block, after, _render = _split_block(data)
    assert before == b"" and after == b"" and data == block
    assert data.startswith(BEGIN_LIT) and data.endswith(END_LIT + b"\n") and not data.endswith(b"\n\n")
    assert [ln for ln in data.split(b"\n") if ln.strip()][-1] == END_LIT
    assert not (md.parent / "CLAUDE.md.pre-engram").exists()
    assert _stamped(md.parent) == []


def test_E3_the_two_profiles_write_different_wrapped_renders_both_with_a_true_hash(tmp_path):
    """E3: the `local` profile is wrapped like the default, and its render really is a different one (so
    parametrizing on the profile is not a held-constant dimension). Equal bodies would mean the flag
    selected nothing; an unwrapped local file fails the hash check."""
    full = _baseline(tmp_path, [])
    local = _baseline(tmp_path, ["--claude-md-profile", "local"])
    assert _split_block(full)[3] != _split_block(local)[3]


# ------------------------------------------------------------------------------ B2 / B2b / C1

B2_CASES = {
    "empty": b"",
    "ends_in_newline": b"my own rules\n",
    "no_final_newline": b"my own rules",
    "crlf": b"line one\r\nline two\r\n",
    "bom": BOM + b"my own rules\n",
    "tabs_and_trailing_spaces": b"a\tb  \n\tindented \n",
    "non_ascii": "café 中文 \U0001F600\n".encode("utf-8"),
    "whitespace_only": b"  \n\t\n \n",
}


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("name", list(B2_CASES))
def test_B2_C1_merge_keeps_the_users_bytes_then_one_blank_line_then_the_block_after_both_backups(tmp_path, prof, name):
    """B2 + C1: a markerless file keeps its exact BYTES (CRLF, BOM, no final newline, tabs, non-ASCII,
    empty, whitespace-only), then one blank line, then the block; `CLAUDE.md.pre-engram` and one stamped
    copy both hold the original and both paths are printed. A merge that dropped or re-encoded the text
    (M1) or took no backup would differ."""
    original = B2_CASES[name]
    md = _plant(tmp_path, original)
    out = _ok(_run(tmp_path, [*prof, "--merge"]))
    data = md.read_bytes()
    assert data.startswith(original), "the user's bytes were not kept byte for byte"
    before, block, after, _ = _split_block(data)
    gap = before[len(original):]
    if name == "empty":
        assert gap in (b"", b"\n", b"\r\n")
    elif original.endswith(b"\n"):
        assert gap in (b"\n", b"\r\n"), gap
    else:
        assert gap in (b"\n\n", b"\r\n\r\n"), gap
    assert after == b""
    pre = md.parent / "CLAUDE.md.pre-engram"
    stamped = _stamped(md.parent)
    assert pre.read_bytes() == original and len(stamped) == 1 and stamped[0].read_bytes() == original
    assert str(pre) in out and str(stamped[0]) in out, "both backup paths are printed (C1)"


@pytest.mark.parametrize("prof", PROFILES)
def test_B2b_an_append_prints_the_older_copy_warning_and_an_update_does_not(tmp_path, prof):
    """B2b: appending to a markerless file prints the contract's one sentence (older ENGRAM text still
    there, now twice, remove it by hand); a plain update of an existing block does not. If the line were
    missing (M23) the user would never be told."""
    sentence = ("a block was appended; if this file already held an older ENGRAM text outside a block, "
                "that text is still there and now appears twice; remove the older copy by hand")
    _plant(tmp_path, b"my own rules\n")
    out = _ok(_run(tmp_path, [*prof, "--merge"]))
    assert sentence.lower() in " ".join(out.lower().split()), out
    _fresh_data_home(tmp_path)
    out2 = _ok(_run(tmp_path, [*prof, "--merge"]))
    assert "now appears twice" not in out2


@pytest.mark.parametrize("prof", PROFILES)
def test_C1_the_backups_are_taken_before_the_first_write(tmp_path, prof):
    """C1: 'the backups ... are taken BEFORE the first write'. Observable: when the folder cannot take
    the backup files (read-only), the run refuses and CLAUDE.md is not written. A merge that wrote first
    and backed up after would have changed the file (an in-place write needs only the file's own
    permission). The cell asserts the refusal and the bytes, never an OS error text (E6)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions, so this ordering cannot be observed here")
    md = _plant(tmp_path, b"my own rules\n")
    _home(tmp_path)
    before = _tree(tmp_path)
    os.chmod(md.parent, 0o555)
    try:
        r = _run(tmp_path, [*prof, "--merge"])
    finally:
        os.chmod(md.parent, 0o755)
    out = _out(r)
    assert "unknown argument" not in out, f"--merge is not implemented yet: {out}"
    assert r.returncode != 0 and "REFUSED" in out, out
    assert md.read_bytes() == b"my own rules\n"
    assert _tree(tmp_path) == before


# ------------------------------------------------------------------------------ C2 .. C5

@pytest.mark.parametrize("prof", PROFILES)
def test_C2_the_refusal_without_a_flag_names_merge_beside_replace_and_force(tmp_path, prof):
    """C2: an existing differing CLAUDE.md and no flag: nothing changes (as today) and the refusal now
    names `--merge` as the option that keeps the user's own text, next to `--replace` and `--force`.
    Without the new words a user is only offered options that discard their text."""
    md = _plant(tmp_path, b"my own rules\n")
    _home(tmp_path)
    before = _tree(tmp_path)
    r = _run(tmp_path, prof)
    out = _out(r)
    assert r.returncode != 0 and md.read_bytes() == b"my own rules\n" and _tree(tmp_path) == before
    assert "--merge" in out and "--replace" in out and "--force" in out, out


def test_C2b_the_merge_hint_is_only_in_a_claude_md_refusal(tmp_path):
    """C2 family: the `--merge` hint belongs to a refusal that lists CLAUDE.md. A refusal that lists only the
    Codex config.toml (where --merge is refused) must not offer it; the CLAUDE.md refusal still names it.
    A hint printed for every path list would send a Codex user to a flag that is then refused."""
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_bytes(b'model = "x"\n')
    r = _run(tmp_path, target="codex")
    out = _out(r)
    assert r.returncode != 0 and "REFUSED" in out and str(cfg) in out, out
    assert "--merge" not in out, out
    other = tmp_path / "claude-home"
    other.mkdir()
    _plant(other, b"my own rules\n")
    r2 = _run(other)
    assert r2.returncode != 0 and "--merge" in _out(r2), _out(r2)


@pytest.mark.parametrize("prof", PROFILES)
def test_C3_force_together_with_merge_writes_the_merge_text_and_keeps_outside_text(tmp_path, prof):
    """C3: `--force --merge` writes the MERGE text; a force never discards text outside the block when
    --merge is given (M5). If --force won, the user's own line would be gone."""
    md = _plant(tmp_path, b"my own rules\n")
    _ok(_run(tmp_path, [*prof, "--force", "--merge"]))
    data = md.read_bytes()
    assert data.startswith(b"my own rules\n")
    _split_block(data)


def _help_text(tmp_path: Path) -> str:
    r = _run(tmp_path, ["--help"])
    assert r.returncode == 0, f"--help did not exit 0:\n{_out(r)}"
    return _out(r)


def test_C3_help_says_that_force_with_merge_never_discards_text_outside_the_block(tmp_path):
    """C3: `--help` says so. A help that only listed the flags would leave --force looking like the
    destructive one it is without --merge."""
    h = _help_text(tmp_path)
    sentences = re.split(r"(?<=[.;])\s+|\n\n", " ".join(h.split()))
    assert any("--force" in s and "--merge" in s and "outside" in s.lower() for s in sentences), h


def test_B9b_help_says_the_refusal_prints_lines_of_the_users_own_file(tmp_path):
    """B9(b) disclosure: `--help` says in one sentence that a refusal prints lines of the user's own
    file (Zephyr 34061: output can be redirected to a log). Without it nobody knows what a log may hold."""
    h = _help_text(tmp_path)
    assert FLAG in h and OWN_TEXT_RE.search(h), h


def test_B11_A4_help_states_the_rule_that_text_you_add_belongs_outside_the_markers(tmp_path):
    """B11 / A4: `--help` states, in plain words, that text you add belongs outside the markers."""
    h = " ".join(_help_text(tmp_path).split())
    assert re.search(r"outside (the|these|its) markers", h, re.I), h


def test_E8_help_states_the_known_limit_about_an_older_unwrapped_render(tmp_path):
    """E8: the known limit is stated in `--help`: an OLDER unwrapped ENGRAM render plus --merge gets the
    block appended, so the ENGRAM text appears twice. Without it the limit exists only in a contract."""
    h = " ".join(_help_text(tmp_path).split()).lower()
    assert "older" in h and "twice" in h, h


def test_C4_merge_with_the_codex_target_is_refused_in_one_line_and_nothing_changes(tmp_path):
    """C4: `--merge` with the Codex target: one line, nothing changed (config.toml already merges;
    AGENTS.md is #4452). If it were accepted, Codex would get a Claude-style block in config.toml."""
    _home(tmp_path)
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_bytes(b'model = "x"\n')
    before = _tree(tmp_path)
    r = _run(tmp_path, ["--merge"], target="codex")
    out = _out(r)
    assert "unknown argument" not in out, f"--merge is not implemented yet: {out}"
    assert r.returncode != 0 and "Traceback" not in out
    assert len(_lines(r)) == 1, out
    assert "--merge" in out
    assert cfg.read_bytes() == b'model = "x"\n' and _tree(tmp_path) == before


@pytest.mark.parametrize("prof", PROFILES)
def test_C4b_merge_with_replace_of_another_path_is_refused_in_one_line(tmp_path, prof):
    """C4b: `--merge` together with `--replace PATH` for any path other than the CLAUDE.md path is
    refused in one line, nothing changed. If it were accepted, --merge would quietly stand for consent
    to replace a file it knows nothing about."""
    md = _plant(tmp_path, b"my own rules\n")
    other = tmp_path / "other.md"
    other.write_bytes(b"not claude\n")
    _home(tmp_path)
    before = _tree(tmp_path)
    r = _run(tmp_path, [*prof, "--merge", "--replace", str(other)])
    out = _out(r)
    assert "unknown argument" not in out, f"--merge is not implemented yet: {out}"
    assert r.returncode != 0 and "Traceback" not in out and len(_lines(r)) == 1, out
    assert str(other) in out
    assert md.read_bytes() == b"my own rules\n" and _tree(tmp_path) == before


@pytest.mark.parametrize("prof", PROFILES)
def test_C4b_control_replace_of_the_claude_md_path_itself_is_allowed_with_merge(tmp_path, prof):
    """C4b control: `--merge --replace <the CLAUDE.md path>` is the same consent as --merge alone and is
    accepted. A check that refused every --replace with --merge would pass the refusal cell above."""
    md = _plant(tmp_path, b"my own rules\n")
    _ok(_run(tmp_path, [*prof, "--merge", "--replace", str(md)]))
    assert md.read_bytes().startswith(b"my own rules\n")


@pytest.mark.parametrize("flag", [pytest.param(["--replace", "@MD@"], id="replace"), pytest.param(["--force"], id="force")])
def test_C4b_regression_replace_and_force_without_merge_still_replace_the_whole_file(tmp_path, flag):
    """C4b regression guard (green on dev): WITHOUT --merge, --replace/--force keep today's meaning:
    backups first, then the whole file is replaced. The user's text is gone from CLAUDE.md and kept in
    the backups."""
    md = _plant(tmp_path, b"my own rules\n")
    args = [str(md) if a == "@MD@" else a for a in flag]
    _ok(_run(tmp_path, args))
    assert b"my own rules" not in md.read_bytes()
    assert (md.parent / "CLAUDE.md.pre-engram").read_bytes() == b"my own rules\n"


@pytest.mark.parametrize("shape", ["two_blocks", "edited_block"])
@pytest.mark.parametrize("prof", PROFILES)
def test_C5_a_refusal_happens_before_the_first_write_of_any_path(tmp_path, prof, shape):
    """C5: a refusal anywhere in the run happens before the first write of ANY path: nothing is seeded
    (no database, manifest, briefing or marker), no backup, no temp file. If the CLAUDE.md check ran
    after seeding, those would exist."""
    if shape == "two_blocks":
        data = BEGIN_LIT + SHA0 + SUFFIX_LIT + b"\nx\n" + END_LIT + b"\n" + BEGIN_LIT + SHA0 + SUFFIX_LIT + b"\ny\n" + END_LIT + b"\n"
    else:
        block = _baseline(tmp_path, prof)
        data = _edit_inside(block, b"MY OWN EDIT\n")
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    r = _run(tmp_path, [*prof, "--merge"])
    out = _out(r)
    assert "unknown argument" not in out, f"--merge is not implemented yet: {out}"
    assert r.returncode != 0
    for name in ("knowledge.db", "seed-manifest.json", "warm-briefing.md", "first-run-pending"):
        assert not (tmp_path / ".engram" / name).exists(), name
    assert md.read_bytes() == data and _tree(tmp_path) == before


# ------------------------------------------------------------------------------ B3

B3_CASES = {
    "nothing_around": (b"", b""),
    "text_both_sides": (b"my rules\n\n", b"\nafter\n"),
    "after_has_no_final_newline": (b"my rules\n\n", b"tail without newline"),
    "crlf_outside": (b"a\r\nb\r\n\r\n", b"\r\nc\r\nd\r\n"),
    "bom_on_the_first_line": (BOM + b"my rules\n\n", b"\n"),
    "bom_directly_before_begin": (BOM, b"after\n"),
    "tabs_and_trailing_spaces": (b"a\tb  \n\t\n", b" tail \t\n"),
    "non_ascii": ("café 中文\n\n".encode(), "☃ end\n".encode()),
}


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("name", list(B3_CASES))
def test_B3_only_the_block_changes_the_bytes_before_and_after_it_stay(tmp_path, prof, name):
    """B3 end to end: a one-block file whose block is out of date (a later version added a section):
    the bytes before BEGIN and after END are identical, the block is the new one (hash verifies). A merge
    that normalised line endings or dropped the BOM / the unterminated tail would differ."""
    before, after = B3_CASES[name]
    old_block = _split_block(_baseline(tmp_path, prof))[1]
    md = _plant(tmp_path, before + old_block + after)
    templates = _templates_with_extra(tmp_path)
    _ok(_run(tmp_path, [*prof, "--merge"], templates=templates))
    b2, block2, a2, render2 = _split_block(md.read_bytes())
    assert b2 == before and a2 == after, "the bytes outside the block changed"
    assert b"Three more lines here." in render2 and block2 != old_block


# ------------------------------------------------------------------------------ B4

def _b4_shapes():
    B = BEGIN_LIT + SHA0 + SUFFIX_LIT
    E = END_LIT
    return {
        "two_blocks": (B + b"\nx\n" + E + b"\n" + B + b"\ny\n" + E + b"\n", {1, 4}),
        "two_end_lines": (B + b"\nx\n" + E + b"\n" + E + b"\n", {3, 4}),
        "begin_without_end": (b"top\n" + B + b"\nx\n", {2}),
        "end_without_begin": (b"top\nx\n" + E + b"\n", {3}),
        "end_before_begin": (E + b"\nx\n" + B + b"\n", {1, 3}),
        "interleaved": (B + b"\na\n" + E + b"\nmid\n" + B + b"\nb\n" + E + b"\n", {1, 5}),
        "near_miss_indented_begin": (b"top\n  " + B + b"\nx\n" + E + b"\n", {2}),
        "near_miss_indented_end": (B + b"\nx\n\t" + E + b"\n", {3}),
    }


B4_SHAPES = _b4_shapes()


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("shape", list(B4_SHAPES))
def test_B4_an_ambiguous_marker_shape_is_refused_with_nothing_written(tmp_path, prof, shape):
    """B4 end to end: two or more BEGIN, two or more END, BEGIN without END, END without BEGIN, END
    before BEGIN, interleaved, or an indented marker: exit non-zero, ONE line naming the file and the
    line numbers, bytes unchanged, tree unchanged (no backup, nothing seeded). A finder that took the
    last END (M2) would write."""
    data, numbers = B4_SHAPES[shape]
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    out = _assert_refused_untouched(tmp_path, _run(tmp_path, [*prof, "--merge"]), md, data, before)
    named = {int(n) for n in re.findall(r"\d+", out)}
    assert numbers <= named, f"message must name line(s) {sorted(numbers)}: {out}"


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("flags", [
    pytest.param(["--merge", FLAG], id="merge_overwrite"),
    pytest.param(["--force", "--merge"], id="force_merge"),
    pytest.param(["--force", "--merge", FLAG], id="force_merge_overwrite"),
])
def test_B4_outranks_every_consent_two_blocks_are_never_written(tmp_path, prof, flags):
    """B4 / B10 / M5 / M14 / M21: on a two-block file `--merge --overwrite-edited-block`, `--force
    --merge` and both together write nothing and take no backup. A flag that turned an ambiguous marker
    state into a write would destroy a block ENGRAM cannot tell from the user's."""
    data, _ = B4_SHAPES["two_blocks"]
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    _assert_refused_untouched(tmp_path, _run(tmp_path, [*prof, *flags]), md, data, before)


CLEAN_VARIANTS = {
    "begin_trailing_spaces": lambda block: _with_begin(block, _begin_line(block) + b"  "),
    "begin_trailing_tab": lambda block: _with_begin(block, _begin_line(block) + b"\t"),
    "begin_trailing_cr": lambda block: _with_begin(block, _begin_line(block) + b"\r"),
    "end_trailing_spaces_and_tab": lambda block: block[: -len(END_LIT) - 1] + END_LIT + b" \t\n",
    "bom_before_begin_on_line_1": lambda block: BOM + block,
}


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("variant", list(CLEAN_VARIANTS))
def test_B4_control_trailing_whitespace_or_a_leading_bom_on_a_marker_is_a_clean_marker(tmp_path, prof, variant):
    """B4 control (M9, M15): editor noise on a marker line is NOT ambiguity: the one block is found and
    updated, the BOM (if any) kept, and no second block is appended. If those were treated as text the
    file would be appended to (two blocks) or refused."""
    block = _split_block(_baseline(tmp_path, prof))[1]
    md = _plant(tmp_path, CLEAN_VARIANTS[variant](block))
    _ok(_run(tmp_path, [*prof, "--merge"], templates=_templates_with_extra(tmp_path)))
    data = md.read_bytes()
    _, _, _, render = _split_block(data)
    assert b"Three more lines here." in render
    if variant.startswith("bom"):
        assert data.startswith(BOM)


@pytest.mark.parametrize("prof", PROFILES)
def test_B4_control_a_sentence_that_mentions_a_marker_is_ordinary_text(tmp_path, prof):
    """Definitions control: marker text inside a longer line is ordinary text; the file is appended to,
    not refused, and the user's lines survive byte for byte."""
    original = b"notes: " + END_LIT + b" closes it,\nand " + BEGIN_LIT + b"<hash> -->\nstarts it\n"
    md = _plant(tmp_path, original)
    _ok(_run(tmp_path, [*prof, "--merge"]))
    assert md.read_bytes().startswith(original)
    _split_block(md.read_bytes())


# ------------------------------------------------------------------------------ B5

@pytest.mark.parametrize("prof", PROFILES)
def test_B5_a_file_that_is_not_utf8_text_is_refused_in_one_line_untouched(tmp_path, prof):
    """B5: a file that is not UTF-8 text is refused (as the Codex merge does today): one line naming the
    file, bytes and tree unchanged. A merge that decoded with errors='replace' would rewrite the user's
    bytes."""
    data = b"\xff\xfe not utf-8 \x80\x81\n"
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    _assert_refused_untouched(tmp_path, _run(tmp_path, [*prof, "--merge"]), md, data, before)


# ------------------------------------------------------------------------------ B6 / B7 / E4

B6_STARTS = {
    "lf_text": b"my own rules\n",
    "crlf_text": b"line one\r\nline two\r\n",
    "no_final_newline": b"my own rules",
    "bom_non_ascii": BOM + "café 中文\n".encode(),
    "whitespace_only": b"  \n\t\n",
}


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("name", list(B6_STARTS))
def test_B6_E4_a_second_merge_run_leaves_bytes_mtime_and_backups_alone(tmp_path, prof, name):
    """B6 + E4: a second `--merge` run (after the data home is set aside, as a reinstall) leaves the
    file's BYTES and its MODIFICATION TIME alone and creates no new backup. That also proves the text the
    check-only pass predicted is the text the first run wrote (E4): a difference would make the second run
    see 'differs', take a backup and rewrite. A CRLF file would expose a read that translated line endings."""
    md = _plant(tmp_path, B6_STARTS[name])
    _ok(_run(tmp_path, [*prof, "--merge"]))
    first = md.read_bytes()
    os.utime(md, (1_000_000_000, 1_000_000_000))
    names_before = sorted(p.name for p in md.parent.iterdir())
    _fresh_data_home(tmp_path)
    _ok(_run(tmp_path, [*prof, "--merge"]))
    assert md.read_bytes() == first
    assert md.stat().st_mtime_ns == 1_000_000_000 * 10**9, "the file was rewritten"
    assert sorted(p.name for p in md.parent.iterdir()) == names_before, "a new backup (or other file) appeared"


@pytest.mark.parametrize("merge", [pytest.param([], id="no_flag"), pytest.param(["--merge"], id="merge")])
@pytest.mark.parametrize("prof", PROFILES)
def test_B7_a_file_that_already_equals_the_new_text_is_not_written_or_backed_up(tmp_path, prof, merge):
    """B7: the file already equals the text this run would write (the pure render from an earlier run):
    nothing is written, nothing is backed up, and the run is not refused. M4: a merge that 'writes' an
    identical file would take a backup and move the mtime."""
    md = _md(tmp_path)
    _install(tmp_path, prof, extra_args=merge)
    first = md.read_bytes()
    _split_block(first)
    os.utime(md, (1_000_000_000, 1_000_000_000))
    _fresh_data_home(tmp_path)
    _ok(_run(tmp_path, [*prof, *merge]))
    assert md.read_bytes() == first and md.stat().st_mtime_ns == 1_000_000_000 * 10**9
    assert not (md.parent / "CLAUDE.md.pre-engram").exists() and _stamped(md.parent) == []


# ------------------------------------------------------------------------------ B9 / B10

UPDATED_RE = re.compile(r"ENGRAM block updated \((\d+) lines? changed\); the previous file is in (\S+)")


@pytest.mark.parametrize("prof", PROFILES)
def test_B9a_an_untouched_block_is_replaced_and_one_line_reports_count_and_backup(tmp_path, prof):
    """B9(a): hash equals the current body -> the block is replaced; exactly ONE line 'ENGRAM block
    updated (N lines changed); the previous file is in <backup>' names a backup whose bytes are the file
    as it was; the user's text on both sides survives byte for byte. A merge that wrote with no line (M12)
    or reported the wrong count would differ."""
    old_block = _split_block(_baseline(tmp_path, prof))[1]
    planted = b"top note\n\n" + old_block + b"\nbottom note\n"
    md = _plant(tmp_path, planted)
    out = _ok(_run(tmp_path, [*prof, "--merge"], templates=_templates_with_extra(tmp_path)))
    hits = UPDATED_RE.findall(out)
    assert len(hits) == 1, f"exactly one report line expected: {out}"
    n, backup = hits[0]
    assert int(n) == EXTRA_LINES
    assert Path(backup).read_bytes() == planted, "the backup named in the line is not the previous file"
    before, _, after, render = _split_block(md.read_bytes())
    assert before == b"top note\n\n" and after == b"\nbottom note\n" and b"Three more lines here." in render
    assert (md.parent / "CLAUDE.md.pre-engram").read_bytes() == planted
    assert [p.read_bytes() for p in _stamped(md.parent)] == [planted]


@pytest.mark.parametrize("prof", PROFILES)
def test_B9a_control_a_body_edited_and_then_edited_back_is_state_a(tmp_path, prof):
    """B9 control: an edit that was edited back leaves the original body, so the hash matches and the
    block is replaced, not refused. A check that remembered 'it was touched' (instead of hashing the
    current body) would refuse here."""
    block = _split_block(_baseline(tmp_path, prof))[1]
    md = _plant(tmp_path, _edit_inside(block, b"TEMP EDIT\n"))
    md.write_bytes(block)  # edited back
    _ok(_run(tmp_path, [*prof, "--merge"], templates=_templates_with_extra(tmp_path)))
    assert b"Three more lines here." in _split_block(md.read_bytes())[3]


@pytest.mark.parametrize("prof", PROFILES)
def test_B9a_control_a_crlf_converted_block_is_state_a_and_the_text_outside_keeps_its_crlf(tmp_path, prof):
    """B9 control / E7 / M18: an editor converted the whole file to CRLF; CRLF is read as LF, so the hash
    still matches and the block is replaced, while the user's CRLF lines outside stay as they were."""
    block = _split_block(_baseline(tmp_path, prof))[1]
    before, after = b"a\r\nb\r\n\r\n", b"\r\nz\r\n"
    md = _plant(tmp_path, before + block.replace(b"\n", b"\r\n") + after)
    _ok(_run(tmp_path, [*prof, "--merge"], templates=_templates_with_extra(tmp_path)))
    b2, _, a2, render = _split_block(md.read_bytes())
    assert b2 == before and a2 == after and b"Three more lines here." in render


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("flags", [pytest.param(["--merge"], id="merge"), pytest.param(["--force", "--merge"], id="force_merge")])
def test_B9b_an_edit_inside_the_block_is_refused_with_count_diff_and_flag_and_nothing_written(tmp_path, prof, flags):
    """B9(b): someone edited inside the block (readable hash that differs): refused, nothing written, NO
    backup, exit non-zero. First line: says it prints lines of the user's own file, gives the count of
    changed lines (2) and names the flag; the diff lines follow and carry the user's text. `--force` does
    not override (Kepler). A merge that replaced the edited block (M16, M19) or left a backup (M20) differs."""
    block = _split_block(_baseline(tmp_path, prof))[1]
    data = _edit_inside(block, b"MY OWN EDIT 1\nMY OWN EDIT 2\n")
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    r = _run(tmp_path, [*prof, *flags])
    out = _assert_refused_untouched(tmp_path, r, md, data, before, one_line=False)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert OWN_TEXT_RE.search(lines[0]) and FLAG in lines[0], lines[0]
    assert "2" in re.findall(r"\d+", lines[0]), "the count of changed lines (2) is in the first line"
    diff = lines[1:]
    assert diff and all(ln[:1] in "-+ @" for ln in diff), diff
    assert any("MY OWN EDIT 1" in ln for ln in diff)
    assert len(diff) <= 40


@pytest.mark.parametrize("prof", PROFILES)
def test_B9b_an_edit_of_one_character_in_the_notice_line_is_state_b(tmp_path, prof):
    """B9: 'an edit of one character in the notice line is (b)': refused, nothing written. A hash that
    left the notice out would let that edit be overwritten silently."""
    block = _split_block(_baseline(tmp_path, prof))[1]
    first, notice, rest = block.split(b"\n", 2)
    data = first + b"\n" + notice[:-1] + b"X\n" + rest
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    out = _assert_refused_untouched(tmp_path, _run(tmp_path, [*prof, "--merge"]), md, data, before, one_line=False)
    assert OWN_TEXT_RE.search(out.splitlines()[0])


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("variant", ["empty_hash", "not_hex", "uppercase_hex", "too_short", "too_long"])
@pytest.mark.parametrize("flags", [pytest.param(["--merge"], id="merge"), pytest.param(["--force", "--merge"], id="force_merge")])
def test_B9c_a_begin_line_with_no_readable_hash_is_refused_like_b_and_names_the_cause(tmp_path, prof, variant, flags):
    """B9(c): a BEGIN line whose hash is missing, not 64 hex, or not LOWER-case hex is a damaged marker or
    a block ENGRAM did not write: refused exactly like (b), the cause in words and the flag named; --force
    does not override. Treating it as (a) (M17) would overwrite a hand-written block."""
    block = _split_block(_baseline(tmp_path, prof))[1]
    good = _begin_line(block)[len(BEGIN_LIT):-len(SUFFIX_LIT)]
    bad = {"empty_hash": b"", "not_hex": b"zzz", "uppercase_hex": good.upper(),
           "too_short": good[:-1], "too_long": good + b"0"}[variant]
    data = _with_begin(block, BEGIN_LIT + bad + SUFFIX_LIT)
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    out = _assert_refused_untouched(tmp_path, _run(tmp_path, [*prof, *flags]), md, data, before, one_line=False)
    assert "did not write" in out and "damaged marker" in out and FLAG in out, out


def test_B10_the_overwrite_flag_alone_is_refused_in_one_line_nothing_changed(tmp_path):
    """B10: `--overwrite-edited-block` is valid only together with `--merge`; alone it is refused in one
    line and nothing changes. If it were silently ignored, a user could believe the flag did something."""
    md = _plant(tmp_path, b"my own rules\n")
    _home(tmp_path)
    before = _tree(tmp_path)
    r = _run(tmp_path, [FLAG])
    out = _out(r)
    assert "unknown argument" not in out, f"{FLAG} is not implemented yet: {out}"
    assert r.returncode != 0 and "Traceback" not in out and len(_lines(r)) == 1, out
    assert FLAG in out and "--merge" in out
    assert md.read_bytes() == b"my own rules\n" and _tree(tmp_path) == before


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("state", ["b_edited", "c_damaged"])
def test_B10_the_flag_writes_states_b_and_c_after_both_backups_and_prints_the_update_line(tmp_path, prof, state):
    """B10: with `--merge --overwrite-edited-block`, (b) and (c) are written after BOTH backups and the
    same one line as (a) is printed with the backup path; the backup holds the edited file. A flag that
    skipped the backups would destroy the user's edit."""
    block = _split_block(_baseline(tmp_path, prof))[1]
    if state == "b_edited":
        planted = b"top note\n\n" + _edit_inside(block, b"MY OWN EDIT\n") + b"\nbottom note\n"
    else:
        planted = b"top note\n\n" + _with_begin(block, BEGIN_LIT + b"zzz" + SUFFIX_LIT) + b"\nbottom note\n"
    md = _plant(tmp_path, planted)
    out = _ok(_run(tmp_path, [*prof, "--merge", FLAG]))
    hits = UPDATED_RE.findall(out)
    assert len(hits) == 1, out
    assert Path(hits[0][1]).read_bytes() == planted
    before, _, after, _ = _split_block(md.read_bytes())
    assert before == b"top note\n\n" and after == b"\nbottom note\n"
    assert (md.parent / "CLAUDE.md.pre-engram").read_bytes() == planted
    assert [p.read_bytes() for p in _stamped(md.parent)] == [planted]


# ------------------------------------------------------------------------------ B11

B11_SENTENCE = "this file already holds this install's ENGRAM text outside a block"


def _render_and_block(tmp_path: Path, prof):
    _, block, _, render = _split_block(_baseline(tmp_path, prof))
    return render, block


@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("where", ["markerless", "before_the_block", "after_the_block"])
@pytest.mark.parametrize("extra", [[], [FLAG]], ids=["merge", "merge_overwrite"])
def test_B11_the_install_text_verbatim_outside_a_block_is_refused_and_no_flag_overrides(tmp_path, prof, where, extra):
    """B11: when this install's render already appears verbatim OUTSIDE any block (a markerless file, or
    before/after a one-block file), the merge refuses in one line with the contract's sentence, nothing
    written, no backup; `--overwrite-edited-block` does not override. Without the check (M22) the ENGRAM
    text would be written twice."""
    render, block = _render_and_block(tmp_path, prof)
    data = {"markerless": b"intro\n\n" + render + b"\nmine\n",
            "before_the_block": render + b"\n" + block,
            "after_the_block": block + b"\n" + render}[where]
    md = _plant(tmp_path, data)
    _home(tmp_path)
    before = _tree(tmp_path)
    out = _assert_refused_untouched(tmp_path, _run(tmp_path, [*prof, "--merge", *extra]), md, data, before)
    assert B11_SENTENCE in " ".join(out.split())


@pytest.mark.parametrize("prof", PROFILES)
def test_B11_control_a_file_with_a_different_older_text_is_appended_not_refused(tmp_path, prof):
    """B11 control (M22b): the check is for the EXACT current text only; an older render (here the first
    line differs) is appended, with the B2b warning. A check that refused everything would pass the refusal cells."""
    render, _ = _render_and_block(tmp_path, prof)
    older = b"# An older first line\n" + render.split(b"\n", 1)[1]
    md = _plant(tmp_path, b"mine\n\n" + older)
    out = _ok(_run(tmp_path, [*prof, "--merge"]))
    assert "now appears twice" in " ".join(out.split()).lower()
    assert md.read_bytes().startswith(b"mine\n\n" + older)
    _split_block(md.read_bytes())


# ------------------------------------------------------------------------------ B12 (the record)

RECORD_NAME = "claude-md-block.json"


def _record_path(tmp_path: Path) -> Path:
    return tmp_path / ".engram" / RECORD_NAME


def _begin_hash(data: bytes) -> str:
    m = re.search(re.escape(BEGIN_LIT) + rb"([0-9a-f]{64})", data)
    assert m, "no BEGIN line with a hash in the file (stage 2 not built)"
    return m.group(1).decode()


def _assert_record_matches(tmp_path: Path) -> dict:
    rec = _record_path(tmp_path)
    assert rec.is_file(), f"{RECORD_NAME} was not written in the ENGRAM home"
    doc = json.loads(rec.read_text(encoding="utf-8"))
    assert set(doc) == {"path", "sha256"}, f"exactly two keys expected, got {sorted(doc)}"
    md = _md(tmp_path)
    assert doc["path"] == os.path.realpath(md), "path is not the real path of the CLAUDE.md"
    assert doc["sha256"] == _begin_hash(md.read_bytes()), "sha256 is not the hash in the BEGIN line"
    return doc


@pytest.mark.parametrize("merge", [pytest.param([], id="no_flag"), pytest.param(["--merge"], id="merge")])
@pytest.mark.parametrize("prof", PROFILES)
def test_B12_a_created_file_writes_the_record(tmp_path, prof, merge):
    """B12: an absent CLAUDE.md that bootstrap creates (with or without --merge) also writes
    `<ENGRAM_HOME>/claude-md-block.json` with exactly two keys, the real path and the BEGIN line's hash.
    Without the record, uninstall (D6) could never tell this home wrote the block."""
    _install(tmp_path, prof, extra_args=merge)
    _assert_record_matches(tmp_path)


@pytest.mark.parametrize("prof", PROFILES)
def test_B12_an_appended_block_writes_the_record(tmp_path, prof):
    """B12: appending a block to the user's file writes the record (path + the new BEGIN hash)."""
    _plant(tmp_path, b"my own rules\n")
    _ok(_run(tmp_path, [*prof, "--merge"]))
    _assert_record_matches(tmp_path)


@pytest.mark.parametrize("prof", PROFILES)
def test_B12_an_updated_block_writes_the_record_with_the_new_hash(tmp_path, prof):
    """B12: an updated block (state (a)) rewrites the record with the NEW hash, not the old one. A record
    kept from the first install would not match the file and uninstall would leave the block (M30)."""
    base = _baseline(tmp_path, prof)
    old_hash = _begin_hash(base)
    _plant(tmp_path, b"top\n\n" + _split_block(base)[1])
    _ok(_run(tmp_path, [*prof, "--merge"], templates=_templates_with_extra(tmp_path)))
    doc = _assert_record_matches(tmp_path)
    assert doc["sha256"] != old_hash


@pytest.mark.parametrize("prof", PROFILES)
def test_B12_an_identical_run_writes_no_record(tmp_path, prof):
    """B12: 'not on identical'. After the data home is set aside, a run over a file that already equals the
    new text writes nothing, so no record appears."""
    _install(tmp_path, prof)
    _assert_record_matches(tmp_path)  # the first (creating) run did write one
    _fresh_data_home(tmp_path)
    _ok(_run(tmp_path, prof))
    assert not _record_path(tmp_path).exists()


REFUSAL_KINDS = ["two_blocks", "edited_block", "verbatim_text", "codex_target", "replace_other_path"]


@pytest.mark.parametrize("planted", [pytest.param(None, id="no_record"), pytest.param(b'{"path": "x", "sha256": "y"}\n', id="existing_record")])
@pytest.mark.parametrize("prof", PROFILES)
@pytest.mark.parametrize("kind", REFUSAL_KINDS)
def test_B12_a_refused_run_writes_no_record_and_keeps_an_existing_one_byte_for_byte(tmp_path, prof, kind, planted):
    """B12: a refused run (B4, B9(b), B11, C4, C4b) writes no record and does not change an existing one.
    If the record were written before the refusal (M29), uninstall would later trust a block this run
    never wrote."""
    if kind == "two_blocks":
        data, _ = B4_SHAPES["two_blocks"]
    elif kind == "edited_block":
        data = _edit_inside(_split_block(_baseline(tmp_path, prof))[1], b"MY OWN EDIT\n")
    elif kind == "verbatim_text":
        render = _split_block(_baseline(tmp_path, prof))[3]
        data = b"intro\n\n" + render
    else:
        data = b"my own rules\n"
    md = _plant(tmp_path, data)
    _home(tmp_path)
    rec = _record_path(tmp_path)
    if planted is not None:
        rec.write_bytes(planted)
    args, target = [*prof, "--merge"], "claude"
    if kind == "codex_target":
        args, target = ["--merge"], "codex"
    elif kind == "replace_other_path":
        args = [*prof, "--merge", "--replace", str(tmp_path / "other.md")]
    r = _run(tmp_path, args, target=target)
    assert "unknown argument" not in _out(r), f"--merge is not implemented yet: {_out(r)}"
    assert r.returncode != 0
    assert md.read_bytes() == data
    if planted is None:
        assert not rec.exists(), "a refused run created the record"
    else:
        assert rec.read_bytes() == planted, "a refused run changed the record"


def test_B12_the_record_name_is_in_the_guards_owned_names():
    """B12: the file is ENGRAM's own bookkeeping, listed among the guard's owned names (rewritten when it
    differs, never backed up). Without the entry the guard would refuse to rewrite it or back it up."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import engram_file_guard as fg
    assert RECORD_NAME in fg.ENGRAM_OWNED_NAMES


# ------------------------------------------------------------------------------ A2 (file level)

@pytest.mark.parametrize("prof", PROFILES)
def test_A2_the_file_bootstrap_writes_is_exactly_what_the_modules_wrap_makes_of_the_render(tmp_path, prof):
    """A2 / E4: ONE function produces the text bootstrap writes and the text the comparators strip back
    to. Observable: wrap(unwrap(file)) is the file, byte for byte. If bootstrap kept its own copy of the
    wrapping (a different notice, an extra blank line) this would differ."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        import engram_claude_block as blk
    except Exception as e:
        raise AssertionError("engram_claude_block is missing: stage 2") from e
    _install(tmp_path, prof)
    text = _md(tmp_path).read_text(encoding="utf-8")
    assert blk.wrap(blk.unwrap(text)) == text


@pytest.mark.parametrize("prof", PROFILES)
def test_A3_a_template_that_documents_the_markers_fails_loudly_before_anything_is_written(tmp_path, prof):
    """A3 end to end: a template whose prose holds a literal marker line makes the run FAIL at render time
    (non-zero, a message), before CLAUDE.md or anything else is written; never a shipped file the next merge
    refuses. If the render did not raise, the next `--merge` would refuse ENGRAM's own file."""
    templates = tmp_path / "templates-bad"
    shutil.copytree(SRC / "templates", templates)
    for name in ("template.CLAUDE.md", "template.CLAUDE.local.md"):
        with open(templates / name, "a", encoding="utf-8") as f:
            f.write("\nTo end the block write\n\n" + END_LIT.decode() + "\n\non a line of its own.\n")
    r = _run(tmp_path, prof, templates=templates)
    assert r.returncode != 0, "a template holding the marker text was rendered"
    assert not _md(tmp_path).exists(), "a file with the marker text in its body was shipped"
    assert not (tmp_path / ".engram" / "knowledge.db").exists(), "the failure came after seeding"
    assert _out(r).strip(), "the failure must say something"


# ------------------------------------------------------------------------------ F2 (file level)

@pytest.mark.parametrize("prof", PROFILES)
def test_F2_a_section_added_to_a_template_lands_inside_the_markers(tmp_path, prof):
    """F2: a later section (here one extra `##` section in a copy of the template) is inside the block,
    and the markers stay the first and last lines; ENGRAM writes nothing outside them. If bootstrap
    appended sections after END they would sit where no merge ever updates them."""
    _install(tmp_path, prof, templates=_templates_with_extra(tmp_path))
    data = _md(tmp_path).read_bytes()
    before, block, after, render = _split_block(data)
    assert before == b"" and after == b"" and b"## Added by a later version" in render
    assert data.startswith(BEGIN_LIT) and data.endswith(END_LIT + b"\n")


# ------------------------------------------------------------------------------ E1, E2, E5, E9, G

def test_E1_regression_the_codex_foreign_stanza_survives_a_rerender_byte_for_byte(tmp_path):
    """E1 regression guard (green on dev): the Codex config.toml merge still keeps a foreign stanza
    (the sibling-vector cell of #4440, kept and run in this change)."""
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    stanza = '[plugins.someone]\nkey = "value"\nother = 3\n'
    cfg.write_text(stanza)
    assert _run(tmp_path, ["--force"], target="codex").returncode == 0
    _fresh_data_home(tmp_path)
    assert _run(tmp_path, target="codex").returncode == 0
    assert stanza in cfg.read_text()
    assert cfg.read_text().count('instructions = """') == 1


@pytest.mark.parametrize("prof", PROFILES)
def test_E2_settings_json_is_untouched_by_the_merge_path(tmp_path, prof):
    """E2: `settings.json` handling is untouched by the merge path: a settings.json (with its own keys)
    is byte-identical after a `--merge` run that did write CLAUDE.md. (The run must succeed, so this is
    red until --merge exists; the twin below is the green regression guard.)"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    raw = b'{"theme":  "dark",\n "x": [1,2]}\n'
    settings.write_bytes(raw)
    md = _plant(tmp_path, b"my own rules\n")
    _ok(_run(tmp_path, [*prof, "--merge"]))
    assert settings.read_bytes() == raw
    assert md.read_bytes().startswith(b"my own rules\n")


def test_E2_regression_settings_json_is_untouched_by_a_plain_run(tmp_path):
    """E2 regression guard (green on dev): without --retention-days a plain run never touches settings.json."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    raw = b'{"theme":  "dark"}\n'
    settings.write_bytes(raw)
    assert _run(tmp_path).returncode == 0
    assert settings.read_bytes() == raw


@pytest.mark.parametrize("prof", PROFILES)
def test_E5_a_symlinked_claude_md_keeps_its_link_and_the_merge_goes_into_the_target(tmp_path, prof):
    """E5: a symlinked CLAUDE.md stays a symlink and the merge is written into the TARGET file (the guard
    writes in place and never replaces the link); the backups are real files of their own."""
    real = tmp_path / "real-claude.md"
    real.write_bytes(b"my own rules\n")
    md = _md(tmp_path)
    md.parent.mkdir()
    md.symlink_to(real)
    _ok(_run(tmp_path, [*prof, "--merge"]))
    assert md.is_symlink() and os.path.samefile(md, real)
    assert real.read_bytes().startswith(b"my own rules\n")
    _split_block(real.read_bytes())
    pre = md.parent / "CLAUDE.md.pre-engram"
    assert pre.is_file() and not pre.is_symlink() and pre.read_bytes() == b"my own rules\n"


@pytest.mark.parametrize("prof", PROFILES)
def test_E5_a_hard_linked_claude_md_stays_one_inode_with_the_merged_text(tmp_path, prof):
    """E5: a hard-linked CLAUDE.md keeps its link (in-place write, same inode); both names read the
    merged text. A temp-file + rename write would split the link."""
    md = _plant(tmp_path, b"my own rules\n")
    other = tmp_path / "other-name.md"
    os.link(md, other)
    _ok(_run(tmp_path, [*prof, "--merge"]))
    assert os.path.samefile(md, other) and other.read_bytes() == md.read_bytes()
    assert md.read_bytes().startswith(b"my own rules\n")
    _split_block(md.read_bytes())


@pytest.mark.parametrize("prof", PROFILES)
def test_E5_a_backup_name_that_is_a_hard_link_to_claude_md_is_refused_even_with_merge(tmp_path, prof):
    """E5: the existing same-file guard still holds on the merge path: `CLAUDE.md.pre-engram` hard-linked
    to the live file is refused (a merge would overwrite the only backup too), nothing written."""
    md = _plant(tmp_path, b"my own rules\n")
    bak = md.parent / "CLAUDE.md.pre-engram"
    os.link(md, bak)
    _home(tmp_path)
    before = _tree(tmp_path)
    r = _run(tmp_path, [*prof, "--merge"])
    out = _out(r)
    assert "unknown argument" not in out, f"--merge is not implemented yet: {out}"
    assert r.returncode != 0 and "same file" in out
    assert md.read_bytes() == b"my own rules\n" and _tree(tmp_path) == before


@pytest.mark.parametrize("prof", PROFILES)
def test_E5_a_dangling_claude_md_symlink_is_refused_even_with_merge(tmp_path, prof):
    """E5: a dangling CLAUDE.md symlink is refused on the merge path too, nothing is created at its target."""
    md = _md(tmp_path)
    md.parent.mkdir()
    gone = tmp_path / "nowhere.md"
    md.symlink_to(gone)
    _home(tmp_path)
    before = _tree(tmp_path)
    r = _run(tmp_path, [*prof, "--merge"])
    out = _out(r)
    assert "unknown argument" not in out, f"--merge is not implemented yet: {out}"
    assert r.returncode != 0 and "symlink" in out
    assert md.is_symlink() and not gone.exists() and _tree(tmp_path) == before


def test_E9_a_differently_cased_user_file_keeps_its_name_and_receives_the_merge(tmp_path):
    """E9 (measured by the contract on a case-insensitive APFS volume): a user file named `claude.md`
    keeps its on-disk name after the write; the cell asserts `samefile` and the CONTENT, never a directory
    listing. It can only be exercised on a case-insensitive volume; elsewhere it is skipped, and the
    handoff says so."""
    home = tmp_path / ".claude"
    home.mkdir()
    probe = home / "Probe.tmp"
    probe.write_bytes(b"x")
    insensitive = (home / "probe.TMP").exists()
    probe.unlink()
    if not insensitive:
        pytest.skip("case-sensitive volume: E9 is measured only on a case-insensitive one (contract E9)")
    lower = home / "claude.md"
    lower.write_bytes(b"my own rules\n")
    _ok(_run(tmp_path, ["--merge"]))
    assert os.path.samefile(lower, home / "CLAUDE.md")
    assert lower.read_bytes().startswith(b"my own rules\n")
    _split_block(lower.read_bytes())


def test_G_regression_install_sh_is_not_changed_by_this_work(tmp_path):
    """G regression guard (green on dev): `install.sh` still asks its one question and passes
    `--replace "$CLAUDE_MD"`; it does not pass `--merge` (Ariadne 34038: `--yes` keeps meaning replace)."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in sh.splitlines() if not ln.lstrip().startswith("#"))
    assert "--merge" not in code and "--overwrite-edited-block" not in code
    assert '_bootstrap_flags=(--replace "$CLAUDE_MD")' in sh


# ------------------------------------------------------------------------------ A1 / A5 / A6 (comparators)

LIVE_TEMPLATE = "# I'm {{AGENT_NAME}}, an AI agent.\n\nFirst paragraph of the rules.\n\n## Section A\n\nSecond paragraph of the rules.\n\n## Section B\n\nThird paragraph of the rules.\n"
LIVE_FLAGS = ["--user-name", "Test User", "--agent-name", "Test Agent", "--self-node-id", "pn_test", "--engram-home", "/home/x/.engram"]


def _live_render() -> str:
    return LIVE_TEMPLATE.replace("{{AGENT_NAME}}", "Test Agent")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _live_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "live_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.invalid")
    _git(repo, "config", "user.name", "T")
    d = repo / "src" / "engram" / "templates"
    d.mkdir(parents=True)
    (d / "template.CLAUDE.md").write_text(LIVE_TEMPLATE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "live fixture")
    return repo


def _drift(tmp_path: Path, live_text: str):
    live = tmp_path / "live-CLAUDE.md"
    live.write_bytes(live_text.encode("utf-8"))
    p = subprocess.run([sys.executable, str(DRIFT_TOOL), "--repo", str(_live_repo(tmp_path)), "--head", "HEAD",
                        "--live", str(live), *LIVE_FLAGS], capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def _blk_or_red():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        import engram_claude_block as blk
    except Exception as e:
        raise AssertionError("engram_claude_block is missing: stage 2") from e
    return blk


def test_A1_the_drift_tool_reads_no_drift_for_a_live_file_that_is_the_hashed_wrapped_render(tmp_path):
    """A1 (drift tool `--live`): a live file that is exactly the HASHED wrapped render (a real hash) reads
    'no drift', with the BEGIN line, notice and END ignored. A comparator keyed on one exact BEGIN string
    (M10) or one that treated the markers as template text would report drift."""
    blk = _blk_or_red()
    rc, out, err = _drift(tmp_path, blk.wrap(_live_render()))
    assert rc == 0 and "0 missing" in out, f"rc={rc}\n{out}\n{err}"


def test_A1_the_drift_tool_compares_the_blocks_inside_not_the_users_text_outside(tmp_path):
    """A1: the comparator compares the block's INSIDE. A rule missing from the block but present in the
    user's own text OUTSIDE it is still drift: the template unit is reported missing. If the tool searched
    the whole file, the user's text could mask a block that no longer carries the rule."""
    blk = _blk_or_red()
    missing = "Second paragraph of the rules."
    inside = _live_render().replace(missing + "\n\n", "")
    assert missing not in inside
    rc, out, err = _drift(tmp_path, blk.wrap(inside) + "\n" + missing + "\n")
    assert rc == 1 and "MISSING" in out and "Second paragraph" in out, f"rc={rc}\n{out}\n{err}"


def test_A5_an_unwrapped_live_file_equal_to_the_old_template_still_reads_no_drift(tmp_path):
    """A5 regression guard (green on dev): every seat today holds an UNWRAPPED file; the comparator still
    compares it as a whole file, unchanged from today. A comparator that rejected a markerless file (M24)
    would call every existing seat drifted."""
    rc, out, err = _drift(tmp_path, _live_render())
    assert rc == 0 and "0 missing" in out, f"rc={rc}\n{out}\n{err}"


def test_A6_a_crlf_converted_hashed_wrapped_file_reads_no_drift(tmp_path):
    """A6: the comparator reads CRLF as LF the way the hash does: a CRLF-converted hashed wrapped file
    reads 'no drift'. A comparator on raw CR bytes (M25) would not."""
    blk = _blk_or_red()
    rc, out, err = _drift(tmp_path, blk.wrap(_live_render()).replace("\n", "\r\n"))
    assert rc == 0 and "0 missing" in out, f"rc={rc}\n{out}\n{err}"


def test_A1_the_upgrade_skill_names_the_comparator_rule():
    """A1 (the upgrade skill is a document, not code): the identity-template inverse-merge gate (Step 7)
    must say that a wrapped live file is compared by the block's INSIDE (naming the marker), with the
    markerless transition of A5. Only the WORDING can be asserted here; the skill's behaviour is a
    reader's, not a program's."""
    text = UPGRADE_SKILL.read_text(encoding="utf-8")
    step7 = text[text.index("## Step 7"):]
    step7 = step7[: step7.index("\n## Step 8")] if "\n## Step 8" in step7 else step7
    assert "engram:begin" in step7, "Step 7 does not name the block markers"
    assert re.search(r"(inside|between)[^.\n]{0,80}(block|markers)|(block|markers)[^.\n]{0,80}inside", step7, re.I), \
        "Step 7 does not say the comparison is of the block's inside"
