"""Contract-first tests for engram-alpha #3658: the utility-credit Stop hook
takes `_with_state_lock` for its read-modify-write of principle-trigger-state.json.

Defect: `engram-utility-credit-mention-stop.py::_check_enactments` read the
state, incremented `enactments`, and rewrote the file WITHOUT the flock that the
surface hook's write-back holds. If the surface write-back landed between the
utility's read and its write, the utility overwrote it with a stale snapshot
and the surface's update (a fire count, `last_fired_prompt`) was lost.

Contract (maintainer, binding):
  1. The utility's read-modify-write runs under `_with_state_lock` on the SAME
     lock path as the surface hook (`<state>.lock`), with the READ inside the
     lock. A concurrent surface write-back never loses an update.
  2. The degraded path (lock file un-openable / no fcntl) runs UNLOCKED with the
     existing stderr line and never crashes or skips the write.
  3. The lock stays blocking with no timeout (same as the surface hook). If a
     timeout is ever added it must be added to every caller in one PR.

Cells:
  L1 lost-update (interleaved, real second process)   L2 sequential control
  L3 degraded path                                    L4 row-40 unchanged
  L5 three-copy AST parity of `_with_state_lock`      L6 healthy-path bytes vs merge-base

L1 injection seam: the module's `read_state_json_classified` is replaced by a
wrapper that calls the real one (the utility now holds its snapshot, and, once
fixed, the lock) and then starts the surface hook in a separate process and
waits a BOUNDED time for its write. Under the lock the surface process is
blocked until the utility releases, so the wait simply times out (no deadlock);
without the lock the surface write lands inside the wait and is then overwritten.
"""
from __future__ import annotations

import ast
import errno
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import io
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src" / "engram"
HOOK_DIR = SRC / "hooks" / "claude"
UTILITY = HOOK_DIR / "engram-utility-credit-mention-stop.py"
SURFACE = HOOK_DIR / "engram-surface-hook.py"
CORE = SRC / "engram_core.py"
HOOK_REL = "src/engram/hooks/claude"

STATE_NAME = "principle-trigger-state.json"
REGISTRY = {
    "ob_ta": [{"principle_id": "ls_a", "kind": "lesson", "claim": "A.", "nudge": "A."}],
    "ob_tb": [{"principle_id": "ls_b", "kind": "lesson", "claim": "B.", "nudge": "B."}],
}
START = {
    "ls_a": {"last_fired_prompt": 0, "strength": 1.0, "enactments": 0, "fires": 0},
    "ls_b": {"last_fired_prompt": 0, "strength": 1.0, "enactments": 0, "fires": 0},
}
# Upper bound on how long the injected hook waits for the surface process's write.
# L1's timing assumption: its RED side (no lock) relies on the surface child finishing
# well inside this wait, so its write lands before the utility's stale write. Measured
# child wall time is about 0.3 s, i.e. ~15x headroom under 5 s; on a machine where the
# child could take longer than this, raise WAIT_S (the green side only waits it out).
WAIT_S = 5.0

SURFACE_CHILD = r'''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("surface_hook_under_test", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
text, rendered = m.check_principle_triggers_full(["ob_tb"], 500)
print("surface fired:", [r[1] for r in rendered])
'''

_COUNTER = 0


@pytest.fixture(autouse=True)
def _restore_hooklib_module():
    """_load_utility re-imports _hooklib per hook dir; put sys.modules back."""
    saved = sys.modules.get("_hooklib")
    yield
    if saved is None:
        sys.modules.pop("_hooklib", None)
    else:
        sys.modules["_hooklib"] = saved


def _setup(home: Path, state=START):
    (home / "principle_triggers.json").write_text(json.dumps(REGISTRY))
    (home / "prompt-counter.json").write_text(json.dumps({"prompts_since_compaction": 500}))
    if state is not None:
        (home / STATE_NAME).write_text(json.dumps(state))


