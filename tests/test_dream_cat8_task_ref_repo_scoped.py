"""Dream-fairy Category 8 (open tasks with stale external references) and the
two dream-master passages that act on its findings must resolve PR/issue
references against the repo they belong to, never the cwd checkout
(alpha#3427; same class as alpha#3188 / #3418).

A bare `#N` has no repo: `gh pr view N` silently resolves it against whichever
repo is the current checkout, so a same-numbered PR in the WRONG repo can read
as MERGED/CLOSED and a live task gets proposed for closure.

The fix: extraction matches only `owner/repo#N`, every gh call passes
`--repo` taken from the reference, a bare `#N` is structurally unmatched, and a
gh error is UNKNOWN. These tests read the extraction regex and the bash block
out of the fairy spec (single source of truth) and execute them.
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
    """Run the extraction pattern like `grep -o` + the sed boundary strip."""
    return [m.group(0)[len(m.group(1)):] for m in _pattern().finditer(text)]


def _pattern() -> re.Pattern[str]:
    m = re.search(r"grep -oE '([^']+)'", _cat8())
    assert m, "Category 8 grep -oE pattern not found -- bash block reshaped?"
    return re.compile(m.group(1))


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
    "alpha#2400",  # bare repo shorthand: no owner/
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
