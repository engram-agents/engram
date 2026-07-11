# Spec: #1712 — replace hardcoded runtime-dir fallback with walk-parents

**Author:** Sol · **Date:** 2026-07-08. Root-caused during #1698 slice 3's
build (see issue #1712's comment thread) and confirmed as the SAME hazard
class CLAUDE.md already documents under "Plugin build restructures code
paths — never rely on the `src/` layout" -- just manifesting via
worktree-nesting during tests, not deployed-vs-source at runtime. The fix
CLAUDE.md prescribes for that class ("walk-parents search for the actual
target file... never a fixed path guess") is exactly what's needed here too.

## What's broken

Two DIFFERENT `_resolve_runtime_dir` functions (same name, same hazard,
different files, different final targets) both end their fallback ladder
with a **hardcoded absolute path guess** instead of a walk-parents search:

1. `src/engram/hooks/claude/engram-lesson-tripwire-hook.py` (~line 159,
   no-arg signature) -- locates `engram_idf.py`. Final fallback:
   `os.path.expanduser("~/engram-alpha/src/engram")`.
2. `src/engram/hooks/claude/engram-surface-hook.py` (~line 33, takes
   `engram_home` arg) -- locates `engram_client.py`. Final fallback:
   `os.path.expanduser("~/engram-alpha")`.

**Why it's wrong**: `~/engram-alpha` is only correct when the process
happens to be running from that exact primary checkout. When a test (or a
coder-fairy) runs inside a git worktree nested under that same checkout
(`.claude/worktrees/<id>/`, this repo's own dispatch pattern -- see
`engram-fairy-orchestration` skill), the first two ladder steps (plugin
root, `$ENGRAM_HOME` snapshot) can both miss in a test sandbox, falling
through to this hardcoded guess -- which resolves to the **primary
checkout**, not the worktree the test is actually running in. A later bare
`import engram_core`/`import engram_client` in the same pytest process then
silently imports the WRONG (primary-checkout) copy instead of the
worktree's edited one. Confirmed root cause of #1712 (an order-dependent
test failure between `tests/test_in_turn_recall.py` and
`tests/test_principle_triggers_registry.py`) and independently rediscovered
during #1698 slice 3's own build (worked around locally in that PR's test
helper, not fixed at the source -- this spec fixes it at the source).

## Fix

Replace each hardcoded final fallback with a walk-parents search, mirroring
the canonical pattern already used elsewhere in this codebase (see
`engram-baton-prompt-hook.py` lines ~59-67 for the reference shape):

```python
def _resolve_runtime_dir() -> str:  # lesson-tripwire-hook.py's version
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(plugin_root, "engram_idf.py")):
        return plugin_root
    if os.path.exists(os.path.join(ENGRAM_HOME, "engram_idf.py")):
        return ENGRAM_HOME
    # Walk parents from __file__ and take the first ancestor that actually
    # contains engram_idf.py, instead of guessing a fixed absolute path
    # (#1712: the fixed guess resolves to the WRONG checkout when this
    # process is running from a git worktree nested under the guessed path,
    # e.g. .claude/worktrees/<id>/ under ~/engram-alpha -- silently shadowing
    # the actually-running copy with a sibling one).
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "src" / "engram"
        if (candidate / "engram_idf.py").exists():
            return str(candidate)
    return os.path.expanduser("~/engram-alpha/src/engram")  # last-ditch, unchanged
```

Same shape for `engram-surface-hook.py`'s version, walking for
`engram_client.py` under each parent directly (not `parent / "src" /
"engram"` -- `engram_client.py` lives at the plugin/repo root per that
function's own docstring, check the actual layout before assuming the path
suffix matches the other file's).

**Both files need `from pathlib import Path` available** (check existing
imports first -- likely already imported for other purposes in at least one
of them; add if missing).

**Keep the original hardcoded path as the absolute last-ditch fallback**
(not removed) -- the walk-parents search itself could theoretically fail to
find anything (e.g. a truly standalone hook file with no repo above it at
all), and degrading to the old guess is still better than crashing. This
makes the fix purely additive/hardening, not a behavior removal.

## Tests

Recreate the worktree-nested hazard directly (mirrors CLAUDE.md's own
prescribed guard shape: "a regression test that recreates the flattened
layout... runs with `$CLAUDE_PLUGIN_ROOT` unset to exercise the walk-parents
fallback" -- same idea, different nesting scenario):

1. Build a temp directory tree simulating a nested worktree: `<tmp>/main-repo/src/engram/engram_idf.py` (a real target file, can be a stub) and `<tmp>/main-repo/.claude/worktrees/fake-agent/` (empty, no `engram_idf.py` anywhere under it). Copy or point the hook module's `__file__` resolution at a hook script placed under the WORKTREE path (e.g. `<tmp>/main-repo/.claude/worktrees/fake-agent/src/engram/hooks/claude/engram-lesson-tripwire-hook.py`, matching this repo's real relative depth).
2. Call `_resolve_runtime_dir()` with plugin-root and `$ENGRAM_HOME` both missing/empty (so the first two ladder steps fall through) -- assert it returns the WORKTREE's own `src/engram` if that has `engram_idf.py`, not the main-repo's.
3. A second case: worktree has NO `engram_idf.py` anywhere in its own tree, but the main-repo (an ancestor beyond the worktree) does -- assert the walk-parents search correctly finds the ancestor's copy (this is the "legitimately found via walking up" case, distinct from case 2's "worktree has its own copy" case).
4. Absolute last-ditch case: nothing found anywhere in the walked parents -- assert it still returns the original hardcoded `~/engram-alpha/...` string (proving the old fallback is preserved, not removed).

Also add a regression test reproducing #1712's original symptom directly, if feasible without excessive fixture complexity: running `test_in_turn_recall.py` then `test_principle_triggers_registry.py` in the same pytest invocation should no longer produce the 2 failures documented in #1712 (this may need to live as a meta-test that shells out to a sub-pytest invocation, or may not be practical to automate cleanly -- if it's awkward, skip it and note why; the direct `_resolve_runtime_dir` unit tests above are the real regression guard).

## Scope

Two files only: `engram-lesson-tripwire-hook.py`, `engram-surface-hook.py`.
Do not touch the OTHER files that already correctly use the walk-parents
pattern (`engram-baton-prompt-hook.py`, `engram-forum-prompt-hook.py`,
`engram-deference-detector-prompt.py`, `engram-time-bar-hook.py`,
`engram-inter-agent-prompt-hook.py`) -- they're already fixed, not part of
this defect class.

## PR metadata

Title: `fix(hooks): walk-parents fallback for _resolve_runtime_dir [closes #1712]`.
Tier: T2 (test-infra reliability hardening on T1 hook files, but the defect
itself only manifests in test/worktree sandboxes, not production single-checkout installs).
