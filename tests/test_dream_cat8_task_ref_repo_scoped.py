"""Dream-fairy Category 8 (open tasks with stale external references) and the
two dream-master passages that act on its findings must resolve PR/issue
references against the repo they belong to, never the cwd checkout
(alpha#3427; same class as alpha#3188 / #3418).

A bare `#N` has no repo: `gh pr view N` silently resolves it against whichever
repo is the current checkout, so a same-numbered PR in the WRONG repo can read
as MERGED/CLOSED and a live task gets proposed for closure.

The fix: extraction matches `owner/repo#N` and the two declared bare-repo
aliases (`engram-alpha`, `umwelt`) resolved through a closed map, every gh call
passes `--repo` taken from the resolved reference, a bare `#N` and any unlisted
alias are structurally unmatched, and a gh error is UNKNOWN. These tests read
the extraction regexes and the bash block out of the fairy spec (single source
of truth) and execute them.
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


def _refs(text: str) -> list[str]:
    """The tokens the two `grep -o` stages extract, before stage 2's map rewrite
    and the final `sort -u`: each stage's own match stream, sed-stripped.

    BOUND: this models only the boundary `sed`, not stage 2's `s/ +#/#/`. An
    alias written with the optional space therefore comes back as `umwelt #1069`,
    space intact. That is deliberate — the space form's behaviour is asserted
    end-to-end through `_run_cat8`, which is where the join actually runs — but a
    future cell that reads the SPACED token out of `_refs` would be asserting on
    a string the block never produces. Assert the space form through `_run_cat8`."""
    out: set[str] = set()
    for pat in (_pattern(), _alias_pattern()):
        for m in pat.finditer(text):
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


def _aliases_literal() -> str:
    m = re.search(r"^ALIASES='([^']*)'$", _bash_block(), re.MULTILINE)
    assert m, "Category 8 ALIASES= line not found -- bash block reshaped?"
    return m.group(1)


def _alias_pattern() -> re.Pattern[str]:
    """Stage 2: the declared aliases, with $ALIASES expanded from the block."""
    m = re.search(r'grep -oE "([^"]+)"', _cat8())
    assert m, "Category 8 stage-2 grep -oE pattern not found -- bash block reshaped?"
    return re.compile(m.group(1).replace("$ALIASES", _aliases_literal()))


def _bash_block() -> str:
    m = re.search(r"```bash\n(.*?)```", _cat8(), re.DOTALL)
    assert m, "Category 8 fenced bash block not found"
    return textwrap.dedent(m.group(1))


def test_pattern_matches_owner_repo_hash_n_with_captures():
    p = _pattern()
    refs = _refs("blocked on example-org/repo-a#12 landing")
    assert refs == ["example-org/repo-a#12"]
    repo, n = refs[0].rsplit("#", 1)
    assert (repo, n) == ("example-org/repo-a", "12")
    assert _refs("example-org/repo-a#12 at line start") == ["example-org/repo-a#12"]
    assert _refs("see example-org/repo-a#5") == ["example-org/repo-a#5"]


def test_left_boundary_rejects_longer_path():
    # a/b/c#5 must not be mis-read as b/c#5
    assert _refs("path a/b/c#5 here") == []
    assert _refs("x.y/z/w#5") == []


@pytest.mark.parametrize("text", [
    "#12", "PR #12", "closes #12", "issue #12", "pull request #12",
    "Closes #12 and fixes #13", "seq 7183", "forum post 7183",
    "alpha#2400",  # not a map key: the map is closed, so this stays unmatched
])
def test_pattern_does_not_match_bare_or_other_namespace(text):
    assert not _refs(text), f"pattern matched {text!r}"


def test_mixed_claim_extracts_only_qualified():
    prose = "tk: example-org/repo-a#12 is open; PR #12 elsewhere; closes #99; seq 7183"
    assert _refs(prose) == ["example-org/repo-a#12"]


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
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
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
    proc = subprocess.run(
        [bash, str(script)], cwd=tmp_path, env=env, input=_CLAIM,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(proc.stdout.splitlines()) == _EXPECTED, proc.stdout
    assert "WRONGREPO" not in proc.stdout
    assert "example-org/repo-a#7 pr CLOSED" in proc.stdout.splitlines()
    assert not any(l.startswith("b/c#5") for l in proc.stdout.splitlines())
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls, "stub gh never called"
    for call in calls:
        argv = call.split()
        assert "--repo" in argv, f"gh call without --repo: {call!r}"
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", argv[argv.index("--repo") + 1]
        ), call
    # bare numbers never reached gh as a bare-number call
    assert not any(c.split()[:3] in (["pr", "view", "12"],) and "--repo" not in c
                   for c in calls)


# --- the declared repo map: a closed vocabulary, resolved before reporting --
#
# The two stages run from one `IN=$(cat)` so both see the whole claim; stage 2
# rewrites each declared alias to its full owner/repo inside the stage, so the
# final `sort -u` dedupes after the rewrite. `_refs` models the two grep/sed
# stages (pre-rewrite, pre-dedupe); `_run_cat8` runs the block end to end.


def _run_cat8(tmp_path, claim: str):
    """Run the fairy's bash block under the stub gh."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
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
    proc = subprocess.run(
        [bash, str(script)], cwd=tmp_path, env=env, input=claim,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines(), log.read_text(encoding="utf-8").splitlines()


def _repos(calls: list[str]) -> list[str]:
    return [c.split()[c.split().index("--repo") + 1] for c in calls]


def test_alias_engram_alpha_resolves_to_full_ref(tmp_path):
    lines, calls = _run_cat8(tmp_path, "blocked on engram-alpha#3613 landing")
    assert lines == ["engram-agents/engram-alpha#3613 - UNKNOWN"], lines
    assert set(_repos(calls)) == {"engram-agents/engram-alpha"}, calls


def test_alias_umwelt_resolves_to_full_ref(tmp_path):
    lines, calls = _run_cat8(tmp_path, "see umwelt#1069 for context")
    assert lines == ["engram-agents/umwelt#1069 - UNKNOWN"], lines
    assert set(_repos(calls)) == {"engram-agents/umwelt"}, calls


def test_alias_with_optional_space_resolves_to_full_ref(tmp_path):
    # the dominant authoring style writes `umwelt #1069`; one optional space is
    # accepted and joined before the map lookup, so the ref reaches gh qualified
    lines, calls = _run_cat8(tmp_path, "see umwelt #1069 for context")
    assert lines == ["engram-agents/umwelt#1069 - UNKNOWN"], lines
    assert set(_repos(calls)) == {"engram-agents/umwelt"}, calls


def test_unlisted_org_repo_is_not_an_alias(tmp_path):
    # `engram` and `engram-alpha` are different repos with overlapping numbers
    assert _refs("blocked on engram#5 landing") == []
    lines, _ = _run_cat8(tmp_path, "blocked on engram#5 landing")
    assert lines == [], lines


@pytest.mark.parametrize("text", ["issue #12", "the #5", "PR #12"])
def test_whitespace_between_token_and_hash_is_never_a_ref(text):
    # standing guard: the extractor never tolerates whitespace before the `#`
    assert _refs(text) == [], f"two-stage extraction matched {text!r}"


def test_whitespace_guard_runs_end_to_end(tmp_path):
    lines, calls = _run_cat8(tmp_path, "issue #12; the #5; PR #12")
    assert lines == [] and calls == []


def test_qualified_alias_repo_ref_is_not_double_counted(tmp_path):
    assert _refs("see engram-agents/engram-alpha#3613") == [
        "engram-agents/engram-alpha#3613"
    ]
    lines, calls = _run_cat8(tmp_path, "see engram-agents/engram-alpha#3613")
    assert lines == ["engram-agents/engram-alpha#3613 - UNKNOWN"], lines
    assert set(_repos(calls)) == {"engram-agents/engram-alpha"}, calls
    assert len(calls) <= 2, calls  # one ref: at most the pr + issue probe


def test_two_segment_path_is_qualified_and_not_rewritten(tmp_path):
    # owner `a`, repo `engram-alpha`: stage 1 matches it; stage 2 must not
    # rewrite the alias tail to the full org repo
    assert _refs("a/engram-alpha#5") == ["a/engram-alpha#5"]
    lines, calls = _run_cat8(tmp_path, "a/engram-alpha#5")
    assert lines == ["a/engram-alpha#5 - UNKNOWN"], lines
    assert set(_repos(calls)) == {"a/engram-alpha"}, calls
    assert not any("engram-agents/engram-alpha#5" in line for line in lines)


def test_alias_left_boundary_rejects_hyphen_prefix(tmp_path):
    assert _refs("x-engram-alpha#5") == []
    lines, _ = _run_cat8(tmp_path, "x-engram-alpha#5")
    assert lines == [], lines


def test_alias_and_qualified_forms_of_one_artifact_dedupe(tmp_path):
    # both stages describe the same artifact; the rewrite happens before the
    # merge, so `sort -u` collapses them to one checked ref
    lines, calls = _run_cat8(
        tmp_path, "see umwelt#12 and engram-agents/umwelt#12"
    )
    assert lines == ["engram-agents/umwelt#12 - UNKNOWN"], lines
    assert set(_repos(calls)) == {"engram-agents/umwelt"}, calls
    assert len(calls) <= 2, calls  # one artifact: at most the pr + issue probe


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


def test_repomap_keys_equal_aliases_alternation():
    block = _bash_block()
    m = re.search(r"^repo_for\(\) \{\n(.*?)^\}", block, re.DOTALL | re.MULTILINE)
    assert m, "repo_for function not found -- bash block reshaped?"
    arms = re.findall(
        r"^\s*([A-Za-z0-9_.-]+)\)\s*echo\s+(\S+)", m.group(1), re.MULTILINE
    )
    keys = {key for key, _ in arms}
    values = {value for _, value in arms}
    aliases = _aliases_literal().split("|")
    assert keys, "repo_for declares no aliases"
    assert aliases and all(aliases), f"ALIASES is empty or malformed: {aliases!r}"
    assert keys == set(aliases), (keys, aliases)
    for value in sorted(values):
        assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value), (
            f"map value is not a two-segment owner/repo: {value!r}")


def test_report_cat8_finding_carries_full_qualified_ref():
    text = FAIRY_MD.read_text(encoding="utf-8")
    s = text.index("## Category 8")
    nxt = text.find("\n## ", s + 1)
    sec = text[s:nxt if nxt != -1 else len(text)]
    assert "claim references owner/repo#N" in sec
    assert "external reference owner/repo#N <state>" in sec
    assert "always repo-qualified" in sec
    assert "resolved to its full `owner/repo` before the finding is written" in sec
