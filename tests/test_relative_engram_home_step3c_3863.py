"""#3863 step 3c-ii: the ten repo-root dev-tool sites (nine files) stop reading ENGRAM_HOME raw.

Ruling: Borges's rulings on #3863, 2026-10-21 (entry points refuse; one shared resolver, tools/_owner_read.engram_home()).
The Luria default on 3c-ii (comment on #3863, pending Borges's answer to question 1): the T3 dev tools call the shared
resolver and each CLI turns its ValueError into ONE stderr line and exit 2, the same outcome as the step-3a/3b tools.

Contract, one parametrised cell per tool x case, so a tool that accepts the relative value reddens only its cell:
  rel    a RELATIVE ENGRAM_HOME: exit 2, exactly ONE stderr line carrying the one refusal text, nothing on stdout, and
         nothing created, changed or removed under the cwd, the (temporary) HOME or the inputs dir. A fixture is
         planted under cwd/rel/x, so a tool that wrongly accepts the value would act on it.
  unset  ENGRAM_HOME unset: the default ~/.engram. Output byte-identical to origin/dev (the golden text in each check
         was produced by running the tool at origin/dev against the same fixture).
  abs    an absolute value: same, under that directory.
  empty  `ENGRAM_HOME=""` means the default home (DECISION, as in step 3b).
  tilde  `ENGRAM_HOME="~/x"` expands (DECISION, as in step 3b).
  help   `--help` with a RELATIVE value does not raise (no tool may compute the home while building its parser).
  imp    importing the module with a RELATIVE value does not raise (the import-time constants record a refusal).
  over   an explicit override (--engram-home, --db, --db-path, --ledger, --real-store) never reads the variable, so a
         relative ENGRAM_HOME beside it is not refused. Exception, stated and pinned: propose-generalizes-migration
         imports engram_core, which itself refuses a relative home at import, so the tool refuses even beside --db.
The HOME of every run is a temporary directory, so a tool cannot touch the real home; the real home is not listed.

engramd_a6_latency has two home reads behind its own entry paths, each reached without a build or a layout child:
  store    `--store <home>/knowledge.db` with a tiny sqlite planted there: `_is_live_store` compares the path to the
           home's store and refuses ("this seat's live store"), exit 2, before anything is built.
  surface  `--hook surface` with no --real-store: the real-store source defaults to <home>/knowledge.db and, when it is
           not a file, the run stops with exit 2 before anything is built.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
REL = "rel/x"
REFUSAL = "ENGRAM_HOME must be an absolute path, got 'rel/x'"


# ------------------------------------------------------------------------------------------------ the harness

def _env(home: Path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "ENGRAM_REPO", "ENGRAM_PY",
                        "ENGRAM_RUNTIME_DIR")}
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


def _sqlite(path: Path, ddl: str, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(ddl)
    for sql, params in rows:
        conn.execute(sql, params)
    conn.commit()
    conn.close()


# Each cell: argv(ctx) -> list (after the interpreter, or after `-m <module>`), plant(root, ctx) the fixture under the
# would-be home, check(r, H, ctx) the success golden, the prefix of the refusal line, and the per-tool run shape.

def _mk(root, ctx=None):
    root.mkdir(parents=True, exist_ok=True)


# -- engram-backfill-special-moment-edges.py: no knowledge.db at the home -> its own "not found" line, exit 1
def _check_backfill(r, H, ctx):
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == f"ERROR: knowledge.db not found at {H}/knowledge.db\n", r.stderr


# -- engramd_a6_latency.py (two entry paths)
def _plant_a6_store(root, ctx):
    _sqlite(root / "knowledge.db", "CREATE TABLE nodes (id TEXT)")


def _argv_a6_store(ctx):
    return ["--store", str(ctx["home_for_store"] / "knowledge.db")]


def _check_a6_store(r, H, ctx):
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == (f"engramd_a6_latency: error -- {H}/knowledge.db is this seat's live store; "
                        "refusing. Pass a COPY of a test store.\n"), r.stderr


def _check_a6_surface(r, H, ctx):
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == (f"engramd_a6_latency: error -- the real-store source {H}/knowledge.db is not a file; the "
                        "real-store copy is the GATE cell. Name one with --real-store PATH (or --no-real-store for a "
                        "synthetic-only run, which is never a pass).\n"), r.stderr


# -- perf/bench_in_turn_recall.py: prints a JSON report that carries the home it measured; the timings are masked
def _check_bench(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    doc = json.loads(r.stdout)
    assert doc["engram_home"] == str(H), doc["engram_home"]
    assert (doc["n_calls"], doc["min_idf"], doc["min_idf_ratio"]) == (1, None, 0.7)
    assert sorted(doc["scenarios"]) == ["A_baseline_disabled", "B_worst_case_enabled", "C_realistic_mix"]


# -- probes/mods-recall/novel_terms.py: no knowledge.db -> the store-path classifier's line, exit 1
def _check_novel(r, H, ctx):
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == f"novel_terms: ENGRAM database not found: {H}/knowledge.db\n", r.stderr


# -- propose-generalizes-migration.py: no knowledge.db -> its refusal, exit 2, no report written
def _check_propose(r, H, ctx):
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == (f"ENGRAM database not found: {H}/knowledge.db\n"
                        "Refusing to emit a report — an empty report and an unreadable database must not look "
                        "alike.\n"), r.stderr
    assert not (ctx["inputs"] / "report.md").exists()


# -- rc-upgrade-verify.py: it runs main() at import; no knowledge.db -> _first() names the path it tried, exit 1
def _check_rc(r, H, ctx):
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr == (f"FAIL: could not locate your knowledge.db. Tried:\n  - {H}/knowledge.db\n"
                        "Set the matching env var and re-run (ENGRAM_HOME / ENGRAM_REPO / ENGRAM_PY).\n"), r.stderr


# -- recall_measurement_harness.py: a JSON report that carries the home and the three paths derived from it
def _check_harness(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    doc = json.loads(r.stdout)
    assert doc["engram_home"] == str(H), doc["engram_home"]
    assert doc["seat"] == H.name, doc["seat"]
    assert doc["sources"] == {
        "ledger_path": f"{H}/in-turn-recall-ledger.jsonl",
        "surface_ledger_path": f"{H}/surface-ledger.json",
        "index_db_path": f"{H}/logs/index.db",
    }, doc["sources"]


# -- recall_repetition_analysis.py: one planted surface-fire event -> the report names the db it read
def _plant_rra(root, ctx):
    _sqlite(root / "logs" / "index.db",
            "CREATE TABLE events (sessionId TEXT, ts TEXT, event_type TEXT, data TEXT)",
            ("INSERT INTO events VALUES ('s1', '2026-10-01T00:00:00Z', 'engram.surface.fire', ?)",
             (json.dumps({"matched_ids": ["ob_1", "ob_2"]}),)))


def _check_rra(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    assert f"Source: {H}/logs/index.db (engram.surface.fire events)\n" in r.stdout, r.stdout
    assert "Sessions: 1 | surface-fire prompts: 1 | total surfaced IDs (incl. repeats): 2\n" in r.stdout, r.stdout


# -- ws4/extract_spans.py: one planted ledger row (an unlocatable chain fire) -> counts that only THIS home gives
def _plant_ws4(root, ctx):
    root.mkdir(parents=True, exist_ok=True)
    row = {"ts_iso": "2026-09-28T01:00:00Z", "arm": "chain", "ts": 1.0, "transcript_path": "/nonexistent/t.jsonl",
           "tool_use_id": "tu1"}
    (root / "tripwire-fire-ledger.jsonl").write_text(json.dumps(row) + "\n")


def _check_ws4(r, H, ctx):
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stderr == ""
    assert r.stdout == (
        "eligible=1 sampled=1 unsampled=0\n"
        "spans_out=0 unlocatable=1\n"
        "invariant sampled == spans_out + unlocatable: 1 == 0 + 1\n"
        "invariant eligible == sampled + unsampled: 1 == 1 + 0\n"
        "flagged (head_named_in_span): 0\n"), r.stdout


# name -> dict(path or module, argv, plant, check, prefix, extra env, stdin, home_for_store)
CELLS = {
    "backfill": dict(path="engram-backfill-special-moment-edges.py", argv=lambda c: ["--dry-run"], plant=_mk,
                     check=_check_backfill, prefix="engram-backfill-special-moment-edges: "),
    "a6-store": dict(path="engramd_a6_latency.py", argv=_argv_a6_store, plant=_plant_a6_store,
                     check=_check_a6_store, prefix="engramd_a6_latency: error -- "),
    "a6-surface": dict(path="engramd_a6_latency.py", argv=lambda c: ["--hook", "surface"], plant=_mk,
                       check=_check_a6_surface, prefix="engramd_a6_latency: error -- "),
    "bench": dict(path="perf/bench_in_turn_recall.py", argv=lambda c: ["--n-calls", "1", "--concurrency", "1"],
                  plant=_mk, check=_check_bench, prefix="bench_in_turn_recall: "),
    "novel": dict(path="probes/mods-recall/novel_terms.py",
                  argv=lambda c: ["--state", str(c["inputs"] / "seen.json")], plant=_mk, check=_check_novel,
                  prefix="novel_terms: ", stdin="hello world"),
    "propose": dict(path="propose-generalizes-migration.py",
                    argv=lambda c: ["--out", str(c["inputs"] / "report.md")], plant=_mk, check=_check_propose,
                    prefix="propose-generalizes-migration: "),
    "rc": dict(path="rc-upgrade-verify.py", argv=lambda c: [], plant=_mk, check=_check_rc,
               prefix="rc-upgrade-verify: ", repo_env=True),
    "harness": dict(path="recall_measurement_harness.py", argv=lambda c: ["--format", "json"], plant=_mk,
                    check=_check_harness, prefix="recall_measurement_harness: "),
    "rra": dict(path="recall_repetition_analysis.py", argv=lambda c: [], plant=_plant_rra, check=_check_rra,
                prefix="recall_repetition_analysis: "),
    "ws4": dict(module="tools.ws4.extract_spans",
                argv=lambda c: ["--date", "2026-09-28", "--out", str(c["inputs"] / "ws4-out")], plant=_plant_ws4,
                check=_check_ws4, prefix="extract_spans: "),
}
CELL_IDS = list(CELLS)
# the tools that parse arguments (rc-upgrade-verify runs main() at import and takes none)
HELP_IDS = [k for k in CELLS if k != "rc"]


class Run:
    """One scratch world: a temporary HOME, a cwd, an inputs dir. Paths are resolved (macOS /var -> /private/var)."""

    def __init__(self, tmp_path: Path):
        base = tmp_path.resolve()
        self.home = base / "home"
        self.cwd = base / "work"
        self.inputs = base / "inputs"
        for d in (self.home, self.cwd, self.inputs):
            d.mkdir()
        self.ctx = {"inputs": self.inputs, "home_for_store": self.home / ".engram"}

    def command(self, name, argv):
        cell = CELLS[name]
        extra = {}
        if "module" in cell:
            extra["PYTHONPATH"] = str(REPO)
            cmd = [sys.executable, "-m", cell["module"], *argv]
        else:
            cmd = [sys.executable, str(TOOLS / cell["path"]), *argv]
        if cell.get("repo_env"):
            extra.update(ENGRAM_REPO=str(REPO), ENGRAM_PY=sys.executable)
        return cmd, extra

    def go(self, name, env_value, root, extra_args=(), plant=True, argv=None):
        cell = CELLS[name]
        self.ctx["home_for_store"] = root
        if plant:
            cell["plant"](root, self.ctx)
        cmd, extra = self.command(name, list(argv if argv is not None else cell["argv"](self.ctx)) + list(extra_args))
        env = _env(self.home, **extra) if env_value is None else _env(self.home, ENGRAM_HOME=env_value, **extra)
        return subprocess.run(cmd, cwd=self.cwd, env=env, capture_output=True, text=True, timeout=300,
                              input=cell.get("stdin", ""))


# ------------------------------------------------------------------------------------------------- rel / ok cells

@pytest.mark.parametrize("name", CELL_IDS)
def test_rel_a_relative_home_is_refused_with_one_line_and_nothing_is_written(name, tmp_path):
    """The ruled outcome for a CLI: exit 2, one stderr line naming the value, nothing on stdout, nothing written
    anywhere. Mutation: accept the relative value again -> red (the tool runs on the planted cwd/rel/x)."""
    w = Run(tmp_path)
    w.ctx["home_for_store"] = w.cwd / "rel" / "x"
    CELLS[name]["plant"](w.cwd / "rel" / "x", w.ctx)
    before = _snapshot(w.cwd, w.home, w.inputs)
    r = w.go(name, REL, w.cwd / "rel" / "x", plant=False)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == "", r.stdout
    assert len(_lines(r.stderr)) == 1 and REFUSAL in r.stderr, r.stderr
    assert r.stderr.startswith(CELLS[name]["prefix"]), r.stderr
    assert _snapshot(w.cwd, w.home, w.inputs) == before


@pytest.mark.parametrize("name", CELL_IDS)
def test_unset_resolves_the_default_home(name, tmp_path):
    w = Run(tmp_path)
    root = w.home / ".engram"
    r = w.go(name, None, root)
    CELLS[name]["check"](r, root, w.ctx)


@pytest.mark.parametrize("name", CELL_IDS)
def test_abs_an_absolute_home_is_used(name, tmp_path):
    w = Run(tmp_path)
    root = w.inputs / "my home"
    r = w.go(name, str(root), root)
    CELLS[name]["check"](r, root, w.ctx)


@pytest.mark.parametrize("name", CELL_IDS)
def test_empty_string_means_the_default_home(name, tmp_path):
    """DECISION (PR body): `ENGRAM_HOME=""` falls back to ~/.engram in every tool."""
    w = Run(tmp_path)
    root = w.home / ".engram"
    r = w.go(name, "", root)
    CELLS[name]["check"](r, root, w.ctx)


@pytest.mark.parametrize("name", CELL_IDS)
def test_tilde_is_expanded(name, tmp_path):
    """DECISION (PR body): `ENGRAM_HOME="~/x"` expands, in every tool."""
    w = Run(tmp_path)
    root = w.home / "x"
    r = w.go(name, "~/x", root)
    CELLS[name]["check"](r, root, w.ctx)
    assert not (w.cwd / "~").exists()


@pytest.mark.parametrize("name", HELP_IDS)
def test_help_with_a_relative_home_never_raises(name, tmp_path):
    """No tool may compute the home while building its parser (or at import) in a way that raises: `--help` works."""
    w = Run(tmp_path)
    r = w.go(name, REL, w.cwd / "rel" / "x", plant=False, argv=["--help"])
    assert r.returncode == 0 and "usage" in r.stdout.lower(), (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr and REFUSAL not in r.stderr, r.stderr


_IMPORT = (
    "import importlib, importlib.util, sys\n"
    "target = sys.argv[1]\n"
    "if target.endswith('.py'):\n"
    "    spec = importlib.util.spec_from_file_location('_step3c_probe', target)\n"
    "    mod = importlib.util.module_from_spec(spec)\n"
    "    sys.modules['_step3c_probe'] = mod\n"
    "    spec.loader.exec_module(mod)\n"
    "else:\n"
    "    importlib.import_module(target)\n"
    "print('imported')\n"
)


@pytest.mark.parametrize("name", HELP_IDS)
def test_imp_importing_the_module_with_a_relative_home_never_raises(name, tmp_path):
    """The import-time constants (bench's DEFAULT_ENGRAM_HOME, propose's engram_core import, the harness) record a
    refusal instead of raising; main() refuses at its first action."""
    w = Run(tmp_path)
    cell = CELLS[name]
    target = cell.get("module") or str(TOOLS / cell["path"])
    env = _env(w.home, ENGRAM_HOME=REL, PYTHONPATH=str(REPO))
    r = subprocess.run([sys.executable, "-c", _IMPORT, target], cwd=w.cwd, env=env, capture_output=True, text=True,
                       timeout=300)
    assert (r.returncode, r.stdout.strip()) == (0, "imported"), (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr


def test_imp_bench_default_matches_the_resolver_when_the_home_is_absolute(tmp_path):
    w = Run(tmp_path)
    code = ("import importlib.util, sys\n"
            "s = importlib.util.spec_from_file_location('b', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(s); sys.modules['b'] = m; s.loader.exec_module(m)\n"
            "print(m.DEFAULT_ENGRAM_HOME)\n")
    for value, want in ((None, w.home / ".engram"), ("", w.home / ".engram"), ("~/x", w.home / "x"),
                        (str(w.inputs / "abs"), w.inputs / "abs")):
        env = _env(w.home, PYTHONPATH=str(REPO)) if value is None else _env(w.home, ENGRAM_HOME=value,
                                                                          PYTHONPATH=str(REPO))
        r = subprocess.run([sys.executable, "-c", code, str(TOOLS / "perf" / "bench_in_turn_recall.py")], cwd=w.cwd,
                           env=env, capture_output=True, text=True, timeout=300)
        assert (r.returncode, r.stdout.strip()) == (0, str(want)), (value, r.returncode, r.stdout, r.stderr)


# ------------------------------------------------------------------------- explicit overrides never read the env

def _run_with_rel(w, name, argv, plant_root=None):
    cell = CELLS[name]
    cmd, extra = w.command(name, argv)
    return subprocess.run(cmd, cwd=w.cwd, env=_env(w.home, ENGRAM_HOME=REL, **extra), capture_output=True, text=True,
                          timeout=300, input=cell.get("stdin", ""))


def test_over_backfill_an_explicit_engram_home_wins(tmp_path):
    w = Run(tmp_path)
    root = w.inputs / "explicit"
    r = _run_with_rel(w, "backfill", ["--engram-home", str(root), "--dry-run"])
    assert (r.returncode, r.stdout, r.stderr) == (1, "", f"ERROR: knowledge.db not found at {root}/knowledge.db\n"), r


def test_over_bench_an_explicit_engram_home_wins(tmp_path):
    w = Run(tmp_path)
    root = w.inputs / "explicit"
    root.mkdir()
    r = _run_with_rel(w, "bench", ["--engram-home", str(root), "--n-calls", "1", "--concurrency", "1"])
    assert r.returncode == 0 and json.loads(r.stdout)["engram_home"] == str(root), r


def test_over_novel_an_explicit_db_wins(tmp_path):
    w = Run(tmp_path)
    db = w.inputs / "none.db"
    r = _run_with_rel(w, "novel", ["--state", str(w.inputs / "seen.json"), "--db", str(db)])
    assert (r.returncode, r.stdout, r.stderr) == (1, "", f"novel_terms: ENGRAM database not found: {db}\n"), r


def test_over_harness_an_explicit_engram_home_wins(tmp_path):
    w = Run(tmp_path)
    root = w.inputs / "explicit"
    root.mkdir()
    r = _run_with_rel(w, "harness", ["--format", "json", "--engram-home", str(root)])
    assert r.returncode == 0 and r.stderr == "" and json.loads(r.stdout)["engram_home"] == str(root), r


def test_over_rra_an_explicit_db_path_wins(tmp_path):
    w = Run(tmp_path)
    _plant_rra(w.inputs / "explicit", w.ctx)
    db = w.inputs / "explicit" / "logs" / "index.db"
    r = _run_with_rel(w, "rra", ["--db-path", str(db)])
    assert r.returncode == 0 and f"Source: {db} " in r.stdout and r.stderr == "", r


def test_over_ws4_an_explicit_ledger_wins(tmp_path):
    w = Run(tmp_path)
    _plant_ws4(w.inputs / "explicit", w.ctx)
    r = _run_with_rel(w, "ws4", ["--date", "2026-09-28", "--out", str(w.inputs / "o"), "--ledger",
                                 str(w.inputs / "explicit" / "tripwire-fire-ledger.jsonl")])
    _check_ws4(r, w.inputs / "explicit", w.ctx)


def test_over_a6_an_explicit_real_store_wins(tmp_path):
    w = Run(tmp_path)
    src = w.inputs / "nope.db"
    r = _run_with_rel(w, "a6-surface", ["--hook", "surface", "--real-store", str(src)])
    assert r.returncode == 2 and r.stderr.startswith(
        f"engramd_a6_latency: error -- the real-store source {src} is not a file;"), r


def test_over_propose_refuses_even_beside_an_explicit_db(tmp_path):
    """DECISION (PR body): engram_core, which the tool imports, refuses a relative home at import (step 2), so an
    explicit --db cannot rescue the run; the tool says so in one line instead of a traceback."""
    w = Run(tmp_path)
    r = _run_with_rel(w, "propose", ["--out", str(w.inputs / "report.md"), "--db", str(w.inputs / "x.db")])
    assert r.returncode == 2 and r.stdout == "" and len(_lines(r.stderr)) == 1 and REFUSAL in r.stderr, r
    assert not (w.inputs / "report.md").exists()


def test_a6_the_home_is_not_read_when_no_entry_path_needs_it(tmp_path):
    """DECISION (PR body): a6 refuses a relative value only on the two paths that read the home (--store, and
    --hook surface without --real-store); a run that reads neither is not refused for it."""
    w = Run(tmp_path)
    r = _run_with_rel(w, "a6-surface", ["--n", "1"])  # N below the floor: stops at its own exit-2 line, home unread
    assert r.returncode == 2 and REFUSAL not in r.stderr and "N must be >=" in r.stderr, r
