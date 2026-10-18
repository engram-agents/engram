#!/usr/bin/env python3
"""tools/engramd_a6_latency.py -- the A6 latency-delta measurement for U4d
(#3197, part of #2652; docs/2652-engrammanager-design.md SS6.1).

Measures the user-identity hook's person-node read two ways, in ONE run, on ONE
store copy, on one host, at the same N:

  baseline  the hook's old path:   hook.get_person_nodes()      (opens knowledge.db)
  owner     the hook's new path:   hook.read_people_via_owner() (engramd_client.read
                                   -> a REAL engramd_owner process -> `read`)

The owner is a separate OS process launched from src/engram/engramd_owner.py
(its argv carries `engramd_owner`, the marker #3285's pid-liveness guard keys
on) against a COPY of a test store in a scratch ENGRAM_HOME -- never the seat's
live store (a --store that resolves to it is refused), never an echo server.

Arms (each reports its OWN same-arm baseline; none is a gate, an arm where the
owner loses is reported, not omitted):
  sequential   one client, warm, calls interleaved baseline/owner per iteration
  contended    two client threads at once, each path measured separately
  cold-cache   OS page cache for the store evicted (posix_fadvise DONTNEED)
               before every call. NOTE: the owner's own in-process SQLite page
               cache cannot be evicted by a client, so for the owner path this
               arm measures the held connection's benefit as much as "cold".

Caveats printed with the table: function-level timings exclude interpreter
start and imports (one per hook invocation either way); the contended arm's two
client threads share one GIL, which affects both paths equally; the ratio does
not survive across SQLite builds (the doc shows this), so the build is named.

    python tools/engramd_a6_latency.py [--n 300] [--nodes 5000] [--people 5]
                                       [--warmup 30] [--store PATH] [--layout-timeout 1800]
Exit 0 ok; 1 the two paths answered differently; 2 bad input (N below the 300 floor,
--store is the live store, --exclusion without --hook tripwire, the surface-only switches on another
hook, or no real-store source for --hook surface); 3 (--hook tripwire, session-start or surface only) a contract violation (see u8_contract_violations / ss_contract_violations);
4 (any --hook) a BLIND ARM: the run raised ArmBlind or LayoutViolation, or the plugin build step
exceeded its fixed 600 s limit, so no number was produced (one stderr line names the cause); 5 a layout child outran --layout-timeout
(#3755; EXIT_LAYOUT_TIMEOUT; one stderr line names the layout, the limit and the flag; the child runs in
its own process group and the whole group is killed, so no engramd owner is left behind).
`--layout-timeout SECONDS` (int > 0, default 1800) is the per-layout child limit; the session-start arm
on a real-size store can need more (each fire is a fresh process), so raise it there.
Exit 1 does NOT occur for tripwire or session-start: the two paths answering differently is a
differing stdout, which is itself a violation, and exit 3 outranks exit 1. A blind arm is never
exit 1; it is exit 4.

`--hook tripwire` (U6b stage 2c-2, #3665; the U8 gate, #3710) measures ONE FULL FIRE of the
lesson-tripwire hook's main() on a fixed payload -- all four of its store-read sites run --
baseline = the engramd.lesson_tripwire_reads_via_owner flag OFF (the hook opens the store),
owner = flag ON with a REAL owner process. `--exclusion on` turns the in-context exclusion
on (config flag + a live probe directory under the hook process's HOME). The result also
carries the direct store opens counted per fire (the gate quantity is `opens_flag_on == 0`).

`--hook surface` (A6 arm for the surface hook's owner reads, #3821 contract item 4) measures ONE FULL FIRE of the
surface hook's main() per sample, baseline = engramd.surface_reads_via_owner OFF, owner = flag ON with a REAL owner, the
recall daemon stubbed identically at `query_daemon` on both paths. It measures TWO cells per layout -- a synthetic store and
a COPY of a real store (`--real-store PATH`, default $ENGRAM_HOME/knowledge.db; a read-only backup into a scratch
directory, never measured in place) -- and the verdict line names the REAL-STORE cell on the INSTALLED layout as THE GATE
CELL (sequential p50 owner/baseline <= 1.0); `--no-real-store` or `--layout source` leaves no gate cell, so the run prints
NOT A PASS and exits 3. A hook without the flag is a BLIND ARM (exit 4, the flag named). Contended and cold-cache are printed
as owed measurements; any arm the owner loses is printed as a FINDING (not an exit code). Exit 3 also covers a stdout
mismatch, a missed recall block, or a flag-ON open of the store other than the named person-moment cursor write.

`--hook session-start` (A6 arm for U9, #3735; this arm #3747) measures ONE FULL FIRE of the
session-start hook as a FRESH PROCESS per fire (the harness runs it that way; the dominant cost is
the interpreter, the imports and the calibration block's EngramClient), baseline = the
engramd.session_start_reads_via_owner flag OFF, owner = flag ON with a REAL owner already up (WARM).
The contract is the design seat's ruling on #3747, an ALLOW-LIST: on the flag-ON path EVERY recorded
open of knowledge.db is a violation that names the opener's frames (not "no open from the six readers":
a deny-list passes silently when a seventh opener appears). Since #3741 the calibration block also reads
through the owner on the flag-ON path (one typed `confidence_by_type` request), so the allow-list is empty
and the gate cell is `opens_flag_on == 0`; on flag OFF the calibration block still opens the store through
`engram_client.py` and is counted, never gated. ONE constant, CALIBRATION_STILL_OPENS (now False), drives
both modes: True = allow-list = opens with a frame in `engram_client.py` and the must-engage raise on
(the pre-#3741 world, kept for the cells that test it); False = allow-list empty, raise off, `opens == 0`.
If no open is attributed to the calibration path on either path the arm RAISES (ArmBlind, "exclusion
never engaged") instead of passing; so it does when a reader is not reached on a path, or a WARM fire
prints a `waited N ms` notice. The gate cell is the INSTALLED layout's sequential p50 owner/baseline
<= 1.0 on the production build; the contended and cold-cache arms and the COLD row (owner launched in
the same instant as the hook, --n-cold fires) are reported. Exit 3 on a contract violation.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import unquote_to_bytes
from typing import Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-user-identity-hook.py"
STOP_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-stop-hook.py"
TRIPWIRE_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"
SESSION_START_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-session-start-hook.py"
SURFACE_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-surface-hook.py"
HOOKS = ("user-identity", "stop", "tripwire", "session-start", "surface")  # --hook; the default stays user-identity
N_FLOOR = 300


def _ensure_src_importable() -> None:
    """Make src/engram importable WITHOUT winning name resolution: appended, never
    inserted at index 0, so a repo-root package of the same name as something in
    src/engram (`tools`) keeps resolving to the repo root (the #2565 sys.path
    collision guard in tests/conftest.py trips on an index-0 insert)."""
    if str(SRC_ENGRAM) not in sys.path:
        sys.path.append(str(SRC_ENGRAM))


def owner_argv() -> List[str]:
    """The owner's launch argv. It names the module file, so the real process's
    command line contains `engramd_owner` (#3285)."""
    return [sys.executable, str(SRC_ENGRAM / "engramd_owner.py")]


# --- the measurement layout (#3678) -------------------------------------------------
#
# A hook's owner path re-resolves its runtime directory on EVERY call
# (`_import_engramd_client` -> `_hooklib.resolve_runtime_dir`). In the source
# checkout that falls through to a walk over parent directories; in an installed
# plugin (flat hooks/, runtime modules at the plugin root) it resolves on its
# first check. The direct path resolves once at import. So measuring the source
# checkout charges the OWNER for a lookup production never does. The INSTALLED
# layout -- a plugin built from the tree under test into a scratch directory,
# never installed -- is the gate cell; the source layout is a development-time
# number and is labelled so.

BUILD_SCRIPT = REPO_ROOT / "tools" / "build-plugin.sh"
LAYOUTS = ("source", "installed")
EXIT_LAYOUT_TIMEOUT = 5          # a layout child outran --layout-timeout (#3755); 0-4 are taken (see the docstring)
DEFAULT_LAYOUT_TIMEOUT = 1800    # seconds; the per-layout child limit when --layout-timeout is not given
# build-relative path -> source-relative path: the runtime files a measurement
# loads. Byte-identical between the build and the tree is what makes the
# installed cell a measurement of THIS tree.
LAYOUT_NAME = "source"  # set by _apply_layout in a layout child; the session-start arm sets CLAUDE_PLUGIN_ROOT only for "installed"
RUNTIME_FILES = {
    "engramd_owner.py": "src/engram/engramd_owner.py",
    "engramd_client.py": "src/engram/engramd_client.py",
    "engramd_read_registry.py": "src/engram/engramd_read_registry.py",
    "engramd_read_templates.json": "src/engram/engramd_read_templates.json",
    "engram_walguard.py": "src/engram/engram_walguard.py",
    "hooks/engram-stop-hook.py": "src/engram/hooks/claude/engram-stop-hook.py",
    "hooks/engram-user-identity-hook.py": "src/engram/hooks/claude/engram-user-identity-hook.py",
    "hooks/_hooklib.py": "src/engram/hooks/claude/_hooklib.py",
    # U6b stage 2c-2 (#3665): the tripwire arm's hook, its flag helper and the modules its
    # fire loads (derived by running the fire in the built layout; the closure cell in
    # tests/test_engramd_a6_latency.py keeps this list honest).
    "hooks/engram-lesson-tripwire-hook.py": "src/engram/hooks/claude/engram-lesson-tripwire-hook.py",
    "hooks/_harness.py": "src/engram/hooks/claude/_harness.py",
    "engramd_owner_flags.py": "src/engram/engramd_owner_flags.py",
    "engram_idf.py": "src/engram/engram_idf.py",
    "engram_core.py": "src/engram/engram_core.py",
    # engram_core's own load-time imports (the fire loads engram_core for generalization_chain)
    "engram_backup.py": "src/engram/engram_backup.py",
    "engram_confidence.py": "src/engram/engram_confidence.py",
    "engram_config_io.py": "src/engram/engram_config_io.py",
    "engram_pidfile.py": "src/engram/engram_pidfile.py",
    "engram_regular_io.py": "src/engram/engram_regular_io.py",
    "engram_restamp.py": "src/engram/engram_restamp.py",
    # #3856: the shared person-moment cursor advance. The surface hook imports it at module level (and engramd_owner
    # imports it), so a fire in the installed layout loads it; without this entry the closure cell would flag it.
    "engram_moment_cursor.py": "src/engram/engram_moment_cursor.py",
    "engram_search_rules.py": "src/engram/engram_search_rules.py",
    "tools/__init__.py": "src/engram/tools/__init__.py",
    "tools/recall_summary_validator.py": "src/engram/tools/recall_summary_validator.py",
    # U9 / #3747: the session-start arm's fire (derived by running the sanity driver in a built layout, both
    # flag states, and scanning what it loaded from the build; the closure cell keeps this list honest). The
    # calibration block's EngramClient loads the server runtime, hence the many engram_*.py modules and server.py.
    "hooks/engram-session-start-hook.py": "src/engram/hooks/claude/engram-session-start-hook.py",
    # #3821: the surface arm's hook. Its fire loads nothing the tripwire / session-start entries above do not already
    # cover (derived by firing the built hook; the closure cell in tests/test_engramd_a6_latency.py keeps it honest).
    "hooks/engram-surface-hook.py": "src/engram/hooks/claude/engram-surface-hook.py",
    "hooks/context_tracker.py": "src/engram/hooks/claude/context_tracker.py",
    "engram_anchoring.py": "src/engram/engram_anchoring.py",
    "engram_client.py": "src/engram/engram_client.py",
    "engram_conformance.py": "src/engram/engram_conformance.py",
    "engram_cornerstone.py": "src/engram/engram_cornerstone.py",
    "engram_dag_conformance.py": "src/engram/engram_dag_conformance.py",
    "engram_derive.py": "src/engram/engram_derive.py",
    "engram_epistemic.py": "src/engram/engram_epistemic.py",
    "engram_focus.py": "src/engram/engram_focus.py",
    "engram_lifecycle.py": "src/engram/engram_lifecycle.py",
    "engram_log_emitter.py": "src/engram/engram_log_emitter.py",
    "engram_observation.py": "src/engram/engram_observation.py",
    "engram_query.py": "src/engram/engram_query.py",
    "engram_recall_summaries.py": "src/engram/engram_recall_summaries.py",
    "engram_revision.py": "src/engram/engram_revision.py",
    "engram_stats.py": "src/engram/engram_stats.py",
    "engram_tasks.py": "src/engram/engram_tasks.py",
    "engram_telemetry_store.py": "src/engram/engram_telemetry_store.py",
    "engram_trust.py": "src/engram/engram_trust.py",
    "server.py": "src/engram/server.py",
}


class LayoutViolation(RuntimeError):
    """An installed-layout run did not measure the installed layout."""


class Layout:
    """A code layout to measure. A plain class, not a dataclass: this tool is
    loaded by file path (spec_from_file_location) by its own tests and by the
    stage profiler, and a dataclass needs its module registered in sys.modules."""

    def __init__(self, name: str, root: Path, stop_hook: Path, user_hook: Path,
                 tripwire_hook: Optional[Path] = None, session_start_hook: Optional[Path] = None,
                 surface_hook: Optional[Path] = None) -> None:
        self.name = name
        self.root = Path(root)            # the directory holding engramd_owner.py / engramd_client.py
        self.stop_hook = Path(stop_hook)
        self.user_hook = Path(user_hook)
        # the lesson-tripwire hook (#3665); beside the stop hook when not named
        self.tripwire_hook = (Path(tripwire_hook) if tripwire_hook is not None
                              else self.stop_hook.parent / "engram-lesson-tripwire-hook.py")
        # the session-start hook (#3747); beside the stop hook when not named
        self.session_start_hook = (Path(session_start_hook) if session_start_hook is not None
                                   else self.stop_hook.parent / "engram-session-start-hook.py")
        # the surface hook (#3821); beside the stop hook when not named
        self.surface_hook = (Path(surface_hook) if surface_hook is not None
                             else self.stop_hook.parent / "engram-surface-hook.py")

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "root": str(self.root),
                           "stop_hook": str(self.stop_hook), "user_hook": str(self.user_hook),
                           "tripwire_hook": str(self.tripwire_hook),
                           "session_start_hook": str(self.session_start_hook),
                           "surface_hook": str(self.surface_hook)})

    @staticmethod
    def from_json(text: str) -> "Layout":
        d = json.loads(text)
        return Layout(name=d["name"], root=Path(d["root"]),
                      stop_hook=Path(d["stop_hook"]), user_hook=Path(d["user_hook"]),
                      tripwire_hook=Path(d["tripwire_hook"]) if d.get("tripwire_hook") else None,
                      session_start_hook=Path(d["session_start_hook"]) if d.get("session_start_hook") else None,
                      surface_hook=Path(d["surface_hook"]) if d.get("surface_hook") else None)


def source_layout() -> Layout:
    return Layout(name="source", root=SRC_ENGRAM, stop_hook=STOP_HOOK_PATH, user_hook=HOOK_PATH,
                  tripwire_hook=TRIPWIRE_HOOK_PATH, session_start_hook=SESSION_START_HOOK_PATH,
                  surface_hook=SURFACE_HOOK_PATH)


