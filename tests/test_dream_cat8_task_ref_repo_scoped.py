"""Dream-fairy Category 8 (open tasks with stale external references) and the
two dream-master passages that act on its findings must resolve PR/issue
references against the repo they belong to, never the cwd checkout.

A bare `#N` has no repo: `gh pr view N` silently resolves it against whichever
repo is the current checkout, so a same-numbered PR in the WRONG repo can read
as MERGED/CLOSED and a live task gets proposed for closure.

The fix: extraction matches `owner/repo#N` and bare-repo aliases declared by the
SEAT in `${ENGRAM_HOME:-$HOME/.engram}/repo-aliases`. The map ships empty, so an
install that declares nothing keeps today's qualified-refs-only behaviour; every
gh call passes `--repo` taken from the resolved reference; a bare `#N` and any
alias the file does not declare are structurally unmatched; a gh error is
UNKNOWN. These tests read the bash block out of the fairy spec (single source of
truth) and execute it against a temporary ENGRAM_HOME.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
FAIRY_MD = REPO / "src/engram/agents/claude/engram-dream-fairy.md"
MASTER_MD = REPO / "src/engram/agents/claude/engram-dream-master.md"

_START = "8. **Open tasks with stale external references.**"
_END = "9. **Cornerstone auto-load surface coverage gaps.**"


def _cat8() -> str:
    text = FAIRY_MD.read_text(encoding="utf-8")
    assert _START in text, "Category 8 heading moved; update this gate"
    s = text.index(_START)
    assert _END in text[s:], "Category 9 heading moved; update this gate"
    return text[s:text.index(_END, s)]


def _cat8_all() -> str:
    """Category 8 text across the WHOLE fairy file: the methodology section
    plus the report-format section (the output contract the fairy copies)."""
    text = FAIRY_MD.read_text(encoding="utf-8")
    parts = [_cat8()]
    marker = "## Category 8"
    assert marker in text, "report-format Category 8 heading moved"
    s = text.index(marker)
    nxt = text.find("\n## ", s + 1)
    parts.append(text[s:nxt if nxt != -1 else len(text)])
    return "\n".join(parts)


def _stage1_refs(text: str) -> list[str]:
    """Stage 1's match stream: already-qualified `owner/repo#N`, sed-stripped.

    BOUND: stage 2's alias alternation is built at runtime from the seat's
    `repo-aliases` file, not from a literal in the block, so alias resolution is
    asserted end-to-end through `_run_cat8` rather than modelled here."""
    out: set[str] = set()
    for m in _pattern().finditer(text):
        tok = m.group(0)
        if tok[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-":
            tok = tok[1:]  # `sed -E 's/^[^A-Za-z0-9_.-]//'`
        out.add(tok)
    return sorted(out)


def _pattern() -> re.Pattern[str]:
    """Stage 1: already-qualified `owner/repo#N`."""
    m = re.search(r"grep -oE '([^']+)'", _cat8())
    assert m, "Category 8 grep -oE pattern not found -- bash block reshaped?"
    return re.compile(m.group(1))


def _bash_block() -> str:
    m = re.search(r"```bash\n(.*?)```", _cat8(), re.DOTALL)
    assert m, "Category 8 fenced bash block not found"
    return textwrap.dedent(m.group(1))


def test_pattern_matches_owner_repo_hash_n_with_captures():
    p = _pattern()
    refs = _stage1_refs("blocked on example-org/repo-a#12 landing")
    assert refs == ["example-org/repo-a#12"]
    repo, n = refs[0].rsplit("#", 1)
    assert (repo, n) == ("example-org/repo-a", "12")
    assert _stage1_refs("example-org/repo-a#12 at line start") == ["example-org/repo-a#12"]
    assert _stage1_refs("see example-org/repo-a#5") == ["example-org/repo-a#5"]


def test_left_boundary_rejects_longer_path():
    # a/b/c#5 must not be mis-read as b/c#5
    assert _stage1_refs("path a/b/c#5 here") == []
    assert _stage1_refs("x.y/z/w#5") == []


@pytest.mark.parametrize("text", [
    "#12", "PR #12", "closes #12", "issue #12", "pull request #12",
    "Closes #12 and fixes #13", "seq 7183", "forum post 7183",
    "alpha#2400",  # not a map key: the map is closed, so this stays unmatched
])
def test_pattern_does_not_match_bare_or_other_namespace(text):
    assert not _stage1_refs(text), f"pattern matched {text!r}"


def test_mixed_claim_extracts_only_qualified():
    prose = "tk: example-org/repo-a#12 is open; PR #12 elsewhere; closes #99; seq 7183"
    assert _stage1_refs(prose) == ["example-org/repo-a#12"]


def test_old_bare_pattern_is_gone():
    sec = _cat8()
    assert "(?<!\\w)#(\\d+)" not in sec
    assert "gh pr view N " not in sec
    assert "or add --repo OWNER/REPO" not in sec


def test_suggest_action_and_guard_text():
    sec = _cat8()
    assert "external reference owner/repo#N <state>" in sec
    assert "A bare `#N` is never a closure candidate" in sec
    assert "never as a `task_closures` suggestion" in sec


def test_no_bare_ref_anywhere_in_category_8_text():
    """Widened (round 2): the report-format template is the output contract the
    fairy copies; a bare `#N` there makes the master see bare refs."""
    sec = _cat8_all()
    assert "external reference owner/repo#N <state>" in sec
    for bad in ("external reference #N", "claim references #N"):
        assert bad not in sec, f"bare-ref template survives: {bad!r}"
    # every `#N` occurrence is preceded by owner/repo, or is the guard's own
    # quoted bare-`#N` prose
    for m in re.finditer(r"#N", sec):
        pre = sec[max(0, m.start() - 10):m.start()]
        assert pre.endswith("repo") or pre.endswith("`") or pre.endswith(
            ("PR ", "issue ", "closes ", "bare ", "a ")), (
            f"unqualified #N: {sec[max(0, m.start() - 40):m.end() + 10]!r}")


def test_kind_rule_prose_and_dream_master():
    sec = _cat8()
    assert "Only `pr MERGED` or `issue CLOSED` makes a closure candidate" in sec
    assert "`pr CLOSED` (closed without merging) is reported, never a closure" in sec
    assert "</dev/null" in sec
    master = MASTER_MD.read_text(encoding="utf-8")
    s = master.index("**task_closures**")
    seg = master[s:master.index("**unknown bucket**", s)]
    assert "`pr MERGED` or `issue CLOSED`" in seg and "NOT a closure" in seg
    s = master.index("- **Category 8 stale-task-ref closures**")
    seg = master[s:master.index("\n", s)]
    assert "`pr MERGED` or `issue CLOSED`" in seg and "unmerged `pr CLOSED`" in seg


def test_dream_master_task_closures_passage():
    text = MASTER_MD.read_text(encoding="utf-8")
    s = text.index("**task_closures**")
    seg = text[s:text.index("**unknown bucket**", s)]
    assert '--repo "$repo"' in seg
    assert "owner/repo#N" in seg
    assert "note='externally resolved: owner/repo#N <state>" in seg
    assert "externally resolved: #N" not in seg


def test_dream_master_cat8_bullet():
    text = MASTER_MD.read_text(encoding="utf-8")
    s = text.index("- **Category 8 stale-task-ref closures**")
    seg = text[s:text.index("\n", s)]
    assert seg.count('--repo \\"$repo\\"') == 0  # not escaped in the source
    assert seg.count('--repo "$repo"') >= 2
    assert "gh pr view N --json" not in seg
    assert "externally resolved: owner/repo#N <state>" in seg
    assert "bare `#N`" in seg and "never closed" in seg


# --- executing test: run the fairy's own bash block under a stub gh --------

_STUB_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$GH_ARGV_LOG"
cat >/dev/null   # a real gh may read stdin; the loop must shield its own
sub="$1"; verb="$2"; n="$3"; shift 3
repo=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--repo" ]; then repo="$2"; shift; fi
  shift
done
[ "$verb" = "view" ] || exit 1
if [ -z "$repo" ]; then echo "WRONGREPO"; exit 0; fi
case "$sub $repo $n" in
  "pr example-org/repo-a 12")      echo MERGED ;;
  "pr example-org/repo-b 12")      echo OPEN ;;
  "issue example-org/repo-b 40")   echo CLOSED ;;
  "pr example-org/repo-a 7")       echo CLOSED ;;   # abandoned, unmerged
  *) exit 1 ;;
esac
"""

