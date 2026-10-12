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
                                       [--warmup 30] [--store PATH]
Exit 0 ok; 1 the two paths answered differently; 2 bad input (N below the 300 floor,
--store is the live store, or --exclusion without --hook tripwire); 3 (--hook tripwire
only) a U8 contract violation (see u8_contract_violations). Exit 1 does NOT occur for
`--hook tripwire`: the two paths answering differently is a differing stdout, which is itself
a U8 violation, and exit 3 outranks exit 1.

`--hook tripwire` (U6b stage 2c-2, #3665; the U8 gate, #3710) measures ONE FULL FIRE of the
lesson-tripwire hook's main() on a fixed payload -- all four of its store-read sites run --
baseline = the engramd.lesson_tripwire_reads_via_owner flag OFF (the hook opens the store),
owner = flag ON with a REAL owner process. `--exclusion on` turns the in-context exclusion
on (config flag + a live probe directory under the hook process's HOME). The result also
carries the direct store opens counted per fire (the gate quantity is `opens_flag_on == 0`).
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote_to_bytes
from typing import Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-user-identity-hook.py"
STOP_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-stop-hook.py"
TRIPWIRE_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"
HOOKS = ("user-identity", "stop", "tripwire")  # --hook; the default stays user-identity
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
# build-relative path -> source-relative path: the runtime files a measurement
# loads. Byte-identical between the build and the tree is what makes the
# installed cell a measurement of THIS tree.
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
    "engram_search_rules.py": "src/engram/engram_search_rules.py",
    "tools/__init__.py": "src/engram/tools/__init__.py",
    "tools/recall_summary_validator.py": "src/engram/tools/recall_summary_validator.py",
}


class LayoutViolation(RuntimeError):
    """An installed-layout run did not measure the installed layout."""


class Layout:
    """A code layout to measure. A plain class, not a dataclass: this tool is
    loaded by file path (spec_from_file_location) by its own tests and by the
    stage profiler, and a dataclass needs its module registered in sys.modules."""

    def __init__(self, name: str, root: Path, stop_hook: Path, user_hook: Path,
                 tripwire_hook: Optional[Path] = None) -> None:
        self.name = name
        self.root = Path(root)            # the directory holding engramd_owner.py / engramd_client.py
        self.stop_hook = Path(stop_hook)
        self.user_hook = Path(user_hook)
        # the lesson-tripwire hook (#3665); beside the stop hook when not named
        self.tripwire_hook = (Path(tripwire_hook) if tripwire_hook is not None
                              else self.stop_hook.parent / "engram-lesson-tripwire-hook.py")

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "root": str(self.root),
                           "stop_hook": str(self.stop_hook), "user_hook": str(self.user_hook),
                           "tripwire_hook": str(self.tripwire_hook)})

    @staticmethod
    def from_json(text: str) -> "Layout":
        d = json.loads(text)
        return Layout(name=d["name"], root=Path(d["root"]),
                      stop_hook=Path(d["stop_hook"]), user_hook=Path(d["user_hook"]),
                      tripwire_hook=Path(d["tripwire_hook"]) if d.get("tripwire_hook") else None)


def source_layout() -> Layout:
    return Layout(name="source", root=SRC_ENGRAM, stop_hook=STOP_HOOK_PATH, user_hook=HOOK_PATH,
                  tripwire_hook=TRIPWIRE_HOOK_PATH)


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
                    tripwire_hook=out_dir / "hooks" / "engram-lesson-tripwire-hook.py")
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
    global SRC_ENGRAM, STOP_HOOK_PATH, HOOK_PATH, TRIPWIRE_HOOK_PATH
    SRC_ENGRAM, STOP_HOOK_PATH, HOOK_PATH = layout.root, layout.stop_hook, layout.user_hook
    TRIPWIRE_HOOK_PATH = layout.tripwire_hook


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


