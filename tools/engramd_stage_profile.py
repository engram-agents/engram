#!/usr/bin/env python3
"""tools/engramd_stage_profile.py -- the U7 stage profiler for the engramd owner
round trip (#3678, part of #2652; docs/internal/2652-engrammanager-design.md SS6.1).

It ATTRIBUTES the time of one `engramd_client.read("read", ...)` across named
stages. It does not set the level: the wrappers it installs cost time of their
own, so every cut's before/after and the U7 target are judged by the UNWRAPPED
A6 run (tools/engramd_a6_latency.py). This tool reports an unwrapped end-to-end
from the same run beside its wrapped one, so its own overhead is a number.

How it times, without a profile mode in the owner:
  * the OWNER side is a real owner process started by this file's launcher mode
    (`--serve-owner`), which loads src/engram/engramd_owner.py, wraps named
    owner callables with timers, then runs the owner exactly as `main()` would.
    Its command line carries the owner module path, so `engramd_owner` is in it
    (#3285). Each request's raw stamps go to a records file, one JSON line each.
  * the CLIENT side is timed in this process by wrapping engramd_client's own
    module attributes (socket path resolution, socket creation, connect, send,
    receive, JSON encode/decode).
  * both sides stamp time.monotonic(); on Linux CLOCK_MONOTONIC is one clock
    for the whole host, so the stamps compare across the two processes.

How a call is divided (computed per call, after the run):
  * client stages run from the call's start until the request is fully sent,
    and the response decode runs after the reply is received;
  * owner and between stages count only time AFTER the client finished
    sending. Before that the owner is waiting on the client (its request read
    blocks on the socket), and that wait is the client's time, not the owner's;
  * a nested owner stage's time is EXCLUSIVE: a parent's own time excludes the
    stages inside it, so no millisecond is counted twice;
  * `unattributed` = the independently timed end-to-end call minus the sum of
    all stages. It is reported, never spread across stages.

Name-wrapping only times calls that go through the wrapped attribute. A call
made through a local alias or a bound method captured before the owner starts
is missed silently. The per-stage injected-delay test in
tests/test_engramd_stage_profile_3678.py is the check: re-run it after any cut
that moves or renames a stage.

    python tools/engramd_stage_profile.py [--n 1000] [--nodes 5000] [--warmup 50]
                                          [--store PATH] [--hook stop|user-identity]
Exit 0 ok; 2 bad input (N below the 1000 floor, --store is the live store, or --store is missing /
unusable: a FIFO, directory, symlink loop or unsearchable parent; refused before any build or temp dir).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
OWNER_MODULE = SRC_ENGRAM / "engramd_owner.py"
A6_TOOL = REPO_ROOT / "tools" / "engramd_a6_latency.py"
N_FLOOR = 1000

CLIENT_STAGES = (
    "client_import",     # the hook's _import_engramd_client (runtime-dir resolution, sys.path, import)
    "resolve_path",      # engramd_client._resolve_socket_path
    "socket_create",     # socket.socket(AF_UNIX, SOCK_STREAM)
    "connect",           # sock.connect
    "request_encode",    # json.dumps of the request
    "request_send",      # sock.sendall
    "response_decode",   # json.loads of the reply
)
OWNER_STAGES = (
    "thread_handoff",    # accept loop builds the per-connection thread -> handler entry
    "peer_credentials",  # EngramdOwner._check_peer_credentials
    "request_read",      # EngramdOwner._read_request_line (after the client finished sending)
    "request_decode",    # json.loads of the request
    "version_check",     # engramd_client.check_contract_version (owner side)
    "walguard",          # engram_walguard.assert_not_displaced
    "registry_check",    # engramd_read_registry.Registry.check
    "arm_deadline",      # engramd_owner._arm_read_deadline
    "execute",           # _ClientReadGate._execute_admitted, excl. arm_deadline
    "read_handler",      # EngramdOwner._handle_read, excl. its children (lock, row checks)
    "response_encode",   # a read reply: _render_read_rows (row checks, one dumps per row,
                         # assembly); any other reply: json.dumps at the send
    "response_send",     # conn.sendall of the reply
    "owner_other",       # handler + dispatch code not in a named stage
)
BETWEEN_STAGES = (
    "accept_wakeup",     # client connected -> owner accept returned
    "response_delivery", # owner finished sending -> client has the whole reply
)
STAGES = CLIENT_STAGES + OWNER_STAGES + BETWEEN_STAGES
INJECTABLE_STAGES = CLIENT_STAGES + OWNER_STAGES
# A delay inside the end-to-end timer but outside every stage: it exists only to
# prove end-to-end is timed on its own and the remainder is real.
UNNAMED_GAP = "unnamed_gap"


def _inject_into_function(modules: Dict[str, object], target: str, delay: float) -> None:
    """Patch a delay INTO a real callable named "module:attr[.attr]", underneath
    any stage wrapper (call this before the wrappers go on). It is the
    independent half of the discrimination check: a stage-named injection rides
    the stage's own wrapper, so it cannot catch a wrapper put on the wrong
    function; this one names the function, so a mislabelled wrapper reports the
    delay under the wrong stage."""
    mod_name, _, attr_path = target.partition(":")
    obj = modules[mod_name]
    parts = attr_path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    real = getattr(obj, parts[-1])

    def delayed(*a, **k):
        time.sleep(delay)
        return real(*a, **k)
    setattr(obj, parts[-1], delayed)


def _ensure_src_importable() -> None:
    if str(SRC_ENGRAM) not in sys.path:
        sys.path.append(str(SRC_ENGRAM))


_A6 = None


def _load_a6():
    """The A6 tool, loaded once per process: its layout (source or installed)
    is module state, so every caller must see the same instance."""
    global _A6
    if _A6 is None:
        spec = importlib.util.spec_from_file_location("engramd_a6_latency_for_profile", A6_TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _A6 = mod
    return _A6


def make_test_store(nodes: int = 5000, people: int = 5, source: Optional[Path] = None) -> Path:
    """The A6 tool's own store builder (same schema, same synthetic rows), so the
    two instruments always measure the same kind of store."""
    return _load_a6().make_test_store(nodes=nodes, people=people, source=source)


# =============================================================================
# Owner side: the launcher (runs in the owner process)
# =============================================================================

class _Clock:
    """Per-thread nesting of owner stages: exclusive time is computed later
    from the raw (name, start, end, parent) records this collects."""

    def __init__(self) -> None:
        self.local = threading.local()

    def begin(self) -> None:
        self.local.records = []
        self.local.stack = []

    def active(self) -> bool:
        return getattr(self.local, "stack", None) is not None

    def wrap(self, name: str, fn, inject: Dict[str, float]):
        delay = inject.get(name, 0.0)
        clock = self

        def timed(*args, **kwargs):
            if not clock.active():
                return fn(*args, **kwargs)
            recs, stack = clock.local.records, clock.local.stack
            idx = len(recs)
            recs.append([name, time.monotonic(), None, stack[-1] if stack else None])
            stack.append(idx)
            try:
                if delay:
                    time.sleep(delay)
                return fn(*args, **kwargs)
            finally:
                stack.pop()
                recs[idx][2] = time.monotonic()

        timed.__wrapped__ = fn
        return timed


class _TimedConn:
    """The accepted connection, with sendall timed. Everything else is the
    real socket's own method."""

    def __init__(self, conn, wrap) -> None:
        self._conn = conn
        self.sendall = wrap("response_send", conn.sendall)

    def __getattr__(self, attr):
        return getattr(self._conn, attr)


