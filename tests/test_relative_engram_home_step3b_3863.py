"""#3863 step 3b: the 13 tools under src/engram/tools/ stop reading ENGRAM_HOME raw.

Ruling: Borges's rulings on #3863, 2026-10-21 (entry points raise or refuse; a library a hook or server
imports degrades into its own typed refusal; a standalone script carries a verbatim pinned copy of
tools/_owner_read.engram_home() only where it cannot import the sibling).

Contract, one parametrised cell per tool x case, so a tool that accepts the relative value reddens only its cell:
  rel    a RELATIVE ENGRAM_HOME: exit 2, exactly ONE stderr line carrying the one refusal text, nothing on
         stdout, and nothing created, changed or removed under the cwd or the (temporary) home. A full
         fixture is planted under cwd/rel/x, so a tool that wrongly accepts the value would act on it.
  unset  ENGRAM_HOME unset: the default ~/.engram. Output and effect byte-identical to origin/dev (the golden
         text below was diffed against an origin/dev worktree).
  abs    an absolute value: same, under that directory.
  empty  `ENGRAM_HOME=""` means the default (DECISION: it used to be Path("") = the cwd in codex-umwelt-wake and
         supervision_probe; now the default, as everywhere else).
  tilde  `ENGRAM_HOME="~/x"` expands (DECISION: it used to be the literal relative path ./~/x in ten of the twelve
         CLIs below, and in baton_gate, which read the variable raw).
  over   an explicit override (--db, --home, --engram-home, --config) never reads the variable, so a relative
         ENGRAM_HOME beside it is not refused.
  lay    the same absolute cell with the tool copied into a flat layout (tools/ under a plugin root), so the
         sibling import works where the source layout does not exist.
baton_gate is a library loaded by path (the forum server `cp -L`s only that file), so it carries a pinned copy and
refuses with its own typed error; those cells are the `bg*` ones below.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
TOOLS = SRC / "tools"
# The #728 advisory engram_core prints at import time on an interpreter without sqlite-vec. It is deliberately
# loud ("Loud, not silent"), so surgical.py's stderr carries it there; the test tolerates exactly this line.
SQLITE_VEC_ADVISORY_PREFIX = "[engram] sqlite-vec unavailable"
REL = "rel/x"
REFUSAL = "ENGRAM_HOME must be an absolute path, got 'rel/x'"


# ------------------------------------------------------------------------------------------------ the harness

def _env(home: Path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "BATON_POOL_SENTINEL")}
    env.update({"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra)
    return env


def _snapshot(*roots: Path) -> dict:
    """Every path under the roots -> (kind, size, sha256 of the content): a created, changed or removed entry shows."""
    out = {}
    for root in roots:
        for p, dirs, files in os.walk(root):
            for name in dirs:
                out[str(Path(p) / name)] = ("dir", 0, "")
            for name in files:
                fp = Path(p) / name
                out[str(fp)] = ("file", fp.stat().st_size, hashlib.sha256(fp.read_bytes()).hexdigest())
    return out


def _lines(text):
    return [l for l in text.splitlines() if l.strip()]


def _store(root: Path, extra_columns=False):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "knowledge.db"))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, embedding TEXT, "
                 "confidence_history TEXT, metadata TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'observation', NULL, '[{\"value\": 0.5}]', '{}')")
    conn.commit()
    conn.close()


# Each tool: argv(ctx) -> list, plant(root, ctx) -> None (the fixture under the would-be home), check(...) for the
# success cells. `ctx` is a dict of per-test scratch paths. A check gets the resolved home as `H` and the result.

def _argv_ack(ctx):
    return []


def _plant_ack(root, ctx):
    root.mkdir(parents=True, exist_ok=True)
    (root / "marker.txt").write_text("m")


def _check_ack(r, H, ctx):
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == f"no such database: {H}/knowledge.db\n", r.stderr


def _argv_migrate(ctx):
    return ["--dry-run", "--projects-dir", str(ctx["inputs"] / "projects"), "--forum-url", "http://localhost:1"]


def _plant_migrate(root, ctx):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"agent_name": "migrator"}))
    (ctx["inputs"] / "projects").mkdir(parents=True, exist_ok=True)


def _check_migrate(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    assert r.stdout == (
        "baton-migrate-to-coord-store [DRY-RUN]\n"
        f"  projects-dir: {ctx['inputs'] / 'projects'}\n"
        "  forum-url:    http://localhost:1\n"
        "  agent:        migrator\n"
        "\n"
        "\n"
        # #3901 added the separate "closed" tally (closed legacy batons are not imported).
        "Summary: would import 0, skipped 0, closed 0, failed 0\n"
    ), r.stdout


def _argv_cites(ctx):
    return ["--report", str(ctx["inputs"] / "report.json"), "--phases", "a"]


def _plant_cites(root, ctx):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text("{}")
    (ctx["inputs"] / "report.json").write_text(json.dumps(
        {"seat": "s", "lineage": "l", "frame": {"seed": 1}, "strata": [], "edges": []}))


def _check_cites(r, H, ctx):
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == f"REFUSED: config.json at {H} lacks agent_name / self_lineage\n", r.stderr


def _argv_codex(ctx):
    return ["status"]


def _plant_codex(root, ctx):
    root.mkdir(parents=True, exist_ok=True)


def _check_codex(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    doc = json.loads(r.stdout)
    assert doc["log_path"] == f"{H}/codex-wake/watcher.log", doc
    assert doc["state_path"] == f"{H}/codex-wake/state.json", doc
    assert doc["mode"] == "stopped" and doc["running"] is False, doc


def _argv_compact(ctx):
    return ["--clear"]


def _plant_compact(root, ctx):
    (root / "compact").mkdir(parents=True, exist_ok=True)
    (root / "compact" / "request.json").write_text("{}")
    (root / "compact" / "context.md").write_text("c")


def _check_compact(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert (r.stdout, r.stderr) == ("compact-request: cleared\n", "")
    assert not (H / "compact" / "request.json").exists()
    assert not (H / "compact" / "context.md").exists()


def _argv_dream(ctx):
    return ["--pick", "1", "did it"]


def _plant_dream(root, ctx):
    (root / "history" / "dream").mkdir(parents=True, exist_ok=True)
    (root / "history" / "dream" / "2026-10-01.md").write_text("# Dream\n\n## For the awake seat\n\n1. First item\n")


def _check_dream(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    assert r.stdout == f"wrote p1 (picked up) to {H}/history/dream/2026-10-01.md\n", r.stdout
    assert "p1. picked up did it" in (H / "history" / "dream" / "2026-10-01.md").read_text()


def _argv_erase(ctx):
    return []


_WARM = "Letter line\n\n## From this session\n\nvolatile text\n"


def _plant_erase(root, ctx):
    root.mkdir(parents=True, exist_ok=True)
    (root / "warm-briefing.md").write_text(_WARM)


def _check_erase(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    assert r.stdout.startswith("erased: kept "), r.stdout
    assert (H / "warm-briefing.md").read_text() == "Letter line\n\n## From this session\n"


def _argv_inspect(ctx):
    return ["ob_1"]


def _plant_inspect(root, ctx):
    _store(root)


def _check_inspect(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    assert r.stdout.startswith(f"Node: ob_1  (type=observation)\nDB: {H}/knowledge.db\n"), r.stdout


def _argv_forum_url(ctx):
    return ["http://localhost:5002"]


def _plant_forum_url(root, ctx):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text("{}")


def _check_forum_url(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    # The planted config differs from the result, so the guard saves it first and prints one
    # `  backup: <file> -> <backup>` line before the tool's own line.
    backup_line, written_line = r.stdout.splitlines(keepends=True)
    assert backup_line.startswith("  backup: ") and ".engram-backup." in backup_line, r.stdout
    assert (written_line, r.stderr) == ("written: forum.url set to http://localhost:5002\n", "")
    assert json.loads((H / "config.json").read_text()) == {"forum": {"url": "http://localhost:5002"}}


def _argv_probe(ctx):
    return ["--proc-root", str(ctx["inputs"] / "proc")]


def _plant_probe(root, ctx):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"agent_name": "alice"}))


def _check_probe(r, H, ctx):
    # rc 1: nothing is running in the empty proc root. The seat name comes from THIS home's config.json, so the
    # line proves which home was read. (The tail of each line names the host's supervisor, which differs by OS.)
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert r.stderr == ""
    first, second = _lines(r.stdout)
    assert first.startswith("alice daemon pid=none "), r.stdout
    assert second.startswith("alice mcp-server pid=none "), r.stdout


def _argv_surgical(ctx):
    return ["preview", "recompute_confidence", "--node-id", "ob_9", "--reason", "r", "--bug-ref", "ob_1"]


def _plant_surgical(root, ctx):
    _store(root)


def _check_surgical(r, H, ctx):
    rest = [ln for ln in r.stderr.splitlines() if not ln.startswith(SQLITE_VEC_ADVISORY_PREFIX)]
    assert rest == [], r.stderr
    doc = json.loads(r.stdout)
    assert "ob_9" in json.dumps(doc), doc  # the store was opened and the node looked up in it
    assert r.returncode == 0, (r.returncode, r.stdout)


def _argv_sleep(ctx):
    return ["5", "3", "2026-10-01T00:00:00Z"]


def _plant_sleep(root, ctx):
    root.mkdir(parents=True, exist_ok=True)


def _check_sleep(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    assert r.stdout == f"wrote {H}/sessions/last-sleep-success.json: turn 5, 3 nodes consolidated\n", r.stdout
    marker = json.loads((H / "sessions" / "last-sleep-success.json").read_text())
    assert (marker["turn_advanced_to"], marker["nodes_consolidated"]) == (5, 3)


# tool file -> (argv, plant, check, stderr prefix of the refusal line, needs the shim layout copy?)
CELLS = {
    "ack_recompute_sweep.py": (_argv_ack, _plant_ack, _check_ack, "ack_recompute_sweep: "),
    "baton-migrate-to-coord-store.py": (_argv_migrate, _plant_migrate, _check_migrate, "baton-migrate: "),
    "cites_step3.py": (_argv_cites, _plant_cites, _check_cites, "REFUSED: "),
    "codex-umwelt-wake.py": (_argv_codex, _plant_codex, _check_codex, "codex-umwelt-wake: "),
    "compact-request.py": (_argv_compact, _plant_compact, _check_compact, "compact-request: "),
    "dream_pickup.py": (_argv_dream, _plant_dream, _check_dream, "dream_pickup: "),
    "erase_session_block.py": (_argv_erase, _plant_erase, _check_erase, "erase_session_block: "),
    "inspect_raw.py": (_argv_inspect, _plant_inspect, _check_inspect, "ERROR: "),
    "set_forum_url.py": (_argv_forum_url, _plant_forum_url, _check_forum_url, "set_forum_url: "),
    "supervision_probe.py": (_argv_probe, _plant_probe, _check_probe, "supervision_probe: "),
    "surgical.py": (_argv_surgical, _plant_surgical, _check_surgical, "surgical: "),
    "write_sleep_marker.py": (_argv_sleep, _plant_sleep, _check_sleep, "write_sleep_marker: "),
}
TOOL_IDS = list(CELLS)


class Run:
    """One scratch world: a temporary HOME, a cwd, an inputs dir. Paths are resolved (macOS /var -> /private/var)."""

    def __init__(self, tmp_path: Path):
        base = tmp_path.resolve()
        self.home = base / "home"
        self.cwd = base / "work"
        self.inputs = base / "inputs"
        for d in (self.home, self.cwd, self.inputs):
            d.mkdir()
        (self.inputs / "proc").mkdir()
        self.ctx = {"inputs": self.inputs}
        self.tools = TOOLS

    def go(self, tool, env_value, root, extra_args=(), tools_dir=None, plant=True):
        argv_fn, plant_fn, _, _ = CELLS[tool]
        if plant:
            plant_fn(root, self.ctx)
        env = _env(self.home) if env_value is None else _env(self.home, ENGRAM_HOME=env_value)
        script = (tools_dir or self.tools) / tool
        return subprocess.run([sys.executable, str(script), *argv_fn(self.ctx), *extra_args], cwd=self.cwd,
                              env=env, capture_output=True, text=True, timeout=120)


def _need(tool):
    if tool == "codex-umwelt-wake.py":
        pytest.importorskip("websockets")


# ------------------------------------------------------------------------------------------------- rel / ok cells

@pytest.mark.parametrize("tool", TOOL_IDS)
def test_rel_a_relative_home_is_refused_with_one_line_and_nothing_is_written(tool, tmp_path):
    """The ruled outcome for a CLI: exit 2, one stderr line naming the value, nothing on stdout, nothing written
    anywhere. Mutation: accept the relative value again -> red (the tool runs on the planted cwd/rel/x)."""
    _need(tool)
    w = Run(tmp_path)
    CELLS[tool][1](w.cwd / "rel" / "x", w.ctx)
    before = _snapshot(w.cwd, w.home)
    r = w.go(tool, REL, w.cwd / "rel" / "x", plant=False)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == "", r.stdout
    assert len(_lines(r.stderr)) == 1 and REFUSAL in r.stderr, r.stderr
    assert r.stderr.startswith(CELLS[tool][3]), r.stderr
    assert _snapshot(w.cwd, w.home) == before


@pytest.mark.parametrize("tool", TOOL_IDS)
def test_unset_resolves_the_default_home(tool, tmp_path):
    _need(tool)
    w = Run(tmp_path)
    root = w.home / ".engram"
    r = w.go(tool, None, root)
    CELLS[tool][2](r, root, w.ctx)


@pytest.mark.parametrize("tool", TOOL_IDS)
def test_abs_an_absolute_home_is_used(tool, tmp_path):
    _need(tool)
    w = Run(tmp_path)
    root = w.inputs / "my home"
    r = w.go(tool, str(root), root)
    CELLS[tool][2](r, root, w.ctx)


@pytest.mark.parametrize("tool", TOOL_IDS)
def test_empty_string_means_the_default_home(tool, tmp_path):
    """DECISION (PR body): `ENGRAM_HOME=""` falls back to ~/.engram in every tool. It was Path("") (the cwd) in
    codex-umwelt-wake and supervision_probe."""
    _need(tool)
    w = Run(tmp_path)
    root = w.home / ".engram"
    r = w.go(tool, "", root)
    CELLS[tool][2](r, root, w.ctx)


@pytest.mark.parametrize("tool", TOOL_IDS)
def test_tilde_is_expanded(tool, tmp_path):
    """DECISION (PR body): `ENGRAM_HOME="~/x"` expands, in every tool. It was the literal path ./~/x in the ten
    CLIs below that read the variable raw (cites_step3 and supervision_probe already expanded it)."""
    _need(tool)
    w = Run(tmp_path)
    root = w.home / "x"
    r = w.go(tool, "~/x", root)
    CELLS[tool][2](r, root, w.ctx)
    assert not (w.cwd / "~").exists()


@pytest.mark.parametrize("tool", TOOL_IDS)
def test_lay_the_sibling_import_works_in_a_flat_plugin_layout(tool, tmp_path):
    """The deployed layout has tools/ directly under the plugin root. Copy the top-level runtime modules and
    tools/ into a bare dir (no src/engram above it) and run the absolute cell from there."""
    _need(tool)
    w = Run(tmp_path)
    plugin = tmp_path.resolve() / "plugin_root"
    plugin.mkdir()
    for p in SRC.glob("*.py"):
        shutil.copy(p, plugin / p.name)
    shutil.copytree(TOOLS, plugin / "tools", ignore=shutil.ignore_patterns("__pycache__", "mods", "engram-pkg"))
    root = w.inputs / "my home"
    r = w.go(tool, str(root), root, tools_dir=plugin / "tools")
    CELLS[tool][2](r, root, w.ctx)


# ------------------------------------------------------------------------- explicit overrides never read the env

def test_over_ack_an_explicit_db_does_not_read_the_variable(tmp_path):
    w = Run(tmp_path)
    r = subprocess.run([sys.executable, str(TOOLS / "ack_recompute_sweep.py"), "--db", str(w.inputs / "none.db")],
                       cwd=w.cwd, env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert (r.returncode, r.stderr) == (2, f"no such database: {w.inputs}/none.db\n"), r


def test_over_dream_an_explicit_home_stays_raw_and_wins(tmp_path):
    """DECISION: --home stays raw and explicit (only the env fallback goes through the resolver)."""
    w = Run(tmp_path)
    root = w.inputs / "explicit"
    _plant_dream(root, w.ctx)
    r = subprocess.run([sys.executable, str(TOOLS / "dream_pickup.py"), *_argv_dream(w.ctx), "--home", str(root)],
                       cwd=w.cwd, env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout == f"wrote p1 (picked up) to {root}/history/dream/2026-10-01.md\n", r


def test_over_probe_an_explicit_engram_home_wins_and_help_never_raises(tmp_path):
    """DECISION: the argparse default is None, so building the parser (for --help) never raises on a relative
    value; the value is refused after parsing, only when it is actually used."""
    w = Run(tmp_path)
    root = w.inputs / "explicit"
    _plant_probe(root, w.ctx)
    r = subprocess.run([sys.executable, str(TOOLS / "supervision_probe.py"), *_argv_probe(w.ctx),
                        "--engram-home", str(root)],
                       cwd=w.cwd, env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and r.stdout.startswith("alice daemon pid=none "), r
    h = subprocess.run([sys.executable, str(TOOLS / "supervision_probe.py"), "--help"], cwd=w.cwd,
                       env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert h.returncode == 0 and "--engram-home" in h.stdout and h.stderr == "", h


def test_over_forum_url_an_explicit_config_does_not_read_the_variable(tmp_path):
    w = Run(tmp_path)
    cfg = w.inputs / "c.json"
    cfg.write_text("{}")
    r = subprocess.run([sys.executable, str(TOOLS / "set_forum_url.py"), "http://localhost:5002", "--config", str(cfg)],
                       cwd=w.cwd, env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.endswith("written: forum.url set to http://localhost:5002\n"), r
    assert r.stdout.count("\n") == 2 and r.stdout.startswith("  backup: "), r  # the guard's one backup line, then the tool's


# ----------------------------------------------------------------------------------------- the daemon's subcommands

@pytest.mark.parametrize("sub", ["run", "start", "stop", "status"])
def test_daemon_every_subcommand_refuses_a_relative_home_before_anything(sub, tmp_path):
    """codex-umwelt-wake is long-lived: refuse at start, before binding, spawning or writing anything, exit 2.
    Mutation: resolve (create the state dir) before refusing -> the snapshot differs -> red."""
    pytest.importorskip("websockets")
    w = Run(tmp_path)
    (w.cwd / "rel" / "x").mkdir(parents=True)
    before = _snapshot(w.cwd, w.home)
    r = subprocess.run([sys.executable, str(TOOLS / "codex-umwelt-wake.py"), sub], cwd=w.cwd,
                       env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and r.stdout == "", (r.returncode, r.stdout, r.stderr)
    assert len(_lines(r.stderr)) == 1 and REFUSAL in r.stderr, r.stderr
    assert _snapshot(w.cwd, w.home) == before


def test_daemon_import_never_raises_on_a_relative_home(tmp_path):
    """DECISION: the module-level constants become non-raising at import (a relative value sets a refusal and a
    poison path); `--help` still works."""
    pytest.importorskip("websockets")
    w = Run(tmp_path)
    r = subprocess.run([sys.executable, str(TOOLS / "codex-umwelt-wake.py"), "--help"], cwd=w.cwd,
                       env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "usage" in r.stdout.lower(), r


def test_compact_request_refuses_before_its_first_action(tmp_path):
    """The old request.json is unlinked at the top of main (any outcome). On a relative value nothing may be touched,
    so the planted cwd/rel/x/compact/request.json survives. Mutation: resolve before refusing -> it is gone -> red."""
    w = Run(tmp_path)
    _plant_compact(w.cwd / "rel" / "x", w.ctx)
    r = subprocess.run([sys.executable, str(TOOLS / "compact-request.py"), "--session", "s1"], cwd=w.cwd,
                       env=_env(w.home, ENGRAM_HOME=REL), capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and REFUSAL in r.stderr and len(_lines(r.stderr)) == 1, r
    assert (w.cwd / "rel" / "x" / "compact" / "request.json").exists()


# -------------------------------------------------------------------------------------------- baton_gate (library)

def _load_gate(name="_baton_gate_3b"):
    spec = importlib.util.spec_from_file_location(name, TOOLS / "baton_gate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    mod._POOL_SENTINEL_CACHE.clear()
    return mod


def _config_at(root: Path, primary_user="Lei Shi"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"primary_user": primary_user}))


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    monkeypatch.delenv("BATON_POOL_SENTINEL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_bg1_a_relative_home_raises_the_servers_typed_error_naming_the_value(clean_env, monkeypatch):
    """DECISION (PR body): baton_gate raises PoolSentinelUnresolved, not ValueError, because the 11 handlers in
    src/forum/server.py map that type to a fail-closed 422; a ValueError would be a Flask 500.
    Mutation: accept the relative value -> the planted cwd/rel/x/config.json is read -> red."""
    gate = _load_gate()
    _config_at(clean_env / "rel" / "x")
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(gate.PoolSentinelUnresolved) as e:
        gate._resolve_pool_sentinel()
    assert REFUSAL in str(e.value)
    assert not gate._POOL_SENTINEL_CACHE


def test_bg2_a_relative_home_is_not_rescued_by_the_env_sentinel(clean_env, monkeypatch):
    gate = _load_gate()
    monkeypatch.setenv("ENGRAM_HOME", REL)
    monkeypatch.setenv("BATON_POOL_SENTINEL", "env-sentinel")
    with pytest.raises(gate.PoolSentinelUnresolved) as e:
        gate._resolve_pool_sentinel()
    assert REFUSAL in str(e.value)


@pytest.mark.parametrize("how", ["unset", "empty", "abs", "tilde"])
def test_bg3_unset_empty_absolute_and_tilde_resolve_the_config(how, clean_env, monkeypatch):
    gate = _load_gate()
    home = clean_env / "home"
    if how in ("unset", "empty"):
        _config_at(home / ".engram")
        if how == "empty":
            monkeypatch.setenv("ENGRAM_HOME", "")
    elif how == "abs":
        _config_at(clean_env / "elsewhere")
        monkeypatch.setenv("ENGRAM_HOME", str(clean_env / "elsewhere"))
    else:
        _config_at(home / "x")
        monkeypatch.setenv("ENGRAM_HOME", "~/x")
    assert gate._resolve_pool_sentinel() == "lei shi"


def test_bg4_an_explicit_home_argument_wins_over_a_relative_env(clean_env, monkeypatch):
    gate = _load_gate()
    _config_at(clean_env / "explicit")
    monkeypatch.setenv("ENGRAM_HOME", REL)
    assert gate._resolve_pool_sentinel(str(clean_env / "explicit")) == "lei shi"


def test_bg5_the_server_handler_maps_the_refusal_to_a_fail_closed_status(clean_env, monkeypatch):
    """Every caller in src/forum/server.py catches `gate.PoolSentinelUnresolved`; pin that the type raised for a
    relative home is the very class those handlers name (the server loads this file as `gate`)."""
    gate = _load_gate()
    monkeypatch.setenv("ENGRAM_HOME", REL)
    with pytest.raises(gate.PoolSentinelUnresolved):
        gate._is_legacy_pool_sentinel("lei")
    handlers = (REPO / "src" / "forum" / "server.py").read_text().count("except gate.PoolSentinelUnresolved")
    assert handlers >= 11, handlers


def test_inspect_raw_import_never_raises_on_a_relative_home_and_points_below_a_file(tmp_path):
    """DECISION: the module constants are non-raising at import. A relative value leaves DB_PATH below /dev/null (a
    file), so nothing can be opened or created by accident; the CLI itself refuses in _get_row (the rel cell)."""
    w = Run(tmp_path)
    code = ("import sys\nsys.path.insert(0, %r)\nimport inspect_raw\nprint(inspect_raw.DB_PATH)\n") % str(TOOLS)
    r = subprocess.run([sys.executable, "-c", code], cwd=w.cwd, env=_env(w.home, ENGRAM_HOME=REL),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().startswith("/dev/null/"), (r.returncode, r.stdout, r.stderr)
    assert _snapshot(w.cwd, w.home) == {}
