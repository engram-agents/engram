"""#3863 step 2: the runtime layer refuses (entry points) or degrades (hook-imported modules) on a relative
ENGRAM_HOME.

Ruling: Borges's step-2 rulings on #3863, 2026-10-21.
  1. ONE shared runtime-layer module (src/engram/engram_home_resolver.py), pinned in the parity LAYERS table.
  2. PROCESS ENTRY POINTS RAISE (ValueError naming the value); anything a hook IMPORTS never raises: it
     disables its own writes with at most ONE stderr line.
  3. Refuse everywhere: engramd_owner's cwd-anchoring is not an exception.

Cells (each its own parametrised case, so a path that accepts the relative value reddens only its cell):
  e*  every entry point raises ValueError naming the value;
  h*  every hook-imported module exits 0, writes nothing under the cwd or the relative dir, and prints at
      most one stderr line (module direct, a real hook, and a flattened-layout copy of the runtime dir);
  r*  the new module's refusal text equals the canonical text (also pinned in the parity test's r1 cell).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
REL = "rel/dir"
TEXT = "ENGRAM_HOME must be an absolute path, got 'rel/dir'"


def _run(code, cwd, extra_env=None, path=SRC):
    env = {**os.environ, "ENGRAM_HOME": REL, "HOME": str(Path(cwd) / "_home"),
           "PYTHONPATH": str(path), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra_env or {})
    (Path(cwd) / "_home").mkdir(exist_ok=True)
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=180)


def _tree(cwd):
    """Every file and dir under cwd except the scratch HOME."""
    out = []
    for p, dirs, files in os.walk(cwd):
        if Path(p) == Path(cwd) and "_home" in dirs:
            dirs.remove("_home")
        out += [str(Path(p) / f) for f in files]
        out += [str(Path(p) / d) for d in dirs]
    return sorted(out)


# ---------------------------------------------------------------------------------------------- e: entry points

def test_e1_engram_core_import_fails_naming_the_value(tmp_path):
    r = _run("import engram_core", tmp_path)
    assert r.returncode != 0, r.stdout
    assert "ValueError" in r.stderr and TEXT in r.stderr, r.stderr


def test_e2_engramd_client_resolve_socket_path_raises(tmp_path, monkeypatch):
    import engramd_client
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(ValueError) as e:
        engramd_client._resolve_socket_path(None)
    assert REL in str(e.value)


def test_e3_engramd_owner_resolve_engram_home_raises_and_the_explicit_argument_is_unchanged(tmp_path, monkeypatch):
    import engramd_owner
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(ValueError) as e:
        engramd_owner._resolve_engram_home(None)
    assert REL in str(e.value)
    # the explicit-argument branch is a programmatic caller's own path: unchanged (reported as an open question)
    assert engramd_owner._resolve_engram_home("x/y") == tmp_path / "x" / "y"


def test_e4_server_marker_helper_raises_and_the_best_effort_writer_writes_nothing(tmp_path, monkeypatch):
    import server
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(ValueError) as e:
        server._mcp_ready_marker_path()
    assert REL in str(e.value)
    server._write_mcp_ready_marker()  # best effort: swallowed, and never a file under the cwd
    assert _tree(tmp_path) == []


def test_e5_telemetry_verdicts_path_helper_raises(tmp_path, monkeypatch):
    import engram_telemetry_verdicts as v
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(ValueError) as e:
        v._default_data_dir()
    assert REL in str(e.value)
    with pytest.raises(ValueError):
        v._default_db_path()


# ---------------------------------------------------------------------------------------- h: hook-imported modules

_USE_ALL = """
import engram_log_emitter as m, engram_telemetry_store as s, engram_log_indexer as ix
em = m.Emitter.init("sid-1", "/nonexistent/t.jsonl", initial_turn=3)
em.bind(a=1)
assert em.emit("engram.test.thing", 1, {"k": "v"}) is None
assert s.write_telemetry_record("test.site", "engram.test.thing", {"k": 1}) is False
res = ix.Indexer.run_once()
assert res["rows_inserted"] == 0, res
print("ok")
"""


def _stderr_lines(r):
    return [l for l in r.stderr.splitlines() if l.strip()]


def test_h1_hook_imported_modules_exit_zero_write_nothing_and_print_at_most_one_line(tmp_path):
    r = _run(_USE_ALL, tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok", (r.stdout, r.stderr)
    assert _tree(tmp_path) == [], _tree(tmp_path)
    assert len(_stderr_lines(r)) <= 1, r.stderr


@pytest.mark.parametrize("mod,call", [
    ("engram_log_emitter", "m.Emitter.init('s', '/x/t.jsonl', initial_turn=1).emit('engram.test.thing', 1, {})"),
    ("engram_telemetry_store", "m.write_telemetry_record('test.site', 'engram.test.thing', {})"),
    ("engram_log_indexer", "m.Indexer.run_once()"),
])
def test_h2_each_hook_imported_module_alone_never_raises(mod, call, tmp_path):
    r = _run(f"import {mod} as m\n{call}\nprint('ok')", tmp_path)
    assert r.returncode == 0, r.stderr
    assert _tree(tmp_path) == [], _tree(tmp_path)
    assert len(_stderr_lines(r)) <= 1, r.stderr


def test_h3_a_real_hook_that_imports_the_emitter_exits_zero_and_writes_nothing(tmp_path):
    """Hook-level framing. Every hook that imports the emitter or the store already stops EARLIER under a
    relative value (_hooklib.engram_home_or_exit() at import, or engram-loop-arm-stop-hook's own startup
    refusal), so no real hook reaches the modules' degrade path; the module-level cells above are where that
    path is exercised. This cell pins the hook-level outcome (exit 0, nothing written) and is expected GREEN on
    dev as well as on this branch: it guards against a regression, it is not the red cell."""
    hook = SRC / "hooks" / "claude" / "engram-postcompact-hook.py"
    env = {**os.environ, "ENGRAM_HOME": REL, "HOME": str(tmp_path / "_home"), "CLAUDE_PLUGIN_ROOT": str(SRC),
           "PYTHONDONTWRITEBYTECODE": "1"}
    (tmp_path / "_home").mkdir()
    r = subprocess.run([sys.executable, str(hook)], cwd=tmp_path, env=env, capture_output=True, text=True,
                       input='{"session_id": "s1", "transcript_path": "/x/t.jsonl", "hook_event_name": "PostCompact"}',
                       timeout=120)
    assert r.returncode == 0, r.stderr
    assert _tree(tmp_path) == [], _tree(tmp_path)
    assert len(_stderr_lines(r)) <= 1, r.stderr


def test_h4_the_flattened_deployed_layout_imports_and_degrades_without_a_src_prefix(tmp_path):
    """CLAUDE.md 'Plugin build restructures code paths': copy the flat runtime modules into a bare dir (no src/
    prefix, no package) and import the emitter exactly as a hook does, with that dir on sys.path."""
    flat = tmp_path / "plugin_root"
    flat.mkdir()
    for name in ("engram_home_resolver.py", "engram_log_emitter.py", "engram_telemetry_store.py",
                 "engram_log_indexer.py", "engram_regular_io.py"):
        shutil.copy(SRC / name, flat / name)
    work = tmp_path / "work"
    work.mkdir()
    r = _run(_USE_ALL, work, path=flat)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    assert _tree(work) == [], _tree(work)
    assert len(_stderr_lines(r)) <= 1, r.stderr
    # and the flat copy WORKS under an absolute home (it is the resolver that disabled it, not an import error)
    home = tmp_path / "abs_home"
    home.mkdir()
    r2 = _run("import engram_log_emitter as m\nem = m.Emitter.init('s','/x/t.jsonl', initial_turn=1)\n"
              "assert em.emit('engram.test.thing', 1, {}) is not None\nprint('ok')", work,
              extra_env={"ENGRAM_HOME": str(home)}, path=flat)
    assert r2.returncode == 0 and r2.stdout.strip() == "ok", r2.stderr
    assert list((home / "logs" / "sessions").glob("*.l1.jsonl"))


# ------------------------------------------------------------------------------------------ r: identical refusal

def test_r1_the_runtime_module_refusal_text_equals_the_canonical_text(tmp_path, monkeypatch):
    import engram_home_resolver as rr
    spec = importlib.util.spec_from_file_location("_canon_owner_read_step2", SRC / "tools" / "_owner_read.py")
    canon = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = canon  # the module defines dataclasses, which look themselves up by name
    sys.path.insert(0, str(SRC))
    try:
        spec.loader.exec_module(canon)
    finally:
        sys.path.remove(str(SRC))
        sys.modules.pop(spec.name, None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(ValueError) as e:
        rr.engram_home()
    with pytest.raises(ValueError) as e2:
        canon.engram_home()
    assert str(e.value) == str(e2.value) == TEXT


def test_r2_the_runtime_module_is_pinned_in_the_parity_table_and_the_gate():
    """Mutation: drop the module from the parity LAYERS table -> red here (the parity cells would simply stop
    covering it and stay green); drop it from the gate's PINNED_RESOLVERS -> red here and in the gate's cells."""
    spec = importlib.util.spec_from_file_location("_parity_for_step2", REPO / "tests" / "test_engram_home_parity_3863.py")
    parity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parity)
    assert parity.LAYERS.get("runtime") == SRC / "engram_home_resolver.py"
    gate_spec = importlib.util.spec_from_file_location("_gate_for_step2", REPO / "tools" / "raw_home_read_gate.py")
    gate = importlib.util.module_from_spec(gate_spec)
    gate_spec.loader.exec_module(gate)
    assert "src/engram/engram_home_resolver.py" in gate.PINNED_RESOLVERS
