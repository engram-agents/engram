"""engram-sleep Step 4's ask-list sweep (and the dream-master spec's step 8a
sweep, alpha#3418 -- the same fix, ported) must resolve PR/issue references
against the repo they actually belong to, never the cwd checkout (alpha#3188).

A bare `#N` has no repo to check against: `gh pr view "$n"` / `gh issue view
"$n"` silently resolve it against whichever repo happens to be the current
checkout. On a multi-repo ask-file, two repos can share a number -- the cwd
repo's own same-numbered PR/issue then answers in place of the real one, with
no error. Measured four separate times on the same four PR numbers
(engram-alpha#3188): a bare `#N` swept from engram-alpha's checkout reported
four live ai-portfolio PRs as MERGED.

The fix is mechanical: references are written `owner/repo#N`, the sweep's
extraction pattern only matches that shape, and `gh` is called with
`--repo` taken from the reference itself. A bare `#N` is therefore never
even a sweep candidate -- not "usually skipped", structurally unmatched.

These tests read the sweep's own documented regex out of SKILL.md (the
single source of truth an agent actually runs) and exercise it directly,
rather than asserting on prose alone -- a doc-only check that only verifies
*words* changed, not that the extraction *behavior* changed.
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
SLEEP_MD = REPO / "src/engram/skills/claude/engram-sleep/SKILL.md"
DREAM_MASTER_MD = REPO / "src/engram/agents/claude/engram-dream-master.md"

# (path, section start marker, section end marker). Both specs carry the same
# sweep; parametrizing keeps them from drifting apart again (alpha#3418).
_SWEEPS = {
    "sleep": (
        SLEEP_MD,
        "**Deterministic external-state sweep (the prune gate).**",
        "**Walk the day's commit log**",
    ),
    "dream-master": (
        DREAM_MASTER_MD,
        "**(a) Deterministic external-state sweep",
        "**(b) Everything else",
    ),
}

both = pytest.mark.parametrize("which", sorted(_SWEEPS))


def _ask_list_sweep_section(which: str = "sleep") -> str:
    path, marker, end_marker = _SWEEPS[which]
    text = path.read_text(encoding="utf-8")
    assert marker in text, f"{which}: ask-list sweep section heading moved; this gate needs updating"
    start = text.index(marker)
    assert end_marker in text[start:], f"{which}: sweep section's next marker moved; this gate needs updating"
    end = text.index(end_marker, start)
    return text[start:end]


def _extraction_pattern(which: str = "sleep") -> re.Pattern[str]:
    """Pull the `grep -oE '...'` pattern out of the sweep's own bash block."""
    section = _ask_list_sweep_section(which)
    m = re.search(r"grep -oE '([^']+)'", section)
    assert m, f"{which}: sweep's grep -oE pattern not found -- bash block reshaped?"
    return re.compile(m.group(1))


@both
def test_extraction_pattern_matches_owner_repo_hash_n(which):
    pattern = _extraction_pattern(which)
    assert pattern.search("engram-agents/engram#3188")
    assert pattern.search("LeiShi/ai-portfolio#83")
    # bare "alpha#2400" (no owner/) must NOT match -- "owner/repo#N" is required
    assert not pattern.fullmatch("alpha#2400")


@both
def test_extraction_pattern_does_not_match_bare_hash_n(which):
    """The bug: a bare #N used to be swept and resolved against the cwd repo."""
    pattern = _extraction_pattern(which)
    for bare in ("#83", "#84", "#1650", "#3188"):
        assert not pattern.search(bare), (
            f"extraction pattern matched bare {bare!r} -- a bare #N must never "
            "be a sweep candidate (alpha#3188)"
        )
    # a real mixed entry: one owner/repo#N reference alongside a bare #N in the
    # same string -- the pattern must extract ONLY the qualified one.
    prose = (
        "LeiShi/ai-portfolio#83 is still OPEN; #1650 is the sibling "
        "bug in baton gc (alpha#2400 is unrelated)."
    )
    matches = pattern.findall(prose)
    assert matches == ["LeiShi/ai-portfolio#83"], (
        f"expected only the qualified reference to be extracted from {prose!r}, "
        f"got {matches}"
    )


