"""Golden-list parity test for the PR-anchor grammar across the client/
server boundary (#1715 follow-up, Borges's fast-follow suggestion on
PR #1722).

The `pr/<N>` / `pr/<owner>/<repo>/<N>` anchor grammar is defined TWICE:
`tools/baton.py`'s `_parse_github_anchor` (client CLI) and
`src/forum/board_projects.py`'s `_PR_ANCHOR_RE` (server-side gh
reconciliation). #1715 itself was exactly this failure mode -- a regex
silently not matching a form it should have -- so a silent drift between
these two copies is a live risk, not a hypothetical one: a future edit to
one grammar (e.g. tightening/loosening the owner/repo charset) that isn't
mirrored in the other would reintroduce a #1715-shaped bug without any
test catching it. This file is that test: assert both parsers agree on a
shared golden list of accept/reject cases.

Deliberately does NOT assert the two are byte-identical regexes (they
differ in one legitimate way: baton.py's callers lowercase the anchor
before matching, so its regex has no re.IGNORECASE; board_projects.py's
regex carries re.IGNORECASE itself since some of its callers pass through
without a lowering step first). The contract that matters is behavioral
agreement on RESOLVED (repo, pr_number) for the same input, not identical
implementation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

_TOOLS_DIR = str(REPO_ROOT / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import baton as _baton  # noqa: E402


def _load_board_projects():
    """Load board_projects.py directly by file path -- avoids needing
    `src/` on sys.path just to satisfy `from forum import board_projects`
    (board_projects.py's own imports are all stdlib/absolute, so a direct
    file load is safe and doesn't require the `forum` package context)."""
    path = REPO_ROOT / "src" / "forum" / "board_projects.py"
    spec = importlib.util.spec_from_file_location("board_projects_parity_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_board_projects = _load_board_projects()


def _baton_parse(anchor: str):
    """Return (repo, pr_number) via tools/baton.py's parser, or None if
    not a PR-shaped anchor. Mirrors baton.py's own calling convention:
    callers lowercase+strip before calling _parse_github_anchor."""
    parsed = _baton._parse_github_anchor(anchor.strip().lower())
    if parsed is None or parsed[0] != "pr":
        return None
    kind, repo, number = parsed
    return (repo or _baton.DEFAULT_GITHUB_REPO, number)


def _board_projects_parse(anchor: str):
    """Return (repo, pr_number) via board_projects.py's regex, or None.
    Mirrors board_projects.py's own calling convention: callers pass
    .strip() only (the regex itself carries IGNORECASE)."""
    m = _board_projects._PR_ANCHOR_RE.match(anchor.strip())
    if not m:
        return None
    owner, repo_name, number = m.groups()
    repo = f"{owner}/{repo_name}" if owner and repo_name else _board_projects.DEFAULT_GITHUB_REPO
    return (repo, number)


# Golden list: (anchor, expected_repo_or_None_for_default, expected_pr_number_or_None)
# expected_repo=None means "whichever default each parser resolves to" --
# checked for AGREEMENT, not a literal string (the two parsers' defaults
# come from the same env var but are independently read constants).
_ACCEPT_CASES = [
    "pr/1005",
    "pr/22",
    "PR/1005",              # case variance -- must agree despite the
    "Pr/22",                # different case-handling strategy noted above
    "pr/engram-agents/engram-paper/22",
    "PR/Engram-Agents/Engram-Paper/22",
    "pr/some-org/some-repo-with-dashes/9999",
    "pr/a.b/c.d/1",         # dots in owner/repo (charset includes '.')
]

_REJECT_CASES = [
    "",
    "project/4",            # valid FOR baton.py, but board_projects.py's
                             # regex is PR-only by design -- not a shared
                             # form, so both must reject it as non-PR here
    "pr/only-one-segment/22",
    "pr/not-a-number",
    "pr/",
    "nonsense",
    "issue/42",
]


def test_both_parsers_accept_the_same_pr_anchor_forms_and_agree_on_repo():
    for anchor in _ACCEPT_CASES:
        baton_result = _baton_parse(anchor)
        board_result = _board_projects_parse(anchor)
        assert baton_result is not None, f"{anchor!r}: baton.py rejected, expected accept"
        assert board_result is not None, f"{anchor!r}: board_projects.py rejected, expected accept"
        assert baton_result[1] == board_result[1], (
            f"{anchor!r}: PR number disagreement -- baton={baton_result[1]!r} "
            f"board_projects={board_result[1]!r}"
        )
        assert baton_result[0].lower() == board_result[0].lower(), (
            f"{anchor!r}: resolved repo disagreement -- baton={baton_result[0]!r} "
            f"board_projects={board_result[0]!r}"
        )


def test_both_parsers_reject_the_same_non_pr_anchor_forms():
    for anchor in _REJECT_CASES:
        assert _baton_parse(anchor) is None, f"{anchor!r}: baton.py accepted, expected reject"
        assert _board_projects_parse(anchor) is None, (
            f"{anchor!r}: board_projects.py accepted, expected reject"
        )
