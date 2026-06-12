"""End-to-end test for bootstrap.py against a fresh empty ENGRAM_HOME.

Catches the chicken-and-egg failure mode where _get_db()'s fail-loud guards
block bootstrap.py's own seeding (bootstrap calls engram_add_axiom, which
goes through _get_db(), which would raise on a missing DB without the
ENGRAM_BOOTSTRAP=1 env bypass).

Origin: colleague-review of PR #577 caught this as a blocker on
the initial commit (smoke-test only rendered error messages, didn't run
the bootstrap end-to-end). This test is the regression guard.
"""

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