def build_installed_layout(out_dir: Path) -> Layout:
    """Build the plugin from the tree under test into `out_dir` (a scratch
    directory; this build is never installed). The two override flags are the
    build script's own measurement escape hatches: a feature branch and an
    untagged commit are exactly what a PR measurement builds."""
    out_dir = Path(out_dir)
    proc = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "--tier", "dev", "--multi-agent",
         "--allow-branch", "--allow-untagged", "--output", str(out_dir)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"plugin build failed (rc {proc.returncode}): {proc.stderr[-800:]}")
    layout = Layout(name="installed", root=out_dir,
                    stop_hook=out_dir / "hooks" / "engram-stop-hook.py",
                    user_hook=out_dir / "hooks" / "engram-user-identity-hook.py",
                    tripwire_hook=out_dir / "hooks" / "engram-lesson-tripwire-hook.py",
                    session_start_hook=out_dir / "hooks" / "engram-session-start-hook.py",
                    surface_hook=out_dir / "hooks" / "engram-surface-hook.py")
    drift = layout_matches_tree(layout)
    if drift:
        raise LayoutViolation(f"the build differs from the tree under test: {drift}")
    return layout


def layout_matches_tree(layout: Layout) -> List[str]:
    """Build-relative names of runtime files that are missing from the build or
    differ byte-for-byte from the tree under test. [] = the build IS this tree."""
    bad = []
    for built_rel, src_rel in RUNTIME_FILES.items():
        built, src = layout.root / built_rel, REPO_ROOT / src_rel
        if not built.is_file() or built.read_bytes() != src.read_bytes():
            bad.append(built_rel)
    return bad


def check_installed_run(result: Dict, layout: Layout) -> None:
    """The two runtime asserts (Borges, #3678). Raises LayoutViolation."""
    argv = result.get("owner_argv") or []
    owner_file = Path(argv[1]).resolve() if len(argv) > 1 else None
    if owner_file is None or layout.root.resolve() not in owner_file.parents:
        raise LayoutViolation(f"the owner did not run from the build {layout.root}: argv {argv}")
    if result.get("parent_walk_calls") != 0:
        raise LayoutViolation(
            f"the client import walked parent directories {result.get('parent_walk_calls')} time(s); "
            "an installed plugin resolves at the plugin root and never walks")


def _apply_layout(layout: Layout) -> None:
    """Point this module at `layout`. Only ever called in a FRESH process (the
    `--_layout-child` mode), before any runtime module is imported, so nothing
    from the other layout can already sit in sys.modules."""
    global SRC_ENGRAM, STOP_HOOK_PATH, HOOK_PATH, TRIPWIRE_HOOK_PATH, SESSION_START_HOOK_PATH, SURFACE_HOOK_PATH, LAYOUT_NAME
    SRC_ENGRAM, STOP_HOOK_PATH, HOOK_PATH = layout.root, layout.stop_hook, layout.user_hook
    TRIPWIRE_HOOK_PATH = layout.tripwire_hook
    SESSION_START_HOOK_PATH = layout.session_start_hook
    SURFACE_HOOK_PATH = layout.surface_hook
    LAYOUT_NAME = layout.name


def _count_parent_walks(layout: Layout) -> Dict[str, int]:
    """Import the layout's own _hooklib and count the resolver's parent walk:
    its only `Path(__file__).resolve()`. Returns a live counter dict."""
    hooks_dir = str(layout.stop_hook.parent)
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    import _hooklib
    if Path(_hooklib.__file__).resolve().parent != layout.stop_hook.parent.resolve():
        raise LayoutViolation(f"_hooklib loaded from {_hooklib.__file__}, not {hooks_dir}")
    counter = {"walks": 0}
    real_path = _hooklib.Path
    me = str(Path(_hooklib.__file__))

    class _Probe:
        def __init__(self, p):
            self._p = p

        def resolve(self, *a, **k):
            counter["walks"] += 1
            return self._p.resolve(*a, **k)

        def __getattr__(self, attr):
            return getattr(self._p, attr)

    class _CountingPath:
        """Stands in for `_hooklib.Path`: calling it builds a Path (counting the one resolve of
        _hooklib's own file); every other attribute (`Path.home()`, ...) is the real class's. A bare
        function here broke `Path.home()` inside _hooklib -- route_probe_dir() raised, swallowed
        fail-open -- and silently switched the in-context exclusion off in every layout child."""

        def __call__(self, *args, **kwargs):
            p = real_path(*args, **kwargs)
            return _Probe(p) if str(p) == me else p

        def __getattr__(self, attr):
            return getattr(real_path, attr)

    _hooklib.Path = _CountingPath()
    return counter


_KILL_GRACE_S = 2.0   # SIGTERM -> SIGKILL grace for a timed-out layout child's process group


_START_NEW_SESSION = True   # a seam: tests flip it to drive the "child is not a group leader" degraded case


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate `proc` and every descendant in its process group: SIGTERM, a short grace, then SIGKILL.
    `proc` must have been started with start_new_session=True (its pid is then the group id). The SIGKILL
    goes to the group even when the leader already exited, because an orphaned grandchild (the engramd owner)
    stays in the group. PermissionError (Darwin EPERM on a zombie-only group) from killpg means there is
    nothing left to signal; any other OSError propagates. Without os.killpg (non-POSIX) only the direct
    child is killed, as before.

    ProcessLookupError from killpg is AMBIGUOUS: the group is gone, or the pid is not a group id at all
    (the child was not started as a session leader). The leader tells them apart: dead (`proc.poll()` not
    None) is the normal "group gone" path and silent; still ALIVE means the group kill did not work, so one
    stderr line says so and the direct child is killed with proc.kill(). Honest bound: in that degraded
    case the grandchildren are NOT killed; the fallback stops an unbounded wait, it does not recover the
    group kill.

    Every wait here is bounded: after the loop the child gets one more grace, then proc.kill() and a final
    wait of one more grace. If even that expires (a child that survives SIGKILL, e.g. in uninterruptible
    sleep) one stderr line says so and the function returns, so the caller still raises TimeoutExpired; a
    swallowed error can never turn a timeout into an unbounded wait."""
    if not hasattr(os, "killpg"):
        proc.kill()
        proc.wait()
        return
    for sig, grace in ((15, _KILL_GRACE_S), (9, None)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            if proc.poll() is None:
                print(f"engramd_a6_latency: killpg({proc.pid}) found no such group while the child is "
                      f"still running; falling back to killing the direct child only (descendants not "
                      f"killed)", file=sys.stderr)
                proc.kill()
            break
        except PermissionError:
            # On Darwin killpg on a group whose only member is an unreaped zombie raises EPERM; nothing
            # left to signal.
            break
        if grace is not None:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
    try:
        proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            print(f"engramd_a6_latency: child {proc.pid} could not be reaped after SIGKILL within "
                  f"{_KILL_GRACE_S}s; continuing", file=sys.stderr)


def _run_in_own_group(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, capture_output, text, timeout) except the child leads its own session, and on
    expiry (or any interruption) its whole group is killed before TimeoutExpired is raised: subprocess.run
    kills only the direct child, which orphaned the engramd owner the child had launched (#3755). The
    expiry is honoured in bounded time (timeout + two kill graces) even when the group kill cannot work
    (#3757, see _kill_group). On that path both pipes are closed after the kill (a timeout otherwise leaked
    two file descriptors and, under -W error::ResourceWarning, two 'unclosed file' warnings); on success
    communicate() has already closed them."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=_START_NEW_SESSION)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException as exc:
        try:
            _kill_group(proc)
        finally:
            # communicate() closes the pipes only on its own success path; here they would leak until GC,
            # also when _kill_group itself raises. Each close is guarded on its own so one failing close
            # cannot skip the other.
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
        if isinstance(exc, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(cmd, timeout) from None
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def measure_in_layout(layout: Layout, layout_timeout: int = DEFAULT_LAYOUT_TIMEOUT, **kwargs) -> Dict:
    """Run measure() for `layout` in a FRESH process (no runtime module from
    another layout can be cached), return its result. An installed-layout
    result is checked by check_installed_run() before it is returned.
    `layout_timeout` (seconds) bounds the child; expiry kills the child's WHOLE process group (the engramd
    owner it launched included, #3755) and raises subprocess.TimeoutExpired."""
    proc = _run_in_own_group(
        [sys.executable, str(Path(__file__).resolve()), "--_layout-child", layout.to_json(),
         "--_kwargs", json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in kwargs.items()})],
        timeout=layout_timeout)
    if proc.returncode != 0:
        msg = f"{layout.name}-layout run failed (rc {proc.returncode}): {proc.stderr[-1500:]}"
        # The child's last traceback line is `<ExcName>: <text>`; a blind arm / layout violation comes back
        # TYPED (both are RuntimeErrors, so existing handlers still match) so main() can give it exit 4.
        last = next((ln for ln in reversed(proc.stderr.strip().splitlines()) if ln.strip()), "")
        m = re.match(r"(?:[\w.]+\.)?(ArmBlind|LayoutViolation)\b:?\s*(.*)", last)
        if m:
            raise (ArmBlind if m.group(1) == "ArmBlind" else LayoutViolation)(
                f"{layout.name}-layout run failed (rc {proc.returncode}): {m.group(2)}")
        raise RuntimeError(msg)
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    result["layout"] = layout.name
    result["layout_root"] = str(layout.root)
    if layout.name == "installed":
        check_installed_run(result, layout)
    return result


def _layout_child(layout_json: str, kwargs_json: str) -> int:
    layout = Layout.from_json(layout_json)
    _apply_layout(layout)
    counter = _count_parent_walks(layout)
    kwargs = json.loads(kwargs_json)
    if kwargs.get("store"):
        kwargs["store"] = Path(kwargs["store"])
    result = measure(**kwargs)
    result["parent_walk_calls"] = counter["walks"]
    print(json.dumps(result))
    return 0


LAYOUT_LABELS = {
    "installed": "INSTALLED layout -- the gate cell (plugin built from the tree under test; never installed)",
    "source": "SOURCE checkout -- a development-time number, NOT production "
              "(the owner path pays a per-call parent-directory walk an installed plugin never does)",
}


SURFACE_LAYOUT_LABELS = {
    "installed": "INSTALLED layout (plugin built from the tree under test; never installed)",
    "source": LAYOUT_LABELS["source"],
}


def _cell_heading(r: Dict) -> str:
    """The heading of a cell-tagged (`--hook surface`) result: the cell, and whether it is THE gate cell."""
    cell, layout = r["store_cell"], r["layout"]
    if cell == GATE_CELL:
        title = ("real-store copy -- THE GATE CELL (verdict below)" if layout == GATE_LAYOUT
                 else f"real-store copy -- not the gate cell ({layout} layout)")
    else:
        title = "synthetic store -- reported, NOT the gate cell"
    return f"{title}: {SURFACE_LAYOUT_LABELS[layout]}"


def format_layouts(results: List[Dict]) -> str:
    order = {"installed": 0, "source": 1}
    cell_order = {c: i for i, c in enumerate(CELLS)}
    parts = []
    for r in sorted(results, key=lambda r: (cell_order.get(r.get("store_cell"), 0), order.get(r.get("layout"), 9))):
        heading = _cell_heading(r) if r.get("store_cell") else LAYOUT_LABELS[r["layout"]]
        parts.append(f"### {heading}\n"
                     f"Runtime root: {r['layout_root']} | parent-walk calls during the run: {r['parent_walk_calls']}\n\n"
                     + format_table(r))
    return "\n\n".join(parts)


# --- the test store --------------------------------------------------------------

def make_test_store(nodes: int = 5000, people: int = 5, source: Optional[Path] = None) -> Path:
    """A scratch ENGRAM_HOME holding a knowledge.db: a consistent COPY (SQLite
    backup API, source opened read-only) of `source`, or a synthetic store with
    the real schema, `nodes` mixed-type rows and `people` current person nodes.
    Returns the home directory."""
    home = Path(tempfile.mkdtemp(prefix="a6-"))
    db = home / "knowledge.db"
    if source is not None:
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(str(db))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return home
    _ensure_src_importable()
    import engram_core

    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        conn = sqlite3.connect(str(db))
        # Real ENGRAM stores are WAL, so the owner's per-request displacement check
        # inspects real -wal/-shm fds there; a delete-mode store would skip that work
        # and price a path production never runs (#3678, Kepler's ruling).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        kinds = ("observation", "derivation", "lesson", "conjecture")
        conn.executemany(
            "INSERT INTO nodes (id, type, claim, created_at, logical_chain, metadata, is_current) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00', ?, '{}', 1)",
            ((f"n_{i:06d}", kinds[i % len(kinds)], ("claim text " * 12) + str(i), "chain " * 8)
             for i in range(nodes)),
        )
        conn.executemany(
            "INSERT INTO nodes (id, type, claim, created_at, logical_chain, metadata, is_current) "
            "VALUES (?, 'person', ?, '2026-01-01T00:00:00', ?, ?, 1)",
            ((f"pn_{i:04d}", f"Person {i}", f"profile line for person {i}",
              json.dumps({"name": f"Person {i}", "aliases": [f"p{i}"], "role": "colleague"}))
             for i in range(people)),
        )
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return home


# --- the real owner --------------------------------------------------------------

class OwnerHandle:
    def __init__(self, proc: subprocess.Popen, home: Path):
        self.proc = proc
        self.home = home
        self.socket_path = home / "run" / "engramd.sock"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()  # SIGTERM -> the owner's own clean-stop handler
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def launch_owner(home: Path) -> OwnerHandle:
    """Start a REAL owner process on `home` and wait until it answers `ping`."""
    _ensure_src_importable()
    import engramd_client as ec

    env = dict(os.environ, ENGRAM_HOME=str(home))
    env.pop("ENGRAM_OWNER_ALLOW_DYNAMIC_TEMPLATES", None)
    proc = subprocess.Popen(owner_argv(), env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=str(home))
    handle = OwnerHandle(proc, home)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"owner exited early (code {proc.returncode})")
        if isinstance(ec.read("ping", {}, timeout=1.0, socket_path=handle.socket_path), ec.Ok):
            return handle
        time.sleep(0.1)
    handle.stop()
    raise RuntimeError("owner never answered ping")


# --- the hook under test ---------------------------------------------------------