def serve_owner(home: Path, records_path: Path, inject: Dict[str, float],
                inject_fn: Optional[Dict[str, float]] = None, owner_module: Path = OWNER_MODULE) -> int:
    """Launcher mode: the real owner, with timers wrapped around its callables."""
    os.environ["ENGRAM_HOME"] = str(home)
    runtime_dir = str(Path(owner_module).resolve().parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    spec = importlib.util.spec_from_file_location("engramd_owner", owner_module)
    eo = importlib.util.module_from_spec(spec)
    sys.modules["engramd_owner"] = eo
    spec.loader.exec_module(eo)

    for target, delay in (inject_fn or {}).items():
        _inject_into_function({"engramd_owner": eo, "engram_walguard": eo.engram_walguard,
                               "engramd_read_registry": eo.rr, "engramd_client": eo.ec}, target, delay)

    clock = _Clock()

    def wrap(name, fn):
        return clock.wrap(name, fn, inject)

    out = open(records_path, "a", encoding="utf-8", buffering=1)
    write_lock = threading.Lock()

    # json inside the owner: decode is the request; encode is the reply. Since
    # #3678 cut 2 a read reply is encoded once, inside _render_read_rows (wrapped
    # as response_encode below), and sent as wire_text without another dumps. The
    # per-row dumps in there stay unwrapped so they count inside that stage, and
    # are not a second response_encode record per row.
    in_read = threading.local()
    real_json = eo.json

    class _Json:
        def __getattr__(self, attr):
            return getattr(real_json, attr)

    timed_loads = wrap("request_decode", real_json.loads)
    timed_dumps = wrap("response_encode", real_json.dumps)
    proxy = _Json()
    proxy.loads = timed_loads
    proxy.dumps = lambda *a, **k: (real_json.dumps(*a, **k) if getattr(in_read, "on", False)
                                   else timed_dumps(*a, **k))
    eo.json = proxy

    eo.ec.check_contract_version = wrap("version_check", eo.ec.check_contract_version)
    eo.engram_walguard.assert_not_displaced = wrap("walguard", eo.engram_walguard.assert_not_displaced)
    eo.rr.Registry.check = wrap("registry_check", eo.rr.Registry.check)
    eo._arm_read_deadline = wrap("arm_deadline", eo._arm_read_deadline)
    eo._ClientReadGate._execute_admitted = wrap("execute", eo._ClientReadGate._execute_admitted)
    eo.EngramdOwner._check_peer_credentials = wrap("peer_credentials", eo.EngramdOwner._check_peer_credentials)
    eo.EngramdOwner._read_request_line = wrap("request_read", eo.EngramdOwner._read_request_line)
    eo.EngramdOwner._dispatch = wrap("owner_other", eo.EngramdOwner._dispatch)

    real_handle_read = eo.EngramdOwner._handle_read

    def handle_read(self, params, deadline):
        in_read.on = True
        try:
            return real_handle_read(self, params, deadline)
        finally:
            in_read.on = False

    eo.EngramdOwner._handle_read = wrap("read_handler", handle_read)
    eo._render_read_rows = wrap("response_encode", eo._render_read_rows)

    # thread_handoff = from the accept loop's hand-off (`_dispatch_connection`, which
    # gives the connection to an idle handler or starts a new one, #3678) to the
    # handler's entry into `_handle_connection`. Keyed by the connection's fd, which is
    # unique while the connection is open.
    real_dispatch = eo.EngramdOwner._dispatch_connection
    real_handle_connection = eo.EngramdOwner._handle_connection
    handoff_delay = inject.get("thread_handoff", 0.0)
    handed = {}

    def dispatch_connection(self, conn):
        handed[conn.fileno()] = time.monotonic()
        return real_dispatch(self, conn)

    def handle_connection(self, conn):
        spawn = handed.pop(conn.fileno(), None)
        if handoff_delay:
            time.sleep(handoff_delay)
        entry = time.monotonic()
        clock.begin()
        root = clock.wrap("owner_other", lambda: real_handle_connection(self, _TimedConn(conn, wrap)), {})
        try:
            root()
        finally:
            exit_ = time.monotonic()
            rec = {"spawn": spawn if spawn is not None else entry, "entry": entry, "exit": exit_,
                   "records": clock.local.records}
            clock.local.stack = None
            with write_lock:
                out.write(json.dumps(rec) + "\n")

    eo.EngramdOwner._dispatch_connection = dispatch_connection
    eo.EngramdOwner._handle_connection = handle_connection

    owner = eo.EngramdOwner()
    owner.start(install_signal_handlers=True)
    print(f"[profile-owner] listening on {owner.socket_path} (pid {os.getpid()})", file=sys.stderr)
    owner.wait_forever()
    out.close()
    return 0


# =============================================================================
# Client side (runs in this process)
# =============================================================================

class OwnerHandle:
    def __init__(self, proc: subprocess.Popen, home: Path, records: Optional[Path]):
        self.proc = proc
        self.home = home
        self.records = records
        self.socket_path = home / "run" / "engramd.sock"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def _wait_for_ping(handle: OwnerHandle) -> OwnerHandle:
    _ensure_src_importable()
    import engramd_client as ec
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if handle.proc.poll() is not None:
            raise RuntimeError(f"owner exited early (code {handle.proc.returncode})")
        if isinstance(ec.read("ping", {}, timeout=1.0, socket_path=handle.socket_path), ec.Ok):
            return handle
        time.sleep(0.1)
    handle.stop()
    raise RuntimeError("owner never answered ping")


def _owner_env(home: Path) -> Dict[str, str]:
    env = dict(os.environ, ENGRAM_HOME=str(home))
    env.pop("ENGRAM_OWNER_ALLOW_DYNAMIC_TEMPLATES", None)
    return env


def launch_profiled_owner(home: Path, inject: Optional[Dict[str, float]] = None,
                          inject_fn: Optional[Dict[str, float]] = None) -> OwnerHandle:
    """The real owner, wrapped by this file's launcher mode."""
    records = home / f"stage-records-{time.monotonic_ns()}.jsonl"
    argv = [sys.executable, str(Path(__file__).resolve()), "--serve-owner",
            "--owner-module", str(OWNER_MODULE), "--home", str(home),
            "--records", str(records), "--inject", json.dumps(inject or {}),
            "--inject-fn", json.dumps(inject_fn or {})]
    proc = subprocess.Popen(argv, env=_owner_env(home), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=str(home))
    return _wait_for_ping(OwnerHandle(proc, home, records))


def _apply_layout(layout) -> None:
    """Point this tool (and the A6 module it shares hook loading with) at
    `layout`. Only in a fresh process (`--_layout-child`)."""
    global OWNER_MODULE, SRC_ENGRAM
    _load_a6()._apply_layout(layout)
    OWNER_MODULE = layout.root / "engramd_owner.py"
    SRC_ENGRAM = layout.root


def profile_in_layout(layout, **kwargs) -> Dict:
    """profile() for `layout` in a FRESH process, so nothing from another
    layout can be cached. An installed-layout result passes the A6 tool's own
    two runtime asserts (owner argv under the build, zero parent walks)."""
    a6 = _load_a6()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--_layout-child", layout.to_json(),
         "--_kwargs", json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in kwargs.items()})],
        capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"{layout.name}-layout profile failed (rc {proc.returncode}): {proc.stderr[-1500:]}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    result["layout"], result["layout_root"] = layout.name, str(layout.root)
    if layout.name == "installed":
        a6.check_installed_run(result, layout)
    return result