def _load_utility(home: Path, monkeypatch, path: Path = UTILITY, hook_dir: Path = HOOK_DIR):
    """Load the utility hook against `home`, with prompt count and telemetry stubbed."""
    global _COUNTER
    _COUNTER += 1
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    spec = importlib.util.spec_from_file_location(f"uc_3658_{_COUNTER}", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop("_hooklib", None)
    monkeypatch.syspath_prepend(str(hook_dir))
    spec.loader.exec_module(mod)
    mod._read_prompt_count = lambda p: 500
    mod._emit_enactment = lambda *a, **k: None
    return mod


def _spawn_surface(home: Path) -> subprocess.Popen:
    env = dict(os.environ, ENGRAM_HOME=str(home))
    return subprocess.Popen([sys.executable, "-c", SURFACE_CHILD, str(SURFACE)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _surface_sequential(home: Path):
    p = _spawn_surface(home)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err[-500:]
    assert "ls_b" in out, out


def _final(home: Path) -> dict:
    return json.loads((home / STATE_NAME).read_text())


# ---------------------------------------------------------------- L1 / L2

def test_L1_surface_writeback_inside_utility_critical_section_is_not_lost(tmp_path, monkeypatch):
    _setup(tmp_path)
    mod = _load_utility(tmp_path, monkeypatch)
    state_path = str(tmp_path / STATE_NAME)
    real = mod.read_state_json_classified
    holder = {}

    def read_then_surface(path):
        out = real(path)  # the utility holds its snapshot (and, once fixed, the lock)
        proc = _spawn_surface(tmp_path)
        holder["proc"] = proc
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:  # BOUNDED: never deadlocks under the lock
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        return out

    monkeypatch.setattr(mod, "read_state_json_classified", read_then_surface)
    mod._check_enactments(["ls_a"], state_path=state_path)
    proc = holder["proc"]
    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("surface process never finished after the utility returned")
    assert proc.returncode == 0, err[-500:]
    final = _final(tmp_path)
    assert final["ls_a"]["enactments"] == 1, final
    assert final["ls_b"]["fires"] == 1, f"surface update lost: {final}"
    assert final["ls_b"]["last_fired_prompt"] == 500, f"surface update lost: {final}"


def test_L2_sequential_surface_then_utility_keeps_both(tmp_path, monkeypatch):
    _setup(tmp_path)
    _surface_sequential(tmp_path)
    mod = _load_utility(tmp_path, monkeypatch)
    mod._check_enactments(["ls_a"], state_path=str(tmp_path / STATE_NAME))
    final = _final(tmp_path)
    assert final["ls_a"]["enactments"] == 1, final
    assert final["ls_b"]["fires"] == 1 and final["ls_b"]["last_fired_prompt"] == 500, final


# ---------------------------------------------------------------- L3

def test_L3_unopenable_lock_degrades_unlocked_and_still_writes(tmp_path, monkeypatch, capsys):
    _setup(tmp_path)
    (tmp_path / (STATE_NAME + ".lock")).mkdir()  # os.open(O_RDWR) on a directory fails
    mod = _load_utility(tmp_path, monkeypatch)
    mod._check_enactments(["ls_a"], state_path=str(tmp_path / STATE_NAME))
    err = capsys.readouterr().err
    assert err.count("[engram _with_state_lock] flock degraded to UNLOCKED") == 1, err
    assert _final(tmp_path)["ls_a"]["enactments"] == 1


DEGRADED = "[engram _with_state_lock] flock degraded to UNLOCKED"


def test_L3b_flock_failure_degrades_unlocked_and_still_writes(tmp_path, monkeypatch, capsys):
    """flock itself raising (EOPNOTSUPP) is a setup failure too: degrade, don't skip the write."""
    import fcntl
    _setup(tmp_path)
    mod = _load_utility(tmp_path, monkeypatch)

    def no_flock(fd, op):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", no_flock)
    mod._check_enactments(["ls_a"], state_path=str(tmp_path / STATE_NAME))  # must not raise
    err = capsys.readouterr().err
    assert err.count(DEGRADED) == 1, err
    assert "OSError: " in err and ".lock" in err, err
    assert _final(tmp_path)["ls_a"]["enactments"] == 1


FIFO_CHILD = r"""
import importlib.util, os, sys
os.environ["ENGRAM_HOME"] = sys.argv[1]
sys.path.insert(0, sys.argv[3])
spec = importlib.util.spec_from_file_location("uc_fifo", sys.argv[2])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m._read_prompt_count = lambda p: 500
m._emit_enactment = lambda *a, **k: None
m._check_enactments(["ls_a"], state_path=os.path.join(sys.argv[1], "principle-trigger-state.json"))
"""


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is not available")
def test_L3c_fifo_at_lock_path_returns_promptly_and_still_writes(tmp_path):
    import fcntl
    _setup(tmp_path)
    lock = tmp_path / (STATE_NAME + ".lock")
    os.mkfifo(lock)
    # Does flock on a FIFO raise on THIS platform? (macOS: EOPNOTSUPP; Linux: succeeds.)
    fd = os.open(str(lock), os.O_RDWR | os.O_NONBLOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            expect_degraded = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            expect_degraded = 1
    finally:
        os.close(fd)
    t0 = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, "-c", FIFO_CHILD, str(tmp_path), str(UTILITY), str(HOOK_DIR)],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("_check_enactments blocked on a FIFO at the lock path")
    elapsed = time.monotonic() - t0
    print(f"L3c elapsed {elapsed:.2f}s (platform flock-on-FIFO degrades: {bool(expect_degraded)})")
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stderr.count(DEGRADED) == expect_degraded, done.stderr
    assert _final(tmp_path)["ls_a"]["enactments"] == 1


# ---------------------------------------------------------------- L4

def _unknown_case(kind, home: Path):
    p = home / STATE_NAME
    if kind == "corrupt":
        p.write_bytes(b'{"ls_a": {"last_fired_prompt": 1, "strength": 0.7, "enac')
    elif kind == "list":
        p.write_bytes(b"[1, 2, 3]")
    elif kind == "symlink":
        target = home / "elsewhere.json"
        target.write_bytes(json.dumps(START).encode())
        p.unlink()
        p.symlink_to(target)


@pytest.mark.parametrize("kind", ["corrupt", "list", "symlink"])
def test_L4_unknown_state_is_left_untouched_with_one_stderr_line(tmp_path, monkeypatch, capsys, kind):
    _setup(tmp_path, state=None)
    (tmp_path / STATE_NAME).write_bytes(b"{}")
    _unknown_case(kind, tmp_path)
    p = tmp_path / STATE_NAME
    target = (tmp_path / "elsewhere.json") if kind == "symlink" else p
    before = target.read_bytes()
    mod = _load_utility(tmp_path, monkeypatch)
    mod._check_enactments(["ls_a"], state_path=str(p))  # must not raise
    err = capsys.readouterr().err
    assert err.count("[engram principle-trigger-state] unusable state file") == 1, err
    assert target.read_bytes() == before
    if kind == "symlink":
        assert p.is_symlink()
    assert not Path(str(p) + ".corrupt").exists()


# ---------------------------------------------------------------- L5

def _lock_dump(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_with_state_lock"]
    assert len(funcs) == 1, f"{path.name}: expected exactly one top-level _with_state_lock"
    fn = funcs[0]
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # docstring wording may differ, code may not
    fn = ast.FunctionDef(name=fn.name, args=fn.args, body=body,
                         decorator_list=fn.decorator_list, returns=fn.returns,
                         type_comment=None, **({"type_params": []} if hasattr(fn, "type_params") else {}))
    return ast.dump(fn)


def test_L5_three_copies_of_with_state_lock_are_ast_identical():
    dumps = {p.name: _lock_dump(p) for p in (SURFACE, CORE, UTILITY)}
    assert len(set(dumps.values())) == 1, (
        "the three _with_state_lock copies must stay identical (#1720 sync note, #3658): "
        + ", ".join(sorted(dumps)))


# ---------------------------------------------------------------- L6

def _git(*args):
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True)


def _merge_base():
    r = _git("merge-base", "origin/dev", "HEAD")
    if r.returncode != 0:
        pytest.skip("no origin/dev merge-base available")
    if _git("merge-base", "--is-ancestor", "HEAD", "origin/dev").returncode == 0:
        pytest.skip("HEAD is already in origin/dev: nothing to compare against")
    return r.stdout.decode().strip()


@pytest.mark.parametrize("initial", ["absent", "dict"])
def test_L6_healthy_path_final_bytes_match_merge_base(tmp_path, monkeypatch, initial):
    base = _merge_base()
    arc = _git("archive", base, HOOK_REL)
    assert arc.returncode == 0, arc.stderr
    base_root = tmp_path / "base_src"
    base_root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(arc.stdout)) as tf:
        tf.extractall(base_root)
    base_hooks = base_root / HOOK_REL
    results = {}
    for label, hook_path, hook_dir in (("base", base_hooks / UTILITY.name, base_hooks),
                                       ("head", UTILITY, HOOK_DIR)):
        home = tmp_path / f"home_{label}"
        home.mkdir()
        _setup(home, state=START if initial == "dict" else None)
        mod = _load_utility(home, monkeypatch, path=hook_path, hook_dir=hook_dir)
        mod._check_enactments(["ls_a"], state_path=str(home / STATE_NAME))
        results[label] = (home / STATE_NAME).read_bytes()
    assert results["base"] == results["head"], results
