#!/usr/bin/env python3
"""
engram-stale-tree-pull-hook.py — SessionStart auto-pull (Layer 1: PREVENTION).

Part of #1203's stale-tree tripwire, two composing layers:
  Layer 1 (this hook, SessionStart): fast-forward each watched repo clone at
    session start, but ONLY when it is safe to do so unattended.
  Layer 2 (engram-stale-tree-warn-hook.py, PreToolUse): a cheap local
    behind-count warning that catches genuine MID-SESSION drift (this hook
    already fast-forwarded once at session start, so a Layer 2 warning means
    something landed upstream since then).

WHY (see #1203): reading or grepping against a git clone that is behind its
origin has produced confident-wrong conclusions repeatedly — e.g. filing a
PR-review blocker off a tree 16 commits stale. The interim fix was a manual
"pull at session start" ritual; this mechanizes the prevention half of it.

CONFIG — the ONE canonical field, read by both Layer 1 and Layer 2 through
the shared _stale_tree_hooklib.py module (SSoT) — never hardcode
repo paths in either hook:

    "stale_tree_watch": [
      { "path": "~/some-repo", "branch": "main" },
      ...
    ]

in ~/.engram/config.json. `path` may use `~` (expanded here). Absent, empty,
or malformed `stale_tree_watch` -> silent no-op: this hook ships in the
plugin for ALL installs, but only acts once an install opts in by populating
this field (Lei's ruling — do not add auto-discovery of repos).

SAFETY (Lei's explicit requirement — this is the load-bearing part). Per
repo, fast-forward ONLY when the clone is on its CONFIGURED branch AND
CLEAN:
  - Wrong branch, or detached HEAD -> skip + warn. The agent is frequently
    on a feature/PR branch; auto-pull must never touch a branch it wasn't
    told to.
  - Dirty tree (`git status --porcelain` non-empty) -> skip + warn. NEVER
    reset/stash uncommitted work.
  - Diverged from origin -> `git merge --ff-only` fails closed -> warn +
    skip; resolving a real divergence is a human/agent decision, not this
    hook's.

Fail-honest + non-blocking: no failure here (git absent, network down, a bad
configured path, a subprocess timeout) may ever hang or crash session start.
Every git call is wrapped and time-capped; every per-repo step is wrapped in
its own try/except so one repo's failure cannot swallow the rest of the
watch list; main() always exits 0.

OUT OF SCOPE (do not build here): this catches stale *checkouts* (a clone
behind its origin). It does NOT catch git-invisible stale *file-copies*
inside an otherwise-current checkout (e.g. a scatter-era duplicate file) —
a different class of staleness, undetectable by git commands.
"""
import json
import os
import subprocess
import sys

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)

# Caps every individual git subprocess call (fetch included) — keeps a dead
# network or a hung git process from blocking SessionStart.
GIT_TIMEOUT = 10

# Shared config-read (SSoT): stale_tree_watch parsing lives in
# _stale_tree_hooklib.py, which also serves engram-stale-tree-warn-hook.py
# (Layer 2). _stale_tree_hooklib.py lives alongside this file in both build
# topologies (source: hooks/claude/; deployed: hooks/), so add this file's
# own directory to sys.path before importing it — same bootstrap pattern
# every other hook already applies for _hooklib.py/_prompthooklib.py.
# Best-effort: any import failure degrades to a silent no-op (the watch
# list reads as empty) rather than crashing session start.
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
try:
    from _stale_tree_hooklib import load_watch_list as _load_watch_list_impl
    _STALE_TREE_HOOKLIB_OK = True
except Exception:
    _STALE_TREE_HOOKLIB_OK = False

# engram-alpha#2863 WS2: bounded-injection helper + per-harness budget
# table. Same directory, same sys.path bootstrap above (_hooklib.py /
# _harness.py ship alongside every hook in both build topologies) --
# unwrapped, matching engram-surface-hook.py's own import of these two
# (both are core plugin files present on every install, unlike the
# best-effort _stale_tree_hooklib import above).
from _hooklib import emit_context
from _harness import detect_harness, get_harness_budgets


def _load_watch_list() -> list[dict]:
    """Read stale_tree_watch from config.json. Malformed/missing -> [].

    Thin wrapper: delegates to the shared _stale_tree_hooklib implementation,
    passing this hook's own ENGRAM_HOME (a module global read at call time,
    not import time, so test monkeypatching of `mod.ENGRAM_HOME` still
    works exactly as before this was extracted). Never raises — any
    read/parse/shape problem, or the shared module being unimportable,
    degrades to an empty list, which the caller treats as a silent no-op.
    """
    if not _STALE_TREE_HOOKLIB_OK:
        return []
    return _load_watch_list_impl(ENGRAM_HOME)


