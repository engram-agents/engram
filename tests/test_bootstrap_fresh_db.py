"""End-to-end test for bootstrap.py against a fresh empty ENGRAM_HOME.

Catches the chicken-and-egg failure mode where _get_db()'s fail-loud guards
block bootstrap.py's own seeding (bootstrap calls engram_add_axiom, which
goes through _get_db(), which would raise on a missing DB without the
ENGRAM_BOOTSTRAP=1 env bypass).

Origin: colleague-review of PR #577 caught this as a blocker on
the initial commit (smoke-test only rendered error messages, didn't run
the bootstrap end-to-end). This test is the regression guard.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_bootstrap_seeds_fresh_db_under_env_bypass():
    """bootstrap.py against an empty ENGRAM_HOME creates the 6-node seed graph.

    Without the ENGRAM_BOOTSTRAP=1 bypass in _get_db(), bootstrap dies on
    its first engram_add_axiom call because the fail-loud DB-missing guard
    fires. This test exercises the full end-to-end path.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_e2e_"))
    try:
        engram_home = workdir / ".engram"
        claude_home = workdir / ".claude"
        # ENGRAM_HOME must not exist before bootstrap.py — that IS the
        # fresh-install case the bypass exists to support.
        assert not engram_home.exists()
        claude_home.mkdir(parents=True)

        # Mirror the env vars install.sh sets at install.sh:457-463.
        env = os.environ.copy()
        env.update({
            "ALPHA_DIR": str(REPO_ROOT),
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
            "ENGRAM_HOME": str(engram_home),
            "CLAUDE_HOME": str(claude_home),
            "PYTHON_BIN": sys.executable,
            "TARGET": "claude",
        })
        # Defensive: ensure no leftover ENGRAM_BOOTSTRAP from the parent —
        # bootstrap.py sets it itself; we're testing its own setting works.
        env.pop("ENGRAM_BOOTSTRAP", None)

        # bootstrap.py imports server.py from $ENGRAM_HOME (it expects
        # server.py to live alongside the install). For this E2E test we
        # need to either copy server.py into engram_home (mirroring
        # install.sh's step 4) OR run bootstrap from a place where it
        # finds server.py. install.sh copies server.py to ENGRAM_HOME
        # at step 4 BEFORE step 5 runs bootstrap.py. We replicate that
        # minimal step here: copy server.py + engram_*.py runtime
        # modules into engram_home.
        engram_home.mkdir(parents=True)
        runtime_files = [
            "server.py", "engram_client.py", "engram_confidence.py",
            "engram_filter.py", "engram_idf.py", "engram_ids.py",
            "engram_log_emitter.py", "engram_log_indexer.py",
        ]
        for fname in runtime_files:
            src = REPO_ROOT / fname
            if src.exists():
                shutil.copy2(src, engram_home / fname)

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # bootstrap.py exit status reveals the chicken-and-egg failure
        # immediately — if env bypass is missing, the first axiom-add
        # raises RuntimeError(_db_missing_message) and the script dies.
        assert result.returncode == 0, (
            f"bootstrap.py failed:\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )

        # Seed graph created
        db_path = engram_home / "knowledge.db"
        assert db_path.exists(), "bootstrap.py did not create knowledge.db"

        conn = sqlite3.connect(str(db_path))
        try:
            # 3 axioms (honesty, honesty⊥discretion, provenance)
            axiom_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='axiom'"
            ).fetchone()[0]
            assert axiom_count == 3, f"expected 3 axioms, got {axiom_count}"

            # 2 definitions (ENGRAM, epistemic identity)
            def_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='definition'"
            ).fetchone()[0]
            assert def_count == 2, f"expected 2 definitions, got {def_count}"

            # 1 goal (epistemic humility)
            goal_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='goal'"
            ).fetchone()[0]
            assert goal_count == 1, f"expected 1 goal, got {goal_count}"
        finally:
            conn.close()

        # seed-manifest.json + first-run-pending — engram-first-session
        # uses both as bootstrap-completed signals. The first-run-pending
        # assertion is the regression guard for round-3's blocker B fix
        # (bootstrap.py now owns the marker write, not install.sh).
        assert (engram_home / "seed-manifest.json").exists()
        assert (engram_home / "first-run-pending").exists()

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_refuses_overwrite_existing_db():
    """bootstrap.py with an existing knowledge.db raises SystemExit (no escape set).

    Origin: PR #577 added the refuse-overwrite gate as defense-in-depth for
    the plugin path (the retired scatter installer carried the same gate).
    This test guards the gate itself.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_refuse_"))
    try:
        engram_home = workdir / ".engram"
        engram_home.mkdir(parents=True)
        # Plant a dummy knowledge.db so the gate fires.
        (engram_home / "knowledge.db").write_text("")

        env = os.environ.copy()
        env.update({
            "ALPHA_DIR": str(REPO_ROOT),
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
            "ENGRAM_HOME": str(engram_home),
            "CLAUDE_HOME": str(workdir / ".claude"),
            "PYTHON_BIN": sys.executable,
            "TARGET": "claude",
        })
        env.pop("FORCE", None)               # ensure no inherited escape vars
        env.pop("FORCE_RESEED_EMPTY", None)

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # SystemExit produces a non-zero return code.
        assert result.returncode != 0
        # The actionable message points the user at recovery paths.
        assert "refuses to overwrite" in result.stderr or "refuses to overwrite" in result.stdout

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _make_empty_db_env(workdir: Path) -> tuple[Path, dict]:
    """Helper: create an engram_home with an empty knowledge.db and matching env."""
    engram_home = workdir / ".engram"
    engram_home.mkdir(parents=True)
    db_path = engram_home / "knowledge.db"

    # Copy runtime modules so bootstrap can import server.py.
    runtime_files = [
        "server.py", "engram_client.py", "engram_confidence.py",
        "engram_filter.py", "engram_idf.py", "engram_ids.py",
        "engram_log_emitter.py", "engram_log_indexer.py",
    ]
    for fname in runtime_files:
        src = REPO_ROOT / fname
        if src.exists():
            shutil.copy2(src, engram_home / fname)

    # Create the DB file but leave it with no nodes (no tables at all).
    conn = sqlite3.connect(str(db_path))
    conn.close()

    env = os.environ.copy()
    env.update({
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
        "ENGRAM_HOME": str(engram_home),
        "CLAUDE_HOME": str(workdir / ".claude"),
        "PYTHON_BIN": sys.executable,
        "TARGET": "claude",
    })
    env.pop("FORCE", None)
    env.pop("FORCE_RESEED_EMPTY", None)
    return db_path, env


def test_bootstrap_force_on_empty_db_proceeds():
    """FORCE=1 on an empty DB (0 nodes) proceeds and seeds the graph.

    Legitimate use: DB file exists (e.g. from a failed first bootstrap that
    created the file but died before the schema was written) but contains
    no nodes. FORCE=1 is safe here — nothing to corrupt.

    Origin: issue #774 — replacing the old bypass with disciplined-refusal
    + named-escape semantics.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_force_empty_"))
    try:
        db_path, env = _make_empty_db_env(workdir)
        env["FORCE"] = "1"

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"FORCE=1 on empty DB failed (expected success):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        conn = sqlite3.connect(str(db_path))
        try:
            axiom_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='axiom'"
            ).fetchone()[0]
            assert axiom_count == 3, f"expected 3 seed axioms, got {axiom_count}"
        finally:
            conn.close()

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_force_on_nonempty_db_refuses():
    """FORCE=1 on a non-empty DB (nodes present) refuses with exit code 1.

    The old FORCE=1 behavior silently inserted duplicate seed nodes into a
    live identity graph — neither a fresh start nor a safe no-op, just
    silent corruption. The new behavior refuses loudly and points the user
    at the correct recovery path.

    Origin: issue #774 — replacing the old bypass with disciplined-refusal
    + named-escape semantics.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_force_nonempty_"))
    try:
        engram_home = workdir / ".engram"
        engram_home.mkdir(parents=True)
        db_path = engram_home / "knowledge.db"

        # Plant a nodes table with one row to simulate a live (non-empty) graph.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('ax_0001', 'axiom', 'test axiom')"
        )
        conn.commit()
        conn.close()

        env = os.environ.copy()
        env.update({
            "ALPHA_DIR": str(REPO_ROOT),
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
            "ENGRAM_HOME": str(engram_home),
            "CLAUDE_HOME": str(workdir / ".claude"),
            "PYTHON_BIN": sys.executable,
            "TARGET": "claude",
            "FORCE": "1",
        })
        env.pop("FORCE_RESEED_EMPTY", None)

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, (
            "FORCE=1 on non-empty DB should refuse (non-zero exit), but it succeeded"
        )
        combined = result.stdout + result.stderr
        assert "refused" in combined, (
            f"Expected 'refused' in output, got:\n{combined}"
        )
        assert "non-empty" in combined, (
            f"Expected 'non-empty' in output, got:\n{combined}"
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_force_on_unreadable_db_refuses():
    """FORCE=1 on an unreadable DB (PermissionError) refuses with non-zero exit.

    Origin: issue #1051 — outer except Exception: return 0 treated unreadable
    DB as empty. A PermissionError or locked file caused _db_node_count() to
    return 0, so FORCE=1 / FORCE_RESEED_EMPTY=1 would proceed to seed on top
    of potentially live data. The fix raises SystemExit with a clear message
    instead of silently treating the unreadable DB as empty.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_force_unreadable_"))
    db_path = None
    try:
        engram_home = workdir / ".engram"
        engram_home.mkdir(parents=True)
        db_path = engram_home / "knowledge.db"

        # Plant a nodes table with one row so the DB is non-empty (distinct
        # from the no-schema empty case).
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('ax_0001', 'axiom', 'test axiom')"
        )
        conn.commit()
        conn.close()

        # Make the DB unreadable — simulates PermissionError / locked file.
        os.chmod(db_path, 0o000)

        env = os.environ.copy()
        env.update({
            "ALPHA_DIR": str(REPO_ROOT),
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
            "ENGRAM_HOME": str(engram_home),
            "CLAUDE_HOME": str(workdir / ".claude"),
            "PYTHON_BIN": sys.executable,
            "TARGET": "claude",
            "FORCE": "1",
        })
        env.pop("FORCE_RESEED_EMPTY", None)

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, (
            "FORCE=1 on unreadable DB should refuse (non-zero exit), but it succeeded"
        )
        combined = result.stdout + result.stderr
        assert "cannot read" in combined.lower(), (
            f"Expected 'cannot read' in output, got:\n{combined}"
        )

    finally:
        # Restore permissions before rmtree so cleanup can delete the file.
        if db_path is not None and db_path.exists():
            try:
                os.chmod(db_path, 0o644)
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_force_on_locked_db_refuses():
    """FORCE=1 on a locked DB (database is locked OperationalError) refuses.

    Origin: Ariadne's round-2 review of PR #1075 — 'database is locked' is a
    sqlite3.OperationalError that surfaces at the SELECT (not at connect), so the
    original broad inner except sqlite3.OperationalError: return 0 silently
    treated a locked DB as empty, allowing FORCE=1 to proceed to seed on top of
    potentially live data. The fix narrows the inner catch to 'no such table' only
    and raises SystemExit on any other OperationalError including lock errors.
    """
    import threading

    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_force_locked_"))
    lock_held = threading.Event()
    lock_released = threading.Event()

    def hold_exclusive_lock(db_path_str):
        conn = sqlite3.connect(db_path_str)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            lock_held.set()
            lock_released.wait(timeout=30)
        finally:
            conn.close()

    try:
        engram_home = workdir / ".engram"
        engram_home.mkdir(parents=True)
        db_path = engram_home / "knowledge.db"

        # Create DB with nodes table + one row so the DB is non-empty.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('ax_0001', 'axiom', 'test axiom')"
        )
        conn.commit()
        conn.close()

        # Hold an EXCLUSIVE lock in a background thread — simulates a running
        # process that has the DB locked. The SELECT inside _db_node_count will
        # fail with "database is locked" OperationalError.
        locker = threading.Thread(
            target=hold_exclusive_lock, args=(str(db_path),), daemon=True
        )
        locker.start()
        lock_held.wait(timeout=5)

        env = os.environ.copy()
        env.update({
            "ALPHA_DIR": str(REPO_ROOT),
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
            "ENGRAM_HOME": str(engram_home),
            "CLAUDE_HOME": str(workdir / ".claude"),
            "PYTHON_BIN": sys.executable,
            "TARGET": "claude",
            "FORCE": "1",
        })
        env.pop("FORCE_RESEED_EMPTY", None)

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, (
            "FORCE=1 on locked DB should refuse (non-zero exit), but it succeeded"
        )
        combined = result.stdout + result.stderr
        assert "cannot query" in combined.lower(), (
            f"Expected 'cannot query' in output, got:\n{combined}"
        )

    finally:
        lock_released.set()
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_force_reseed_empty_on_empty_db_proceeds():
    """FORCE_RESEED_EMPTY=1 on an empty DB (0 nodes) proceeds and seeds the graph.

    Named escape valve for callers that want explicit intent at the call site.
    Semantics identical to FORCE=1-on-empty-DB.

    Origin: issue #774.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_reseed_empty_"))
    try:
        db_path, env = _make_empty_db_env(workdir)
        env["FORCE_RESEED_EMPTY"] = "1"

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"FORCE_RESEED_EMPTY=1 on empty DB failed (expected success):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        conn = sqlite3.connect(str(db_path))
        try:
            axiom_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='axiom'"
            ).fetchone()[0]
            assert axiom_count == 3, f"expected 3 seed axioms, got {axiom_count}"
        finally:
            conn.close()

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_force_reseed_empty_on_nonempty_db_refuses():
    """FORCE_RESEED_EMPTY=1 on a non-empty DB refuses with exit code 1.

    The named escape valve has the same node-count guard as FORCE=1. If the
    DB already has nodes, FORCE_RESEED_EMPTY=1 refuses with the same helpful
    message pointing at manual deletion.

    Origin: issue #774.
    """
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_reseed_nonempty_"))
    try:
        engram_home = workdir / ".engram"
        engram_home.mkdir(parents=True)
        db_path = engram_home / "knowledge.db"

        # Plant a nodes table with one row.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('ax_0001', 'axiom', 'test axiom')"
        )
        conn.commit()
        conn.close()

        env = os.environ.copy()
        env.update({
            "ALPHA_DIR": str(REPO_ROOT),
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
            "ENGRAM_HOME": str(engram_home),
            "CLAUDE_HOME": str(workdir / ".claude"),
            "PYTHON_BIN": sys.executable,
            "TARGET": "claude",
            "FORCE_RESEED_EMPTY": "1",
        })
        env.pop("FORCE", None)

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, (
            "FORCE_RESEED_EMPTY=1 on non-empty DB should refuse (non-zero exit), "
            "but it succeeded"
        )
        combined = result.stdout + result.stderr
        assert "refused" in combined, (
            f"Expected 'refused' in output, got:\n{combined}"
        )
        assert "non-empty" in combined, (
            f"Expected 'non-empty' in output, got:\n{combined}"
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests for _set_cleanup_period_days (via subprocess — same pattern as above)
#
# #4445: bootstrap no longer sets cleanupPeriodDays on its own. It touches settings.json
# only for `--retention-days N`. The three tests below pinned the old unconditional merge;
# each is rewritten to the new contract (the full cells live in
# tests/test_bootstrap_privacy_4445.py).
# ---------------------------------------------------------------------------


def _run_bootstrap_for_cleanup_test(
    workdir: Path, pre_settings: dict | None = None, extra_args: list[str] | None = None
):
    """Run bootstrap.py in a subprocess and return the resulting settings.json
    content.  pre_settings, if given, is written to CLAUDE_HOME/settings.json
    before bootstrap runs. extra_args are passed to bootstrap.py."""
    engram_home = workdir / ".engram"
    claude_home = workdir / ".claude"
    engram_home.mkdir(parents=True)
    claude_home.mkdir(parents=True)
    if pre_settings is not None:
        (claude_home / "settings.json").write_text(json.dumps(pre_settings))
    runtime_files = [
        "server.py", "engram_client.py", "engram_confidence.py",
        "engram_filter.py", "engram_idf.py", "engram_ids.py",
        "engram_log_emitter.py", "engram_log_indexer.py",
    ]
    for fname in runtime_files:
        src = REPO_ROOT / fname
        if src.exists():
            shutil.copy2(src, engram_home / fname)
    env = os.environ.copy()
    env.update({
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
        "ENGRAM_HOME": str(engram_home),
        "CLAUDE_HOME": str(claude_home),
        "PYTHON_BIN": sys.executable,
        "TARGET": "claude",
    })
    env.pop("ENGRAM_BOOTSTRAP", None)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "src" / "engram" / "bootstrap.py"), *(extra_args or [])],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"bootstrap.py failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )
    settings_path = claude_home / "settings.json"
    return json.loads(settings_path.read_text()) if settings_path.exists() else {}


def test_bootstrap_without_retention_flag_leaves_settings_absent():
    """No --retention-days: bootstrap does not create settings.json (was: sets 36500)."""
    workdir = Path(tempfile.mkdtemp(prefix="engram_cleanup_test_"))
    try:
        data = _run_bootstrap_for_cleanup_test(workdir)
        assert data == {}, f"settings.json must stay absent without the flag, got {data}"
        assert not (workdir / ".claude" / "settings.json").exists()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_retention_flag_merges_into_existing_settings():
    """--retention-days 36500 sets cleanupPeriodDays, preserving existing keys
    (was: merged unconditionally)."""
    workdir = Path(tempfile.mkdtemp(prefix="engram_cleanup_test_"))
    try:
        data = _run_bootstrap_for_cleanup_test(
            workdir, pre_settings={"model": "sonnet", "theme": "dark"},
            extra_args=["--retention-days", "36500"],
        )
        assert data.get("cleanupPeriodDays") == 36500, (
            f"expected 36500, got {data.get('cleanupPeriodDays')}"
        )
        assert data.get("model") == "sonnet", "existing model key should be preserved"
        assert data.get("theme") == "dark", "existing theme key should be preserved"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_does_not_override_existing_cleanup_period_days():
    """Even with --retention-days, an already-set cleanupPeriodDays is kept
    (was: kept without the flag, since the merge was unconditional)."""
    workdir = Path(tempfile.mkdtemp(prefix="engram_cleanup_test_"))
    try:
        data = _run_bootstrap_for_cleanup_test(
            workdir, pre_settings={"cleanupPeriodDays": 90},
            extra_args=["--retention-days", "36500"],
        )
        assert data.get("cleanupPeriodDays") == 90, (
            f"user's value 90 should be preserved, got {data.get('cleanupPeriodDays')}"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests for the plugin-mode tools/hooks symlink step and its skip flag
# ---------------------------------------------------------------------------
#
# None of the tests above exercise this code path at all: they all run
# bootstrap.py directly from REPO_ROOT/src/engram/, which has no plugin.json
# alongside it, so `plugin_marker.is_file()` is already False there — the
# symlink block is dead code from this suite's own point of view. To
# exercise it for real (both the existing default AND the new skip flag),
# these tests build a throwaway "plugin tree": a copy of bootstrap.py plus
# a plugin.json marker and dummy tools/ + hooks/ dirs, mirroring what
# build-plugin.sh actually assembles.


def _make_plugin_tree_env(workdir: Path) -> tuple[Path, Path, dict]:
    """Build a throwaway plugin-tree dir (bootstrap.py + plugin.json +
    tools/ + hooks/) and matching env vars. Returns (plugin_tree_dir,
    engram_home, env)."""
    # A copy of the WHOLE src/engram/ tree, not an enumerated file list --
    # server.py's own import chain (engram_core, engram_focus,
    # engram_recall_summaries, tools.recall_summary_validator, and more)
    # is long, changes over time, and none of it needs restating here: the
    # other tests in this file run bootstrap.py IN PLACE at
    # REPO_ROOT/src/engram/, where every dependency already lives
    # alongside it, so their own `runtime_files` copy list was always a
    # silent no-op (src = REPO_ROOT / fname, a path that doesn't exist in
    # this repo layout) -- it never needed to work. This test copies
    # bootstrap.py OUT to a separate plugin-tree dir specifically to make
    # `plugin.json` presence controllable, so it genuinely needs the real
    # tree alongside it.
    plugin_tree = workdir / "plugin-tree"
    shutil.copytree(
        REPO_ROOT / "src" / "engram", plugin_tree,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (plugin_tree / "plugin.json").write_text('{"name": "engram-test-plugin"}')
    # Overwrite the copied tools/hooks with per-test throwaway markers so
    # the symlink-target assertions below check something written by this
    # test, not merely "some file happens to exist".
    shutil.rmtree(plugin_tree / "tools" / "__pycache__", ignore_errors=True)
    (plugin_tree / "tools" / "test-marker.txt").write_text("plugin tools content")
    (plugin_tree / "hooks" / "test-marker.txt").write_text("plugin hooks content")

    engram_home = workdir / ".engram"
    claude_home = workdir / ".claude"
    env = os.environ.copy()
    env.update({
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "src" / "engram" / "templates"),
        "ENGRAM_HOME": str(engram_home),
        "CLAUDE_HOME": str(claude_home),
        "PYTHON_BIN": sys.executable,
        "TARGET": "claude",
    })
    env.pop("ENGRAM_BOOTSTRAP_SKIP_SYMLINKS", None)
    return plugin_tree, engram_home, env


def test_bootstrap_creates_plugin_symlinks_by_default():
    """Baseline this suite was missing: a real plugin-tree layout (plugin.json
    present) gets ~/.engram/tools and ~/.engram/hooks symlinked to the
    plugin's bundled copies, exactly as documented, when the new skip flag
    is NOT set."""
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_symlink_default_"))
    try:
        plugin_tree, engram_home, env = _make_plugin_tree_env(workdir)

        result = subprocess.run(
            [sys.executable, str(plugin_tree / "bootstrap.py")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"bootstrap.py failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )

        tools_link = engram_home / "tools"
        hooks_link = engram_home / "hooks"
        assert tools_link.is_symlink(), "expected ~/.engram/tools to be a symlink by default"
        assert hooks_link.is_symlink(), "expected ~/.engram/hooks to be a symlink by default"
        assert tools_link.resolve() == (plugin_tree / "tools").resolve()
        assert hooks_link.resolve() == (plugin_tree / "hooks").resolve()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_skip_symlinks_env_var_skips_both_links():
    """ENGRAM_BOOTSTRAP_SKIP_SYMLINKS=1 skips the tools/hooks symlink step
    entirely, even with a real plugin.json present -- added for a per-run
    isolated ENGRAM_HOME build (e.g. a benchmark harness) that must never
    link outside its own sandboxed directory. Seeding must still succeed;
    only the symlinks are skipped."""
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_symlink_skip_"))
    try:
        plugin_tree, engram_home, env = _make_plugin_tree_env(workdir)
        env["ENGRAM_BOOTSTRAP_SKIP_SYMLINKS"] = "1"

        result = subprocess.run(
            [sys.executable, str(plugin_tree / "bootstrap.py")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"bootstrap.py failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )
        assert "SKIP_SYMLINKS" in result.stdout, (
            "expected the skip to announce itself in stdout, got:\n" + result.stdout
        )

        assert not (engram_home / "tools").exists(), (
            "ENGRAM_BOOTSTRAP_SKIP_SYMLINKS=1 must not create ~/.engram/tools at all"
        )
        assert not (engram_home / "hooks").exists(), (
            "ENGRAM_BOOTSTRAP_SKIP_SYMLINKS=1 must not create ~/.engram/hooks at all"
        )

        # Seeding itself must still have succeeded -- the flag skips only
        # the symlinks, not the rest of bootstrap.
        db_path = engram_home / "knowledge.db"
        assert db_path.exists(), "knowledge.db should still be created with symlinks skipped"
        conn = sqlite3.connect(str(db_path))
        try:
            axiom_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='axiom'"
            ).fetchone()[0]
            assert axiom_count == 3, f"expected 3 seed axioms, got {axiom_count}"
        finally:
            conn.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_bootstrap_skip_symlinks_near_miss_value_warns_and_does_not_skip():
    """Ariadne's colleague-pass finding on PR#2171: a near-miss value like
    'true' must not silently fall through as unset -- it must warn AND
    leave symlinks in place (the safe, unskipped default), not silently do
    the opposite of what the caller likely intended."""
    workdir = Path(tempfile.mkdtemp(prefix="engram_bootstrap_symlink_nearmiss_"))
    try:
        plugin_tree, engram_home, env = _make_plugin_tree_env(workdir)
        env["ENGRAM_BOOTSTRAP_SKIP_SYMLINKS"] = "true"

        result = subprocess.run(
            [sys.executable, str(plugin_tree / "bootstrap.py")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"bootstrap.py failed:\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
        )
        assert "WARN" in result.stdout and "not '0' or '1'" in result.stdout, (
            "expected a visible warning for a near-miss value, got:\n" + result.stdout
        )
        # Near-miss treated as unset -- the SAFE default (symlinks created),
        # not the requested-but-misspelled skip.
        assert (engram_home / "tools").is_symlink(), (
            "a near-miss value should fall back to the safe default "
            "(symlinks created), not silently skip them too"
        )
        assert (engram_home / "hooks").is_symlink()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