_CLAIM = (
    "Waiting on example-org/repo-a#12 and example-org/repo-b#12 (same number); "
    "see example-org/repo-b#40 and example-org/repo-b#999 (gone). "
    "abandoned example-org/repo-a#7; not a ref: a/b/c#5. "
    "Also PR #12, closes #40, issue #12 bare; docs/foo.md#7 anchor."
)

_EXPECTED = sorted([
    "example-org/repo-a#12 pr MERGED",
    "example-org/repo-a#7 pr CLOSED",
    "example-org/repo-b#12 pr OPEN",
    "example-org/repo-b#40 issue CLOSED",
    "example-org/repo-b#999 - UNKNOWN",
    "docs/foo.md#7 - UNKNOWN",
])


def test_bash_block_executes_repo_scoped(tmp_path):
    lines, calls = _run_cat8(tmp_path, _CLAIM)
    assert sorted(lines) == _EXPECTED, lines
    assert "WRONGREPO" not in "\n".join(lines)
    assert "example-org/repo-a#7 pr CLOSED" in lines
    assert not any(line.startswith("b/c#5") for line in lines)
    assert calls, "stub gh never called"
    for call in calls:
        argv = call.split()
        assert "--repo" in argv, f"gh call without --repo: {call!r}"
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", argv[argv.index("--repo") + 1]
        ), call


