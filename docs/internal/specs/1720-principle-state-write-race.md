# Spec: #1720 — atomic read-modify-write for principle-trigger-state.json

**Author:** Sol · **Date:** 2026-07-08. Follow-up from #1698 slice 3 (PR
#1717), same TOCTOU class as #1709 (fixed with flock in PR #1714) -- but
**a different fix shape**, not a copy-paste of #1709's pattern. Read the
"Why NOT #1709's non-blocking-suppress pattern" section below before writing
any code; that design decision is the whole point of this spec.

## What's racing

`principle-trigger-state.json` has two independent read-modify-write paths
with no coordination:

1. **Hook-side** (`_check_principle_triggers_inner`,
   `src/engram/hooks/claude/engram-surface-hook.py`, read at ~line 1594,
   write at ~line 1725): reads the whole state file, computes cooldown/decay
   for matched principles, decides what renders, then writes back updated
   `last_fired_prompt`/`fires`/`enactments` for whichever principles fired.
2. **Server-side** (`_reset_principle_enactments`, `src/engram/engram_core.py`
   ~line 4375): reads the whole file, resets one principle's `enactments` to
   0, writes the whole file back. Runs in the MCP server process whenever a
   new exemplar/incident/trigger edge is registered.

Both follow read-whole-file → modify-in-memory → atomic (tmp+`os.replace`)
write-whole-file. That atomicity guards against a **torn read** (a reader
never sees a half-written file) but not a **lost update**: if both race,
whichever writes last wins, silently discarding the other's change to a
*different* principle's entry in the same file.

## Why NOT #1709's non-blocking-suppress pattern

PR #1709/#1714 used a **non-blocking** flock: on contention, the loser
`return ""` immediately -- correct there because `check_in_turn_recall` is
an optional, default-off T2 ambient-recall nicety, and silently skipping a
render on contention is an acceptable, already-tolerated race.

**That reasoning does NOT transfer here.** `check_principle_triggers` (via
`_check_principle_triggers_inner`) is the **primary T1 lesson-tripwire
render path** -- the actual "did the safety lesson fire" decision. If we
wrapped the *whole function* in a non-blocking lock and suppressed on
contention, a real safety tripwire could silently fail to render under
concurrent PreToolUse calls -- trading a low-stakes decay-bookkeeping race
for a much worse one (a missed tripwire).

**The actual fix scope is narrower than #1709's.** The render DECISION
(which principles match, whether they're in cooldown) only needs a
consistent READ of the state at the start -- it does not need exclusivity
with a concurrent writer to be correct enough (a slightly-stale cooldown
read is the existing, already-accepted behavior pre-#1720; this spec is
about the WRITE-BACK race, not the read). Only the **write-back of updated
state** (both here and in `_reset_principle_enactments`) needs to be atomic
with respect to the other writer -- and because a JSON read+merge+write is
fast (no daemon round-trip, unlike #1709), a brief **blocking** wait for the
lock is the right tool: correctness (no lost update) without the render-path
suppression risk.

## Design

A dedicated lockfile: `principle-trigger-state.json` + `.lock` (same
lockfile-not-the-state-file convention as #1709, for the same reason --
`os.replace` on the state file itself would break flock's fd association).

**Both writers take a *blocking* `flock(LOCK_EX)`** (no `LOCK_NB`) around a
**read-modify-write-under-lock** helper, not just the write:

```python
def _with_state_lock(lock_path, fn):
    """Run fn() (a read-modify-write closure) while holding an exclusive,
    BLOCKING flock on lock_path. Unlike #1709's non-blocking pattern, this
    one blocks -- the critical section is a fast local file read+write (no
    daemon round-trip), so a brief wait is cheap and correctness (no lost
    update) matters more than never-block here. Degrades to running fn()
    unlocked if fcntl/lockfile-open fails (non-POSIX, permissions) -- same
    never-crash contract as #1709's degrade path, just without the
    non-blocking contention branch (there's nothing to suppress; fn() still
    runs, just unprotected)."""
    try:
        import fcntl
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:
        return fn()  # degrade: run unlocked rather than crash or skip
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking -- no LOCK_NB
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
```

Both call sites re-read the state file **inside** the locked closure (not
before acquiring the lock) so the read-modify-write is atomic as a unit --
reading before locking would reintroduce the exact race this spec fixes.

**`_reset_principle_enactments`** (`engram_core.py`): wrap its existing
read/modify/write body (lines ~4400-4419) in `_with_state_lock`. Needs its
own copy of the helper or a shared one -- given `engram_core.py` and the
hook script are different processes/import surfaces (no shared import
today), duplicate the small helper rather than engineering a new shared
module for ~15 lines; note this duplication explicitly in a comment on both
copies so a future unification isn't a surprise.

**Hook-side** (`_check_principle_triggers_inner`): the read at ~1594
(cooldown/decay computation) can stay outside the lock -- it's a snapshot
read, same as today. Move the **write-back** (currently ~1725, the
`state[pid] = {...}` mutations + `json.dump`) inside `_with_state_lock`,
re-reading state fresh under the lock immediately before merging in this
call's updates (so a concurrent server-side reset that landed between the
snapshot read and now isn't clobbered).

## Tests

Mirror #1714's test shape but for blocking semantics instead of
suppress-on-contention:

1. Concurrent hook-side writes + a concurrent `_reset_principle_enactments`
   call (threaded, `threading.Barrier`) against the same state file -> after
   all complete, assert **no update was lost**: every principle whose
   enactments the reset call touched shows `enactments == 0` in the final
   state, AND every principle the hook-side calls stamped shows the
   expected `last_fired_prompt`/`fires` -- prove both survive, not just one.
2. A `_with_state_lock` call that can't set up (fcntl unavailable) still
   runs its closure (degrades unlocked, never raises, never skips the
   read-modify-write).
3. Lock is released even if the wrapped closure raises (use a `finally`,
   test via a closure that raises deliberately).

## Non-goals

- No change to the RENDER decision logic (`_check_principle_triggers_inner`'s
  matching/cooldown/priority/cap code) -- this spec is scoped to the
  write-back race only.
- No shared lock-helper module between `engram_core.py` and the hook script
  -- duplicate the ~15-line helper, flagged in both copies' comments.

## PR metadata

Title: `fix(hooks): atomic write for principle-trigger-state.json [closes #1720]`.
Tier: T1 (touches the primary lesson-tripwire mechanism's state file).