def measure_in_layout(layout: Layout, **kwargs) -> Dict:
    """Run measure() for `layout` in a FRESH process (no runtime module from
    another layout can be cached), return its result. An installed-layout
    result is checked by check_installed_run() before it is returned."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--_layout-child", layout.to_json(),
         "--_kwargs", json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in kwargs.items()})],
        capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"{layout.name}-layout run failed (rc {proc.returncode}): {proc.stderr[-1500:]}")
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


def format_layouts(results: List[Dict]) -> str:
    order = {"installed": 0, "source": 1}
    parts = []
    for r in sorted(results, key=lambda r: order.get(r.get("layout"), 9)):
        parts.append(f"### {LAYOUT_LABELS[r['layout']]}\n"
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
    except Exception:
        return


def _ensure_audit_hook() -> None:
    global _audit_hook_installed
    with _AUDIT_HOOK_LOCK:
        if not _audit_hook_installed:
            sys.addaudithook(_audit_open_hook)
            _audit_hook_installed = True


@contextlib.contextmanager
def _counting_store_opens(db: Path):
    """Count EVERY open of the store file `db` made by any route, in any thread, while this
    window is active: the `sqlite3.connect` audit event fires for sqlite3.connect,
    sqlite3.Connection(...), _sqlite3.connect, an early-bound `from sqlite3 import connect` and
    connect_regular_ro (which calls sqlite3.connect once inside), so each open is counted exactly
    once with no wrapping. The match is the realpath of the argument (see _store_path_of) against
    the realpath of the store copy: opens of other files (a pack DB, the probe) and `:memory:` are
    ignored. Opens outside the window are not counted. Yields the record list."""
    _ensure_audit_hook()
    window = {"target": os.path.realpath(str(db)), "record": []}
    _ACTIVE_WINDOWS.append(window)
    try:
        yield window["record"]
    finally:
        _ACTIVE_WINDOWS.remove(window)


def _bind_tripwire(home: Path, exclusion: bool, hook_path: Path, cleanups: List[Callable[[], None]]):
    """-> (baseline_fn, owner_fn, answers_match, answer_size, extra_result_keys).

    HOME is pointed at a scratch directory for the whole arm: the exclusion probe is live
    when `$HOME/.engram/probe/mods-recall/<session_id>/` exists, and `route_probe_dir()` uses
    `Path.home()` of the HOOK PROCESS, not ENGRAM_HOME. With exclusion ON that directory is
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
    probe_root = scratch_home / ".engram" / "probe" / "mods-recall"

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