def load_hook(home: Path, path: Optional[Path] = None, name: str = "engram_user_identity_hook_a6"):
    os.environ["ENGRAM_HOME"] = str(home)
    os.environ.pop("ENGRAM_RUNTIME_DIR", None)
    spec = importlib.util.spec_from_file_location(name, str(path or HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bind_user_identity(home: Path):
    """-> (baseline_fn, owner_fn, answers_match, answer_size, extra_result_keys)."""
    hook = load_hook(home)

    def baseline():
        return hook.get_person_nodes()

    def owner():
        rows, failure = hook.read_people_via_owner()
        if failure is not None:
            raise RuntimeError(f"owner read failed: {failure}")
        return rows

    base_rows, owner_rows = baseline(), owner()
    people_match = base_rows == owner_rows
    return baseline, owner, people_match, len(base_rows), {
        "people_match": people_match, "people_count": len(base_rows)}


def _bind_stop(home: Path):
    """The stop hook (U6a, #3664). Two module instances of the SAME hook file, one
    with the flag reader pinned OFF (its old direct-connect path) and one pinned ON
    (the owner path); both are timed through the hook's own `_maturity_muted()`, so
    the threshold read is common to both and the difference is the read itself.
    The answers compared are the RAW count (old path via a plain SELECT on the store
    copy with the hook's registered statement, owner path via
    `_read_count_via_owner()`) and the decision both modules return."""
    base = load_hook(home, STOP_HOOK_PATH, "engram_stop_hook_a6_base")
    owner_mod = load_hook(home, STOP_HOOK_PATH, "engram_stop_hook_a6_owner")
    base._owner_reads_enabled = lambda: False
    owner_mod._owner_reads_enabled = lambda: True

    def baseline():
        return base._maturity_muted()

    def owner():
        try:
            return owner_mod._maturity_muted()
        except owner_mod._OwnerReadFailure as exc:
            raise RuntimeError(f"owner read failed: {exc.args[0]}")

    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        direct_count = conn.execute(base._CURRENT_COUNT_SQL).fetchone()[0]
    finally:
        conn.close()
    owner_count = owner_mod._read_count_via_owner()
    answers_match = direct_count == owner_count and baseline() == owner()
    return baseline, owner, answers_match, direct_count, {"answer_count": direct_count}


# --- the lesson-tripwire hook: one FULL FIRE (U6b stage 2c-2, #3665) ------------------
#
# The arm times the hook's own main() on one fixed PreToolUse payload, stdin/stdout/stderr
# captured in process. Both paths run the SAME hook file as two module instances: baseline
# reads its flag from config.json (engramd.lesson_tripwire_reads_via_owner = false), owner
# from config-owner-on.json beside it (= true) -- the real flag reader runs on both, only
# the file differs (the hook's CONFIG_PATH; both configs hold the same exclusion section).
# The payload makes all four store-read sites execute: load_tripwires (a tripwire matches),
# generalization_chain (that tripwire has a generalizes ancestor), load_prevention_lessons
# (a shadow lesson matches, a blocking lesson is loaded and does not), check_in_turn_recall
# (high-IDF terms + a daemon result with candidates).

TRIPWIRE_SESSION_ID = "a6-tripwire-session"
TRIPWIRE_COMMAND = "echo tripwirepat zebrafoo quokkazap"
TRIPWIRE_FLAG_KEY = "lesson_tripwire_reads_via_owner"
TRIPWIRE_NUDGE = "Nudge for tripwire one"
PRINCIPLES_HEADER = "[triggered principles]"
RECALL_HEADER = "[ENGRAM in-turn recall"
# The recall daemon's answer at the hook's `_query_recall_daemon` seam: identical for both
# paths (see _bind_tripwire for why that is honest).
STUB_DAEMON_RESULT = {"match_count": 2, "top_claims": [
    {"id": "ob_a6stub01", "recall_summary": "a6 stub recall line one"},
    {"id": "ob_a6stub02", "recall_summary": "a6 stub recall line two"}]}


def _seed_tripwire_fixture(home: Path) -> None:
    """Add the tripwire fixture to the store copy in `home` (synthetic or a real-store copy;
    ids are `a6_*`): a principle head with a `generalizes` edge to a high-priority tripwire
    lesson, a shadow lesson matching the same pattern, a blocking lesson that does NOT match
    the fixed command (a match would deny and skip the other sites), a rare-token
    observation (the recall's high-IDF terms), and the fts5 vocab table the IDF lookup reads."""
    _ensure_src_importable()
    import engram_idf

    def meta(**kw):
        return json.dumps(kw)

    rows = [
        ("a6_cs_head", "cornerstone", "Principle head: verify before acting",
         meta(anchor_line="A6 anchor head line", situation_pattern="a6anchorpat"), "verify first"),
        ("a6_ls_trip", "lesson", "tripwire lesson one claim",
         meta(situation_pattern="tripwirepat", scaffolding_nudge=TRIPWIRE_NUDGE, tripwire_priority="high"),
         "lesson one summary"),
        ("a6_ls_shadow", "lesson", "shadow lesson two claim",
         meta(situation_pattern="tripwirepat", prevention_type="shadow"), None),
        ("a6_ls_block", "lesson", "blocking lesson three claim",
         meta(situation_pattern="a6blockpat", prevention_type="blocking",
              scaffolding_nudge="Do not do a6blockpat"), None),
        ("a6_ob_rare", "observation", "rare marker tokens zebrafoo quokkazap for the in-turn recall", "{}", None),
    ]
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        conn.executemany(
            "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, recall_summary, memory_status) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00', ?, 1, ?, 'active')",
            [(i, t, c, m, r) for i, t, c, m, r in rows])
        conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                     "VALUES ('a6_cs_head', 'a6_ls_trip', 'generalizes', '2026-01-01T00:00:00')")
        engram_idf.ensure_vocab_table(conn)
        conn.commit()
    finally:
        conn.close()


class _ThreadStreams:
    """A sys.stdin/stdout/stderr stand-in with a PER-THREAD target (the contended arm runs
    two fires at once in one process); a thread with no target uses the real stream."""

    def __init__(self, real) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_tl", threading.local())

    def set_target(self, target) -> None:
        self._tl.target = target

    def clear_target(self) -> None:
        self._tl.target = None

    def __getattr__(self, name):
        return getattr(getattr(self._tl, "target", None) or self._real, name)


class _TripwireShared:
    """What the two measured paths of one run share."""

    def __init__(self, home: Path, hook_path: Path, exclusion: bool, probe_root: Path) -> None:
        self.home, self.hook_path, self.exclusion, self.probe_root = home, hook_path, exclusion, probe_root
        self.stdin = _ThreadStreams(sys.stdin)
        self.stdout = _ThreadStreams(sys.stdout)
        self.stderr = _ThreadStreams(sys.stderr)
        self.recall_reached: List[bool] = []   # one entry per fire of either path
        self.probe_seen: List[bool] = []       # owner-path fires: was the probe dir there?


class _TripwireFire:
    """One measured path: callable = ONE fire; `prepare()` loads this thread's hook module
    (untimed); `reset()` clears the per-fire state files (untimed) so every fire starts
    from the same state (the in-turn novelty gate would otherwise go quiet after the first)."""

    def __init__(self, shared: _TripwireShared, name: str, config_path: Path) -> None:
        self.shared, self.name, self.config_path = shared, name, config_path
        self._local = threading.local()
        self.modules: List[object] = []

    def _tag(self) -> str:
        return f"{self.name}-{threading.get_ident()}"

    def prepare(self):
        inst = getattr(self._local, "mod", None)
        if inst is None:
            sh, tag = self.shared, self._tag()
            mod = load_hook(sh.home, sh.hook_path, f"engram_tripwire_hook_a6_{tag}")
            mod.CONFIG_PATH = str(self.config_path)
            mod._query_recall_daemon = lambda query, top_k=5: STUB_DAEMON_RESULT
            mod.STATE_PATH = sh.home / f"in-turn-recall-state-{tag}.json"
            mod.LEDGER_PATH = sh.home / f"in-turn-recall-ledger-{tag}.jsonl"
            mod.PREVENTION_LEDGER_PATH = sh.home / f"lesson-prevention-ledger-{tag}.jsonl"
            # one SESSION per (path, thread): concurrent fires of one session would read each
            # other's shown record / route ledger and drop candidates the other just showed
            sid = f"{TRIPWIRE_SESSION_ID}-{tag}"
            self._local.sid = sid
            self._local.probe = sh.probe_root / sid
            self._local.payload = json.dumps({"session_id": sid, "transcript_path": "", "tool_name": "Bash",
                                              "tool_input": {"command": TRIPWIRE_COMMAND}})
            if sh.exclusion:  # the probe is live for this session; the marker makes the shown record too
                self._local.probe.mkdir(parents=True, exist_ok=True)
                (sh.home / "sessions").mkdir(exist_ok=True)
                (sh.home / "sessions" / f"{sid}.json").write_text(json.dumps(
                    {"session_id": sid, "started_at": "2026-01-01T00:00:00Z", "compaction_count": 0}))
            self._local.mod = inst = mod
            self.modules.append(mod)
        return inst

    def reset(self) -> None:
        mod, sh = self.prepare(), self.shared
        for p in (mod.STATE_PATH, mod.LEDGER_PATH, mod.PREVENTION_LEDGER_PATH,
                  self._local.probe / "routes" / "tool_recall.jsonl",
                  sh.home / "sessions" / f"{self._local.sid}.shown.jsonl"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def __call__(self) -> str:
        mod, sh = self.prepare(), self.shared
        out = io.StringIO()
        sh.stdin.set_target(io.StringIO(self._local.payload))
        sh.stdout.set_target(out)
        sh.stderr.set_target(io.StringIO())
        try:
            mod.main()
        except SystemExit:
            pass
        finally:
            sh.stdin.clear_target()
            sh.stdout.clear_target()
            sh.stderr.clear_target()
        text = out.getvalue()
        sh.recall_reached.append(RECALL_HEADER in text)
        if self.name == "owner":
            sh.probe_seen.append(self._local.probe.is_dir())
        return text


def _store_path_of(arg) -> Optional[str]:
    """The resolved filesystem path a sqlite3.connect() `database` argument names, or None when
    it names none (not str/bytes/PathLike, or `:memory:`). A plain path is resolved against the
    cwd AT THE TIME OF THE CALL; a `file:` URI has its scheme, authority (`//`, `//localhost/`),
    query string and fragment removed and its percent-escapes decoded first. (The audit event does
    not carry the `uri=` flag, so every `file:` string is read as a URI: a plain file literally
    named `file:...` would be over-counted, never missed.) The result is a realpath, so symlinked
    spellings and `..` segments compare equal to the store's."""
    try:
        text = os.fspath(arg)
    except TypeError:
        return None
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if text == ":memory:":
        return None
    if text.startswith("file:"):
        text = text[len("file:"):]
        for sep in ("?", "#"):
            text = text.split(sep, 1)[0]
        if text.startswith("//"):
            text = text[2:]
            if text.startswith("localhost/"):
                text = text[len("localhost"):]
        text = os.fsdecode(unquote_to_bytes(text))
        if text in ("", ":memory:"):
            return None
    return os.path.realpath(text)


# The audit hook is installed ONCE per process (audit hooks cannot be removed). It is inert unless
# a counting window is open: it consults the list of active windows, which is empty when idle.
_ACTIVE_WINDOWS: List[Dict] = []
_AUDIT_HOOK_LOCK = threading.Lock()
_audit_hook_installed = False


_COUNT_EXEMPT = threading.local()


def exempt_this_thread_from_open_counting() -> None:
    """Mark the CALLING thread as not part of the code under measurement. The one user is the
    tests' in-process fake owner, whose handler threads open the store in THIS process; the real
    owner is a separate process the audit hook never sees. A thread that does not call this is
    counted."""
    _COUNT_EXEMPT.on = True


def _audit_open_hook(event: str, args) -> None:
    if event != "sqlite3.connect" or not _ACTIVE_WINDOWS or getattr(_COUNT_EXEMPT, "on", False):
        return
    try:  # an audit hook that raises aborts the open it observes: never let this one
        resolved = _store_path_of(args[0])
        if resolved is None:
            return
        for window in list(_ACTIVE_WINDOWS):
            if resolved == window["target"]:
                window["record"].append(str(args[0]))
                if window.get("stacks"):
                    window["record"].details.append(_open_details(args[0]))
    except Exception:
        return


def _open_details(arg) -> Dict:
    """One recorded open with its Python stack, taken INSIDE the audit hook (the audit event fires inside the
    connect call, so the frame two up from here is the code that made it). Each frame is `file:function:line`,
    outermost first (the order traceback.extract_stack uses), file as the frame's full path. Never raises: a
    failed walk records an empty stack, which attribute_opens reads as unattributed."""
    try:
        frames = traceback.StackSummary.extract(traceback.walk_stack(sys._getframe(2)), lookup_lines=False)
        stack = [f"{f.filename}:{f.name}:{f.lineno}" for f in reversed(frames)]
    except Exception:
        stack = []
    return {"arg": str(arg), "thread": threading.current_thread().name, "stack": stack}


class _OpenRecord(list):
    """The record of a `stacks=True` window: still a list of the opened strings (so `len(rec)` and
    `rec == [...]` mean what they always meant); the stacks ride beside it in `.details`."""

    def __init__(self) -> None:
        super().__init__()
        self.details: List[Dict] = []


def _ensure_audit_hook() -> None:
    global _audit_hook_installed
    with _AUDIT_HOOK_LOCK:
        if not _audit_hook_installed:
            sys.addaudithook(_audit_open_hook)
            _audit_hook_installed = True


@contextlib.contextmanager
def _counting_store_opens(db: Path, stacks: bool = False):
    """Count EVERY open of the store file `db` made by any route, in any thread, while this
    window is active: the `sqlite3.connect` audit event fires for sqlite3.connect,
    sqlite3.Connection(...), _sqlite3.connect, an early-bound `from sqlite3 import connect` and
    connect_regular_ro (which calls sqlite3.connect once inside), so each open is counted exactly
    once with no wrapping. The match is the realpath of the argument (see _store_path_of) against
    the realpath of the store copy: opens of other files (a pack DB, the probe) and `:memory:` are
    ignored. Opens outside the window are not counted. Yields the record list. With `stacks=True` the
    yielded list is an _OpenRecord whose `.details` holds each open's thread and stack (#3747); without
    it the record is the plain list it always was."""
    _ensure_audit_hook()
    window = {"target": os.path.realpath(str(db)), "record": _OpenRecord() if stacks else [], "stacks": stacks}
    _ACTIVE_WINDOWS.append(window)
    try:
        yield window["record"]
    finally:
        _ACTIVE_WINDOWS.remove(window)


def _bind_tripwire(home: Path, exclusion: bool, hook_path: Path, cleanups: List[Callable[[], None]]):
    """-> (baseline_fn, owner_fn, answers_match, answer_size, extra_result_keys).

    HOME is pointed at a scratch directory for the whole arm: the exclusion probe is live
    when `<ENGRAM_HOME>/probe/mods-recall/<session_id>/` exists (`route_probe_dir()` follows the
    hook process's ENGRAM_HOME, which is `home` here). With exclusion ON that directory is
    created by each path's `prepare()` (one session per path and thread, plus a session marker
    and sessions/ dir so the shown record is written too); with it OFF nothing is created.
    `cleanups` undoes HOME and the stream proxies.

    The recall seam: the in-turn recall asks the recall DAEMON (a separate embedding service
    reached over recall-daemon.sock) for candidates through `_query_recall_daemon`; that
    call is replaced by a function returning STUB_DAEMON_RESULT, the same seam the repo's
    own tripwire cells use. It is honest because the daemon is not an owner read and sits on
    NEITHER path's store access: both paths get the identical canned answer, everything
    after it (flock, state file, the exclusion resolver, route ledger, rendering) is the
    hook's real code, and dropping a common cost only makes the owner/baseline ratio
    larger, i.e. harder on the owner, never easier."""
    # The hook under test brings its OWN sibling helpers (_hooklib, _harness): if a copy from
    # another directory is already imported (a variant hook in a test, an earlier layout in
    # this process) evict it, so the fire runs the helpers that sit beside the hook file.
    hooks_dir = str(Path(hook_path).resolve().parent)
    saved_modules = {n: sys.modules.get(n) for n in ("_hooklib", "_harness")}
    saved_path = list(sys.path)
    for name, mod in saved_modules.items():
        if mod is not None and Path(getattr(mod, "__file__", "") or "").resolve().parent != Path(hooks_dir):
            del sys.modules[name]
    sys.path[:] = [hooks_dir] + [p for p in sys.path if p != hooks_dir]

    def restore_imports():
        sys.path[:] = saved_path
        for name, mod in saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    cleanups.append(restore_imports)

    saved_home = os.environ.get("HOME")
    scratch_home = Path(tempfile.mkdtemp(prefix="a6-hookhome-"))
    os.environ["HOME"] = str(scratch_home)
    probe_root = home / "probe" / "mods-recall"

    def restore_home():
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        shutil.rmtree(scratch_home, ignore_errors=True)
    cleanups.append(restore_home)

    shared = _TripwireShared(home, hook_path, exclusion, probe_root)
    saved_streams = (sys.stdin, sys.stdout, sys.stderr)
    sys.stdin, sys.stdout, sys.stderr = shared.stdin, shared.stdout, shared.stderr

    def restore_streams():
        sys.stdin, sys.stdout, sys.stderr = saved_streams
    cleanups.append(restore_streams)

    base_cfg: Dict = {}
    if exclusion:
        base_cfg["recall_in_context_exclusion"] = {"enabled": True}
    off_path, on_path = home / "config.json", home / "config-owner-on.json"
    off_path.write_text(json.dumps(dict(base_cfg, engramd={TRIPWIRE_FLAG_KEY: False})))
    on_path.write_text(json.dumps(dict(base_cfg, engramd={TRIPWIRE_FLAG_KEY: True})))

    baseline, owner = _TripwireFire(shared, "baseline", off_path), _TripwireFire(shared, "owner", on_path)
    for path_fn in (baseline, owner):
        path_fn.prepare()
    if baseline.prepare()._owner_reads_enabled() or not owner.prepare()._owner_reads_enabled():
        raise RuntimeError("the two paths are not flag OFF / flag ON as configured")

    db = home / "knowledge.db"
    # one sanity fire per path (also the opens count, on a reset state): the payload must
    # reach EVERY site, else a zero open count below would be vacuous. The open count is taken
    # from THIS one fire per path (all threads, while the counting window is active), not from
    # the timed fires.
    opens = {}
    texts = {}
    for name, fn in (("flag_off", baseline), ("flag_on", owner)):
        fn.reset()
        # The exclusion is fail-open: a resolver that cannot be built drops nothing and says
        # nothing, so a "0 opens" with it ON could be vacuous. Observe that it was really built.
        built: List[bool] = []
        mod = fn.prepare()
        real_build = mod.build_exclusion_resolver

        def spy_build(*a, _real=real_build, _built=built, **k):  # (U8 adds a read_conn= argument)
            resolver = _real(*a, **k)
            _built.append(resolver is not None)
            return resolver
        mod.build_exclusion_resolver = spy_build
        try:
            with _counting_store_opens(db) as rec:
                texts[name] = fn()
        finally:
            mod.build_exclusion_resolver = real_build
        opens[name] = len(rec)
        for needle in (TRIPWIRE_NUDGE, PRINCIPLES_HEADER, RECALL_HEADER):
            if needle not in texts[name]:
                raise RuntimeError(f"the tripwire fire ({name}) never produced {needle!r}: "
                                   "the payload does not reach every read site")
        if exclusion:
            ledger = fn._local.probe / "routes" / "tool_recall.jsonl"
            if not (built and all(built) and ledger.is_file()):
                raise RuntimeError(
                    f"exclusion ON but the in-context exclusion did not engage on the {name} path "
                    f"(resolver built: {built}, probe route ledger written: {ledger.is_file()}); "
                    "a zero open count would be vacuous")
    stdout_match = texts["flag_off"] == texts["flag_on"]
    baseline.reset()
    owner.reset()
    shared.recall_reached.clear()
    shared.probe_seen.clear()
    return baseline, owner, stdout_match, len(texts["flag_on"].encode("utf-8")), {
        "exclusion": exclusion, "exclusion_engaged": exclusion, "_shared": shared, "stdout_match": stdout_match,
        "opens_flag_on": opens["flag_on"], "opens_flag_off": opens["flag_off"]}


# --- the session-start hook: one FRESH-PROCESS fire (U9 gate, #3747, part of #2652) -----------------
#
# The unit under gate is U9 (#3735): with `engramd.session_start_reads_via_owner` true in
# $ENGRAM_HOME/config.json the hook's six store readers (the count(*) of _check_backup_staleness,
# _check_embedding_restore_flag, focus_block, saved_focus_sets_block, session_checkpoint_block,
# sleep_status_block) read through ONE OwnerReadConnection and never open knowledge.db. The calibration
# block in main() was migrated by #3741: on the flag-ON path it asks the owner for ONE typed
# `confidence_by_type` and never opens the store; on flag OFF it is unchanged (`EngramClient().call(
# "engram_stats", ...)`, which still opens the store and is the hook's dominant cost because it loads the
# server runtime). So flag OFF still shows opens, which is what keeps a zero on flag ON from being a
# blind counter.
#
# THE CONTRACT (the design seat's ruling on #3747, Borges), shape (a), an ALLOW-LIST: while
# CALIBRATION_STILL_OPENS is true (the pre-#3741 world), EVERY recorded flag-ON open of knowledge.db must
# be attributed to the calibration path; any other open is a violation and names the opener's frames.
# With the constant False (now) the allow-list is empty. The calibration path is identified by a stack
# frame whose file BASENAME equals exactly `engram_client.py` -- not `engramd_client.py` (the owner shim,
# which must never open the store) and not a substring match. Calibration opens are counted and reported,
# never gated on their value. It is NOT "no open from the six readers": a deny-list passes silently when a
# seventh opener appears anywhere in main() or its callees.
#
# ONE constant drives both modes (CALIBRATION_STILL_OPENS): True -> allow-list = {calibration path} and the
# must-engage raise is on; False (flipped when #3741 landed) -> allow-list empty, must-engage off,
# the cell is `opens == 0`. If NO open is attributed to the calibration path on either path the arm
# RAISES (ArmBlind "exclusion never engaged"): zero means the attribution, the layout or the fixture is
# blind (the #3739 lesson), which is a loud error, never a pass and never a violation-list entry.
#
# WARM (the owner is up before the fire) is the gate; COLD (owner launched in the same instant as the
# hook) is a reported row, with the wait read from the hook's own notice line.
#
# Each timed fire is a FRESH interpreter (`python <hook>` fed a SessionStart JSON on stdin), because the
# harness runs the hook that way and the dominant cost is import + EngramClient, which an in-process call
# would hide. The fire's environment is a scratch HOME / TMPDIR / ENGRAM_HOME; nothing else from the seat
# is passed on. One home serves both paths: the hook reads the flag from $ENGRAM_HOME/config.json, so the
# config file is rewritten (untimed) before each timed fire.

SS_FLAG_KEY = "session_start_reads_via_owner"
SS_READERS = ("_check_backup_staleness", "_check_embedding_restore_flag", "focus_block",
              "saved_focus_sets_block", "session_checkpoint_block", "sleep_status_block")
CALIBRATION_STILL_OPENS = False           # #3741 landed (the calibration block reads through the owner); was True
CALIBRATION_FRAME_BASENAME = "engram_client.py"   # exact basename equality, never a substring
N_COLD = 30                               # cold-row fires (a reported row, not a gate): small on purpose
SS_INJECTED_THROUGH = "2026-06-01T00:00:00+00:00"   # the pre-written marker's checkpoint boundary
_WAIT_ANY_RE = re.compile(r"waited \d+ ms")
_WAIT_SERVED_RE = re.compile(r"waited (\d+) ms, then served")


class ArmBlind(RuntimeError):
    """The arm cannot see what it claims to measure (an unreached reader, an exclusion that never engaged,
    a warm fire that waited): a loud error, never a pass and never a violation-list entry."""


def exclusion_must_engage() -> bool:
    """True while the calibration path still opens the store (the must-engage raise is on)."""
    return bool(CALIBRATION_STILL_OPENS)


def _frame_basename(frame: str) -> str:
    return os.path.basename(frame.rsplit(":", 2)[0])


def _short_frame(frame: str) -> str:
    path, func, line = frame.rsplit(":", 2) if frame.count(":") >= 2 else (frame, "?", "?")
    return f"{os.path.basename(path)}:{func}:{line}"


def _is_calibration_open(o: Dict) -> bool:
    return any(_frame_basename(f) == CALIBRATION_FRAME_BASENAME for f in (o.get("stack") or []))


def attribute_opens(opens: List[Dict], hook_file) -> Dict:
    """The allow-list. `opens` are recorded opens ({"arg", "thread", "stack": ["file:function:line", ...]},
    outermost frame first). An open is ALLOWED only while CALIBRATION_STILL_OPENS is true and its stack holds
    a frame whose file basename is exactly CALIBRATION_FRAME_BASENAME; every other open is a violation naming
    the innermost frame and every frame in the hook file as `file:function:line`. Pure."""
    hook_base = os.path.basename(str(hook_file))
    calibration, violations = [], []
    for o in opens:
        if CALIBRATION_STILL_OPENS and _is_calibration_open(o):
            calibration.append(o)
            continue
        stack = list(o.get("stack") or [])
        inner = _short_frame(stack[-1]) if stack else "<no stack>"
        in_hook = [_short_frame(f) for f in stack if _frame_basename(f) == hook_base]
        violations.append(
            f"unattributed open of the store ({o.get('arg')}) in thread {o.get('thread')}: innermost frame "
            f"{inner}; frames in {hook_base}: {', '.join(in_hook) or 'none'}")
    return {"calibration": calibration, "violations": violations}


def reader_of(stack_names) -> Optional[str]:
    """The innermost SS_READERS function in `stack_names` ([(file, function), ...], innermost first)."""
    for _file, func in stack_names:
        if func in SS_READERS:
            return func
    return None


def parse_owner_wait_ms(text: str) -> Optional[int]:
    """The hook's own cold-start notice -- `waited N ms, then served` -- as N, or None when absent."""
    m = _WAIT_SERVED_RE.search(text or "")
    return int(m.group(1)) if m else None


def assert_no_warm_wait(texts: List[str], where: str) -> None:
    """A warm fire (owner up before it) never prints a `waited N ms` notice; if one did, the warm assumption
    broke and every warm number is suspect. Raises ArmBlind."""
    for t in texts:
        m = _WAIT_ANY_RE.search(t or "")
        if m:
            raise ArmBlind(f"warm fire waited ({where}: {m.group(0)!r}): the owner was not up when the hook "
                           "fired, so the warm assumption broke")


def summarise_cold(rows: List[Dict]) -> Dict:
    """The cold row from [{"wall_ms", "wait_ms" (None/0 = the owner won the race), "failed"?}]. Reported, never a gate."""
    waits = sorted(r["wait_ms"] for r in rows if r.get("wait_ms"))
    walls = sorted(r["wall_ms"] for r in rows)
    return {"n": len(rows), "served_without_wait": len(rows) - len(waits),
            "owner_failed": sum(1 for r in rows if r.get("failed")),
            "wait_ms_p50": _pctl(waits, 0.50) if waits else None, "wait_ms_max": waits[-1] if waits else None,
            "hook_p50_ms": _pctl(walls, 0.50) if walls else None, "hook_max_ms": walls[-1] if walls else None}


# The output's non-determinism, found by diffing two fires of the SAME path (decision 6): the session id
# (a uuid), the overflow file's `<UTC timestamp>-<pid>.md` name, and the scratch paths (ENGRAM_HOME, the
# fire's HOME/TMPDIR). Each is a named span; nothing else is masked.
_SS_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SS_OVERFLOW_NAME_RE = re.compile(r"\d{8}T\d{6}Z-\d+\.md")


def normalise_session_start_output(text: str, *scratch_paths) -> str:
    """Mask exactly the spans that differ between two fires of one path; everything else (every content line,
    the owner notice) is left as printed. `scratch_paths` are the literal scratch directories (their realpath
    spellings too)."""
    out = text
    spellings = set()
    for p in scratch_paths:
        if p:
            spellings.add(str(p))
            spellings.add(os.path.realpath(str(p)))
    for sp in sorted(spellings, key=len, reverse=True):
        out = out.replace(sp, "<SCRATCH>")
    out = _SS_UUID_RE.sub("<SESSION-ID>", out)
    return _SS_OVERFLOW_NAME_RE.sub("<OVERFLOW>.md", out)


def ss_contract_violations(result: Dict) -> List[str]:
    """The gate on one `--hook session-start` result ([] = clear). Pure.

      * stdout_match is not True    -- the flag-OFF and flag-ON fires printed different (normalised) text;
      * open_violations             -- the allow-list's violations on the flag-ON path (each names the
                                       opener's frames); a missing / non-list value fails CLOSED.
    Calibration opens, the cold row and the timing ratio are reported, never violations."""
    out: List[str] = []
    if result.get("stdout_match") is not True:
        out.append("stdout_match is not true: the flag-OFF and flag-ON fires printed different text "
                   "(after normalising the named non-determinism)")
    ov = result.get("open_violations")
    if not isinstance(ov, list):
        out.append(f"open_violations is missing or not a list ({ov!r}): the allow-list verdict is unknown")
    else:
        out.extend(str(v) for v in ov)
    return out


def _seed_session_start_fixture(home: Path) -> None:
    """Add the session-start fixture to the store copy in `home` (synthetic or a real-store copy; ids are
    `a6_*`) so each of the six readers EXECUTES its store read:
      * an axiom (the server runtime the calibration block loads refuses a store with none);
      * focused nodes (focus_block's `focused_at IS NOT NULL` query returns rows);
      * a saved focus set that is not the active one (saved_focus_sets_block renders it);
      * nodes created after SS_INJECTED_THROUGH (session_checkpoint_block's window);
      * the embedding_restore_state singleton, clear (the schema creates it; ensured here for a copy), and an
        embedding on every node (see below).
    The backup count only runs while knowledge.sql is ABSENT, and sleep_status_block's only while
    sessions/last-sleep-success.json is absent: this fixture simply never creates either file."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = [
        ("a6_ax_seed", "axiom", "a6 fixture seed axiom", "2026-01-01T00:00:00", None, None),
        ("a6_focus_1", "observation", "a6 focused node one", "2026-01-02T00:00:00", now, "a6 shared focus reason"),
        ("a6_focus_2", "observation", "a6 focused node two", "2026-01-02T00:00:01", now, "a6 shared focus reason"),
        ("a6_ck_1", "observation", "a6 node created after the checkpoint boundary", "2026-09-01T00:00:00", None, None),
        ("a6_ck_2", "derivation", "a6 second node after the boundary", "2026-09-01T00:00:01", None, None),
    ]
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status, "
            "focused_at, focus_reason) VALUES (?, ?, ?, ?, '{}', 1, 'active', ?, ?)",
            [(i, t, c, ca, fa, fr) for i, t, c, ca, fa, fr in rows])
        conn.execute("INSERT OR IGNORE INTO focus_state (singleton_key, active_set_name) VALUES (1, NULL)")
        conn.execute("INSERT OR IGNORE INTO focus_sets (name, node_ids, description, created_at) "
                     "VALUES ('a6_set_one', ?, 'a6 saved set', '2026-08-01T00:00:00')", (json.dumps(["a6_focus_1"]),))
        conn.execute("INSERT OR IGNORE INTO embedding_restore_state (singleton_key, not_fully_restored, null_count) "
                     "VALUES (1, 0, 0)")
        # Every node carries an embedding. The calibration block loads the server runtime, whose one-time DB
        # setup finds nodes WITHOUT one, sets the store's embedding-restore flag (the next fire then prints a
        # health line the previous fire caused) and starts a background `engram-embedding-restore-regen`
        # thread that opens the store from a stack with no `engram_client.py` frame -- measured on the
        # synthetic store; a production store has embeddings and starts neither.
        conn.execute("UPDATE nodes SET embedding = '[0.0]' WHERE embedding IS NULL")
        conn.commit()
    finally:
        conn.close()


# The sanity driver: ONE untimed fire per path, run as a subprocess so it has the fire's real environment.
# It installs the tool's own audit-hook counting (stacks on), wraps the owner shim's `execute` to record the
# calling reader, runs the hook's main() on the same stdin, and writes everything to a JSON file.
_SS_DRIVER = r'''
import contextlib, importlib.abc, importlib.machinery, io, json, os, runpy, sys
cfg = json.loads(sys.argv[1])
sys.path.insert(0, cfg["tools_dir"])
import engramd_a6_latency as a6
sys.path.remove(cfg["tools_dir"])
owner_calls = []

def _wrap(module):
    cls = getattr(module, "OwnerReadConnection", None)
    if cls is None:
        return
    orig = cls.execute
    def execute(self, sql, params=()):
        names, f = [], sys._getframe(1)
        while f is not None and len(names) < 16:
            names.append((f.f_code.co_filename, f.f_code.co_name))
            f = f.f_back
        owner_calls.append(names)
        return orig(self, sql, params)
    cls.execute = execute

class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "engramd_client":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return None
        loader, orig_exec = spec.loader, spec.loader.exec_module
        def exec_module(module):
            orig_exec(module)
            _wrap(module)
        loader.exec_module = exec_module
        return spec

sys.meta_path.insert(0, _Finder())
g = runpy.run_path(cfg["hook"], run_name="ss_hook_under_test")
live = g["main"].__globals__   # run_path returns a COPY of the module dict; the functions run against this one
flag_seen = []
orig_flag = live.get("_owner_reads_enabled")
if orig_flag is not None:
    def _probe(*a, **k):
        r = orig_flag(*a, **k)
        flag_seen.append(r)
        return r
    live["_owner_reads_enabled"] = _probe
sys.stdin = io.StringIO(cfg["stdin"])
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    with a6._counting_store_opens(a6.Path(cfg["db"]), stacks=True) as rec:
        try:
            g["main"]()
        except SystemExit:
            pass
def _file(name):
    m = sys.modules.get(name)
    return getattr(m, "__file__", None) if m is not None else None
json.dump({"opens": rec.details, "owner_calls": owner_calls, "flag_seen": flag_seen,
           "flag_helper_present": orig_flag is not None,
           "loaded": a6._runtime_modules_loaded(a6.Path(cfg["layout_root"]), [a6.Path(cfg["hook"])]),
           "engramd_client_file": _file("engramd_client"), "engram_client_file": _file("engram_client"),
           "stdout": out.getvalue(), "stderr": err.getvalue()[-2000:]}, open(cfg["out"], "w"))
'''


class SessionStartRig:
    """One session-start measurement setup: the hook file, the scratch ENGRAM_HOME (`home`, holding the store),
    and a scratch directory (`scratch`) that is the fire's HOME and TMPDIR. `installed` says the hook sits in a
    built plugin (CLAUDE_PLUGIN_ROOT = `layout_root` is then set for the fire, as the harness does)."""

    def __init__(self, hook_path, home, scratch, layout_root, installed: bool = False) -> None:
        self.hook_path, self.home, self.scratch = Path(hook_path), Path(home), Path(scratch)
        self.layout_root, self.installed = Path(layout_root), installed
        (self.scratch / "tmp").mkdir(parents=True, exist_ok=True)
        self.transcript = self.scratch / "transcript.jsonl"
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.scratch), "ENGRAM_HOME": str(self.home),
                    "AGENTS_SHARED_DIR": str(self.scratch / "shared"), "TMPDIR": str(self.scratch / "tmp"),
                    "PYTHONDONTWRITEBYTECODE": "1", "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                    "HF_HUB_OFFLINE": "1"}
        hf = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
        if os.path.isdir(hf):
            self.env["HF_HOME"] = hf   # the embedder's cache, read-only (HF_HUB_OFFLINE): no download, no write
        if installed:
            self.env["CLAUDE_PLUGIN_ROOT"] = str(self.layout_root)

    # -- per-fire state, all untimed -------------------------------------------------------------
    def write_config(self, flag: bool) -> None:
        (self.home / "config.json").write_text(json.dumps({"engramd": {SS_FLAG_KEY: bool(flag)}}))

    def write_marker(self, sid: str) -> None:
        """The session marker with `injected_through` set, so session_checkpoint_block's read executes."""
        sessions = self.home / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / f"{sid}.json").write_text(json.dumps({
            "session_id": sid, "transcript_path": str(self.transcript), "source": "startup", "cwd": str(self.scratch),
            "started_at": "2026-10-01T00:00:00+00:00", "session_started_at": "2026-10-01T00:00:00+00:00",
            "role": "interactive", "purpose": "a6", "injected_through": SS_INJECTED_THROUGH, "compaction_count": 0}))

    def clean(self) -> None:
        """Remove what earlier fires wrote (markers, overflow files, logs, telemetry) so every fire starts from the
        same state; the store, the config and the owner's run/ directory stay."""
        for sub in ("sessions", "session-start-overflow", "logs", "telemetry"):
            shutil.rmtree(self.home / sub, ignore_errors=True)

    def payload(self, sid: str) -> str:
        return json.dumps({"session_id": sid, "transcript_path": str(self.transcript), "cwd": str(self.scratch),
                           "source": "startup", "hook_event_name": "SessionStart"})

    # -- fires --------------------------------------------------------------------------------------
    def fire(self, sid: str) -> str:
        """ONE fire: a fresh interpreter on the hook, the SessionStart JSON on stdin. Returns its stdout."""
        p = subprocess.run([sys.executable, str(self.hook_path)], input=self.payload(sid), env=self.env,
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise RuntimeError(f"the session-start fire exited {p.returncode}: {p.stderr[-800:]}")
        return p.stdout

    def run_driver(self, sid: str) -> Dict:
        """The sanity driver for ONE fire (the config and marker are the caller's). Returns its JSON."""
        out_path = self.scratch / f"driver-{uuid.uuid4().hex}.json"
        cfg = {"tools_dir": str(Path(__file__).resolve().parent), "hook": str(self.hook_path),
               "db": str(self.home / "knowledge.db"), "layout_root": str(self.layout_root),
               "stdin": self.payload(sid), "out": str(out_path)}
        script = self.scratch / "ss_driver.py"
        script.write_text(_SS_DRIVER)
        p = subprocess.run([sys.executable, str(script), json.dumps(cfg)], env=self.env, capture_output=True,
                           text=True, timeout=120)
        if p.returncode != 0 or not out_path.is_file():
            raise RuntimeError(f"the session-start sanity driver failed (rc {p.returncode}): {p.stderr[-1500:]}")
        return json.loads(out_path.read_text())

    def make_paths(self):
        return _SSFire(self, "baseline", False), _SSFire(self, "owner", True)


class _SSFire:
    """One measured path: callable = ONE fire (a fresh process). `reset()` (untimed, the `before` of the arms)
    starts a new session id and writes its marker; while `clean_each` it also clears the earlier fires' files and
    REWRITES THE FLAG for this path -- the sequential and cold arms interleave the paths, so the config must be
    this path's before every timed fire. The contended arm turns `clean_each` off and calls `phase_start()` once:
    two threads of one path would delete each other's files otherwise."""

    def __init__(self, rig: SessionStartRig, name: str, flag: bool) -> None:
        self.rig, self.name, self.flag = rig, name, flag
        self.clean_each = True
        self._local = threading.local()
        self._lock = threading.Lock()
        self.texts: List[str] = []

    def phase_start(self) -> None:
        self.rig.clean()
        self.rig.write_config(self.flag)

    def reset(self) -> None:
        if self.clean_each:
            self.rig.clean()
            self.rig.write_config(self.flag)
        self._local.sid = str(uuid.uuid4())
        self.rig.write_marker(self._local.sid)

    def __call__(self) -> str:
        sid = getattr(self._local, "sid", None)
        if sid is None:
            self.reset()
            sid = self._local.sid
        text = self.rig.fire(sid)
        if self.flag:
            with self._lock:
                self.texts.append(text)
        self._local.sid = None
        return text


def _readers_reached_off(run: Dict, hook_file) -> List[str]:
    hook_base = os.path.basename(str(hook_file))
    got = set()
    for o in run["opens"]:
        names = [(f.rsplit(":", 2)[0], f.rsplit(":", 2)[1]) for f in reversed(o.get("stack") or [])
                 if f.count(":") >= 2 and _frame_basename(f) == hook_base]
        r = reader_of(names)
        if r:
            got.add(r)
    return sorted(got)


def _readers_reached_on(run: Dict, hook_file) -> List[str]:
    hook_base = os.path.basename(str(hook_file))
    got = set()
    for names in run["owner_calls"]:
        r = reader_of([(f, fn) for f, fn in names if os.path.basename(f) == hook_base])
        if r:
            got.add(r)
    return sorted(got)


def session_start_sanity(rig: SessionStartRig) -> Dict:
    """The untimed, once-per-path sanity run, and the contract checks that need no timing. Runs the sanity
    driver with the flag OFF then ON (same store, same stdin shape), and:
      * RAISES ArmBlind when a path did not see the flag state it was configured with;
      * RAISES ArmBlind naming any of the six readers not reached on a path (OFF: an open attributed to it;
        ON: an owner read called by it) -- the payload does not reach every read site;
      * RAISES ArmBlind "warm fire waited" when the flag-ON fire printed a `waited N ms` notice;
      * RAISES ArmBlind "exclusion never engaged" when, with CALIBRATION_STILL_OPENS true, a path has no open
        attributed to the calibration path (zero means the attribution, layout or fixture is blind);
      * otherwise returns the counts, the allow-list's violations for the flag-ON path, and stdout_match."""
    runs: Dict[str, Dict] = {}
    for name, flag in (("off", False), ("on", True)):
        rig.clean()
        sid = str(uuid.uuid4())
        rig.write_config(flag)
        rig.write_marker(sid)
        runs[name] = rig.run_driver(sid)
        run = runs[name]
        if not run["flag_helper_present"] or not run["flag_seen"] or any(f is not flag for f in run["flag_seen"]):
            raise ArmBlind(f"the flag-{name.upper()} fire did not see the flag state {flag} it was configured with "
                           f"(the hook's own reader returned {run['flag_seen']}, helper present: "
                           f"{run['flag_helper_present']})")
        if rig.installed:
            for key in ("engramd_client_file", "engram_client_file"):
                f = run.get(key)
                if f and rig.layout_root.resolve() not in Path(f).resolve().parents:
                    raise LayoutViolation(f"{key} was loaded from {f}, not from the build {rig.layout_root}")
    reached = {"flag-OFF": _readers_reached_off(runs["off"], rig.hook_path),
               "flag-ON": _readers_reached_on(runs["on"], rig.hook_path)}
    for path, got in reached.items():
        missing = [r for r in SS_READERS if r not in got]
        if missing:
            raise ArmBlind(f"reader {missing[0]} was not reached on the {path} path (missing: {', '.join(missing)}): "
                           "the payload does not reach every read site")
    assert_no_warm_wait([runs["on"]["stdout"]], "the sanity fire")
    calib = {n: sum(1 for o in runs[n]["opens"] if _is_calibration_open(o)) for n in ("off", "on")}
    if exclusion_must_engage():
        for n in ("off", "on"):
            if calib[n] == 0:
                raise ArmBlind(
                    f"exclusion never engaged on the flag-{n.upper()} path: no open is attributed to the calibration "
                    f"path (a frame in {CALIBRATION_FRAME_BASENAME}) -- the attribution, the layout or the fixture "
                    "is blind")
    att_on = attribute_opens(runs["on"]["opens"], rig.hook_path)
    scratch = [rig.home, rig.scratch]
    norm = {n: normalise_session_start_output(runs[n]["stdout"], *scratch) for n in ("off", "on")}
    return {"opens_flag_off": len(runs["off"]["opens"]), "opens_flag_on": len(runs["on"]["opens"]),
            "calibration_opens_off": calib["off"], "calibration_opens_on": calib["on"],
            "open_violations": att_on["violations"],
            "readers_reached_off": reached["flag-OFF"], "readers_reached_on": reached["flag-ON"],
            "stdout_match": norm["off"] == norm["on"], "stdout_bytes": len(runs["on"]["stdout"].encode("utf-8")),
            "flag_seen": {"off": runs["off"]["flag_seen"][0], "on": runs["on"]["flag_seen"][0]},
            "loaded": sorted(set(runs["off"]["loaded"]) | set(runs["on"]["loaded"])),
            "normalised": norm}


def session_start_loaded_files(layout: "Layout", nodes: int = 300) -> List[str]:
    """Root-relative runtime files a session-start fire loads from `layout` (both flag states, a real owner):
    the data that RUNTIME_FILES must cover. The closure cell compares against it."""
    home = make_test_store(nodes=nodes)
    scratch = Path(tempfile.mkdtemp(prefix="a6-ss-"))
    handle = None
    try:
        _seed_session_start_fixture(home)
        handle = launch_owner(home)
        rig = SessionStartRig(layout.session_start_hook, home, scratch, layout.root, installed=layout.name == "installed")
        loaded = set()
        for flag in (False, True):
            rig.clean()
            sid = str(uuid.uuid4())
            rig.write_config(flag)
            rig.write_marker(sid)
            loaded.update(rig.run_driver(sid)["loaded"])
        return sorted(loaded)
    finally:
        if handle is not None:
            handle.stop()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)


def _cold_row(rig: SessionStartRig, home: Path, n_cold: int, state: Dict) -> Dict:
    """The COLD row (reported, not a gate): `n_cold` fires, each with the owner STOPPED beforehand; the owner
    launcher and the flag-ON hook are started in the same instant (two Popens, no sleep between) and the hook's
    wall time and its own `waited N ms, then served` notice are recorded (None = the owner won the race)."""
    rows: List[Dict] = []
    env = dict(os.environ, ENGRAM_HOME=str(home))
    env.pop("ENGRAM_OWNER_ALLOW_DYNAMIC_TEMPLATES", None)
    for _ in range(n_cold):
        if state.get("handle") is not None:
            state["handle"].stop()
            state["handle"] = None
        for stale in ("engramd.sock",):
            try:
                (home / "run" / stale).unlink()
            except FileNotFoundError:
                pass
        rig.clean()
        sid = str(uuid.uuid4())
        rig.write_config(True)
        rig.write_marker(sid)
        t0 = time.perf_counter_ns()
        owner = subprocess.Popen(owner_argv(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 cwd=str(home))
        state["handle"] = OwnerHandle(owner, home)
        hook = subprocess.Popen([sys.executable, str(rig.hook_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=rig.env)
        out, _err = hook.communicate(rig.payload(sid), timeout=60)
        wall = (time.perf_counter_ns() - t0) / 1e6
        rows.append({"wall_ms": wall, "wait_ms": parse_owner_wait_ms(out),
                     "failed": "engramd owner read failed" in out})
    return summarise_cold(rows)


# --- timing ----------------------------------------------------------------------

# --- the surface hook: one FULL FIRE (A6 arm for #3821 / the surface read unit, part of #2652) -------------------
#
# The arm times the surface hook's own main() on one fixed UserPromptSubmit payload, stdin/stdout/stderr captured in
# process (the tripwire arm's shape). Both paths run the SAME hook file as two module instances: baseline reads its
# flag from config.json (engramd.surface_reads_via_owner = false), owner from config-owner-on.json beside it
# (= true), through the hook's own `_owner_reads_enabled()` over its module-level CONFIG_PATH (the convention the
# stop and tripwire hooks use). A hook that has no such flag is ArmBlind, with the flag named: the arm measures
# nothing rather than two identical paths.
#
# THE RECALL SEAM. The hook asks the recall DAEMON (a separate embedding service on recall-daemon.sock) through
# `query_daemon`; that one name is replaced on BOTH paths by the same canned answer (STUB_DAEMON_RESULT, as the
# tripwire arm does at `_query_recall_daemon`). With a non-None answer the hook never takes `query_fts_fallback` (its
# in-hook server runtime, the #3741 class), so no path pays a daemon or a fallback. Honest because the daemon is not
# an owner read and sits on neither path's store access; a common cost removed makes the owner/baseline ratio LARGER.
#
# THE GATE CELL. The design's A6 gate is one warm-cache delta (docs/2652-engrammanager-design.md SS6.1), and the store
# axis flipped it on two hooks (the stop hook, #3675; the tripwire, #3740: 0.94x on a synthetic store, 1.03-1.04x on a
# real copy). So the arm measures TWO cells -- a synthetic store and a COPY of a real store -- and the verdict is the
# REAL-STORE cell on the INSTALLED layout; a synthetic-only result is never a pass (surface_gate).

SURFACE_FLAG_KEY = "surface_reads_via_owner"
SURFACE_EXPECTED_SQLITE = "3.53.0"          # the plugin venv's build the gate is stated for (SS6.1 axes); named, never assumed
SURFACE_SESSION_ID = "a6-surface-session"
SURFACE_PROMPT = "what is the plan for the engram owner reads today please"
SURFACE_RECALL_NEEDLE = "[ENGRAM Recall:"   # the recall block the stub's answer renders into
SURFACE_REMAINDER_FUNC = "_advance_person_moment_cursor"   # the one RW open left direct on both flag values (#3821 row 8)
CELLS = ("synthetic", "real-store")
GATE_LAYOUT, GATE_CELL = "installed", "real-store"


def _seed_surface_fixture(home: Path) -> None:
    """Ensure the fts5 vocab table the hook's IDF gate reads exists in the store copy in `home` (synthetic or a
    real-store copy). Nothing else is added: the fire's reads run on whatever the store holds."""
    _ensure_src_importable()
    import engram_idf

    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        engram_idf.ensure_vocab_table(conn)
        conn.commit()
    finally:
        conn.close()


def _require_surface_flag(mod, hook_path: Path) -> None:
    """ArmBlind unless the loaded hook module reads `engramd.surface_reads_via_owner` the way the arm drives it:
    a callable `_owner_reads_enabled` over a module-level `CONFIG_PATH`, and the flag key named in the hook's source."""
    problems = []
    try:
        if SURFACE_FLAG_KEY not in Path(hook_path).read_text(encoding="utf-8", errors="replace"):
            problems.append(f"its source never names {SURFACE_FLAG_KEY!r}")
    except OSError as exc:
        problems.append(f"its source is unreadable ({exc})")
    if not callable(getattr(mod, "_owner_reads_enabled", None)):
        problems.append("it has no _owner_reads_enabled()")
    if not hasattr(mod, "CONFIG_PATH"):
        problems.append("it has no module-level CONFIG_PATH")
    if problems:
        raise ArmBlind(
            f"the surface hook {Path(hook_path).name} has no engramd.{SURFACE_FLAG_KEY} flag the arm can drive "
            f"({'; '.join(problems)}): flag OFF and flag ON would be the same path, so no number is produced. "
            "The arm needs the surface-reads-via-owner hook change (#3821) on this tree.")


class _SurfaceShared:
    """What the two measured paths of one run share."""

    def __init__(self, home: Path, hook_path: Path) -> None:
        self.home, self.hook_path = home, hook_path
        self.stdin, self.stdout, self.stderr = (_ThreadStreams(sys.stdin), _ThreadStreams(sys.stdout),
                                                _ThreadStreams(sys.stderr))
        self.recall_reached: List[bool] = []   # one entry per fire of either path
        self.keep: set = set()                 # relative paths that are NOT per-fire state (set once, after setup)

    def snapshot(self) -> None:
        self.keep = set(self._walk())

    def _walk(self):
        for dirpath, dirnames, filenames in os.walk(self.home):
            rel_dir = os.path.relpath(dirpath, self.home)
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if d != "run"]   # the owner's socket directory is never touched
            for name in filenames:
                yield os.path.normpath(os.path.join(rel_dir, name))

    def clear_fire_state(self) -> None:
        """Remove every file the hook wrote since setup (counters, trigger state, shown records ...) so each fire
        starts from the same state. The store (and its -wal/-shm) and the config files are never touched."""
        for rel in list(self._walk()):
            if rel in self.keep or os.path.basename(rel).startswith("knowledge.db"):
                continue
            try:
                os.unlink(os.path.join(self.home, rel))
            except FileNotFoundError:
                pass


class _SurfaceFire:
    """One measured path: callable = ONE fire of main(); `prepare()` loads this thread's hook module and stubs its
    recall daemon (untimed); `reset()` clears per-fire state (untimed)."""

    def __init__(self, shared: _SurfaceShared, name: str, config_path: Path) -> None:
        self.shared, self.name, self.config_path = shared, name, config_path
        self._local = threading.local()
        self.modules: List[object] = []

    def prepare(self):
        inst = getattr(self._local, "mod", None)
        if inst is None:
            sh, tag = self.shared, f"{self.name}-{threading.get_ident()}"
            mod = load_hook(sh.home, sh.hook_path, f"engram_surface_hook_a6_{tag}")
            _require_surface_flag(mod, sh.hook_path)
            mod.CONFIG_PATH = str(self.config_path)
            mod.query_daemon = lambda prompt, top_k=10, embed_query=None: STUB_DAEMON_RESULT   # the SAME stub on both paths
            sid = f"{SURFACE_SESSION_ID}-{tag}"   # one session per (path, thread): concurrent fires must not share state
            self._local.payload = json.dumps({"session_id": sid, "transcript_path": "", "prompt": SURFACE_PROMPT})
            self._local.mod = inst = mod
            self.modules.append(mod)
        return inst

    def reset(self) -> None:
        self.prepare()
        self.shared.clear_fire_state()

    def __call__(self) -> str:
        mod, sh = self.prepare(), self.shared
        out = io.StringIO()
        sh.stdin.set_target(io.StringIO(self._local.payload))
        sh.stdout.set_target(out)
        sh.stderr.set_target(io.StringIO())
        try:
            mod.main()
        except SystemExit:
            pass
        finally:
            sh.stdin.clear_target()
            sh.stdout.clear_target()
            sh.stderr.clear_target()
        text = out.getvalue()
        sh.recall_reached.append(SURFACE_RECALL_NEEDLE in text)
        return text


def surface_attribute_opens(opens: List[Dict], hook_file) -> Dict:
    """The surface allow-list. `opens` are the recorded flag-ON opens of the store ({"arg", "thread", "stack":
    ["file:function:line", ...]}, outermost first). An open is the NAMED REMAINDER only if its stack holds a frame
    for the function SURFACE_REMAINDER_FUNC in the hook's own file (the person-moment cursor write, which stays direct on
    both flag values until the first owner write unit); every other open is a violation naming the innermost
    frame and the frames in the hook file. Pure."""
    hook_base = os.path.basename(str(hook_file))
    remainder, violations = [], []
    for o in opens:
        stack = list(o.get("stack") or [])
        if any(_frame_basename(f) == hook_base and f.rsplit(":", 2)[-2:-1] == [SURFACE_REMAINDER_FUNC] for f in stack):
            remainder.append(o)
            continue
        inner = _short_frame(stack[-1]) if stack else "<no stack>"
        in_hook = [_short_frame(f) for f in stack if _frame_basename(f) == hook_base]
        violations.append(
            f"unattributed open of the store ({o.get('arg')}) in thread {o.get('thread')}: innermost frame "
            f"{inner}; frames in {hook_base}: {', '.join(in_hook) or 'none'}")
    return {"remainder": remainder, "violations": violations}


def _bind_surface(home: Path, hook_path: Path, cleanups: List[Callable[[], None]]):
    """-> (baseline_fn, owner_fn, answers_match, answer_size, extra_result_keys).

    HOME is pointed at a scratch directory and the stream proxies installed for the whole arm (`cleanups` undoes
    both, and the sys.path / helper-module swap): the hook's probe and state paths hang off `Path.home()` and
    ENGRAM_HOME. One sanity fire per path (flag OFF, flag ON) on cleared state gives the stdout comparison, the
    recall check and the per-path store-open record (stacks, counted in every thread)."""
    hooks_dir = str(Path(hook_path).resolve().parent)
    saved_modules = {n: sys.modules.get(n) for n in ("_hooklib", "_harness")}
    saved_path = list(sys.path)
    for name, mod in saved_modules.items():   # run the helpers that sit beside THIS hook file, not another layout's
        if mod is not None and Path(getattr(mod, "__file__", "") or "").resolve().parent != Path(hooks_dir):
            del sys.modules[name]
    sys.path[:] = [hooks_dir] + [p for p in sys.path if p != hooks_dir]

    def restore_imports():
        sys.path[:] = saved_path
        for name, mod in saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    cleanups.append(restore_imports)

    saved_home = os.environ.get("HOME")
    scratch_home = Path(tempfile.mkdtemp(prefix="a6-hookhome-"))
    os.environ["HOME"] = str(scratch_home)

    def restore_home():
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        shutil.rmtree(scratch_home, ignore_errors=True)
    cleanups.append(restore_home)

    shared = _SurfaceShared(home, hook_path)
    saved_streams = (sys.stdin, sys.stdout, sys.stderr)
    sys.stdin, sys.stdout, sys.stderr = shared.stdin, shared.stdout, shared.stderr

    def restore_streams():
        sys.stdin, sys.stdout, sys.stderr = saved_streams
    cleanups.append(restore_streams)

    off_path, on_path = home / "config.json", home / "config-owner-on.json"
    off_path.write_text(json.dumps({"engramd": {SURFACE_FLAG_KEY: False}}))
    on_path.write_text(json.dumps({"engramd": {SURFACE_FLAG_KEY: True}}))
    baseline, owner = _SurfaceFire(shared, "baseline", off_path), _SurfaceFire(shared, "owner", on_path)
    base_mod, owner_mod = baseline.prepare(), owner.prepare()   # (raises ArmBlind for a hook without the flag)
    if base_mod._owner_reads_enabled() or not owner_mod._owner_reads_enabled():
        raise RuntimeError("the two paths are not flag OFF / flag ON as configured")
    shared.snapshot()

    db = home / "knowledge.db"
    opens, texts, records = {}, {}, {}
    for name, fn in (("flag_off", baseline), ("flag_on", owner)):
        fn.reset()
        with _counting_store_opens(db, stacks=True) as rec:
            texts[name] = fn()
        opens[name] = len(rec)
        records[name] = list(rec.details)
    attributed = surface_attribute_opens(records["flag_on"], hook_path)
    stdout_match = texts["flag_off"] == texts["flag_on"]
    baseline.reset()
    owner.reset()
    shared.recall_reached.clear()
    return baseline, owner, stdout_match, len(texts["flag_on"].encode("utf-8")), {
        "_shared": shared, "stdout_match": stdout_match,
        "opens_flag_on": opens["flag_on"], "opens_flag_off": opens["flag_off"],
        "remainder_opens_flag_on": len(attributed["remainder"]), "open_violations": attributed["violations"]}


def surface_contract_violations(result: Dict) -> List[str]:
    """The contract gate on ONE `--hook surface` result (one layout x one store cell), as human-readable violations
    ([] = clear). Pure: no I/O, the input is not modified.

      * recall_reached is not True  -- a fire never reached the recall block, so the stubbed seam did not run;
      * stdout_match is not True    -- flag OFF and flag ON printed different bytes;
      * open_violations             -- every flag-ON open of the store that is not the named remainder
                                       (SURFACE_REMAINDER_FUNC), each naming its frames; a missing or non-list value
                                       fails CLOSED.
    The open COUNTS and the timing ratio are reported, never violations here (the ratio is surface_gate's)."""
    out: List[str] = []
    if result.get("recall_reached") is not True:
        out.append("recall_reached is not true: a fire never reached the recall block (the stubbed daemon seam did not run)")
    if result.get("stdout_match") is not True:
        out.append("stdout_match is not true: the flag-OFF and flag-ON fires printed different bytes")
    ov = result.get("open_violations")
    if not isinstance(ov, list):
        out.append(f"open_violations is missing or not a list ({ov!r}): the allow-list verdict is unknown")
    else:
        out.extend(str(v) for v in ov)
    return out


def _arm_ratios(result: Dict) -> Dict[str, float]:
    """arm -> p50 owner/baseline for one result's rows."""
    by = {(r["arm"], r["path"]): r for r in result.get("rows") or []}
    return {arm: by[(arm, "owner")]["p50_ms"] / by[(arm, "baseline")]["p50_ms"]
            for arm in ("sequential", "contended", "cold-cache") if (arm, "owner") in by and (arm, "baseline") in by}


def _is_gate_cell(r: Dict) -> bool:
    return r.get("layout") == GATE_LAYOUT and r.get("store_cell") == GATE_CELL


def surface_gate(results: List[Dict]) -> Dict:
    """The A6 verdict for `--hook surface`. The GATE CELL is the REAL-STORE copy on the INSTALLED layout, sequential
    p50 owner/baseline <= 1.0. No other cell can decide it: a result without that cell is `NOT A PASS` whatever the
    synthetic numbers say. Returns {gate_cell_present, passes, ratio, verdict, findings, owed}; `findings` lists
    every arm of every cell where the owner LOSES (> 1.0x); `owed` always names the contended and cold-cache
    arms as owed measurements (reported, not gates). Pure."""
    gate = next((r for r in results if _is_gate_cell(r)), None)
    findings = []
    for r in results:
        for arm, ratio in _arm_ratios(r).items():
            if ratio > 1.0:
                findings.append(f"FINDING: the owner LOSES the {arm} arm on the {r.get('layout', '?')} layout, "
                                f"{r.get('store_cell', '?')} cell: p50 owner/baseline {ratio:.2f}x (> 1.0)")
    owed = ["OWED (Linux seat; reported, not gates): the contended arm (two concurrent clients) and the cold-cache arm, "
            "each against its same-arm direct baseline"]
    if any(r.get("cold_cache_evicts") is False for r in results):
        owed.append("OWED: this host has no os.posix_fadvise, so its cold-cache rows did NOT evict the page cache")
    others = ", ".join(
        f"{r.get('layout', '?')}/{r.get('store_cell', '?')} {_arm_ratios(r)['sequential']:.2f}x"
        for r in results if r is not gate and "sequential" in _arm_ratios(r))
    if gate is None:
        have = ", ".join(f"{r.get('layout', '?')}/{r.get('store_cell', '?')}" for r in results) or "nothing"
        verdict = (f"SURFACE A6 VERDICT -- NO GATE CELL: the {GATE_CELL} copy on the {GATE_LAYOUT} layout was not "
                   f"measured (have: {have}). NOT A PASS: a synthetic-only or source-layout result never counts "
                   "(the store axis flipped the tripwire's gate, #3740).")
        if others:
            verdict += f" Reported, not the gate: {others}."
        return {"gate_cell_present": False, "passes": False, "ratio": None, "verdict": verdict,
                "findings": findings, "owed": owed}
    ratio = _arm_ratios(gate).get("sequential")
    if ratio is None:
        return {"gate_cell_present": False, "passes": False, "ratio": None, "findings": findings, "owed": owed,
                "verdict": "SURFACE A6 VERDICT -- NO GATE CELL: the gate cell has no sequential rows. NOT A PASS."}
    passes = ratio <= 1.0
    sqlite_v = (gate.get("env") or {}).get("sqlite", "?")
    build = (f"SQLite {sqlite_v}" if sqlite_v == SURFACE_EXPECTED_SQLITE else
             f"SQLite {sqlite_v} (expected {SURFACE_EXPECTED_SQLITE}: the build is an axis, this verdict is for "
             f"{sqlite_v} only)")
    verdict = (f"SURFACE A6 VERDICT -- GATE CELL = real-store copy, {GATE_LAYOUT} layout: sequential p50 "
               f"owner/baseline = {ratio:.2f}x -> {'PASS (<= 1.0)' if passes else 'FAIL (> 1.0)'}; {build}.")
    if others:
        verdict += f" Reported, NOT the gate: {others}."
    return {"gate_cell_present": True, "passes": passes, "ratio": ratio, "verdict": verdict,
            "findings": findings, "owed": owed}


def surface_gate_violations(results: List[Dict]) -> List[str]:
    """[] when the gate cell was measured; else ONE violation (exit 3): a run without the real-store copy on the
    installed layout cannot be reported as a pass. Pure."""
    gate = surface_gate(results)
    return [] if gate["gate_cell_present"] else [gate["verdict"]]


def _pctl(sorted_ms: List[float], q: float) -> float:
    return sorted_ms[max(0, math.ceil(q * len(sorted_ms)) - 1)]


def _summarise(arm: str, path: str, ms: List[float]) -> Dict:
    s = sorted(ms)
    return {"arm": arm, "path": path, "n": len(s), "p50_ms": _pctl(s, 0.50),
            "p99_ms": _pctl(s, 0.99), "mean_ms": sum(s) / len(s), "min_ms": s[0]}


def _can_evict_page_cache() -> bool:
    """True where os.posix_fadvise exists (Linux). macOS has none: the cold-cache arm is then
    NOT cold, and the result says so (`cold_cache_evicts`)."""
    return hasattr(os, "posix_fadvise")


def _fadvise_cold(db: Path) -> None:
    if not _can_evict_page_cache():
        return
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            fd = os.open(str(p), os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)


def _timed(fn: Callable[[], object], before: Optional[Callable[[], None]] = None) -> float:
    if before is not None:
        before()
    t0 = time.perf_counter_ns()
    fn()
    return (time.perf_counter_ns() - t0) / 1e6


def _owner_command_line(pid: int) -> bytes:
    """The owner's REAL command line, NUL-separated: /proc where it exists, else the process
    table's own `ps` text (macOS; same fact, whitespace-separated -- the scratch paths this
    tool launches from hold no spaces)."""
    if Path("/proc").is_dir():
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return b""
    return b"\0".join(out.split())


def _runtime_modules_loaded(root: Path, extra_files: List[Path]) -> List[str]:
    """Root-relative paths of the runtime files this process loaded from `root` (modules in
    sys.modules plus `extra_files`, e.g. hook modules that are not registered there). The
    installed layout's RUNTIME_FILES must cover this set."""
    root_r, found = Path(root).resolve(), set()
    candidates = [getattr(m, "__file__", None) for m in list(sys.modules.values())] + [str(f) for f in extra_files]
    for f in candidates:
        if not f:
            continue
        try:
            rp = Path(f).resolve()
        except OSError:
            continue
        if root_r in rp.parents:
            found.add(rp.relative_to(root_r).as_posix())
    return sorted(found)


def measure(n: int = N_FLOOR, nodes: int = 5000, people: int = 5, warmup: int = 30,
            store: Optional[Path] = None, hook: str = "user-identity",
            exclusion: bool = False, tripwire_hook_path: Optional[Path] = None,
            session_start_hook_path: Optional[Path] = None, n_cold: int = N_COLD,
            surface_hook_path: Optional[Path] = None) -> Dict:
    if hook not in HOOKS:
        raise ValueError(f"unknown hook {hook!r}; one of {HOOKS}")
    home = make_test_store(nodes=nodes, people=people, source=store)
    handle = None
    cleanups: List[Callable[[], None]] = []
    saved_env = {k: os.environ.get(k) for k in ("ENGRAM_HOME", "ENGRAM_RUNTIME_DIR")}
    try:
        if hook == "tripwire":
            _seed_tripwire_fixture(home)  # before the owner opens the store
        if hook == "session-start":
            _seed_session_start_fixture(home)
        if hook == "surface":
            _seed_surface_fixture(home)  # before the owner opens the store
        handle = launch_owner(home)
        raw_cmdline = _owner_command_line(handle.proc.pid)
        owner_argv = [a.decode() for a in raw_cmdline.split(b"\0") if a]
        owner_cmdline = raw_cmdline.replace(b"\0", b" ").decode()
        db = home / "knowledge.db"
        if hook == "tripwire":
            hook_file = Path(tripwire_hook_path) if tripwire_hook_path is not None else TRIPWIRE_HOOK_PATH
            baseline, owner, answers_match, answer_size, extra = _bind_tripwire(home, exclusion, hook_file, cleanups)
            shared = extra.pop("_shared")
        elif hook == "surface":
            hook_file = Path(surface_hook_path) if surface_hook_path is not None else SURFACE_HOOK_PATH
            baseline, owner, answers_match, answer_size, extra = _bind_surface(home, hook_file, cleanups)
            shared = extra.pop("_shared")
        elif hook == "session-start":
            hook_file = (Path(session_start_hook_path) if session_start_hook_path is not None
                         else SESSION_START_HOOK_PATH)
            fire_scratch = Path(tempfile.mkdtemp(prefix="a6-ssfire-"))
            cleanups.append(lambda: shutil.rmtree(fire_scratch, ignore_errors=True))
            rig = SessionStartRig(hook_file, home, fire_scratch, SRC_ENGRAM, installed=LAYOUT_NAME == "installed")
            sanity = session_start_sanity(rig)  # raises ArmBlind: a blind arm is an error, never a pass
            baseline, owner = rig.make_paths()
            answers_match, answer_size = sanity["stdout_match"], sanity["stdout_bytes"]
            extra = {k: sanity[k] for k in ("opens_flag_off", "opens_flag_on", "calibration_opens_off",
                                            "calibration_opens_on", "open_violations", "readers_reached_off",
                                            "readers_reached_on", "stdout_match", "flag_seen")}
            extra["runtime_modules_loaded"] = sanity["loaded"]
            shared = None
        else:
            bind = _bind_stop if hook == "stop" else _bind_user_identity
            baseline, owner, answers_match, answer_size, extra = bind(home)
            shared = None
        # per-path hooks of the tripwire fire (plain functions have none): `reset` clears the
        # per-fire state (untimed), `prepare` loads the calling thread's hook module (untimed)
        resets = {id(f): getattr(f, "reset", None) for f in (baseline, owner)}
        for fn in (baseline, owner):
            for _ in range(warmup):
                if resets[id(fn)] is not None:
                    resets[id(fn)]()
                fn()

        rows: List[Dict] = []

        # sequential: interleaved per iteration so drift hits both paths alike
        b, o = [], []
        for _ in range(n):
            b.append(_timed(baseline, before=resets[id(baseline)]))
            o.append(_timed(owner, before=resets[id(owner)]))
        rows += [_summarise("sequential", "baseline", b), _summarise("sequential", "owner", o)]

        # contended: two client threads at once, each path measured on its own
        for path, fn in (("baseline", baseline), ("owner", owner)):
            lat: List[float] = []
            lock = threading.Lock()
            barrier = threading.Barrier(2)

            def worker():
                mine = []
                prepare = getattr(fn, "prepare", None)
                if prepare is not None:
                    prepare()
                barrier.wait()
                for _ in range(n):
                    mine.append(_timed(fn, before=resets[id(fn)]))
                with lock:
                    lat.extend(mine)

            if hasattr(fn, "phase_start"):  # session-start: each path is its own phase (config once)
                fn.clean_each = False
                fn.phase_start()
            threads = [threading.Thread(target=worker) for _ in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            if hasattr(fn, "phase_start"):
                fn.clean_each = True
            rows.append(_summarise("contended", path, lat))

        # cold cache: OS page cache for the store evicted before every call
        b, o = [], []
        def _cold_before(fn):
            reset = resets[id(fn)]

            def before():
                if reset is not None:
                    reset()
                _fadvise_cold(db)
            return before

        for _ in range(n):
            b.append(_timed(baseline, before=_cold_before(baseline)))
            o.append(_timed(owner, before=_cold_before(owner)))
        rows += [_summarise("cold-cache", "baseline", b), _summarise("cold-cache", "owner", o)]
        owner_pid = handle.proc.pid
        if hook == "session-start":
            assert_no_warm_wait(owner.texts, "the timed warm fires")
            state = {"handle": handle}
            try:
                extra["cold"] = _cold_row(rig, home, n_cold, state)
            finally:
                handle = state["handle"]

        _c = sqlite3.connect(str(db))
        node_count = _c.execute("SELECT count(*) FROM nodes").fetchone()[0]
        journal_mode = _c.execute("PRAGMA journal_mode").fetchone()[0]
        _c.close()
        if shared is not None:
            extra["recall_reached"] = bool(shared.recall_reached) and all(shared.recall_reached)
            # probe_dir_live: every owner-path fire (warm-up and timed) found its probe directory
            # present. That is all it shows -- the directory EXISTED. It does not show the
            # exclusion engaged; that is proven by the separate sanity fire in _bind_tripwire
            # (resolver built + the probe's route ledger written).
            if hook == "tripwire":
                extra["probe_dir_live"] = bool(shared.probe_seen) and all(shared.probe_seen)
            extra["runtime_modules_loaded"] = _runtime_modules_loaded(SRC_ENGRAM, [hook_file])
        return {
            "rows": rows, "n": n, "warmup": warmup, "hook": hook,
            "cold_cache_evicts": _can_evict_page_cache(),
            "answers_match": answers_match, "answer_size": answer_size, **extra,
            "owner_pid": owner_pid,
            "owner_cmdline": owner_cmdline, "node_count": node_count,
            "owner_argv": owner_argv, "journal_mode": journal_mode,
            "db_bytes": db.stat().st_size,
            "env": {"platform": platform.platform(), "cpus": os.cpu_count(),
                    "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version},
        }
    finally:
        if handle is not None:
            handle.stop()
        for undo in reversed(cleanups):  # HOME and the stream proxies (tripwire arm)
            undo()
        for k, v in saved_env.items():  # load_hook() repoints the process env at the scratch home
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)


def u8_contract_violations(result: Dict) -> List[str]:
    """The U8 gate (#3710) on one `--hook tripwire` result, as human-readable violations
    ([] = clear). Pure: no I/O, the input is not modified.

      * recall_reached is not True      -- the fire did not reach the in-turn recall path with
                                           candidates, so the exclusion code never ran;
      * stdout_match is not True        -- flag OFF and flag ON printed different bytes;
      * exclusion ON only:
          opens_flag_on is not an int 0 -- the owner path opened the store directly (False and
                                           0.0 are not an int 0; missing counts too);
          probe_dir_live is not True    -- the exclusion probe directory was not live.

    With exclusion OFF the open counts are only reported (the gate is the exclusion-ON cell).
    A result whose `exclusion` is missing or not a bool is itself a violation, and it fails
    CLOSED: the exclusion-ON gates run as if it were ON."""
    out: List[str] = []
    exclusion = result.get("exclusion")
    if not isinstance(exclusion, bool):
        out.append(f"exclusion is missing or not a bool ({exclusion!r}): the exclusion state is unknown, "
                   "so the exclusion-ON gates are applied")
        exclusion = True
    if result.get("recall_reached") is not True:
        out.append("recall_reached is not true: the fire never reached the in-turn recall path "
                   "with candidates (neither exclusion resolver is built)")
    if result.get("stdout_match") is not True:
        out.append("stdout_match is not true: the flag-OFF and flag-ON fires printed different bytes")
    if exclusion:
        opens_on = result.get("opens_flag_on")
        if not (type(opens_on) is int and opens_on == 0):
            out.append(f"opens_flag_on is {result.get('opens_flag_on')!r}, not 0: the flag-ON path "
                       "opened the store directly")
        if result.get("probe_dir_live") is not True:
            out.append("probe_dir_live is not true with exclusion ON: the exclusion probe directory "
                       "was not live during the owner-path fires")
    return out


def _session_start_header_lines(result: Dict) -> List[str]:
    cold = result.get("cold")
    seq = {r["path"]: r for r in result["rows"] if r["arm"] == "sequential"}
    ratio = seq["owner"]["p50_ms"] / seq["baseline"]["p50_ms"]
    lines = [
        f"Contract (allow-list, CALIBRATION_STILL_OPENS={CALIBRATION_STILL_OPENS}): direct opens of the store per "
        f"fire: flag ON {result['opens_flag_on']} (of which calibration-path {result['calibration_opens_on']}), "
        f"flag OFF {result['opens_flag_off']} (calibration-path {result['calibration_opens_off']}); "
        f"unattributed flag-ON opens: {len(result['open_violations'])}; stdout_match={result['stdout_match']}; "
        f"readers reached OFF {len(result['readers_reached_off'])}/{len(SS_READERS)}, "
        f"ON {len(result['readers_reached_on'])}/{len(SS_READERS)}",
        f"GATE cell -- sequential p50 owner/baseline = {ratio:.3f}x "
        f"({'<= 1.0: PASS' if ratio <= 1.0 else '> 1.0: FAIL'}; the merge seat's number is the production build's)",
    ]
    if cold:
        lines.append(
            f"COLD row (reported, not a gate): {cold['n']} fires, owner launched in the same instant as the hook: "
            f"served without a wait {cold['served_without_wait']}/{cold['n']}, wait p50 "
            f"{'-' if cold['wait_ms_p50'] is None else str(cold['wait_ms_p50']) + ' ms'}, max "
            f"{'-' if cold['wait_ms_max'] is None else str(cold['wait_ms_max']) + ' ms'}, hook wall p50 "
            f"{cold['hook_p50_ms']:.0f} ms, owner read failed {cold['owner_failed']}/{cold['n']}")
    return lines


def format_table(result: Dict) -> str:
    env = result["env"]
    stop = result.get("hook") == "stop"
    tripwire = result.get("hook") == "tripwire"
    session_start = result.get("hook") == "session-start"
    surface = result.get("hook") == "surface"
    lines = [
        f"Host: {env['platform']} ({env['cpus']} CPUs) | Python {env['python']} | SQLite {env['sqlite']}",
        f"Store copy: {result['node_count']} nodes, {result['db_bytes']} bytes, "
        f"journal_mode={result.get('journal_mode', '?')}, "
        + (f"{result['answer_count']} current nodes" if stop
           else f"{result['answer_size']} stdout bytes per fire" if (tripwire or session_start or surface)
           else f"{result['people_count']} person rows")
        + f" | N={result['n']} per path per arm "
        f"(contended: 2 threads x N) after {result['warmup']} warm-up calls",
        f"Real owner: pid {result['owner_pid']}, cmdline `{result['owner_cmdline'].strip()}`; "
        + ("owner count and muted decision == baseline: " if stop
           else "owner stdout == baseline stdout: " if (tripwire or surface)
           else "owner stdout == baseline stdout (normalised): " if session_start
           else "owner rows == baseline rows: ")
        + f"{result['answers_match']}",
        *([f"Tripwire fire (exclusion: {'ON' if result['exclusion'] else 'OFF'}): "
           f"probe_dir_live={result['probe_dir_live']} recall_reached={result['recall_reached']} "
           f"stdout_match={result['stdout_match']} | direct store opens per fire: "
           f"opens_flag_on={result['opens_flag_on']} opens_flag_off={result['opens_flag_off']}"] if tripwire else []),
        *([f"Surface fire: recall_reached={result['recall_reached']} stdout_match={result['stdout_match']} | direct store "
           f"opens per fire: opens_flag_on={result['opens_flag_on']} (of which the named remainder, "
           f"{SURFACE_REMAINDER_FUNC}: {result.get('remainder_opens_flag_on', '?')}; unattributed: "
           f"{len(result['open_violations'])}) opens_flag_off={result['opens_flag_off']}"] if surface else []),
        *(_session_start_header_lines(result) if session_start else []),
        "",
        "| arm | path | p50 ms | p99 ms | mean ms | p50 owner/baseline | p99 owner/baseline |",
        "|---|---|---|---|---|---|---|",
    ]
    by = {(r["arm"], r["path"]): r for r in result["rows"]}
    for r in result["rows"]:
        if r["path"] == "owner":
            base = by[(r["arm"], "baseline")]
            p50x, p99x = f"{r['p50_ms'] / base['p50_ms']:.2f}x", f"{r['p99_ms'] / base['p99_ms']:.2f}x"
        else:
            p50x = p99x = "-"
        lines.append(f"| {r['arm']} | {r['path']} | {r['p50_ms']:.3f} | {r['p99_ms']:.3f} | "
                     f"{r['mean_ms']:.3f} | {p50x} | {p99x} |")
    lines += [
        "",
        ("Notes: every value is the wall time of one FRESH process (interpreter start and imports INCLUDED; "
         "the contended arm runs two such processes at once per path). Cold-cache evicts the OS page cache "
         "only; the owner's in-process SQLite cache stays warm. Ratios do not survive across SQLite builds."
         if session_start else
         "Notes: function-level timings (interpreter start and imports excluded; one per hook invocation "
         "on either path). Contended threads share one GIL (both paths alike). Cold-cache evicts the OS "
         "page cache only; the owner's in-process SQLite cache stays warm. Ratios do not survive across "
         "SQLite builds. Owner path includes the per-call runtime-dir resolution and client round trip."),
    ]
    if result.get("cold_cache_evicts") is False:
        lines.append(
            "NOTE: this host has no os.posix_fadvise (macOS): the cold-cache arm did NOT evict the page "
            "cache and measures a warm cache; do not read it as cold.")
    if tripwire:
        lines.append(
            "Tripwire hook (U6b 2c-2, #3665): ONE FULL FIRE of main() on a fixed payload, all four "
            "store-read sites executing (baseline = engramd.lesson_tripwire_reads_via_owner OFF, direct "
            "opens; owner = flag ON, a real owner). The recall daemon's answer is a fixed stub at "
            "`_query_recall_daemon`, identical on both paths. Per-fire state files are reset between "
            "fires (untimed). The contended arm runs a separate hook module per thread. "
            "opens_flag_* count DIRECT opens of the store copy during one fire.")
    if surface:
        lines.append(
            "Surface hook (#3821, part of #2652): ONE FULL FIRE of main() on a fixed UserPromptSubmit payload "
            f"(baseline = engramd.{SURFACE_FLAG_KEY} OFF, direct opens; owner = flag ON, a real owner). The recall "
            "daemon's answer is a fixed stub at `query_daemon`, identical on both paths, so neither path takes the "
            "in-hook FTS fallback. Per-fire state files are cleared between fires (untimed). The contended arm runs a "
            "separate hook module per thread. opens_flag_* count DIRECT opens of the store copy during one fire; the "
            "flag-ON allow-list is the person-moment cursor write (the named remainder) and nothing else. GATE (the "
            f"merge seat runs it on the production build, SQLite {SURFACE_EXPECTED_SQLITE}): the REAL-STORE copy on "
            "the INSTALLED layout, sequential p50 owner/baseline <= 1.0; a synthetic store is reported but never "
            "decides. The contended and cold-cache arms are owed measurements, reported not gated; an arm the owner "
            "loses is a stated finding.")
    if session_start:
        lines.append(
            "Session-start hook (U9 #3735, A6 arm #3747): every timed fire is a FRESH interpreter running the hook "
            "on a SessionStart JSON (the dominant cost is the interpreter, the imports and the calibration "
            "block's EngramClient, which an in-process call would hide), in a scratch HOME/TMPDIR/ENGRAM_HOME. "
            "baseline = engramd.session_start_reads_via_owner OFF (six direct store reads); owner = flag ON with "
            "a REAL owner already up (WARM). One home serves both paths: the config file is rewritten (untimed) "
            "before each timed fire of the sequential and cold-cache arms, once per phase in the contended arm "
            "(two concurrent fires per path). Per-fire files are cleaned between fires (untimed). The contract "
            "line above is the design seat's allow-list (#3747): with CALIBRATION_STILL_OPENS true every flag-ON "
            f"open of the store must carry an `{CALIBRATION_FRAME_BASENAME}` frame (the calibration block, before "
            f"#3741 migrated it); now CALIBRATION_STILL_OPENS={CALIBRATION_STILL_OPENS}, so the allow-list is "
            "empty and the gate is opens_flag_on == 0; flag-OFF calibration opens are counted, never gated "
            "on their value. The COLD row is reported, not a gate: the owner launcher and the hook start in the "
            "same instant, with the wait read from the hook's own notice. GATE (the merge seat runs it on the "
            "production build): sequential p50 owner/baseline <= 1.0; every other row is reported.")
    if stop:
        lines.append(
            "Stop hook (U6a, #3664): both paths are timed through the hook's own `_maturity_muted()` "
            "(baseline = flag OFF, direct connect_regular_ro + SELECT; owner = flag ON, "
            "engramd_client.read); the threshold read is common to both. The gate is the "
            "sequential arm only; the contended and cold-cache arms are reported, not gates, and "
            "their Linux-seat numbers are owed in the PR body.")
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------------

def _is_live_store(path: Path) -> bool:
    live = {Path(os.environ.get("ENGRAM_HOME") or (Path.home() / ".engram")) / "knowledge.db",
            Path.home() / ".engram" / "knowledge.db"}
    target = path.resolve()
    return any(target == p.resolve() for p in live)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer > 0 (got {text!r})")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be an integer > 0 (got {value})")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="A6 latency delta for a hook's owner read (user-identity: U4d, #3197; stop: U6a, #3664; "
                    "tripwire: U6b/U8; session-start: U9, #3747).")
    p.add_argument("--hook", choices=HOOKS, default="user-identity",
                   help="which hook's owner read to measure (default: user-identity)")
    p.add_argument("--exclusion", choices=("off", "on"), default=None,
                   help="(--hook tripwire only) the in-context exclusion: 'on' sets "
                        "recall_in_context_exclusion.enabled and makes the probe dir live (default: off)")
    p.add_argument("--real-store", type=Path, default=None, metavar="PATH",
                   help="(--hook surface only) the store to COPY for the real-store (gate) cell: a read-only backup "
                        "into a scratch directory, never measured or written in place (default: the seat's own "
                        "$ENGRAM_HOME/knowledge.db)")
    p.add_argument("--no-real-store", action="store_true",
                   help="(--hook surface only) measure the synthetic cell alone: reported, but there is no gate "
                        "cell, so the run is never a pass (exit 3)")
    p.add_argument("--n", type=int, default=N_FLOOR)
    p.add_argument("--n-cold", type=int, default=N_COLD,
                   help=f"(--hook session-start only) cold-row fires (default {N_COLD}; reported, not a gate)")
    p.add_argument("--nodes", type=int, default=5000)
    p.add_argument("--people", type=int, default=5)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--store", type=Path, default=None,
                   help="a test store to COPY (never the live store); default: a synthetic one")
    p.add_argument("--layout", choices=("both",) + LAYOUTS, default="both",
                   help="which code layout to measure (#3678): 'installed' builds the plugin from this "
                        "tree into a scratch dir and is the gate cell; 'source' is the checkout; "
                        "default both")
    p.add_argument("--layout-timeout", type=_positive_int, default=DEFAULT_LAYOUT_TIMEOUT, metavar="SECONDS",
                   help=f"per-layout child time limit in seconds, an int > 0 (default {DEFAULT_LAYOUT_TIMEOUT}); "
                        "the session-start arm on a large store can need more")
    p.add_argument("--_layout-child", dest="layout_child", default=None, help=argparse.SUPPRESS)
    p.add_argument("--_kwargs", dest="child_kwargs", default="{}", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.exclusion is not None and args.hook != "tripwire":
        p.error("--exclusion is valid only with --hook tripwire")
    if args.hook != "surface" and (args.real_store is not None or args.no_real_store):
        p.error("--real-store and --no-real-store are valid only with --hook surface")
    if args.hook == "surface" and args.store is not None:
        p.error("--store is not used by --hook surface (it measures a synthetic store AND a real-store copy); "
                "name the copy source with --real-store")
    if args.real_store is not None and args.no_real_store:
        p.error("--real-store and --no-real-store are mutually exclusive")
    if args.layout_child:
        return _layout_child(args.layout_child, args.child_kwargs)
    if args.n < N_FLOOR:
        print(f"engramd_a6_latency: error -- N must be >= {N_FLOOR} (got {args.n})", file=sys.stderr)
        return 2
    if args.store is not None and _is_live_store(args.store):
        print(f"engramd_a6_latency: error -- {args.store} is this seat's live store; "
              "refusing. Pass a COPY of a test store.", file=sys.stderr)
        return 2
    kwargs = dict(n=args.n, nodes=args.nodes, people=args.people, warmup=args.warmup,
                  store=args.store, hook=args.hook, exclusion=(args.exclusion == "on"))
    if args.hook == "session-start":
        kwargs["n_cold"] = args.n_cold
    # the cells of the run: one (store as given) for every hook but surface, which measures a synthetic store and a
    # COPY of a real store (the gate cell), both on every layout
    cells = [(None, args.store)]
    real_home = None
    if args.hook == "surface":
        cells = [("synthetic", None)]
        if not args.no_real_store:
            real_src = args.real_store or (Path(os.environ.get("ENGRAM_HOME") or (Path.home() / ".engram"))
                                           / "knowledge.db")
            if not real_src.is_file():
                print(f"engramd_a6_latency: error -- the real-store source {real_src} is not a file; the real-store "
                      "copy is the GATE cell. Name one with --real-store PATH (or --no-real-store for a synthetic-only "
                      "run, which is never a pass).", file=sys.stderr)
                return 2
            real_home = make_test_store(source=real_src)   # a consistent read-only backup copy; the source is only read
            cells.append(("real-store", real_home / "knowledge.db"))
    wanted = LAYOUTS if args.layout == "both" else (args.layout,)
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="a6-build-") as build_dir:
            for name in ("installed", "source"):
                if name not in wanted:
                    continue
                try:  # the build step raises LayoutViolation too (layout_matches_tree): same exit 4
                    try:  # the build has its OWN fixed limit; --layout-timeout must not be blamed for it
                        layout = build_installed_layout(Path(build_dir) / "plugin") if name == "installed" else source_layout()
                    except subprocess.TimeoutExpired as exc:
                        print(f"engramd_a6_latency: error -- the plugin build step for layout '{name}' exceeded "
                              f"its fixed {int(exc.timeout)}s limit (no number produced)", file=sys.stderr)
                        return 4
                    for cell, store in cells:
                        result = measure_in_layout(layout, layout_timeout=args.layout_timeout,
                                                   **dict(kwargs, store=store))
                        if cell is not None:
                            result["store_cell"] = cell
                        results.append(result)
                except subprocess.TimeoutExpired:
                    print(f"engramd_a6_latency: error -- layout '{name}' exceeded --layout-timeout "
                          f"{args.layout_timeout}s (session-start on a large store can need more; "
                          "pass --layout-timeout)", file=sys.stderr)
                    return EXIT_LAYOUT_TIMEOUT
                except (ArmBlind, LayoutViolation) as exc:
                    print(f"engramd_a6_latency: BLIND ARM ({type(exc).__name__}) -- no number produced: "
                          f"{' '.join(str(exc).split())[:600]}", file=sys.stderr)
                    return 4
    finally:
        if real_home is not None:
            shutil.rmtree(real_home, ignore_errors=True)
    print(format_layouts(results))
    if args.hook == "surface":
        gate = surface_gate(results)
        print("\n" + "\n".join([gate["verdict"], *gate["findings"], *gate["owed"]]))
        violations = [f"[{r.get('layout', '?')} layout, {r.get('store_cell', '?')} cell] {v}"
                      for r in results for v in surface_contract_violations(r)] + surface_gate_violations(results)
        if violations:
            print("\nSurface contract violations:\n" + "\n".join(f"  - {v}" for v in violations))
            return 3
    if args.hook == "tripwire":
        violations = [f"[{r.get('layout', '?')} layout] {v}" for r in results for v in u8_contract_violations(r)]
        if violations:
            print("\nU8 contract violations:\n" + "\n".join(f"  - {v}" for v in violations))
            return 3
    if args.hook == "session-start":
        violations = [f"[{r.get('layout', '?')} layout] {v}" for r in results for v in ss_contract_violations(r)]
        if violations:
            print("\nSession-start contract violations:\n" + "\n".join(f"  - {v}" for v in violations))
            return 3
    # (so exit 1 never occurs for --hook tripwire, session-start or surface: a differing stdout -- answers_match
    # False -- is a stdout_match violation and has already returned 3 above)
    return 0 if all(r["answers_match"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
