#!/usr/bin/env python3
"""engram-alpha#2544 — detect blocking lessons whose auto-demotion lease
(alpha#1944 step 4) is still hot, so a dream cycle can durably write
prevention_type: advisory via engram_update_lesson.

Background: engram-lesson-tripwire-hook.py computes a blocking lesson's
EFFECTIVE prevention_type from the prevention ledger at match time (a
read-time lease, not a durable write) — deliberately, since the hook has no
engram_core/engram_epistemic import and the no-raw-DB-edits rule keeps
field_traces/edit_history/turn-stamp audit logging inside the module that
owns it (engram_update_lesson). A lesson that has crossed the lease
threshold stays stored as prevention_type: "blocking" indefinitely unless
something calls engram_update_lesson on it. This script is that "something"
— the detection half only.

READ-ONLY. This script performs NO writes to knowledge.db. It opens the
database read-only (`mode=ro`) and reads the prevention ledger; the caller
(intended: the dream-master, which has MCP tool access) is responsible for
calling engram_update_lesson on each reported candidate, which is what
provides the audit trail (field_traces, edit_history, turn stamp) for real
rather than as a raw write.

A candidate is only reported while its lease is STILL HOT: the override
count is recounted fresh against `now`, using the identical logic
engram-lesson-tripwire-hook.py itself uses at match time (imported directly
from that file, not reimplemented, to avoid drift) — not read from the
stale timestamp of the ledger's own auto_demoted_lease entry. A lesson whose
overrides have aged out of the window is deliberately NOT a candidate: the
read-time hook would resume blocking on it today if it fired again, so a
durable demotion now would be stricter than the lease itself currently
demands. This also means a lesson that went quiet after one demotion cycle
is correctly left alone — nothing prevents it from being reconsidered the
next time it actually re-crosses the threshold.

Usage:
    python3 reconcile_lesson_demotions.py [--db PATH] [--ledger PATH] [--now EPOCH_SECONDS]

Output: JSON to stdout —
    {"candidates": [{"lesson_id", "override_count", "threshold",
                      "window_days", "reason"}, ...]}
  on success (exit 0). Empty list is the common case: no lesson has ever
  crossed the lease threshold.

  {"error": "...", "candidates": null}
  on a genuine failure to check (exit 1) -- knowledge.db missing, or the
  ledger file exists but could not be read. This is a load-bearing
  distinction (Borges's review of PR-2621): {"candidates": []} must mean
  "checked cleanly, found nothing", never "could not check" -- otherwise a
  silently-broken reconcile pass and a genuinely quiet night are
  indistinguishable in the nightly ritual, which is exactly the failure
  class that left engram-alpha#2570's hook telemetry dark for months on
  more than one seat without anyone noticing.

Owner read (engram-alpha#3824, U12 of #2652). With `engramd.reconcile_demotions_reads_via_owner`
true in $ENGRAM_HOME/config.json (read only through engramd_owner_flags.flag_on; default OFF) the
tool's ONE statement -- the per-ledgered-lesson `prevention_type` read -- goes to the engramd owner
instead of opening knowledge.db, and the tool opens the store ZERO times. The same invariant holds
there: the owner being down, older than this tool, or unable to classify the read against the live
store RAISES ReconcileError (exit 1), each with its own message, because `{"candidates": []}` means
"nothing to demote" and the sleep cycle acts on it. Flag ON never falls back to a direct open.
`db_path.exists()` is replaced by a `ping` to the owner. An explicit `--db` naming a store other than
this home's keeps the direct path: the owner serves one store, and the caller asked for that file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# The owner-read helper lives beside this file (U14, #3862); resolve the package root through the symlink
# so `from tools import ...` works run as a script, loaded by path, or from the flattened plugin layout.
_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from tools import _owner_read as owner_read  # noqa: E402

# Two independent path hazards have already hit this exact computation, so
# resolution can assume neither a fixed distance from the repo root NOR a
# fixed hooks/claude/ nesting:
#  1. .resolve() follows the tools/reconcile_lesson_demotions.py -> ../src/
#     engram/tools/reconcile_lesson_demotions.py symlink (repo-root tools/
#     mirrors worktree-gc.py's own layout) to this file's REAL location --
#     a fixed distance from an unresolved invocation path broke this script
#     the first time it ran through the symlink (doubled path segment,
#     FileNotFoundError), caught before it shipped.
#  2. The plugin build flattens hooks/claude/* -> hooks/* (CLAUDE.md's
#     "Plugin build restructures code paths" rule) -- source-tree nesting
#     does not survive into the deployed plugin. A fixed SRC_ENGRAM/hooks/
#     claude/... computation resolves to a path that exists in the source
#     tree but NOT in a deployed install, where _load_hook_module raised an
#     uncaught FileNotFoundError on every single invocation (Ariadne's
#     review of PR-2621).
# Same _hooklib.resolve_tools_dir precedent (hooks/claude/_hooklib.py):
# prefer $CLAUDE_PLUGIN_ROOT when set, else walk this file's directories up to
# and including its tree root (#3983; never a fixed-depth parents[N]),
# checking both the flattened and un-flattened relative path under each
# candidate root.
_HOOK_RELATIVE_PATHS = (
    "hooks/claude/engram-lesson-tripwire-hook.py",
    "hooks/engram-lesson-tripwire-hook.py",
)


def _resolve_hook_path() -> Path | None:
    """Return the first existing candidate location of
    engram-lesson-tripwire-hook.py, or None if no candidate exists (the
    caller is responsible for turning that into a loud ReconcileError --
    see _load_hook_module)."""
    roots: list[Path] = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        roots.append(Path(plugin_root))
    roots.extend(owner_read.bounded_parents(Path(__file__).resolve()))  # own tree only (#3983)
    for root in roots:
        for rel in _HOOK_RELATIVE_PATHS:
            candidate = root / rel
            # candidate.exists() stats every path component from root, so a
            # parent directory that is unreadable (permission denied on a
            # sibling seat's home dir, a stale/unmounted path, etc.) raises
            # OSError instead of returning False -- letting that escape here
            # would crash resolution outright rather than simply treating
            # this one candidate as "not found" and continuing the walk
            # (engram-alpha#2646).
            try:
                found = candidate.exists()
            except OSError:
                continue
            if found:
                return candidate
    return None


HOOK_PATH = _resolve_hook_path()

DEFAULT_ENGRAM_HOME = owner_read.engram_home()
DEFAULT_DB_PATH = DEFAULT_ENGRAM_HOME / "knowledge.db"
DEFAULT_LEDGER_PATH = DEFAULT_ENGRAM_HOME / "lesson-prevention-ledger.jsonl"

# --- owner read (#3824) ------------------------------------------------------
OWNER_FLAG_KEY = "reconcile_demotions_reads_via_owner"
# One run reads once per ledgered lesson id (a handful), once per sleep: no latency gate, but a bound so
# a stuck owner is an error and not a hang. A JUDGEMENT, not a measurement.
OWNER_READ_TOTAL_BUDGET_SECONDS = 60.0
OWNER_READ_PER_CALL_SECONDS = 10.0


class ReconcileError(RuntimeError):
    """The reconcile check itself could not run -- as opposed to running
    cleanly and finding zero candidates. Callers must distinguish these:
    an empty candidates list from a genuinely broken check looks identical
    to a healthy quiet night unless this (surfaced as main()'s non-zero
    exit + printed error) makes the difference loud."""


def _load_hook_module():
    """Import engram-lesson-tripwire-hook.py as a module (hyphenated
    filename, so a plain `import` can't reach it) to reuse its
    _demotion_config / _count_recent_overrides verbatim rather than
    reimplementing the same lease logic a second time and risking drift.
    Safe to import: the file's only top-level executable statement is the
    `if __name__ == "__main__": main()` guard.

    Raises ReconcileError (rather than letting a missing HOOK_PATH surface
    as an uncaught FileNotFoundError) if resolution failed -- see
    _resolve_hook_path."""
    if HOOK_PATH is None:
        raise ReconcileError(
            "engram-lesson-tripwire-hook.py not found via CLAUDE_PLUGIN_ROOT "
            "or a walk-parents search from this file's own location -- "
            "cannot reuse its demotion-lease logic"
        )
    spec = importlib.util.spec_from_file_location(
        "_engram_lesson_tripwire_hook", str(HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_ledger_lesson_ids(ledger_path: Path) -> set[str]:
    """Distinct lesson_ids that have ever crossed the lease threshold (any
    auto_demoted_lease entry). A missing ledger is the normal, expected
    "nothing has ever been demoted yet" state and returns an empty set --
    NOT an error. An individual malformed JSON line is tolerated (matches
    the hook's own append-only, best-effort posture on its writer side) and
    simply skipped. But a ledger that EXISTS and cannot be read at all
    (permission denied, I/O error) is a real failure and raises
    ReconcileError rather than silently returning an empty set -- the two
    cases must not collapse into the same {"candidates": []} output."""
    lesson_ids: set[str] = set()
    if not ledger_path.exists():
        return lesson_ids
    try:
        with open(ledger_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue  # one malformed line -- tolerated, not fatal
                if entry.get("mode") == "auto_demoted_lease" and entry.get("lesson_id"):
                    lesson_ids.add(entry["lesson_id"])
    except OSError as exc:
        raise ReconcileError(
            f"ledger exists at {ledger_path} but could not be read: {exc}"
        ) from exc
    return lesson_ids


def _runtime_dir() -> Path | None:
    """The directory holding engramd_client.py: the shared helper's one resolution (U14, #3862)."""
    return owner_read.runtime_dir()


def _import_runtime(name: str):
    """Import a runtime module (engramd_client, engramd_owner_flags) from _runtime_dir()."""
    return owner_read.import_runtime(name)


def _owner_spec() -> "owner_read.OwnerReadSpec":
    """This tool's description for the shared owner-read helper, built per call (the constants it reads
    are module-level and a test may move them)."""
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=ReconcileError,
        home=DEFAULT_ENGRAM_HOME,
        never_empty="Not answering from an empty list: that would read as 'nothing to demote'.",
        total_budget=OWNER_READ_TOTAL_BUDGET_SECONDS,
        per_call=OWNER_READ_PER_CALL_SECONDS,
    )


def _owner_flag_applies(db_path: Path) -> bool:
    """True iff the owner flag is ON and `db_path` is THIS home's store (the shared helper's rule)."""
    return owner_read.flag_applies(_owner_spec(), db_path)


def _owner_ping() -> None:
    """Flag ON replaces `db_path.exists()`: the owner must answer. Raises ReconcileError if it does not."""
    owner_read.ping(_owner_spec())


def _open_reader(db_path: Path, use_owner: bool):
    """The read connection: the owner shim under the flag, else a `mode=ro` sqlite connection."""
    return owner_read.open_reader(_owner_spec(), db_path, use_owner)


def find_candidates(
    db_path: Path = DEFAULT_DB_PATH,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    now: float | None = None,
    hook_module=None,
) -> list[dict]:
    """Return the list of lessons that should be durably demoted now:
    stored prevention_type == "blocking" AND the lease is still hot
    (fresh override recount >= threshold).

    Raises ReconcileError (never returns silently) if knowledge.db is
    missing or the ledger exists but can't be read -- see the module
    docstring and ReconcileError for why this distinction is load-bearing.
    With engramd.reconcile_demotions_reads_via_owner on, the same holds for
    the owner: down, older than this tool, or refusing the read raises."""
    if now is None:
        now = time.time()
    use_owner = _owner_flag_applies(db_path)
    if use_owner:
        _owner_ping()  # the owner's answer replaces the db_path.exists() precondition (#3824)
    else:
        # Kept: it wraps the classes in ReconcileError, and must run before the empty-ledger early return below,
        # which never opens the store.
        try:
            owner_read.check_store_path(db_path)
        except FileNotFoundError:
            raise ReconcileError(f"knowledge.db not found at {db_path}") from None
        except owner_read.StoreUnusable as exc:
            # exit 1 is this tool's documented "genuine failure to check"; never an empty candidate list
            raise ReconcileError(str(exc)) from exc
    hook = hook_module or _load_hook_module()
    # hook._count_recent_overrides reads the hook module's own
    # PREVENTION_LEDGER_PATH global, not a parameter -- point it at the
    # same ledger this function was asked to use (matters for tests, which
    # pass a temp-file ledger distinct from the real ~/.engram ($ENGRAM_HOME) one).
    hook.PREVENTION_LEDGER_PATH = ledger_path

    lesson_ids = _read_ledger_lesson_ids(ledger_path)  # raises ReconcileError, doesn't swallow
    if not lesson_ids:
        return []

    cfg = hook._demotion_config()
    candidates: list[dict] = []
    conn = _open_reader(db_path, use_owner)
    try:
        for lesson_id in sorted(lesson_ids):
            # #3485: json_valid-guarded, so a non-JSON metadata blob on one
            # ledgered lesson reads as NULL (skipped below, like a missing
            # row) instead of raising "malformed JSON" out of the whole run.
            row = conn.execute(
                "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, "
                "'$.prevention_type') FROM nodes "
                "WHERE id = ? AND type = 'lesson' AND is_current = 1 "
                "AND memory_status = 'active'",
                (lesson_id,),
            ).fetchone()
            if row is None or row[0] != "blocking":
                continue  # already advisory/shadow, or node gone/superseded
            override_count = hook._count_recent_overrides(
                lesson_id, cfg["window_days"], now
            )
            if override_count < cfg["overrides"]:
                continue  # lease has gone cold -- do not demote on stale history
            candidates.append({
                "lesson_id": lesson_id,
                "override_count": override_count,
                "threshold": cfg["overrides"],
                "window_days": cfg["window_days"],
                "reason": (
                    f"Auto-demotion lease still hot: {override_count} overrides "
                    f"within {cfg['window_days']} day(s) (threshold "
                    f"{cfg['overrides']}) — the read-time hook already treats "
                    f"this lesson as advisory on every call; writing "
                    f"prevention_type=advisory durably per alpha#2544."
                ),
            })
    finally:
        conn.close()
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument(
        "--now", type=float, default=None,
        help="Override the current-time epoch seconds (testing only)."
    )
    args = parser.parse_args()

    try:
        candidates = find_candidates(db_path=args.db, ledger_path=args.ledger, now=args.now)
    except ReconcileError as exc:
        json.dump({"error": str(exc), "candidates": None}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)

    json.dump({"candidates": candidates}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
