"""Layout guard for ``docs/``: public user docs at the top, everything else in ``docs/internal/``.

The rule that decides where a doc lives: a doc stays directly under ``docs/``
(public) if a shipped surface -- a skill, a tool's help text, the README, or
the public paper -- sends a user or a user's agent to it.  Everything else
about *building* ENGRAM goes in ``docs/internal/``, which ONE entry in
``release-minus-set.txt`` keeps out of the public snapshot.  Nobody has to
remember to add each new internal doc to the minus-set.

Files inside ``docs/handbook/`` and ``docs/assets/`` are public by definition.
This guard polices only the top level of ``docs/`` and the set of sub-folders,
not what is inside those two folders.

Contract (one cell each; stdlib only, reads ``git ls-files``):

- G1. Every tracked file directly under ``docs/`` is on ALLOWLIST below, and
  each entry carries a one-line reason naming the shipped surface that sends
  a reader to it.  A new top-level file fails with a message saying where it
  belongs and why.  Every allowlist entry also names a file that exists.
- G2. The only sub-folders of ``docs/`` are ``assets/``, ``handbook/`` and
  ``internal/``, each with a reason (a shipped surface, or for ``internal/``
  the one minus-set entry).
- G3. ``release-minus-set.txt`` carries the entry ``docs/internal/`` and none
  of the old per-file / per-folder ``docs/...`` entries for moved docs.
- G4. The cutover tool's own matcher (``_apply_minus_set`` in
  ``src/engram/tools/release-cutover.py``), run over an export of
  ``git ls-files``, (a) warns about NO stale minus-set entry and (b) leaves a
  snapshot whose ``docs/`` is exactly ALLOWLIST plus the public sub-folders
  (``assets/`` and ``handbook/``; ``internal/`` is gone).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# name directly under docs/ -> the shipped surface that sends a reader to it.
# A new top-level file is only added here WITH such a reason; otherwise it
# belongs in docs/internal/.
ALLOWLIST = {
    "CHANGELOG.md": "release history, the file users read to see what changed between versions",
    "FORUM-SERVER.md": "README.md and README-AGENT.md send users to it for running the forum server",
    "README.md": "the index of docs/ itself, for public readers (carries the docs/internal/ pointer sentence)",
    "ZH-TERMINOLOGY.md": "the public paper's Chinese chapters cite it as the terminology ruling",
    "baton-protocol.md": "the engram-baton skill, the baton prompt hook, tools/baton.py, README-AGENT.md and the forum service template send readers to it",
    "cascade-machine.md": "the engram-resolve-cascade skill and the dream-master agent send agents to it",
    "compaction-continuity.md": "the engram-upgrade skill (src/engram/skills/claude/engram-upgrade/SKILL.md) names it twice, where it explains the CLAUDE.md render source",
    "macos-agent-setup.md": "README-AGENT.md links it from its install section (Mac users installing ENGRAM)",
    "os-harness-constraints.md": "tools/os-harness-check.py names it in its docstring and its error output",
}
# The only sub-folders of docs/, each with its reason.  "internal" is the one
# folder the public snapshot does NOT carry (one release-minus-set.txt entry).
SUBFOLDER_REASONS = {
    "assets": "README.md and README.zh.md show assets/engram-triptych.png",
    "handbook": "README.md, README.zh.md and README-AGENT.md send readers to docs/handbook/ for the deep internals",
    "internal": "design notes, specs, research, team process and release procedure; one release-minus-set.txt entry keeps it out of the public release",
}
ALLOWED_SUBFOLDERS = set(SUBFOLDER_REASONS)
PUBLIC_SUBFOLDERS = ALLOWED_SUBFOLDERS - {"internal"}

# The docs that moved into docs/internal/, by their OLD top-level name
# (files, and folders written with a trailing slash).  Their per-file or
# per-folder minus-set entries are replaced by the single docs/internal/ line.
MOVED_DOC_NAMES = [
    "PR-MERGE-POLICY.md", "PR-REVIEW-CONVENTION.md", "PR-ISSUE-CLOSURE.md",
    "GH-STACK.md", "CONTRACT-FIRST-TESTS.md", "CI-COST-MODEL.md",
    "LANE-PRIORITIZATION.md", "conclusion-warrant-falsifier-log.md",
    "RELEASING.md", "DEVELOPMENT.md", "rc-upgrade-runbook.md",
    "2265-2347-edge-correction-design.md", "2574-telemetry-design.md",
    "2613-retraction-replacement-contract.md", "2652-as-built-brief.md",
    "2652-engrammanager-design.md", "2652-migration-ledger.md",
    "2652-security-slice-design.md", "864-multi-env-harness-design.md",
    "principle-triggers-design.md", "recall-triggering-blueprint.md",
    "health-series-design.md", "claude-code-plugin-system.md",
    ".scan-leaks-local.json.example",
    "spec/", "specs/", "design/", "cycle/", "perf/",
    "inter-agent/", "archive/", "983-review/",
]


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.split("\0") if p]


def _minus_set_entries() -> list[str]:
    entries = []
    for line in (REPO_ROOT / "release-minus-set.txt").read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            entries.append(s)
    return entries


def _load_cutover():
    path = REPO_ROOT / "src" / "engram" / "tools" / "release-cutover.py"
    spec = importlib.util.spec_from_file_location("release_cutover_for_docs_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot_after_minus_set() -> tuple[list[str], list[str]]:
    """Export git ls-files as empty files, run the real matcher, return (warned, docs/ listing)."""
    cutover = _load_cutover()
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for rel in _tracked():
            f = tmp / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("")
        _removed, warned = cutover._apply_minus_set(tmp, _minus_set_entries())
        docs = tmp / "docs"
        listing = sorted(os.listdir(docs)) if docs.is_dir() else []
    return warned, listing


def test_g1_every_top_level_docs_file_is_on_the_allowlist():
    top = sorted(
        p[len("docs/"):] for p in _tracked() if p.startswith("docs/") and "/" not in p[len("docs/"):]
    )
    stray = [n for n in top if n not in ALLOWLIST]
    assert not stray, (
        f"New top-level file(s) directly under docs/: {stray}. docs/ holds only docs that a "
        "shipped surface (a skill, a tool's help, the README or the public paper) sends a user "
        "or a user's agent to. Docs about BUILDING ENGRAM (design notes, specs, research, team "
        "process, release procedure) go in docs/internal/, which release-minus-set.txt keeps out "
        "of the public release. If a shipped surface really does send readers to the new file, "
        "add it to ALLOWLIST in tests/test_docs_layout_guard.py with that surface as its reason."
    )
    assert all(r.strip() for r in ALLOWLIST.values()), "every ALLOWLIST entry needs a reason"
    missing = sorted(n for n in ALLOWLIST if n not in top)
    assert not missing, f"ALLOWLIST names file(s) that are not tracked under docs/: {missing}"


def test_g2_only_assets_handbook_and_internal_subfolders():
    subs = {
        p.split("/")[1] for p in _tracked() if p.startswith("docs/") and p.count("/") >= 2
    }
    extra = sorted(subs - ALLOWED_SUBFOLDERS)
    assert all(r.strip() for r in SUBFOLDER_REASONS.values()), "every sub-folder needs a reason"
    assert not extra, (
        f"docs/ has sub-folder(s) {extra}; the only sub-folders are {sorted(ALLOWED_SUBFOLDERS)}. "
        "Put building-ENGRAM material under docs/internal/<folder>/."
    )


def test_g3_minus_set_has_the_single_internal_entry_and_no_old_entries():
    entries = _minus_set_entries()
    assert "docs/internal/" in entries, "release-minus-set.txt must carry the entry docs/internal/"
    old = sorted(
        e for e in entries
        if e.lstrip("/").startswith("docs/") and e.lstrip("/")[len("docs/"):] in MOVED_DOC_NAMES
    )
    assert not old, (
        f"release-minus-set.txt still has per-doc entries for docs that moved into docs/internal/: "
        f"{old}. The single docs/internal/ entry covers them; delete these lines."
    )


def test_g4_matcher_reports_no_stale_minus_set_entry():
    warned, _listing = _snapshot_after_minus_set()
    assert warned == [], f"release-minus-set.txt entries that match nothing in git ls-files: {warned}"


def test_g4_snapshot_docs_is_exactly_the_allowlist_plus_public_subfolders():
    _warned, listing = _snapshot_after_minus_set()
    assert listing == sorted(set(ALLOWLIST) | PUBLIC_SUBFOLDERS), (
        f"the public snapshot's docs/ is {listing}; it must be exactly the ALLOWLIST plus "
        f"{sorted(PUBLIC_SUBFOLDERS)} (docs/internal/ must not be in it)"
    )