@both
def test_gh_calls_are_repo_scoped_from_the_reference(which):
    section = _ask_list_sweep_section(which)
    assert "--repo \"$repo\"" in section, (
        "gh pr view / gh issue view calls must pass --repo \"$repo\" taken "
        "from the matched reference, not rely on the cwd checkout"
    )
    assert section.count("--repo \"$repo\"") >= 2, (
        "both the gh pr view and gh issue view fallback calls must be --repo-scoped"
    )


@both
def test_bare_reference_contract_is_stated_explicitly(which):
    section = _ask_list_sweep_section(which)
    assert "never a bare `#N`" in section
    assert "never prune-eligible, unconditionally" in section
    assert "UNKNOWN (no repo)" in section


@both
def test_old_unscoped_pattern_is_gone(which):
    """Regression guard: the old bug's exact pattern must not reappear."""
    section = _ask_list_sweep_section(which)
    assert "grep -oE '#[0-9]+'" not in section
    assert "tr -d '#'" not in section


@both
def test_commit_audit_trail_line_is_repo_qualified(which):
    """Pruned entries are listed as `owner/repo#N <summary> -- MERGED/CLOSED per gh`."""
    if which == "sleep":
        text = SLEEP_MD.read_text(encoding="utf-8")
    else:
        text = DREAM_MASTER_MD.read_text(encoding="utf-8")
    assert "`owner/repo#N <one-line summary> — MERGED/CLOSED per gh`" in text
    assert "`#N <one-line summary> — MERGED/CLOSED per gh`" not in text


@both
def test_unknown_fallback_is_in_the_snippet(which):
    """A failed `gh` lookup must degrade to UNKNOWN (leave untouched), never to
    an empty/guessed state that could be read as prune-eligible (alpha#3418)."""
    section = _ask_list_sweep_section(which)
    assert "|| state=UNKNOWN" in section, (
        f"{which}: the sweep snippet lost its `|| state=UNKNOWN` fallback"
    )


@both
def test_one_time_migration_note_is_present(which):
    """Old bare-`#N` / bare-`repo#N` entries silently drop out of coverage; the
    spec must say so and tell the agent to rewrite them."""
    section = _ask_list_sweep_section(which)
    assert "One-time migration note" in section, (
        f"{which}: the one-time migration note was removed"
    )
    assert "bare-`repo#N` shorthand" in section, (
        f"{which}: the migration note no longer names the old shorthand"
    )


@both
def test_non_github_path_digits_caveat_is_present(which):
    """The pattern can match `path/segment#digits` (a doc path with a line
    anchor); the spec must say that is harmless and fails to UNKNOWN."""
    section = _ask_list_sweep_section(which)
    assert "non-GitHub `path/segment#digits`" in section, (
        f"{which}: the non-GitHub path#digits caveat was removed"
    )


# ---------------------------------------------------------------------------
# Executing test (round 3, alpha#3418 / Clio's review on #3426). The tests
# above read the snippet's TEXT; two one-token breakages pass all of them:
# `repo="${ref#*#}"` (every gh call becomes `--repo <number>`, everything
# UNKNOWN, nothing ever pruned) and `echo "#$n $state"` (same-numbered items
# from different repos collapse into one name). Only running the block
# catches those, so run it under a stub `gh`.
# ---------------------------------------------------------------------------

# The ask-file path as each spec literally writes it.
_ASK_PATH_PLACEHOLDERS = {
    "sleep": '"${ENGRAM_HOME:-$HOME/.engram}"/ask-{{USER_NAME}}.md',
    "dream-master": "~/.engram/ask-<primary_user>.md",
}