# --- the seat's declared repo map: a closed vocabulary, resolved before reporting
#
# The two stages run from one `IN=$(cat)` so both see the whole claim; stage 2
# runs only when at least one alias is declared, and rewrites each declared
# alias to its full owner/repo inside the stage, so the final `sort -u` dedupes
# after the rewrite. Alias resolution is asserted end-to-end through
# `_run_cat8`, which always points ENGRAM_HOME at a temp dir.

_ALIAS_A = ("alias-a", "example-org/repo-a")
_ALIAS_B = ("alias-b", "example-org/repo-b")
_MAP_AB = [_ALIAS_A, _ALIAS_B]


def _write_map(home: pathlib.Path, aliases) -> None:
    """Materialise the seat's ENGRAM_HOME.

    `aliases` is None (no `repo-aliases` file at all), a string (written to the
    file verbatim, for comment-only / malformed cases), or an iterable of
    (key, value) pairs rendered as `key=value` lines.
    """
    home.mkdir(parents=True, exist_ok=True)
    if aliases is None:
        return
    if isinstance(aliases, str):
        content = aliases
    else:
        content = "".join(f"{k}={v}\n" for k, v in aliases)
    (home / "repo-aliases").write_text(content, encoding="utf-8")


def _run_cat8(tmp_path, claim: str, aliases=None):
    """Run the fairy's bash block under the stub gh, against a temporary seat.

    ENGRAM_HOME always points at the temp dir, so the cell never reads the
    running seat's own map (this repo's test host sets ENGRAM_HOME globally).
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    home = tmp_path / "home"
    _write_map(home, aliases)
    script = tmp_path / "cat8.sh"
    script.write_text(_bash_block(), encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_STUB_GH, encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "argv.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["GH_ARGV_LOG"] = str(log)
    env["ENGRAM_HOME"] = str(home)
    proc = subprocess.run(
        [bash, str(script)], cwd=tmp_path, env=env, input=claim,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines(), log.read_text(encoding="utf-8").splitlines()


def _repos(calls: list[str]) -> list[str]:
    return [c.split()[c.split().index("--repo") + 1] for c in calls]


def test_empty_map_leaves_a_bare_hash_n_unmatched(tmp_path):
    """Cell 1 — THE TRAP CELL. An empty alternation is not an empty match:
    `() ?#[0-9]+` would resolve a bare `#N`. With no repo-aliases file, stage 2
    must not run at all, so `see #1069` yields no ref and no gh call."""
    lines, calls = _run_cat8(tmp_path, "see #1069", None)
    assert lines == [] and calls == [], (lines, calls)


def test_empty_map_file_behaves_like_no_file(tmp_path):
    """Cell 2 — a file present but with zero lines declares no aliases."""
    lines, calls = _run_cat8(tmp_path, "see #1069", "")
    assert lines == [] and calls == [], (lines, calls)


def test_declared_alias_resolves_to_full_ref(tmp_path):
    """Cell 3 — a declared alias resolves end to end, with the right --repo."""
    lines, calls = _run_cat8(tmp_path, "blocked on alias-a#12 landing", [_ALIAS_A])
    assert lines == ["example-org/repo-a#12 pr MERGED"], lines
    assert _repos(calls) == ["example-org/repo-a"], calls


def test_declared_alias_with_optional_space_resolves_to_full_ref(tmp_path):
    """Cell 4 — one optional space between a declared alias and `#N` resolves."""
    lines, calls = _run_cat8(tmp_path, "see alias-a #7 for context", [_ALIAS_A])
    assert lines == ["example-org/repo-a#7 pr CLOSED"], lines
    assert _repos(calls) == ["example-org/repo-a"], calls


def test_undeclared_alias_resolves_to_nothing_with_populated_map(tmp_path):
    """Cell 5 — the closed-vocabulary cell. The map is NON-EMPTY, so stage 2
    runs; the unlisted key still resolves to nothing. (Against an empty map this
    cell would pass for the wrong reason and test nothing.)"""
    assert _stage1_refs("blocked on alias-z#12 landing") == []
    lines, calls = _run_cat8(
        tmp_path, "blocked on alias-z#12 landing", _MAP_AB)
    assert lines == [] and calls == [], (lines, calls)


@pytest.mark.parametrize("bad_line", [
    "bad=repo",       # single segment
    "bad=a/b/c",      # three segments
    "bad=",           # empty value
    "bad=a/b c",      # bad character in a segment
    "bad a=x/y",      # bad character in the key
])
def test_malformed_declaration_is_ignored(tmp_path, bad_line):
    """Cell 6 — a malformed line is ignored: that key is undeclared. Each file
    also declares a valid alias, so stage 2 is demonstrably running and the
    assertion is not passing on an empty map."""
    lines, calls = _run_cat8(
        tmp_path,
        "bad#12 and alias-a#12",
        f"{bad_line}\nalias-a=example-org/repo-a\n",
    )
    assert lines == ["example-org/repo-a#12 pr MERGED"], lines
    assert _repos(calls) == ["example-org/repo-a"], calls


def test_comment_and_blank_lines_declare_no_aliases(tmp_path):
    """Cell 7 — a file whose only lines are comments/blank declares nothing."""
    content = "# a comment\n\n   \n# another comment\n"
    lines, calls = _run_cat8(tmp_path, "alias-a#12 and alias-a #7", content)
    assert lines == [] and calls == [], (lines, calls)


@pytest.mark.parametrize("aliases", [None, _MAP_AB])
def test_qualified_refs_pass_through_with_and_without_map(tmp_path, aliases):
    """Cell 8 — a qualified `owner/repo#N` is unchanged whether or not the
    seat's file exists."""
    lines, calls = _run_cat8(
        tmp_path,
        "see example-org/repo-a#12 and example-org/repo-b#40",
        aliases,
    )
    assert lines == [
        "example-org/repo-a#12 pr MERGED",
        "example-org/repo-b#40 issue CLOSED",
    ], lines
    assert set(_repos(calls)) == {"example-org/repo-a", "example-org/repo-b"}, calls