def _layout_child(layout_json: str, kwargs_json: str) -> int:
    a6 = _load_a6()
    layout = a6.Layout.from_json(layout_json)
    _apply_layout(layout)
    counter = a6._count_parent_walks(layout)
    kwargs = json.loads(kwargs_json)
    if kwargs.get("home"):
        kwargs["home"] = Path(kwargs["home"])
    result = profile(**kwargs)
    result["parent_walk_calls"] = counter["walks"]
    # The profiled owner process is this tool's launcher; the owner code it ran is
    # the --owner-module it loaded, so that path is what the installed assert checks.
    seen = result.pop("owner_argv_seen", [])
    module = seen[seen.index("--owner-module") + 1] if "--owner-module" in seen else ""
    result["owner_argv"] = [seen[0] if seen else "", module]
    print(json.dumps(result))
    return 0


def launch_plain_owner(home: Path) -> OwnerHandle:
    """The owner with no wrappers at all, exactly as the A6 tool launches it."""
    proc = subprocess.Popen([sys.executable, str(OWNER_MODULE)], env=_owner_env(home),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(home))
    return _wait_for_ping(OwnerHandle(proc, home, None))


class _ClientTimer:
    """Wraps engramd_client's module attributes for the duration of a run and
    collects one stamp dict per call."""

    def __init__(self, ec, inject: Dict[str, float], hook_mod=None) -> None:
        self.ec = ec
        self.hook_mod = hook_mod
        self.inject = inject
        self.call: Optional[Dict[str, List]] = None
        self._saved: Dict[str, object] = {}

    def _span(self, name, fn):
        delay = self.inject.get(name, 0.0)
        timer = self

        def timed(*a, **k):
            t0 = time.monotonic()
            try:
                if delay:
                    time.sleep(delay)
                return fn(*a, **k)
            finally:
                if timer.call is not None:
                    timer.call.setdefault(name, []).append((t0, time.monotonic()))
        return timed

    def install(self) -> None:
        ec, span, timer = self.ec, self._span, self
        real_socket_mod, real_json = ec.socket, ec.json

        class _Sock:
            def __init__(self, sock) -> None:
                self._sock = sock
                self.connect = span("connect", sock.connect)
                self.sendall = span("request_send", sock.sendall)
                real_recv = sock.recv

                def recv(*a, **k):
                    data = real_recv(*a, **k)
                    if timer.call is not None:
                        timer.call["recv_end"] = [time.monotonic()]
                    return data
                self.recv = recv

            def __getattr__(self, attr):
                return getattr(self._sock, attr)

        make_socket = span("socket_create", real_socket_mod.socket)

        class _SocketMod:
            def __getattr__(self, attr):
                return getattr(real_socket_mod, attr)

            @staticmethod
            def socket(*a, **k):
                return _Sock(make_socket(*a, **k))

        class _JsonMod:
            def __getattr__(self, attr):
                return getattr(real_json, attr)

        jm = _JsonMod()
        jm.dumps = span("request_encode", real_json.dumps)
        jm.loads = span("response_decode", real_json.loads)

        self._saved = {"socket": ec.socket, "json": ec.json,
                       "_resolve_socket_path": ec._resolve_socket_path}
        ec.socket = _SocketMod()
        ec.json = jm
        ec._resolve_socket_path = span("resolve_path", ec._resolve_socket_path)
        if self.hook_mod is not None:
            self._saved_hook = self.hook_mod._import_engramd_client
            self.hook_mod._import_engramd_client = span("client_import", self._saved_hook)

    def uninstall(self) -> None:
        for attr, val in self._saved.items():
            setattr(self.ec, attr, val)
        if self.hook_mod is not None and hasattr(self, "_saved_hook"):
            self.hook_mod._import_engramd_client = self._saved_hook