# Answers by (subcommand, repo, number). Anything absent exits 1.
_STUB_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$GH_ARGV_LOG"
sub="$1"; verb="$2"; n="$3"; shift 3
repo=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--repo" ]; then repo="$2"; shift; fi
  shift
done
[ "$verb" = "view" ] || exit 1
if [ -z "$repo" ]; then
  # A missing --repo must be visible: answer like some OTHER repo's item.
  echo "WRONGREPO"; exit 0
fi
case "$sub $repo $n" in
  "pr example-org/repo-a 3417") echo MERGED ;;
  "pr example-org/repo-b 3417")       echo OPEN ;;
  "pr example-org/repo-b 88")         echo CLOSED ;;
  "issue example-org/repo-c 3500")    echo CLOSED ;;
  *) exit 1 ;;
esac
"""

_ASK_FILE = """\
# Ask file
- example-org/repo-a#3417 ready to merge
- example-org/repo-b#3417 same number, different repo
- example-org/repo-c#3500 is an issue, not a PR
- example-org/repo-b#88 listed here ...
- ... and example-org/repo-b#88 again further down
- bare #3374 must never be a candidate
- bare shorthand alpha#2400 must never be a candidate
- non-GitHub anchor docs/foo.md#12 falls to UNKNOWN
"""

_EXPECTED_LINES = sorted([
    "example-org/repo-a#3417 MERGED",
    "example-org/repo-b#3417 OPEN",
    "example-org/repo-c#3500 CLOSED",
    "example-org/repo-b#88 CLOSED",
    "docs/foo.md#12 UNKNOWN",
])


def _sweep_bash_block(which: str) -> str:
    section = _ask_list_sweep_section(which)
    m = re.search(r"```bash\n(.*?)```", section, re.DOTALL)
    assert m, f"{which}: sweep's fenced bash block not found"
    return textwrap.dedent(m.group(1))


@both
def test_sweep_block_executes_repo_scoped(which, tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable; cannot execute the sweep block")

    placeholder = _ASK_PATH_PLACEHOLDERS[which]
    block = _sweep_bash_block(which)
    assert placeholder in block, (
        f"{which}: ask-file path placeholder {placeholder!r} not found in the "
        "bash block -- spec changed its spelling; update _ASK_PATH_PLACEHOLDERS"
    )
    ask = tmp_path / "ask.md"
    ask.write_text(_ASK_FILE, encoding="utf-8")
    script = tmp_path / "sweep.sh"
    script.write_text(block.replace(placeholder, str(ask)), encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_STUB_GH, encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "gh_argv.log"
    log.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["GH_ARGV_LOG"] = str(log)
    proc = subprocess.run(
        [bash, str(script)], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"{which}: sweep block failed: {proc.stderr}"

    # Exact output. sorted() only makes it independent of `sort -u`'s locale;
    # the list comparison still catches duplicates and extras.
    got = sorted(proc.stdout.splitlines())
    assert got == _EXPECTED_LINES, f"{which}: unexpected sweep output:\n{proc.stdout}"
    # bare `#3374` / bare-shorthand `alpha#2400` are never candidates.
    assert not any(line.startswith(("#3374", "alpha#2400")) for line in got)
    assert "WRONGREPO" not in proc.stdout

    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls, f"{which}: stub gh was never called"
    for call in calls:
        argv = call.split()
        assert "--repo" in argv, f"{which}: gh call without --repo: {call!r}"
        repo_arg = argv[argv.index("--repo") + 1]
        assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_arg), (
            f"{which}: --repo value is not owner/repo: {call!r}"
        )
    # The issue-only ref was resolved via `gh issue view` (after `pr view` failed).
    assert "issue view 3500 --repo example-org/repo-c --json state -q .state" in calls
    # The doubled ref was queried once.
    assert sum(c.startswith("pr view 88 ") for c in calls) == 1