def test_alias_word_in_non_reference_prose_resolves(tmp_path):
    """Cell 9 — the STATED COST of the optional-space rule, pinned not fixed.

    A declared alias word followed by a space and a number reads as a reference
    even in prose. Keeping that behaviour is deliberate; the closed map (only
    seat-declared keys) is what bounds the false-positive surface."""
    lines, calls = _run_cat8(tmp_path, "the alias-a #1 priority", [_ALIAS_A])
    assert lines == ["example-org/repo-a#1 - UNKNOWN"], lines
    assert _repos(calls) == ["example-org/repo-a", "example-org/repo-a"], calls


@pytest.mark.parametrize("text", ["issue #12", "the #5", "PR #12"])
def test_whitespace_between_token_and_hash_is_never_a_ref(text):
    # standing guard: stage 1 never tolerates whitespace before the `#`
    assert _stage1_refs(text) == [], f"stage 1 matched {text!r}"


def test_whitespace_guard_runs_end_to_end(tmp_path):
    lines, calls = _run_cat8(tmp_path, "issue #12; the #5; PR #12", _MAP_AB)
    assert lines == [] and calls == [], (lines, calls)


def test_qualified_ref_is_not_double_counted(tmp_path):
    assert _stage1_refs("see example-org/repo-a#12") == ["example-org/repo-a#12"]
    lines, calls = _run_cat8(
        tmp_path, "see example-org/repo-a#12", _MAP_AB)
    assert lines == ["example-org/repo-a#12 pr MERGED"], lines
    assert _repos(calls) == ["example-org/repo-a"], calls