def _bind_hook(hook: str, home: Path):
    """-> (hook module, owner_fn, expected answer). owner_fn is the hook's OWN
    owner-read function (stop: `_read_count_via_owner`; user-identity:
    `read_people_via_owner`), so the per-call client import the hook does is
    inside the timed call, as it is in the A6 run. The threshold read the stop
    hook's `_maturity_muted()` adds is NOT included: it is common to both A6
    paths and is not part of the owner round trip."""
    a6 = _load_a6()
    if hook == "stop":
        mod = a6.load_hook(home, a6.STOP_HOOK_PATH, "engram_stop_hook_profile")
        conn = sqlite3.connect(str(home / "knowledge.db"))
        try:
            expected = conn.execute(mod._CURRENT_COUNT_SQL).fetchone()[0]
        finally:
            conn.close()
        return mod, mod._read_count_via_owner, expected
    mod = a6.load_hook(home)
    expected = mod.get_person_nodes()

    def owner_fn():
        rows, failure = mod.read_people_via_owner()
        if failure is not None:
            raise RuntimeError(f"owner read failed: {failure}")
        return rows
    return mod, owner_fn, expected


def _clip(start: float, end: float, floor: float) -> float:
    return max(0.0, end - max(start, floor))


def _owner_stage_times(rec: Dict, send_end: float) -> Dict[str, float]:
    """Exclusive owner stage time per name, counting only time after the client
    finished sending."""
    records = rec["records"]
    incl = [_clip(r[1], r[2], send_end) for r in records]
    child = [0.0] * len(records)
    for i, r in enumerate(records):
        if r[3] is not None:
            child[r[3]] += incl[i]
    out: Dict[str, float] = {}
    for i, r in enumerate(records):
        out[r[0]] = out.get(r[0], 0.0) + max(0.0, incl[i] - child[i])
    out["thread_handoff"] = out.get("thread_handoff", 0.0) + _clip(rec["spawn"], rec["entry"], send_end)
    return out