# --- timing ----------------------------------------------------------------------

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
            exclusion: bool = False, tripwire_hook_path: Optional[Path] = None) -> Dict:
    if hook not in HOOKS:
        raise ValueError(f"unknown hook {hook!r}; one of {HOOKS}")
    home = make_test_store(nodes=nodes, people=people, source=store)
    handle = None
    cleanups: List[Callable[[], None]] = []
    saved_env = {k: os.environ.get(k) for k in ("ENGRAM_HOME", "ENGRAM_RUNTIME_DIR")}
    try:
        if hook == "tripwire":
            _seed_tripwire_fixture(home)  # before the owner opens the store
        handle = launch_owner(home)
        raw_cmdline = _owner_command_line(handle.proc.pid)
        owner_argv = [a.decode() for a in raw_cmdline.split(b"\0") if a]
        owner_cmdline = raw_cmdline.replace(b"\0", b" ").decode()
        db = home / "knowledge.db"
        if hook == "tripwire":
            hook_file = Path(tripwire_hook_path) if tripwire_hook_path is not None else TRIPWIRE_HOOK_PATH
            baseline, owner, answers_match, answer_size, extra = _bind_tripwire(home, exclusion, hook_file, cleanups)
            shared = extra.pop("_shared")
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

            threads = [threading.Thread(target=worker) for _ in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
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
            extra["probe_dir_live"] = bool(shared.probe_seen) and all(shared.probe_seen)
            extra["runtime_modules_loaded"] = _runtime_modules_loaded(SRC_ENGRAM, [hook_file])
        return {
            "rows": rows, "n": n, "warmup": warmup, "hook": hook,
            "cold_cache_evicts": _can_evict_page_cache(),
            "answers_match": answers_match, "answer_size": answer_size, **extra,
            "owner_pid": handle.proc.pid,
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


def format_table(result: Dict) -> str:
    env = result["env"]
    stop = result.get("hook") == "stop"
    tripwire = result.get("hook") == "tripwire"
    lines = [
        f"Host: {env['platform']} ({env['cpus']} CPUs) | Python {env['python']} | SQLite {env['sqlite']}",
        f"Store copy: {result['node_count']} nodes, {result['db_bytes']} bytes, "
        f"journal_mode={result.get('journal_mode', '?')}, "
        + (f"{result['answer_count']} current nodes" if stop
           else f"{result['answer_size']} stdout bytes per fire" if tripwire
           else f"{result['people_count']} person rows")
        + f" | N={result['n']} per path per arm "
        f"(contended: 2 threads x N) after {result['warmup']} warm-up calls",
        f"Real owner: pid {result['owner_pid']}, cmdline `{result['owner_cmdline'].strip()}`; "
        + ("owner count and muted decision == baseline: " if stop
           else "owner stdout == baseline stdout: " if tripwire else "owner rows == baseline rows: ")
        + f"{result['answers_match']}",
        *([f"Tripwire fire (exclusion: {'ON' if result['exclusion'] else 'OFF'}): "
           f"probe_dir_live={result['probe_dir_live']} recall_reached={result['recall_reached']} "
           f"stdout_match={result['stdout_match']} | direct store opens per fire: "
           f"opens_flag_on={result['opens_flag_on']} opens_flag_off={result['opens_flag_off']}"] if tripwire else []),
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
        "Notes: function-level timings (interpreter start and imports excluded; one per hook invocation "
        "on either path). Contended threads share one GIL (both paths alike). Cold-cache evicts the OS "
        "page cache only; the owner's in-process SQLite cache stays warm. Ratios do not survive across "
        "SQLite builds. Owner path includes the per-call runtime-dir resolution and client round trip.",
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


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="A6 latency delta for a hook's owner read (user-identity: U4d, #3197; stop: U6a, #3664).")
    p.add_argument("--hook", choices=HOOKS, default="user-identity",
                   help="which hook's owner read to measure (default: user-identity)")
    p.add_argument("--exclusion", choices=("off", "on"), default=None,
                   help="(--hook tripwire only) the in-context exclusion: 'on' sets "
                        "recall_in_context_exclusion.enabled and makes the probe dir live (default: off)")
    p.add_argument("--n", type=int, default=N_FLOOR)
    p.add_argument("--nodes", type=int, default=5000)
    p.add_argument("--people", type=int, default=5)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--store", type=Path, default=None,
                   help="a test store to COPY (never the live store); default: a synthetic one")
    p.add_argument("--layout", choices=("both",) + LAYOUTS, default="both",
                   help="which code layout to measure (#3678): 'installed' builds the plugin from this "
                        "tree into a scratch dir and is the gate cell; 'source' is the checkout; "
                        "default both")
    p.add_argument("--_layout-child", dest="layout_child", default=None, help=argparse.SUPPRESS)
    p.add_argument("--_kwargs", dest="child_kwargs", default="{}", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.exclusion is not None and args.hook != "tripwire":
        p.error("--exclusion is valid only with --hook tripwire")
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
    wanted = LAYOUTS if args.layout == "both" else (args.layout,)
    results = []
    with tempfile.TemporaryDirectory(prefix="a6-build-") as build_dir:
        for name in ("installed", "source"):
            if name not in wanted:
                continue
            layout = build_installed_layout(Path(build_dir) / "plugin") if name == "installed" else source_layout()
            results.append(measure_in_layout(layout, **kwargs))
    print(format_layouts(results))
    if args.hook == "tripwire":
        violations = [f"[{r.get('layout', '?')} layout] {v}" for r in results for v in u8_contract_violations(r)]
        if violations:
            print("\nU8 contract violations:\n" + "\n".join(f"  - {v}" for v in violations))
            return 3
    # (so exit 1 never occurs for --hook tripwire: a differing stdout -- answers_match False -- is
    # a stdout_match violation and has already returned 3 above)
    return 0 if all(r["answers_match"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