def test_two_segment_path_is_qualified_and_not_rewritten(tmp_path):
    # owner `a`, repo `alias-a`: stage 1 matches it; stage 2's left boundary
    # excludes `/`, so the alias tail is NOT rewritten to a declared repo
    assert _stage1_refs("a/alias-a#5") == ["a/alias-a#5"]
    lines, calls = _run_cat8(tmp_path, "a/alias-a#5", _MAP_AB)
    assert lines == ["a/alias-a#5 - UNKNOWN"], lines
    assert set(_repos(calls)) == {"a/alias-a"}, calls
    assert not any("example-org/repo-a#5" in line for line in lines)


def test_alias_left_boundary_rejects_hyphen_prefix(tmp_path):
    assert _stage1_refs("x-alias-a#5") == []
    lines, calls = _run_cat8(tmp_path, "x-alias-a#5", _MAP_AB)
    assert lines == [] and calls == [], (lines, calls)


def test_alias_and_qualified_forms_of_one_artifact_dedupe(tmp_path):
    # both stages describe the same artifact; the rewrite happens before the
    # merge, so `sort -u` collapses them to one checked ref
    lines, calls = _run_cat8(
        tmp_path, "see alias-a#12 and example-org/repo-a#12", _MAP_AB
    )
    assert lines == ["example-org/repo-a#12 pr MERGED"], lines
    assert _repos(calls) == ["example-org/repo-a"], calls


# Constructs bash 3.2 lacks. NOT exhaustive -- bash has more 4.x-only syntax
# than one list can hold -- so the cell's claim is "none of THESE appear", which
# is why the docstring says so rather than "the block is bash-4-free".
_BASH4_ONLY = (
    # 4.0
    "declare -A", "local -A", "declare -g", "mapfile", "readarray", "|&", "&>>",
    # 4.2
    "[[ -v ",
    # 4.3
    "local -n", "declare -n", ";;&",
    # 4.4 / 4.0 case-modification and case-conversion operators
    "@Q}", ",,}", "^^}", "^}", ",}",
)