def _divide_call(call: Dict[str, List], rec: Dict, t0: float, t1: float) -> Dict[str, float]:
    send_end = call["request_send"][-1][1]
    stages = {s: 0.0 for s in STAGES}
    for name in CLIENT_STAGES:
        stages[name] = sum(e - s for s, e in call.get(name, []))
    for name, ms in _owner_stage_times(rec, send_end).items():
        stages[name] += ms
    connect_end = call["connect"][-1][1]
    stages["accept_wakeup"] = _clip(connect_end, rec["spawn"], send_end)
    owner_send_end = max((r[2] for r in rec["records"] if r[0] == "response_send"), default=rec["exit"])
    stages["response_delivery"] = max(0.0, call["recv_end"][0] - owner_send_end)
    stages["unattributed"] = (t1 - t0) - sum(stages[s] for s in STAGES)
    return stages


def _pctl(sorted_ms: List[float], q: float) -> float:
    return sorted_ms[max(0, math.ceil(q * len(sorted_ms)) - 1)]


def _row(ms: List[float], **extra) -> Dict:
    s = sorted(ms)
    return dict(n=len(s), p50_ms=round(_pctl(s, 0.50), 4), p99_ms=round(_pctl(s, 0.99), 4),
                mean_ms=round(sum(s) / len(s), 4), **extra)


