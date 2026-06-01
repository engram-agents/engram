"""End-to-end test for bootstrap.py against a fresh empty ENGRAM_HOME.

Catches the chicken-and-egg failure mode where _get_db()'s fail-loud guards
block bootstrap.py's own seeding (bootstrap calls engram_add_axiom, which
goes through _get_db(), which would raise on a missing DB without the
ENGRAM_BOOTSTRAP=1 env bypass).

Origin: Borges's colleague-review of PR #577 caught this as a blocker on
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
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "templates"),
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
            [sys.executable, str(REPO_ROOT / "bootstrap.py")],
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
    """bootstrap.py with an existing knowledge.db raises SystemExit unless FORCE=1.

    Origin: PR #577 added the refuse-overwrite gate as defense-in-depth for
    the plugin path (install.sh has the same gate at install.sh:178). This
    test guards the gate itself.
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
            "ALPHA_TEMPLATES_DIR": str(REPO_ROOT / "templates"),
            "ENGRAM_HOME": str(engram_home),
            "CLAUDE_HOME": str(workdir / ".claude"),
            "PYTHON_BIN": sys.executable,
            "TARGET": "claude",
        })
        env.pop("FORCE", None)  # ensure no inherited FORCE bypass

        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bootstrap.py")],
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