def _run_git(path: str, *args: str, timeout: int = GIT_TIMEOUT):
    """Run a git subprocess rooted at `path`. Returns CompletedProcess, or
    None if the process could not be run/timed out."""
    try:
        return subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def process_repo(entry: dict) -> str | None:
    """Attempt to fast-forward one watched repo.

    Returns a one-line status string (warning or success), or None when the
    repo path doesn't exist / isn't a git repo (silent skip — not every
    install has every watched clone present).
    """
    path = entry["path"]
    branch = entry["branch"]

    if not os.path.isdir(path) or not os.path.isdir(os.path.join(path, ".git")):
        return None  # not present / not a repo — skip silently, not a warning

    # Branch safety: never touch a branch we weren't told to.
    current = _run_git(path, "branch", "--show-current")
    if current is None or current.returncode != 0:
        return f"⚠ {path}: could not determine current branch — not auto-pulling."
    current_branch = current.stdout.strip()
    if not current_branch or current_branch != branch:
        shown = current_branch or "(detached HEAD)"
        return f"⚠ {path} is on branch '{shown}', not '{branch}' — not auto-pulling."

    # Dirty safety: never reset/stash uncommitted work.
    status = _run_git(path, "status", "--porcelain")
    if status is None or status.returncode != 0:
        return f"⚠ {path}: could not check working-tree status — not auto-pulling."
    if status.stdout.strip():
        return f"⚠ {path} has uncommitted changes — not auto-pulling."

    # Fetch, then fast-forward-only merge. --ff-only fails closed on divergence.
    fetch = _run_git(path, "fetch", "origin", branch)
    if fetch is None:
        return f"⚠ {path}: fetch timed out or errored — not auto-pulling."
    if fetch.returncode != 0:
        reason = (fetch.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        return f"⚠ {path}: fetch failed ({reason[0][:120]}) — not auto-pulling."

    merge = _run_git(path, "merge", "--ff-only", f"origin/{branch}")
    if merge is None:
        return f"⚠ {path}: merge --ff-only timed out or errored — not auto-pulling."
    if merge.returncode != 0:
        return f"⚠ {path} has diverged from origin/{branch} — not auto-pulling; resolve manually."

    rev = _run_git(path, "rev-parse", "--short", "HEAD")
    short_sha = rev.stdout.strip() if (rev is not None and rev.returncode == 0) else "?"
    return f"↑ pulled {path} → {short_sha}"


def _assemble_response(body_blocks: list) -> dict:
    """Build the SessionStart response dict from this session's fired
    per-repo status lines, routed through the shared bounded renderer
    `_hooklib.emit_context()` (engram-alpha#2863 WS2).

    `body_blocks` is the list of already-rendered, non-empty per-repo
    status lines `main()` collected this session (warnings first, then
    successful-pull confirmations — see main()'s own comment for why) —
    exactly the kind of config-driven, arbitrary-length list
    `emit_context()` exists to bound: `stale_tree_watch` carries no length
    cap, so an install watching many repos could otherwise grow this block
    without limit (the epic's concern #2). This hook has no separate
    single-fact "head" line independent of the per-repo results themselves
    (unlike engram-surface-hook.py's daemon/DB-liveness alerts), so `head`
    is only a short fixed caption and every actual status line lives in
    `body`. Split out from `main()` purely for testability, mirroring
    engram-surface-hook.py's own `_assemble_response()` — `main()`'s
    behavior is otherwise unchanged.

    Returns `{}` when nothing fired this session (mirrors the pre-migration
    `if lines:` guard) — `emit_context()` is never called with an empty
    body. Otherwise returns the full `{"hookSpecificOutput": {...}}`
    response dict, ready for `json.dumps()`.
    """
    if not body_blocks:
        return {}

    harness = detect_harness()
    budgets = get_harness_budgets(harness)
    rendered = emit_context(
        "[stale-tree auto-pull]",
        body_blocks,
        budget=budgets["budget"],
        # Deliberate, not head_budget (engram-alpha#2863 WS2 spec): this
        # hook has no separate soft economy target of its own — like
        # engram-surface-hook.py's own choice, the hard ceiling doubles as
        # the soft one so render uses everything available before cutting.
        target=budgets["budget"],
        # No single body item here is expected to be large enough to need
        # the single-overflow allowance (unlike surface-hook's
        # principle_triggers) — a per-repo line is a short, bounded message.
        allow_single_overflow=False,
        # No shared overflow-persistence helper exists yet in _hooklib.py
        # (see engram-surface-hook.py's own note on this same point).
        overflow_writer=None,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": rendered,
        }
    }


def main() -> None:
    try:
        json.load(sys.stdin)  # consume the SessionStart stdin payload (unused)
    except (json.JSONDecodeError, EOFError, ValueError):
        pass

    # engram-alpha#2863 WS2: warnings (need action) are gathered separately
    # from success confirmations so _assemble_response() below can order
    # body_blocks warnings-first (most-important-first, per the WS2 spec).
    # This changes only the ORDER of an unbounded, config-driven list —
    # process_repo()'s own branching for WHEN a given line fires is
    # unchanged.
    warnings: list[str] = []
    successes: list[str] = []
    try:
        watch_list = _load_watch_list()
        for entry in watch_list:
            try:
                result = process_repo(entry)
            except Exception as exc:
                # Fail-honest per-repo: one repo's unexpected error must never
                # swallow the rest of the watch list or crash session start.
                result = f"⚠ {entry.get('path', '?')}: stale-tree auto-pull failed unexpectedly ({exc})."
            if result:
                (warnings if result.startswith("⚠") else successes).append(result)
    except Exception:
        warnings, successes = [], []  # never let a config bug break session start

    response = _assemble_response(warnings + successes)
    if response:
        print(json.dumps(response))

    sys.exit(0)


if __name__ == "__main__":
    main()