def _read_records(path: Path, want: int) -> List[Dict]:
    deadline = time.monotonic() + 10
    while True:
        # Only complete lines: the owner may be mid-write on the last one.
        lines = path.read_text(encoding="utf-8").split("\n")[:-1] if path.exists() else []
        if len(lines) >= want or time.monotonic() > deadline:
            return [json.loads(x) for x in lines]
        time.sleep(0.05)


def profile(n: int = N_FLOOR, home: Optional[Path] = None, *, warmup: int = 50,
            inject: Optional[Dict[str, float]] = None, hook: str = "stop",
            nodes: int = 5000, unwrapped: bool = True,
            inject_owner_fn: Optional[Dict[str, float]] = None,
            inject_client_fn: Optional[Dict[str, float]] = None) -> Dict:
    """Profile N owner reads on `home` (a scratch store; built if None).

    inject: stage name -> seconds, applied inside that stage's own wrapper.
    inject_owner_fn / inject_client_fn: "module:attr" -> seconds, patched into
    the real function UNDER the wrappers (owner side: in the owner process;
    client side: `engramd_client` or `hook` in this process)."""
    inject = dict(inject or {})
    unknown = set(inject) - set(INJECTABLE_STAGES) - {UNNAMED_GAP}
    if unknown:
        raise ValueError(f"unknown stage name(s) to inject: {sorted(unknown)}")
    home = home or make_test_store(nodes=nodes)
    os.environ["ENGRAM_HOME"] = str(home)
    os.environ.pop("ENGRAM_RUNTIME_DIR", None)
    _ensure_src_importable()
    import engramd_client as ec

    mod, owner_fn, expected = _bind_hook(hook, home)
    owner_inject = {k: v for k, v in inject.items() if k in OWNER_STAGES}
    client_inject = {k: v for k, v in inject.items() if k in CLIENT_STAGES}
    gap = inject.get(UNNAMED_GAP, 0.0)

    saved_client_fns = []
    for target, delay in (inject_client_fn or {}).items():
        mod_name, _, attr = target.partition(":")
        holder = {"engramd_client": ec, "hook": mod}[mod_name]
        saved_client_fns.append((holder, attr, getattr(holder, attr)))
        _inject_into_function({"engramd_client": ec, "hook": mod}, target, delay)

    handle = launch_profiled_owner(home, owner_inject, inject_owner_fn)
    timer = _ClientTimer(ec, client_inject, mod)
    calls: List[Dict] = []
    rows_equal = True
    try:
        timer.install()
        before = len(_read_records(handle.records, 0))
        for i in range(warmup + n):
            timer.call = {}
            t0 = time.monotonic()
            if gap:
                time.sleep(gap)
            res = owner_fn()
            t1 = time.monotonic()
            if sys.modules.get("engramd_client") is not ec:
                raise RuntimeError("the hook imported a different engramd_client than the one being timed")
            if res != expected:
                rows_equal = False
            if i >= warmup:
                calls.append(dict(stamps=timer.call, t0=t0, t1=t1))
            timer.call = None
        records = _read_records(handle.records, before + warmup + n)
        owner_argv_seen = [x.decode() for x in Path(f"/proc/{handle.proc.pid}/cmdline").read_bytes().split(b"\0") if x] \
            if Path("/proc").is_dir() else []
    finally:
        timer.uninstall()
        for holder, attr, real in saved_client_fns:
            setattr(holder, attr, real)
        owner_pid = handle.proc.pid
        handle.stop()
    # Requests are sequential, so the last N records are the measured calls, in order.
    records = [r for r in records if any(x[0] == "read_handler" for x in r["records"])][-n:]
    if len(records) != n:
        raise RuntimeError(f"expected {n} owner records, got {len(records)}")

    per_stage: Dict[str, List[float]] = {s: [] for s in STAGES}
    rest, e2e = [], []
    for call, rec in zip(calls, records):
        divided = _divide_call(call["stamps"], rec, call["t0"], call["t1"])
        for s in STAGES:
            per_stage[s].append(divided[s] * 1000)
        rest.append(divided["unattributed"] * 1000)
        e2e.append((call["t1"] - call["t0"]) * 1000)

    side = {**{s: "client" for s in CLIENT_STAGES}, **{s: "owner" for s in OWNER_STAGES},
            **{s: "between" for s in BETWEEN_STAGES}}
    result = {
        "hook": hook, "n": n,
        "stages": {s: _row(per_stage[s], side=side[s]) for s in STAGES},
        "end_to_end": _row(e2e),
        "unattributed": _row(rest),
        "rows_equal": rows_equal,
        "owner_pid": owner_pid,
        "build": {"host": platform.platform(), "python": platform.python_version(),
                  "sqlite": sqlite3.sqlite_version},
        "unwrapped_end_to_end": None, "unwrapped_owner_pid": None,
        "owner_argv_seen": owner_argv_seen,
    }
    if unwrapped and not (inject or inject_owner_fn or inject_client_fn):
        plain = launch_plain_owner(home)
        try:
            ms = []
            for i in range(warmup + n):
                t0 = time.monotonic()
                res = owner_fn()
                t1 = time.monotonic()
                if res != expected:
                    raise RuntimeError("unwrapped owner answered differently from a direct read")
                if i >= warmup:
                    ms.append((t1 - t0) * 1000)
            result["unwrapped_end_to_end"] = _row(ms)
            result["unwrapped_owner_pid"] = plain.proc.pid
        finally:
            plain.stop()
    return result