def test_cat8_bash_block_has_no_bash4_only_constructs():
    """The block must run under stock macOS bash 3.2: an unsupported
    declaration does not abort the script, it leaves the map unset and the
    stage emits NOTHING with exit 0 -- the silent under-reporting this
    category exists to prevent. Checks executable lines only, so a comment
    naming a construct cannot satisfy or trip the cell.

    BOUND, stated because the list cannot be exhaustive: this proves the block
    contains none of _BASH4_ONLY's entries. It is a construct inventory, not a
    3.2 run -- the acceptance evidence for the platform is a Mac seat's own
    /bin/bash, which has been run once (bash 3.2.57, macOS 27.0.1, 2026-11-14)
    and is not reproduced here."""
    block = _bash_block()  # asserts the block exists; fails loudly if reshaped
    executable = "\n".join(
        line for line in block.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert executable.strip(), "no executable lines in the Category 8 bash block"
    for construct in _BASH4_ONLY:
        assert construct not in executable, (
            f"bash-4-only construct {construct!r} in the Category 8 block; "
            f"stock macOS ships bash 3.2")


def test_block_has_no_hardcoded_alias_map():
    """The map moved to the seat's file. A hardcoded alias table in the shipped
    block would re-introduce one organisation's repository names; this is the
    structural counterpart to the no-org-name cell below."""
    block = _bash_block()
    executable = "\n".join(
        line for line in block.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "repo-aliases" in executable, "the block does not read the seat map"
    assert 'case "$1" in' not in executable, "a repo_for case table is hardcoded"
    assert not re.search(
        r"^\s*[A-Za-z0-9_-]+\)\s*echo\s+\S+/\S+", executable, re.MULTILINE
    ), "a literal `key) echo owner/repo` arm is a hardcoded alias map"


# Repository-reference forms that must never ship in the fairy spec. The bare
# word `engram` is deliberately NOT here: it is the PRODUCT name and legitimately
# appears throughout the file (the fairy's own name, ~/.engram, ENGRAM_HOME,
# `engram_*` MCP tools). What must not ship is any org/repo identity.
_BANNED_ORG_REFS = ("engram-agents", "engram-alpha", "umwelt")


def test_no_org_or_repo_name_in_shipped_fairy():
    """Cell 10 — the shipped spec carries no organisation or repository name.

    Uses Python `in`, not `grep`: in this environment `grep` is a shell function
    wrapping ugrep with `-E` alternation inert, so a `grep -E` cell can answer
    the wrong question while exiting plausibly.

    The positive control proves the matcher is not vacuous: it must catch each
    banned form in a literal that contains them."""
    assert _BANNED_ORG_REFS, "banned-name list is empty -- the cell would test nothing"
    probe = "see engram-agents/engram-alpha#5 and umwelt#9"
    caught = [name for name in _BANNED_ORG_REFS if name in probe]
    assert caught == list(_BANNED_ORG_REFS), (
        f"matcher is vacuous: only caught {caught!r}")
    text = FAIRY_MD.read_text(encoding="utf-8")
    hits = [name for name in _BANNED_ORG_REFS if name in text]
    assert not hits, f"organisation/repo name(s) still shipped: {hits}"


def test_report_cat8_finding_carries_full_qualified_ref():
    text = FAIRY_MD.read_text(encoding="utf-8")
    s = text.index("## Category 8")
    nxt = text.find("\n## ", s + 1)
    sec = text[s:nxt if nxt != -1 else len(text)]
    assert "claim references owner/repo#N" in sec
    assert "external reference owner/repo#N <state>" in sec
    assert "always repo-qualified" in sec
    assert "resolved to its full `owner/repo` before the finding is written" in sec
