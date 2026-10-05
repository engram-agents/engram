"""Contract tests for tools/erase_session_block.py (#3141).

`engram-sleep` Step 3 erases the warm-briefing's volatile "## From this
session" section.  Described line-by-line, the natural implementation
(`"\\n".join(lines[:anchor_index])`) silently drops ONE BYTE of the protected
region above the anchor (the blank line before the heading).  The tool erases
by CHARACTER OFFSET and asserts byte-identity of everything above the anchor
as a post-condition.  Every cell names the plausible-wrong implementation it
discriminates.  Every cell uses tmp_path; ENGRAM_HOME is pointed at tmp_path
in subprocess runs so the real ~/.engram is never touched.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "src" / "engram" / "tools" / "erase_session_block.py"
TEMPLATE = REPO_ROOT / "src" / "engram" / "templates" / "template.warm-briefing.md"
HEADING = "## From this session"


def _load():
    spec = importlib.util.spec_from_file_location("erase_session_block", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool():
    return _load()


def _join_variant(text: str) -> str:
    """Plausible-wrong: line-oriented erase via splitlines + join."""
    lines = text.splitlines()
    idx = max(i for i, l in enumerate(lines) if re.match(r"## From this session", l))
    # join() turns the blank line's terminator into a separator, so the heading
    # lands one "\n" early: the prefix is exactly one byte short.
    return "\n".join(lines[:idx]) + HEADING + "\n"


def _run(args, tmp_path, **kw):
    env = dict(os.environ, ENGRAM_HOME=str(tmp_path / "fake_engram_home"))
    return subprocess.run(
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env, **kw
    )


def _write(tmp_path, text, name="wb.md"):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


LETTER = "# Letter\n\nDear agent,\nlove, Lei\n\n"  # ends with a blank line


# T1 -------------------------------------------------------------------
def test_t1_issue_case_prefix_byte_identical(tool):
    # one blank line between the letter's last line and the heading
    prefix = "# Letter\n\nDear agent,\nlove, Lei\n\n"
    text = prefix + HEADING + "\n- arc 1\n- arc 2\n- arc 3\n"
    out = tool._erase_core(text)
    assert out == prefix + HEADING + "\n"
    assert len(out[: len(prefix)]) == len(prefix)
    # CONTROL: the join variant loses exactly one byte of the prefix
    wrong = _join_variant(text)
    wrong_prefix = wrong[: wrong.index(HEADING)]
    assert len(wrong_prefix) == len(prefix) - 1
    assert wrong != out


# T2 -------------------------------------------------------------------
def test_t2_last_anchor_is_target(tool):
    early = HEADING + "\nuser pasted this old heading\nmore user words\n"
    text = "# Letter\n" + early + "\nend of letter\n\n" + HEADING + "\n- arc\n"
    out = tool._erase_core(text)
    assert out == "# Letter\n" + early + "\nend of letter\n\n" + HEADING + "\n"
    # first-match variant truncates the letter
    first = re.search(r"(?m)^## From this session", text).start()
    wrong = text[:first] + HEADING + "\n"
    assert "end of letter" not in wrong and "end of letter" in out


# T3 -------------------------------------------------------------------
def test_t3_unanchored_occurrences_ignored(tool):
    text = (
        "# L\n  ## From this session\nsee ## From this session here\n"
        "<!-- keep the ## From this session heading -->\n\n"
    )
    out = tool._erase_core(text)
    # no anchored match -> case B: stub appended, everything else intact
    assert out == text + HEADING + "\n"
    out2, info = tool.erase_session_block(text)
    assert out2 == out


# T4 -------------------------------------------------------------------
def test_t4_already_bare_cli_noop(tmp_path):
    p = _write(tmp_path, LETTER + HEADING + "\n")
    before = p.read_bytes()
    m = p.stat().st_mtime_ns
    r = _run(["--file", str(p)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "already bare: no change" in r.stdout
    assert p.read_bytes() == before and p.stat().st_mtime_ns == m


def test_t4_bare_without_trailing_newline(tool):
    text = LETTER + HEADING
    out = tool._erase_core(text)
    assert out == LETTER + HEADING + "\n"
    # a wrong impl that appends a second heading / blank line would differ
    assert out.count(HEADING) == 1


# T5 -------------------------------------------------------------------
def test_t5_no_section_stub_appended(tool):
    text = "# L\nbody"  # no trailing newline
    out = tool._erase_core(text)
    assert out == "# L\nbody\n" + HEADING + "\n"  # exactly one newline added
    text2 = "# L\nbody\n"
    assert tool._erase_core(text2) == text2 + HEADING + "\n"
    assert out.startswith(text)


# T6 -------------------------------------------------------------------
def test_t6_drift_stop_level2_and_level1(tool, tmp_path):
    for bad in ("## Notes from the user", "# X"):
        text = LETTER + HEADING + "\n- arc\n" + bad + "\nuser words\n"
        p = _write(tmp_path, text)
        with pytest.raises(tool.DriftStop) as ei:
            tool.erase_session_block(text)
        assert bad in str(ei.value)
        assert str(text[: text.index(bad)].count("\n") + 1) in str(ei.value)
        r = _run(["--file", str(p)], tmp_path)
        assert r.returncode == 3
        assert p.read_bytes() == text.encode()


def test_t6_level3_subheading_allowed(tool):
    text = LETTER + HEADING + "\n### sub\n- arc\n"
    assert tool._erase_core(text) == LETTER + HEADING + "\n"
    tool.erase_session_block(text)  # must not raise


# T7 -------------------------------------------------------------------
def test_t7_cli(tmp_path):
    text = LETTER + HEADING + "\n- a\n- b\n"
    p = _write(tmp_path, text)
    m = p.stat().st_mtime_ns
    r = _run(["--file", str(p), "--dry-run"], tmp_path)
    assert r.returncode == 0 and p.read_bytes() == text.encode()
    assert p.stat().st_mtime_ns == m
    r = _run(["--file", str(p)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert p.read_bytes() == (LETTER + HEADING + "\n").encode()
    assert re.search(
        rf"erased: kept {len(LETTER)} chars above the anchor \(byte-identical\), "
        r"removed 8 chars / 2 lines",
        r.stdout,
    ), r.stdout
    m2 = p.stat().st_mtime_ns
    r = _run(["--file", str(p)], tmp_path)
    assert r.returncode == 0 and "already bare" in r.stdout
    assert p.stat().st_mtime_ns == m2
    r = _run(["--file", str(tmp_path / "nope.md")], tmp_path)
    assert r.returncode == 2


def test_t7_default_path_uses_engram_home(tmp_path):
    home = tmp_path / "fake_engram_home"
    home.mkdir()
    (home / "warm-briefing.md").write_bytes((LETTER + HEADING + "\n- x\n").encode())
    r = _run([], tmp_path)
    assert r.returncode == 0, r.stderr
    assert (home / "warm-briefing.md").read_bytes() == (LETTER + HEADING + "\n").encode()


# T8 TEETH -------------------------------------------------------------
def test_t8_postcondition_fires_on_join_variant(tool, monkeypatch, tmp_path):
    text = LETTER + HEADING + "\n- arc\n"
    monkeypatch.setattr(tool, "_erase_core", _join_variant)
    with pytest.raises(tool.PostconditionError):
        tool.erase_session_block(text)


def test_t8_p1_isolated_same_length_prefix_corruption(tool, monkeypatch):
    # A wrong core that rewrites a prefix char but keeps its LENGTH, the anchor
    # position and idempotence: P2/P3/P4 all stay green, only P1 (byte
    # identity) can catch it. Without this cell P1 is masked by P3's position check.
    def corrupt(text):
        i = text.index(HEADING)
        return text[:i].replace("Dear", "DEAR") + HEADING + "\n"

    monkeypatch.setattr(tool, "_erase_core", corrupt)
    with pytest.raises(tool.PostconditionError, match="P1"):
        tool.erase_session_block(LETTER + HEADING + "\n- arc\n")


def test_t8_cli_exit4_file_untouched(tmp_path):
    text = LETTER + HEADING + "\n- arc\n"
    p = _write(tmp_path, text)
    # run the CLI main() with _erase_core patched to the join variant
    code = (
        "import importlib.util,sys,re\n"
        f"s=importlib.util.spec_from_file_location('e',{str(TOOL)!r})\n"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
        "H='## From this session'\n"
        "def j(t):\n"
        "    l=t.splitlines();i=max(k for k,x in enumerate(l) if x.startswith(H))\n"
        "    return '\\n'.join(l[:i])+H+'\\n'\n"
        "m._erase_core=j\n"
        f"sys.exit(m.main(['--file',{str(p)!r}]))\n"
    )
    env = dict(os.environ, ENGRAM_HOME=str(tmp_path / "h"))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 4, (r.stdout, r.stderr)
    assert p.read_bytes() == text.encode()


# T9 CRLF --------------------------------------------------------------
def test_t9_crlf_preserved(tool, tmp_path):
    prefix = "# L\r\n\r\nDear\r\nlove\r\n\r\n"
    text = prefix + HEADING + "\r\n- arc\r\n"
    p = _write(tmp_path, text)
    r = _run(["--file", str(p)], tmp_path)
    assert r.returncode == 0, r.stderr
    got = p.read_bytes()
    assert got.startswith(prefix.encode())  # no \r\n -> \n translation
    assert got == prefix.encode() + (HEADING + "\n").encode()


# T10 template ---------------------------------------------------------
def test_t10_template_noop(tool):
    text = TEMPLATE.read_bytes().decode("utf-8")
    assert text.endswith("\n" + HEADING + "\n"), "template no longer ends bare; STOP"
    assert tool._erase_core(text) == text


# T11 drift regex: ATX level-1/2 per CommonMark (best-effort proxy) -----
@pytest.mark.parametrize(
    "bad",
    [" ## Foo", "   ## Foo", "##\tFoo", "##", "#", "## ", " # Foo"],
)
def test_t11_drift_stop_wider_atx_forms(bad, tmp_path):
    # Plausible-wrong: BELOW_RE = r"^#{1,2} " misses indented / tab / bare headings.
    text = LETTER + HEADING + "\n- arc\n" + bad + "\nuser words\n"
    p = _write(tmp_path, text)
    r = _run(["--file", str(p)], tmp_path)
    assert r.returncode == 3, (bad, r.stdout, r.stderr)
    assert p.read_bytes() == text.encode()


@pytest.mark.parametrize(
    "ok", ["#tag", "##Foo", "    ## Foo", "### sub", "####x", "\t## Foo"]
)
def test_t11_not_a_heading_erase_proceeds(ok, tool):
    # 4+ spaces = indented code block; #tag / ##Foo / ### / #### are not level-1/2 ATX.
    text = LETTER + HEADING + "\n- arc\n" + ok + "\nmore\n"
    assert tool._erase_core(text) == LETTER + HEADING + "\n"
    tool.erase_session_block(text)  # must not raise DriftStop


# T11b CRLF: the line is split on "\n" only, so a bare heading is "##\r" -------
CRLF_LETTER = "# L\r\n\r\nDear\r\nlove\r\n\r\n"


@pytest.mark.parametrize("bad", ["##", "#", " ##"])
def test_t11_drift_stop_bare_heading_in_crlf_file(bad, tmp_path):
    # Plausible-wrong: BELOW_RE terminator `(?:[ \t]|$)` misses the "\r" of "##\r".
    text = CRLF_LETTER + HEADING + "\r\n- arc\r\n" + bad + "\r\nuser words\r\n"
    p = _write(tmp_path, text)
    r = _run(["--file", str(p)], tmp_path)
    assert r.returncode == 3, (bad, r.stdout, r.stderr)
    assert p.read_bytes() == text.encode()


@pytest.mark.parametrize("ok", ["#tag", "##Foo", "    ## Foo"])
def test_t11_not_a_heading_in_crlf_file_erase_proceeds(ok, tool):
    # The CR-tolerant terminator must not turn non-headings into drift-STOPs.
    text = CRLF_LETTER + HEADING + "\r\n- arc\r\n" + ok + "\r\nmore\r\n"
    assert tool._erase_core(text) == CRLF_LETTER + HEADING + "\n"
    tool.erase_session_block(text)  # must not raise DriftStop


# T12 TEETH for P2 / P3 / P4 (one wrong core each, violating ONLY that clause)
def _assert_wrong_core_refused(tool, monkeypatch, tmp_path, core, clause):
    text = LETTER + HEADING + "\n- arc\n"
    monkeypatch.setattr(tool, "_erase_core", core)
    with pytest.raises(tool.PostconditionError, match=clause):
        tool.erase_session_block(text)
    p = _write(tmp_path, text)
    assert tool.main(["--file", str(p)]) == 4
    assert p.read_bytes() == text.encode()


def test_t12_p2_final_line_not_bare_heading(tool, monkeypatch, tmp_path):
    # Keeps the prefix (P1), anchor at the boundary (P3) and is idempotent (P4);
    # only P2 sees the trailing " x".
    def core(text):
        i = text.index(HEADING)
        return text[:i] + HEADING + " x\n"

    _assert_wrong_core_refused(tool, monkeypatch, tmp_path, core, "P2")


def test_t12_p3_last_anchor_off_prefix_boundary(tool, monkeypatch, tmp_path):
    # Inserts one blank line between the preserved prefix and the heading:
    # P1 (prefix intact), P2 (final line bare), count (1) all hold and the core is
    # idempotent (P4); only P3's boundary-position clause sees the shift.
    def core(text):
        i = text.index(HEADING)
        head = text[:i]
        if not head.endswith("\n\n\n"):
            head += "\n"
        return head + HEADING + "\n"

    _assert_wrong_core_refused(tool, monkeypatch, tmp_path, core, "P3")


def test_t12_p4_not_idempotent(tool, monkeypatch, tmp_path):
    # Correct on a section-bearing input (P1-P3 pass), but on an already-bare
    # input it appends a marker, so erasing its own output differs.
    def core(text):
        if text.endswith(HEADING + "\n"):
            return text + "marker\n"
        i = text.index(HEADING)
        return text[:i] + HEADING + "\n"

    _assert_wrong_core_refused(tool, monkeypatch, tmp_path, core, "P4")
