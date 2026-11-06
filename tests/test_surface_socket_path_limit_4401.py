"""Contract tests for engram-alpha#4401 -- the surface daemon has no AF_UNIX
socket-path length check: an over-long ENGRAM_HOME dies at server.bind() after
the model load, in a log only, and the per-prompt hook alternates "warming up"
and "relaunch requested" without ever naming the cause.

Contract (written before the implementation; design confirmed by the design
owner, #4401 comment 6692106793, riders R1-R5 in the comment):
  C1/R2 socket_path_too_long_reason(path) with no new arguments is
        BYTE-IDENTICAL to the pre-#4401 text (pinned against a literal).
  C2/R4 keyword-only `service` and `relative`: with relative="recall-daemon.sock"
        the reason names the byte length, the limit, the path, ENGRAM_HOME, the
        layout <ENGRAM_HOME>/recall-daemon.sock and +19 bytes (never
        run/engramd.sock or 17), and carries the one human line with BOTH
        numbers. Same SOCKET_PATH_MAX_BYTES (one definition); bytes, not chars.
  C3    the daemon, socket path limit+1: exit 78 (EXIT_SOCKET_PATH_TOO_LONG),
        ONE stderr line `[engram-daemon] refusing to start: <reason> (exit 78)`,
        nothing created in the home, the refusal backoff honoured.
  C4    at the limit the check refuses nothing; the constant matches the OS.
  C5/R1 the hook, over the limit: prints the reason, neither "warming up" nor
        "did not answer", and never calls _spawn_daemon_relaunch, whatever the
        warm-up stamp says. Under the limit the old branches are unchanged.
  C6/R3 the DAEMON that cannot import the helper skips the check; the HOOK that
        cannot import it fails TOWARDS the louder message (no "warming up").
  R3    both imports work in the FLATTENED installed layout with
        CLAUDE_PLUGIN_ROOT unset.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"
HOOKS_SRC = SRC / "hooks" / "claude"
DAEMON_PY = HOOKS_SRC / "engram-surface-daemon.py"
HOOK_PY = HOOKS_SRC / "engram-surface-hook.py"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import engramd_client as ec  # noqa: E402

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

REL = "recall-daemon.sock"
TAIL = "/" + REL  # 19 bytes
LIMIT = ec.SOCKET_PATH_MAX_BYTES


def _blen(p) -> int:
    return len(os.fsencode(p))


@pytest.fixture
def base():
    d = tempfile.mkdtemp(prefix="sp4401-")
    try:
        yield Path(os.path.realpath(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _home_for(base: Path, total: int, *, multibyte: int = 0) -> Path:
    """An ENGRAM_HOME (not created) under base whose socket path
    (<home>/recall-daemon.sock) is exactly `total` BYTES."""
    comp_bytes = total - (_blen(base) + 1 + len(TAIL))
    if comp_bytes < 2 * multibyte + 1:
        pytest.skip(f"tmp base {base} too long for a {total}-byte socket path")
    comp = "é" * multibyte + "h" * (comp_bytes - 2 * multibyte)
    home = base / comp
    assert _blen(str(home) + TAIL) == total
    return home


def _load(path: Path, name: str, home: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ENGRAM_HOME": str(home)}):
        spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- C1 / R2
OLD_TEXT = (
    "engramd socket path is {n} bytes, over the {limit}-byte AF_UNIX limit: {path}. "
    "Shorten ENGRAM_HOME (the socket lives at <ENGRAM_HOME>/run/engramd.sock, "
    "which adds 17 bytes)."
)


def test_c1_default_call_is_byte_identical_to_the_old_text():
    path = "/" + "x" * (LIMIT + 4)
    n = _blen(path)
    assert ec.socket_path_too_long_reason(path) == OLD_TEXT.format(n=n, limit=LIMIT, path=path)


def test_c1_default_call_none_under_the_limit():
    assert ec.socket_path_too_long_reason("/" + "x" * (LIMIT - 1)) is None


# --------------------------------------------------------------- C2 / R4
def _named(path):
    return ec.socket_path_too_long_reason(path, service="recall daemon", relative=REL)


def test_c2_named_layout_over_the_limit():
    home = "/" + "h" * (LIMIT - len(TAIL))  # socket path = LIMIT + 1
    path = home + TAIL
    n = _blen(path)
    assert n == LIMIT + 1
    r = _named(path)
    assert r is not None
    for needle in (f"{n} bytes", str(LIMIT), path, "ENGRAM_HOME",
                   "<ENGRAM_HOME>/recall-daemon.sock", "adds 19 bytes"):
        assert needle in r, (needle, r)
    assert "run/engramd.sock" not in r and "17" not in r
    # R4: one line carrying both numbers a human needs
    m = LIMIT - len(TAIL)
    assert (f"ENGRAM_HOME is {_blen(home)} bytes; the longest it can be here is {m} "
            f"(the socket path is {n} bytes, the limit {LIMIT})") in r
    assert "\n" not in r


def test_c2_named_layout_none_at_and_under_the_limit():
    at = "/" + "h" * (LIMIT - len(TAIL) - 1) + TAIL
    assert _blen(at) == LIMIT
    assert _named(at) is None
    assert _named(at[:-1]) is None


def test_c2_limit_is_in_bytes_not_characters():
    # 2-byte characters: the character count fits, the byte count does not
    home = "/" + "é" * ((LIMIT - len(TAIL)) // 2 + 1)
    path = home + TAIL
    assert len(path) <= LIMIT < _blen(path)
    assert _named(path) is not None


def test_c2_keywords_are_keyword_only():
    with pytest.raises(TypeError):
        ec.socket_path_too_long_reason("/x" * 100, "recall daemon", REL)


# --------------------------------------------------------------- C3
def _run_daemon(home: Path, cwd: Path, extra_env=None, script=DAEMON_PY):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(home)
    env["ENGRAM_DAEMON_REFUSAL_BACKOFF_S"] = "0"
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(script)], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=60)


def test_c3_daemon_refuses_first_and_creates_nothing(base):
    home = _home_for(base, LIMIT + 1)
    home.mkdir()
    before = sorted(os.listdir(home))
    p = _run_daemon(home, base)
    assert p.returncode == 78, (p.returncode, p.stderr)
    lines = [ln for ln in p.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, p.stderr
    reason = ec.socket_path_too_long_reason(str(home) + TAIL, service="recall daemon", relative=REL)
    assert lines[0].startswith("[engram-daemon] refusing to start:")
    assert reason in lines[0] and lines[0].rstrip().endswith("(exit 78)")
    assert sorted(os.listdir(home)) == before == []


def test_c3_refusal_waits_the_backoff_before_exiting(base):
    home = _home_for(base, LIMIT + 1)
    home.mkdir()
    t0 = time.monotonic()
    p = _run_daemon(home, base, {"ENGRAM_DAEMON_REFUSAL_BACKOFF_S": "1.5"})
    assert p.returncode == 78
    assert time.monotonic() - t0 >= 1.5


def test_c3_exit_code_constant_and_siblings():
    mod = _load(DAEMON_PY, "d4401_const", Path(tempfile.gettempdir()))
    assert mod.EXIT_SOCKET_PATH_TOO_LONG == 78
    assert (mod.EXIT_SOCKET_HELD, mod.EXIT_SOCKET_UNPROVEN, mod.EXIT_PIDFILE_UNWRITABLE) == (75, 76, 77)


# --------------------------------------------------------------- C4
def test_c4_daemon_check_refuses_nothing_at_the_limit(base):
    at = _home_for(base, LIMIT)
    over = _home_for(base, LIMIT + 1)
    mod = _load(DAEMON_PY, "d4401_at", at)
    assert _blen(mod.SOCKET_PATH) == LIMIT
    assert mod._socket_path_too_long_reason() is None
    mod2 = _load(DAEMON_PY, "d4401_over", over)
    assert mod2._socket_path_too_long_reason() is not None


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"),
                    reason="AF_UNIX limit pinned on Linux/Darwin only")
def test_c4_constant_matches_the_os(base):
    import socket

    def try_bind(total):
        d = base / "b"
        d.mkdir(exist_ok=True)
        name = "s" * (total - _blen(d) - 1)
        if len(name) < 1:
            pytest.skip("tmp base too long")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(d / name))
        finally:
            s.close()
            try:
                os.unlink(d / name)
            except OSError:
                pass

    try_bind(LIMIT)
    with pytest.raises(OSError):
        try_bind(LIMIT + 1)


# --------------------------------------------------------------- C5 / R1
def _hook(home: Path):
    return _load(HOOK_PY, "h4401", home)


def _run_hook(hook, spawner):
    buf = io.StringIO()
    with mock.patch.object(hook, "query_daemon", return_value=None), \
         mock.patch.object(hook, "query_fts_fallback", return_value={}), \
         mock.patch.object(hook, "_spawn_daemon_relaunch", spawner), \
         mock.patch("sys.stdin", io.StringIO(json.dumps({"prompt": "hello, what is next?"}))), \
         mock.patch("sys.stdout", buf):
        try:
            hook.main()
        except SystemExit:
            pass
    out = buf.getvalue().strip()
    if not out:
        return ""
    return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")


def _stamp(home: Path, age_s: int):
    home.mkdir(exist_ok=True)
    (home / "daemon-launch-attempt").write_text(str(int(time.time()) - age_s))


@pytest.mark.parametrize("age_s", [5, 100000])
def test_c5_over_limit_names_the_cause_and_never_relaunches(base, age_s):
    home = _home_for(base, LIMIT + 1)
    _stamp(home, age_s)  # young (warm-up window) or old: no difference (R1)
    hook = _hook(home)
    calls = []
    ctx = _run_hook(hook, lambda s: calls.append(s))
    reason = ec.socket_path_too_long_reason(hook.SOCKET_PATH, service="recall daemon", relative=REL)
    assert reason and reason in ctx
    assert "warming up" not in ctx and "did not answer this prompt" not in ctx
    assert calls == []


def test_c5_under_limit_old_branches_unchanged(base):
    home = _home_for(base, LIMIT)
    home.mkdir()
    hook = _hook(home)
    calls = []
    ctx = _run_hook(hook, lambda s: calls.append(s))  # no stamp
    assert "did not answer this prompt" in ctx and len(calls) == 1
    _stamp(home, 5)
    calls.clear()
    ctx = _run_hook(hook, lambda s: calls.append(s))
    assert "warming up" in ctx and calls == []

    def boom(s):
        raise OSError("x")
    (home / "daemon-launch-attempt").unlink()
    assert "CRITICAL" in _run_hook(hook, boom)


# --------------------------------------------------------------- C6
def test_c6_daemon_without_the_helper_skips_the_check(base):
    mod = _load(DAEMON_PY, "d4401_noimp", _home_for(base, LIMIT + 1))
    with mock.patch.dict(sys.modules, {"engramd_client": None}):
        assert mod._socket_path_too_long_reason() is None


def test_c6_hook_without_the_helper_fails_towards_the_louder_message(base):
    home = _home_for(base, LIMIT)  # a FINE path: only the import is broken
    _stamp(home, 5)  # young stamp: would show "warming up" if the check were quiet
    hook = _hook(home)
    calls = []
    with mock.patch.dict(sys.modules, {"engramd_client": None}), \
         mock.patch.object(hook, "_import_engramd_client", side_effect=ImportError("x")):
        ctx = _run_hook(hook, lambda s: calls.append(s))
    assert "warming up" not in ctx
    assert "did not answer this prompt" in ctx or "CRITICAL" in ctx


# --------------------------------------------------------------- R3 flattened
def _flatten(root: Path) -> Path:
    """The installed layout: plugin/hooks/<hooks/claude/*>, plugin/<src/engram/*.py>."""
    (root / "hooks").mkdir(parents=True)
    for f in SRC.glob("*.py"):
        shutil.copy(f, root / f.name)
    for f in HOOKS_SRC.glob("*"):
        if f.is_file():
            shutil.copy(f, root / "hooks" / f.name)
    return root


def test_r3_flattened_layout_daemon_and_hook_import_the_helper(base):
    plugin = _flatten(base / "plugin")
    home = _home_for(base, LIMIT + 1)
    home.mkdir()
    p = _run_daemon(home, plugin / "hooks", script=plugin / "hooks" / "engram-surface-daemon.py")
    assert p.returncode == 78, (p.returncode, p.stderr)
    assert "refusing to start:" in p.stderr
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR")}
    env["ENGRAM_HOME"] = str(home)
    code = (
        "import importlib.util,sys\n"
        "s=importlib.util.spec_from_file_location('h','hooks/engram-surface-hook.py')\n"
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        "k,r=m._surface_socket_check()\n"
        "print(k); print(r)\n"
    )
    q = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(plugin),
                       capture_output=True, text=True, timeout=120)
    assert q.returncode == 0, q.stderr
    assert q.stdout.splitlines()[0] == "too_long", (q.stdout, q.stderr)
    assert "recall-daemon.sock" in q.stdout


# --------------------------------------------------------------- remedy leads, path last
# A hook injection is truncated from the tail, so the useful part (the remedy) must lead.
def test_named_layout_remedy_precedes_the_path():
    home = "/" + "h" * (LIMIT - len(TAIL))
    path = home + TAIL
    r = _named(path)
    assert r is not None
    assert r.count(path) == 1, r
    assert r.index("Shorten ENGRAM_HOME") < r.index(path), r
    assert r.endswith(f"Socket path: {path}"), r
    assert "\n" not in r


def test_c5_hook_output_shows_the_remedy_before_the_path(base):
    home = _home_for(base, LIMIT + 1)
    _stamp(home, 100000)
    hook = _hook(home)
    ctx = _run_hook(hook, lambda s: None)
    path = str(hook.SOCKET_PATH)
    assert "Shorten ENGRAM_HOME" in ctx and path in ctx, ctx
    assert ctx.index("Shorten ENGRAM_HOME") < ctx.index(path), ctx


def test_import_engramd_client_docstring_names_the_daemon_down_importer():
    import ast
    tree = ast.parse(HOOK_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_import_engramd_client")
    doc = ast.get_docstring(fn) or ""
    assert "_surface_socket_check" in doc, doc
