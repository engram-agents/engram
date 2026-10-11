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
Exit 0 ok; 2 bad input (N below the 300 floor, or --store is the live store).
"""
from __future__ import annotations

import argparse
import importlib.util
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
from typing import Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-user-identity-hook.py"
STOP_HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-stop-hook.py"
HOOKS = ("user-identity", "stop")  # --hook; the default stays user-identity
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
}


class LayoutViolation(RuntimeError):
    """An installed-layout run did not measure the installed layout."""


class Layout:
    """A code layout to measure. A plain class, not a dataclass: this tool is
    loaded by file path (spec_from_file_location) by its own tests and by the
    stage profiler, and a dataclass needs its module registered in sys.modules."""

    def __init__(self, name: str, root: Path, stop_hook: Path, user_hook: Path) -> None:
        self.name = name
        self.root = Path(root)            # the directory holding engramd_owner.py / engramd_client.py
        self.stop_hook = Path(stop_hook)
        self.user_hook = Path(user_hook)

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "root": str(self.root),
                           "stop_hook": str(self.stop_hook), "user_hook": str(self.user_hook)})

    @staticmethod
    def from_json(text: str) -> "Layout":
        d = json.loads(text)
        return Layout(name=d["name"], root=Path(d["root"]),
                      stop_hook=Path(d["stop_hook"]), user_hook=Path(d["user_hook"]))


def source_layout() -> Layout:
    return Layout(name="source", root=SRC_ENGRAM, stop_hook=STOP_HOOK_PATH, user_hook=HOOK_PATH)


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
                    user_hook=out_dir / "hooks" / "engram-user-identity-hook.py")
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
    global SRC_ENGRAM, STOP_HOOK_PATH, HOOK_PATH
    SRC_ENGRAM, STOP_HOOK_PATH, HOOK_PATH = layout.root, layout.stop_hook, layout.user_hook


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

    def counting_path(*args, **kwargs):
        p = real_path(*args, **kwargs)
        return _Probe(p) if str(p) == me else p

    _hooklib.Path = counting_path
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


# --- timing ----------------------------------------------------------------------

def _pctl(sorted_ms: List[float], q: float) -> float:
    return sorted_ms[max(0, math.ceil(q * len(sorted_ms)) - 1)]


def _summarise(arm: str, path: str, ms: List[float]) -> Dict:
    s = sorted(ms)
    return {"arm": arm, "path": path, "n": len(s), "p50_ms": _pctl(s, 0.50),
            "p99_ms": _pctl(s, 0.99), "mean_ms": sum(s) / len(s), "min_ms": s[0]}


def _fadvise_cold(db: Path) -> None:
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


def measure(n: int = N_FLOOR, nodes: int = 5000, people: int = 5, warmup: int = 30,
            store: Optional[Path] = None, hook: str = "user-identity") -> Dict:
    if hook not in HOOKS:
        raise ValueError(f"unknown hook {hook!r}; one of {HOOKS}")
    home = make_test_store(nodes=nodes, people=people, source=store)
    handle = None
    saved_env = {k: os.environ.get(k) for k in ("ENGRAM_HOME", "ENGRAM_RUNTIME_DIR")}
    try:
        handle = launch_owner(home)
        raw_cmdline = Path(f"/proc/{handle.proc.pid}/cmdline").read_bytes() if Path("/proc").is_dir() else b""
        owner_argv = [a.decode() for a in raw_cmdline.split(b"\0") if a]
        owner_cmdline = raw_cmdline.replace(b"\0", b" ").decode()
        db = home / "knowledge.db"
        bind = _bind_stop if hook == "stop" else _bind_user_identity
        baseline, owner, answers_match, answer_size, extra = bind(home)
        for _ in range(warmup):
            baseline()
            owner()

        rows: List[Dict] = []

        # sequential: interleaved per iteration so drift hits both paths alike
        b, o = [], []
        for _ in range(n):
            b.append(_timed(baseline))
            o.append(_timed(owner))
        rows += [_summarise("sequential", "baseline", b), _summarise("sequential", "owner", o)]

        # contended: two client threads at once, each path measured on its own
        for path, fn in (("baseline", baseline), ("owner", owner)):
            lat: List[float] = []
            lock = threading.Lock()
            barrier = threading.Barrier(2)

            def worker():
                mine = []
                barrier.wait()
                for _ in range(n):
                    mine.append(_timed(fn))
                with lock:
                    lat.extend(mine)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            rows.append(_summarise("contended", path, lat))

        # cold cache: OS page cache for the store evicted before every call
        b, o = [], []
        for _ in range(n):
            b.append(_timed(baseline, before=lambda: _fadvise_cold(db)))
            o.append(_timed(owner, before=lambda: _fadvise_cold(db)))
        rows += [_summarise("cold-cache", "baseline", b), _summarise("cold-cache", "owner", o)]

        _c = sqlite3.connect(str(db))
        node_count = _c.execute("SELECT count(*) FROM nodes").fetchone()[0]
        journal_mode = _c.execute("PRAGMA journal_mode").fetchone()[0]
        _c.close()
        return {
            "rows": rows, "n": n, "warmup": warmup, "hook": hook,
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
        for k, v in saved_env.items():  # load_hook() repoints the process env at the scratch home
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)


def format_table(result: Dict) -> str:
    env = result["env"]
    stop = result.get("hook") == "stop"
    lines = [
        f"Host: {env['platform']} ({env['cpus']} CPUs) | Python {env['python']} | SQLite {env['sqlite']}",
        f"Store copy: {result['node_count']} nodes, {result['db_bytes']} bytes, "
        f"journal_mode={result.get('journal_mode', '?')}, "
        + (f"{result['answer_count']} current nodes" if stop else f"{result['people_count']} person rows")
        + f" | N={result['n']} per path per arm "
        f"(contended: 2 threads x N) after {result['warmup']} warm-up calls",
        f"Real owner: pid {result['owner_pid']}, cmdline `{result['owner_cmdline'].strip()}`; "
        + ("owner count and muted decision == baseline: " if stop else "owner rows == baseline rows: ")
        + f"{result['answers_match']}",
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
                  store=args.store, hook=args.hook)
    wanted = LAYOUTS if args.layout == "both" else (args.layout,)
    results = []
    with tempfile.TemporaryDirectory(prefix="a6-build-") as build_dir:
        for name in ("installed", "source"):
            if name not in wanted:
                continue
            layout = build_installed_layout(Path(build_dir) / "plugin") if name == "installed" else source_layout()
            results.append(measure_in_layout(layout, **kwargs))
    print(format_layouts(results))
    return 0 if all(r["answers_match"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