def format_table(result: Dict) -> str:
    b = result["build"]
    lines = [
        f"Host: {b['host']} | Python {b['python']} | SQLite {b['sqlite']} | hook read: {result['hook']} | N={result['n']}",
        f"Owner answered the same rows as a direct read: {result['rows_equal']}",
        "",
        "| stage | side | p50 ms | p99 ms | mean ms |",
        "|---|---|---|---|---|",
    ]
    for s in STAGES:
        r = result["stages"][s]
        lines.append(f"| {s} | {r['side']} | {r['p50_ms']:.4f} | {r['p99_ms']:.4f} | {r['mean_ms']:.4f} |")
    for label, key in (("unattributed", "unattributed"), ("**end-to-end (wrapped)**", "end_to_end"),
                       ("**end-to-end (unwrapped owner)**", "unwrapped_end_to_end")):
        r = result.get(key)
        if r:
            lines.append(f"| {label} | - | {r['p50_ms']:.4f} | {r['p99_ms']:.4f} | {r['mean_ms']:.4f} |")
    lines += ["", "Stage p50s do not add to the end-to-end p50 (percentiles do not sum); means do. "
              "The wrapped run attributes; the unwrapped run and the A6 tool set the level."]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Stage profile of the engramd owner round trip (U7, #3678).")
    p.add_argument("--serve-owner", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--owner-module", type=Path, default=OWNER_MODULE, help=argparse.SUPPRESS)
    p.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    p.add_argument("--records", type=Path, default=None, help=argparse.SUPPRESS)
    p.add_argument("--inject", default="{}", help=argparse.SUPPRESS)
    p.add_argument("--inject-fn", default="{}", help=argparse.SUPPRESS)
    p.add_argument("--n", type=int, default=N_FLOOR)
    p.add_argument("--nodes", type=int, default=5000)
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--hook", choices=("stop", "user-identity"), default="stop")
    p.add_argument("--store", type=Path, default=None,
                   help="profile a COPY of this knowledge.db (never the live store)")
    p.add_argument("--layout", choices=("both", "source", "installed"), default="both",
                   help="code layout (#3678): installed = plugin built from this tree, the one that matters; default both")
    p.add_argument("--_layout-child", dest="layout_child", default=None, help=argparse.SUPPRESS)
    p.add_argument("--_kwargs", dest="child_kwargs", default="{}", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if a.layout_child:
        return _layout_child(a.layout_child, a.child_kwargs)
    if a.serve_owner:
        return serve_owner(a.home, a.records, json.loads(a.inject), json.loads(a.inject_fn), a.owner_module)
    if a.n < N_FLOOR:
        print(f"--n must be >= {N_FLOOR} (#3678)", file=sys.stderr)
        return 2
    if a.store is not None:
        # #3962: classify the path BEFORE `_is_live_store` (its resolve() raises on a symlink loop) and before
        # make_test_store (a FIFO hangs its mode=ro connect; a missing path leaves a temp dir). Reuses a6's helper.
        store_problem = _load_a6()._check_store_arg(a.store)
        if store_problem is not None:
            print(f"refusing: {store_problem}", file=sys.stderr)
            return 2
    if a.store is not None and _load_a6()._is_live_store(a.store):
        print(f"refusing: {a.store} is this seat's live store; pass a copy", file=sys.stderr)
        return 2
    a6 = _load_a6()
    home = make_test_store(nodes=a.nodes, source=a.store)
    wanted = ("installed", "source") if a.layout == "both" else (a.layout,)
    with tempfile.TemporaryDirectory(prefix="profile-build-") as build_dir:
        for name in wanted:
            layout = a6.build_installed_layout(Path(build_dir) / "plugin") if name == "installed" else a6.source_layout()
            r = profile_in_layout(layout, n=a.n, home=home, warmup=a.warmup, hook=a.hook)
            print(f"### {a6.LAYOUT_LABELS[name]}\nparent-walk calls: {r['parent_walk_calls']}\n")
            print(format_table(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
