"""Contract-first tests for the embedding pipeline restore fix (#3075).

Written BEFORE the implementation, per this repo's contract-first-tests
convention (CLAUDE.md) and Lei's explicit instruction on #3075. Every test
below is expected to be RED against unfixed `dev` and GREEN after the C1-C5
fix lands (see the PR's own red/green pytest output for the actual proof).

Coverage, matching the issue's C1-C5 contract exactly:

  C1. _embedding_text_for_node is independent of the caller's row_factory.
  C2. _compute_and_store_embedding distinguishes EXPECTED skips from
      UNEXPECTED failures, and reports which (via an explicit outcome value,
      never bare None), logging unexpected failures at WARNING level.
  C3. _reembed_all_nodes reports {processed, embedded, skipped, failed}
      counts and logs one WARNING-level summary line if any node failed.
  C4. tools/engram-regenerate-embeddings.py fails loud (non-zero exit) if
      any node failed, and its final stderr line carries the same four
      counts.
  C5. Restore (_run_db_one_time_setup, the natural home per
      _backfill_vec_nodes's own precedent) regenerates NULL embeddings and
      fails LOUD (not silently) if it cannot complete.

      ROUND 2 (#3075/#3076, Borges's design ruling): "fail loud" is a
      DURABLE flag (embedding_restore_state — engram_core.py's
      _set_embedding_restore_flag / _clear_embedding_restore_flag /
      _get_embedding_restore_flag), surfaced by the hooks every prompt
      until cleared — not a one-shot stderr print. The NULL-only
      regeneration itself runs OUTSIDE _db_setup_lock, on a daemon thread
      started AFTER the lock is released (_run_embedding_restore_regeneration),
      never inside it — round 1's synchronous-inside-the-lock re-embed pass
      froze every other concurrent session's DB access for the pass's full
      duration on a real restore.

Out of scope (per the issue): retiring the disabled-embeddings mode (#345),
unifying DB channels (#2652), the WAL-sidecar restore hazard (separate
issue).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# conftest.py adds src/engram to sys.path.
import engram_core  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NODES_SCHEMA = """
    CREATE TABLE nodes (
        id              TEXT PRIMARY KEY,
        type            TEXT,
        claim           TEXT,
        source_title    TEXT,
        predicted_event TEXT,
        recall_summary  TEXT,
        recall_keywords TEXT,
        embedding       TEXT,
        is_current      INTEGER DEFAULT 1
    )
"""


def _make_min_nodes_db(path: Path, rows: list[dict]) -> None:
    """Write a minimal `nodes` table (the columns _embedding_text_for_node
    and _compute_and_store_embedding actually touch) to `path`, then close.

    Caller re-opens with whatever row_factory it needs to test.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(_NODES_SCHEMA)
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, source_title, predicted_event, "
            "recall_summary, recall_keywords, embedding, is_current) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r["id"],
                r.get("type", "observation"),
                r.get("claim"),
                r.get("source_title"),
                r.get("predicted_event"),
                r.get("recall_summary"),
                r.get("recall_keywords"),
                r.get("embedding"),
                r.get("is_current", 1),
            ),
        )
    conn.commit()
    conn.close()


def _open_row_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


class InterceptingConn(sqlite3.Connection):
    """sqlite3.Connection subclass that raises sqlite3.OperationalError for
    any conn.execute() call whose SQL contains `fail_on_substring`.

    A real sqlite3.Connection's attributes are read-only (C extension type),
    so mock.patch.object(conn, "execute", ...) is not possible — subclassing
    via the `factory=` kwarg of sqlite3.connect() is the supported injection
    point. Used by C2's "DB error on the UPDATE" test.
    """

    fail_on_substring: str | None = None

    def execute(self, sql, params=()):
        if self.fail_on_substring and self.fail_on_substring in sql:
            raise sqlite3.OperationalError("simulated DB error (test double)")
        return super().execute(sql, params)


def _fresh_core_in_tmp(prefix: str):
    """Return (core, core_test_dir) — a fresh engram_core instance configured
    against a fresh temp dir, ready for _get_db()/_run_db_one_time_setup
    exercise. Caller owns cleanup (shutil.rmtree core_test_dir in a finally).

    Mirrors tests/test_engram_backup.py's own `_fresh_core_in_tmp` helper
    (not cross-imported — no cross-test-file import convention exists in
    this repo; duplicated locally, test-only, deliberately not shared).
    """
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from conftest import fresh_module

    core = fresh_module(engram_core)
    core_test_dir = tempfile.mkdtemp(prefix=prefix)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    core._configure_paths(core_test_dir)
    core._ensure_data_dir()
    return core, core_test_dir


# ---------------------------------------------------------------------------
# C1 — _embedding_text_for_node is independent of the caller's row_factory
# ---------------------------------------------------------------------------

class TestC1RowFactoryIndependence:
    """A connection with row_factory=None (tuples) and one with
    sqlite3.Row must return IDENTICAL text for every node-type branch
    _embedding_text_for_node has (evidence, prediction, feeling_report,
    default/observation-or-derivation), with and without
    recall_summary/recall_keywords.
    """

    FIXTURE_ROWS = [
        {"id": "ob_default", "type": "observation",
         "claim": "a default observation claim"},
        {"id": "ev_with_title", "type": "evidence",
         "source_title": "Some Source Title",
         "claim": "claim is IGNORED for evidence nodes"},
        {"id": "ev_no_title", "type": "evidence",
         "source_title": None, "claim": "also ignored"},
        {"id": "pr_with_event", "type": "prediction",
         "predicted_event": "the predicted event text",
         "claim": "fallback claim (unused since predicted_event is set)"},
        {"id": "pr_fallback_claim", "type": "prediction",
         "predicted_event": None, "claim": "fallback claim text"},
        {"id": "fl_hedge", "type": "feeling_report",
         "claim": "I reported feeling: calm and steady"},
        {"id": "dv_summary_only", "type": "derivation",
         "claim": "a derivation claim",
         "recall_summary": "a one-line summary"},
        {"id": "dv_keywords_only", "type": "derivation",
         "claim": "another derivation claim",
         "recall_keywords": json.dumps(["kwA", "kwB"])},
        {"id": "dv_summary_and_keywords", "type": "derivation",
         "claim": "third derivation claim",
         "recall_summary": "third summary",
         "recall_keywords": json.dumps(["kwC"])},
    ]

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_3075_c1_"))
        self.db_path = self.tmp / "fixture.db"
        _make_min_nodes_db(self.db_path, self.FIXTURE_ROWS)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identical_text_regardless_of_row_factory(self):
        tuple_conn = sqlite3.connect(str(self.db_path))  # default row_factory=None
        row_conn = _open_row_conn(self.db_path)
        assert tuple_conn.row_factory is None, (
            "test precondition: tuple_conn must NOT have sqlite3.Row set"
        )
        try:
            for row in self.FIXTURE_ROWS:
                node_id = row["id"]
                text_tuple = engram_core._embedding_text_for_node(tuple_conn, node_id)
                text_row = engram_core._embedding_text_for_node(row_conn, node_id)
                assert text_tuple == text_row, (
                    f"node {node_id}: tuple-factory text {text_tuple!r} != "
                    f"Row-factory text {text_row!r}"
                )
        finally:
            tuple_conn.close()
            row_conn.close()

    def test_tuple_conn_row_factory_untouched_after_call(self):
        """Implementation-hint check: the fix must not mutate the CALLER's
        conn.row_factory as a side effect (binds Row on its own cursor
        instead) — a caller sharing `conn` with other code must not have its
        row_factory silently flipped out from under it.
        """
        tuple_conn = sqlite3.connect(str(self.db_path))
        try:
            engram_core._embedding_text_for_node(tuple_conn, "ob_default")
            assert tuple_conn.row_factory is None, (
                "conn.row_factory must remain untouched by "
                "_embedding_text_for_node"
            )
        finally:
            tuple_conn.close()

    def test_golden_values_sanity(self):
        """Spot-check a few branches against their documented formula, so
        the equality checks above aren't vacuously true for a function that
        returns "" for everything.
        """
        row_conn = _open_row_conn(self.db_path)
        try:
            assert engram_core._embedding_text_for_node(row_conn, "ob_default") == (
                "a default observation claim"
            )
            assert engram_core._embedding_text_for_node(row_conn, "ev_with_title") == (
                "Some Source Title"
            )
            assert engram_core._embedding_text_for_node(row_conn, "ev_no_title") == ""
            assert engram_core._embedding_text_for_node(row_conn, "pr_with_event") == (
                "the predicted event text"
            )
            assert engram_core._embedding_text_for_node(row_conn, "pr_fallback_claim") == (
                "fallback claim text"
            )
            assert engram_core._embedding_text_for_node(row_conn, "fl_hedge") == (
                "calm and steady"
            )
        finally:
            row_conn.close()


# ---------------------------------------------------------------------------
# C2 — _compute_and_store_embedding: explicit outcome, loud on failure
# ---------------------------------------------------------------------------

class TestC2ComputeAndStoreEmbeddingOutcome:

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_3075_c2_"))
        self.db_path = self.tmp / "fixture.db"

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self, rows):
        _make_min_nodes_db(self.db_path, rows)
        return _open_row_conn(self.db_path)

    def _embedding_of(self, conn, node_id):
        return conn.execute(
            "SELECT embedding FROM nodes WHERE id=?", (node_id,)
        ).fetchone()[0]

    # -- expected skips: no WARNING, embedding stays NULL --------------

    def test_skipped_when_embeddings_disabled_via_config(self, monkeypatch, caplog):
        conn = self._db([{"id": "n1", "claim": "some claim text"}])
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": False, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "skipped"
        assert self._embedding_of(conn, "n1") is None
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        conn.close()

    def test_skipped_when_embedder_unavailable(self, monkeypatch, caplog):
        conn = self._db([{"id": "n1", "claim": "some claim text"}])
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: False)
        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "skipped"
        assert self._embedding_of(conn, "n1") is None
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        conn.close()

    def test_skipped_when_text_is_empty(self, monkeypatch, caplog):
        # evidence with no source_title -> _embedding_text_for_node returns ""
        conn = self._db([{"id": "n1", "type": "evidence", "source_title": None}])
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
        embed_mock = mock.Mock(side_effect=AssertionError("embed() must not be called for empty text"))
        monkeypatch.setattr(engram_core._embedder, "embed", embed_mock)
        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "skipped"
        assert self._embedding_of(conn, "n1") is None
        embed_mock.assert_not_called()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        conn.close()

    # -- unexpected failures: WARNING logged, embedding stays NULL -----

    def test_failed_when_embed_raises(self, monkeypatch, caplog):
        conn = self._db([{"id": "n1", "claim": "some claim text"}])
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)

        def _boom(text, model_name):
            raise RuntimeError("simulated embed() failure")

        monkeypatch.setattr(engram_core._embedder, "embed", _boom)
        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "failed", (
            f"expected 'failed', got {outcome!r} — a raised exception from "
            f"embed() must not be silently swallowed"
        )
        assert self._embedding_of(conn, "n1") is None
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("n1" in m for m in warnings), (
            f"expected a WARNING naming the failed node id, got {warnings}"
        )
        conn.close()

    def test_failed_when_text_function_raises_typeerror(self, monkeypatch, caplog):
        """Simulates C1's exact original bug class (a TypeError from the
        text function) reaching _compute_and_store_embedding — must be
        reported as 'failed', not silently swallowed by a bare except.
        """
        conn = self._db([{"id": "n1", "claim": "some claim text"}])
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)

        def _raise_typeerror(conn, node_id):
            raise TypeError("tuple indices must be integers or slices, not str")

        monkeypatch.setattr(engram_core, "_embedding_text_for_node", _raise_typeerror)
        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "failed"
        assert self._embedding_of(conn, "n1") is None
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("n1" in m for m in warnings)
        conn.close()

    def test_failed_on_db_error_during_update(self, monkeypatch, caplog):
        _make_min_nodes_db(self.db_path, [{"id": "n1", "claim": "some claim text"}])
        conn = sqlite3.connect(str(self.db_path), factory=InterceptingConn)
        conn.row_factory = sqlite3.Row
        conn.fail_on_substring = "UPDATE nodes SET embedding"

        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
        monkeypatch.setattr(engram_core._embedder, "embed", lambda text, model_name: [0.1] * 384)

        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "failed", (
            f"a DB error on the UPDATE must surface as 'failed', got {outcome!r}"
        )
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("n1" in m for m in warnings)
        conn.close()

    # -- success path ----------------------------------------------------

    def test_embedded_success(self, monkeypatch, caplog):
        conn = self._db([{"id": "n1", "claim": "some claim text"}])
        if engram_core._VEC_BACKEND_AVAILABLE:
            # (round 4, Borges's colleague pass required 1 on PR#3076,
            # 2026-09-23) _compute_and_store_embedding's vec-mirror-write
            # branch only runs when the backend is genuinely available —
            # this fixture never created a vec_nodes table, so on any
            # real sqlite-vec install (Borges's own venvs; the plugin
            # venv) the mirror INSERT raised OperationalError and this
            # test's own `vec_mirror_ok is True` assertion below was
            # FALSE. It passed only in environments where the backend
            # import itself fails (this repo's own default sandbox has
            # no sqlite_vec — confirmed directly, not just inferred).
            # Create the real table here so the happy path is genuinely
            # exercised end-to-end regardless of environment.
            # test_vec_mirror_failure_is_counted_not_silent (below) is
            # the one that deliberately wants NO vec_nodes table to force
            # the degraded path — this setup stays local to THIS test,
            # not the shared _db() helper, so that test's own guarantee
            # (a real install still exercises genuine degradation) is
            # unaffected.
            conn.enable_load_extension(True)
            engram_core._sqlite_vec.load(conn)
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0("
                "node_id TEXT PRIMARY KEY, "
                "embedding float[384] distance_metric=cosine)"
            )
        fake_vector = [0.25] * 384
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
        monkeypatch.setattr(engram_core._embedder, "embed", lambda text, model_name: fake_vector)

        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "embedded"
        assert outcome.vec_mirror_ok is True
        assert json.loads(self._embedding_of(conn, "n1")) == fake_vector
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        conn.close()

    def test_vec_mirror_failure_is_counted_not_silent(self, monkeypatch):
        """The vec_nodes mirror-write failure stays a TOLERATED degradation
        (JSON nodes.embedding is canonical and the status is still
        "embedded") but must now be COUNTED in the returned outcome rather
        than silently swallowed — #3075 C2's explicit requirement.
        """
        conn = self._db([{"id": "n1", "claim": "some claim text"}])
        fake_vector = [0.25] * 384
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
        monkeypatch.setattr(engram_core._embedder, "embed", lambda text, model_name: fake_vector)
        # Force the vec-mirror branch to be attempted against a DB with no
        # vec_nodes table -> sqlite3.OperationalError on the DELETE, the
        # exact "vec_nodes missing or locked" tolerated-degradation path.
        monkeypatch.setattr(engram_core, "_VEC_BACKEND_AVAILABLE", True)
        monkeypatch.setattr(engram_core, "_sqlite_vec", object())

        outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert outcome.status == "embedded", (
            "JSON write succeeded before the vec-mirror attempt — status "
            "must still be 'embedded'"
        )
        assert outcome.vec_mirror_ok is False, (
            "the vec-mirror failure must be visible on the outcome, not "
            "silently swallowed"
        )
        assert json.loads(self._embedding_of(conn, "n1")) == fake_vector
        conn.close()


# ---------------------------------------------------------------------------
# C3 — _reembed_all_nodes: {processed, embedded, skipped, failed} + one
# WARNING-level summary line on any failure
# ---------------------------------------------------------------------------

class TestC3ReembedAllNodesReporting:

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_3075_c3_"))
        self.db_path = self.tmp / "fixture.db"

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self, n=3):
        rows = [{"id": f"n{i}", "claim": f"claim text {i}"} for i in range(n)]
        _make_min_nodes_db(self.db_path, rows)
        return _open_row_conn(self.db_path)

    def test_all_succeed(self, monkeypatch, caplog):
        conn = self._db(3)
        fake_vector = [0.1] * 384
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
        monkeypatch.setattr(engram_core._embedder, "embed", lambda text, model_name: fake_vector)

        with caplog.at_level(logging.WARNING, logger="engram_core"):
            counts = engram_core._reembed_all_nodes(conn)

        assert counts == {"processed": 3, "embedded": 3, "skipped": 0, "failed": 0}
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        remaining_null = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE embedding IS NULL"
        ).fetchone()[0]
        assert remaining_null == 0
        conn.close()

    def test_all_fail_reports_accurate_counts_and_warns_once(self, monkeypatch, caplog):
        """The originating bug: today a 100%-failure re-embed pass is
        indistinguishable from a 100%-success pass — this test must
        genuinely fail against unfixed `dev` (old _reembed_all_nodes returns
        a bare int, so `counts["failed"]` raises TypeError) and pass after
        C2/C3 land.
        """
        conn = self._db(3)
        monkeypatch.setattr(
            engram_core, "_get_embedding_config",
            lambda: {"enabled": True, "model": "x"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)

        def _raise_for_every_node(conn, node_id):
            raise RuntimeError(f"forced failure for {node_id} (#3075 C3 test)")

        monkeypatch.setattr(engram_core, "_embedding_text_for_node", _raise_for_every_node)

        with caplog.at_level(logging.WARNING, logger="engram_core"):
            counts = engram_core._reembed_all_nodes(conn)

        assert counts["processed"] == 3
        assert counts["failed"] == counts["processed"], (
            f"expected every node to be reported failed, got {counts}"
        )
        assert counts["embedded"] == 0
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 1, "expected at least one WARNING-level summary line"
        # ONE summary line, not one per node (_compute_and_store_embedding
        # already logs the per-node WARNING separately).
        summary_lines = [m for m in warnings if "_reembed_all_nodes" in m]
        assert len(summary_lines) == 1, (
            f"expected exactly one _reembed_all_nodes summary WARNING, got "
            f"{summary_lines}"
        )
        conn.close()


# ---------------------------------------------------------------------------
# C4 — tools/engram-regenerate-embeddings.py fails loud
# ---------------------------------------------------------------------------

class TestC4RegenerateEmbeddingsScriptFailsLoud:

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_3075_c4_"))
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"

    def _load_regen_module(self):
        regen_script = REPO_ROOT / "tools" / "engram-regenerate-embeddings.py"
        spec = importlib.util.spec_from_file_location(
            "engram_regenerate_embeddings_3075_test", str(regen_script)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_c1_fix_and_fail_loud_combined(self, capsys):
        """Combines C1's fix verification (a tuple-row-factory connection to
        the SAME db file resolves _embedding_text_for_node without
        TypeError) with C4's exit-code/summary-counts contract, driving the
        script's actual backfill logic (not a subprocess) with a mocked
        embedder that fails exactly one of three nodes.

        Judgment call (documented in the PR handoff): the script's OWN
        connection keeps conn.row_factory = sqlite3.Row (required by its own
        dict-style `r["id"]` access on other queries — explicitly out of
        C1's scope to touch). C1's independence is verified via a SEPARATE,
        directly-constructed tuple-factory connection to the same db file,
        in the same test function, rather than by injecting a tuple-factory
        connection into backfill_embeddings itself (which would break the
        script's own unrelated row-access code).
        """
        engram_home = self.tmp
        db_path = engram_home / "knowledge.db"
        rows = [{"id": f"ob_{i}", "claim": f"claim text {i}"} for i in range(3)]
        _make_min_nodes_db(db_path, rows)

        # -- C1 verification: real, unmocked _embedding_text_for_node via a
        # tuple-row-factory connection must not TypeError. --
        import server as srv
        srv._configure_paths(engram_home)
        tuple_conn = sqlite3.connect(str(db_path))
        assert tuple_conn.row_factory is None
        text0 = srv._embedding_text_for_node(tuple_conn, "ob_0")
        assert text0 == "claim text 0"
        tuple_conn.close()

        # -- C4 verification: drive the actual script, one node fails. --
        regen_mod = self._load_regen_module()
        fake_vector = [0.5] * 384

        def _fake_embed_batch(texts, model_name):
            return [None if "claim text 1" in t else fake_vector for t in texts]

        with mock.patch.object(srv._embedder, "is_available", return_value=True), \
             mock.patch.object(srv._embedder, "_load_model", return_value=None), \
             mock.patch.object(srv._embedder, "_model", "fake_model", create=True), \
             mock.patch.object(srv._embedder, "embed_batch", side_effect=_fake_embed_batch), \
             mock.patch.object(regen_mod, "_find_server_py",
                                return_value=REPO_ROOT / "src" / "engram" / "server.py"), \
             mock.patch.object(regen_mod, "_import_server", return_value=srv):
            capsys.readouterr()
            with pytest.raises(SystemExit) as exc_info:
                regen_mod.backfill_embeddings(engram_home)
            captured = capsys.readouterr()

        assert exc_info.value.code == 1, (
            f"expected sys.exit(1) when a node fails to embed, got "
            f"code={exc_info.value.code}; stderr={captured.err!r}"
        )
        assert "processed=3" in captured.err, captured.err
        assert "embedded=2" in captured.err, captured.err
        assert "skipped=0" in captured.err, captured.err
        assert "failed=1" in captured.err, captured.err
        # The counts summary must be the FINAL stderr line (#3075 C4).
        stripped = captured.err.rstrip("\n")
        last_line = stripped.splitlines()[-1] if stripped else ""
        assert "processed=3" in last_line and "failed=1" in last_line, (
            f"expected the counts summary to be the LAST stderr line, "
            f"got last line: {last_line!r}"
        )


# ---------------------------------------------------------------------------
# C5 — restore regenerates embeddings, fails loud if it can't
#
# ROUND 2 (#3075/#3076, Borges's design ruling — a reviewer-fairy found two
# real blockers in round 1's implementation):
#   1. The check must not fire (noisily) on a deliberately-disabled-
#      embeddings install (server.py:8017 precedent). "Fails loud" is now a
#      DURABLE flag (embedding_restore_state), not a one-shot stderr print
#      nobody reads on an MCP stdio server.
#   2. The NULL-only regeneration must run OUTSIDE _db_setup_lock, on a
#      daemon thread started AFTER the lock is released — never
#      synchronously inside it (round 1's defect: freezes every other
#      concurrent session's DB access for the pass's full duration).
# ---------------------------------------------------------------------------

class TestC5RestoreRegeneratesEmbeddings:

    def teardown_method(self):
        pass  # each test manages its own core_test_dir cleanup

    def _insert_null_embedding_nodes(self, conn, n=3):
        for i in range(n):
            conn.execute(
                "INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
                "VALUES (?, 'observation', '2026-01-01T00:00:00Z', ?, 'active', 1)",
                (f"ob_c5_{i}", f"claim text {i}"),
            )
        conn.commit()

    def _wait_until(self, predicate, timeout=5.0, interval=0.05):
        """Poll `predicate` (a zero-arg callable) until it returns truthy or
        `timeout` seconds elapse. Returns the last (falsy-or-truthy) result.
        Used to wait for the async regeneration daemon thread (#3075 C5
        round 2) to finish, since the flag/DB state it updates is no longer
        visible synchronously the instant core._get_db() returns.
        """
        deadline = time.time() + timeout
        result = predicate()
        while not result and time.time() < deadline:
            time.sleep(interval)
            result = predicate()
        return result

    def test_success_all_nodes_embedded(self):
        """(a) embedder available and enabled, all nodes succeed -> zero
        NULL embeddings remain, and the restore-completeness flag CLEARS
        (round 2: async — the regeneration runs on a background thread
        after core._get_db() returns, so this polls for completion rather
        than asserting immediately)."""
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_success_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)  # embedder "available"
            conn = core._get_db()  # bootstrap real schema at latest version
            self._insert_null_embedding_nodes(conn, 3)
            null_before = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            assert null_before == 3
            conn.close()

            fake_vector = [0.1] * 384
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", return_value=fake_vector):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()  # re-triggers _run_db_one_time_setup

                # Round 2: regeneration is ASYNC — poll for the flag to clear
                # (the mock context must stay active for the background
                # thread's own embed() calls, so the wait happens inside it).
                settled = self._wait_until(
                    lambda: not core._get_embedding_restore_flag(conn2)["not_fully_restored"]
                )
            assert settled, (
                "restore-completeness flag never cleared within the timeout"
            )

            null_after = conn2.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            flag = core._get_embedding_restore_flag(conn2)
            conn2.close()
            assert null_after == 0, (
                "expected the async restore-completeness regeneration to "
                "backfill every NULL embedding when an embedder is available"
            )
            assert flag == {
                "not_fully_restored": False, "null_count": 0,
                "cause": None, "updated_at": flag["updated_at"],
            }, flag
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_disabled_by_config_sets_flag_no_regeneration(self):
        """(new, round 2) embeddings disabled via config.json
        (embedding.enabled=false) -> the flag fires SYNCHRONOUSLY with
        cause "disabled-by-config", naming the count; NO regeneration is
        attempted (Borges's ruling point 3: "gate the regeneration on
        enabled, but the flag still fires"). This is the server.py:8017
        precedent's counterpart for the restore-completeness check: an
        operator who deliberately disabled embeddings must not see this
        fire as an unexpected error, and must not have embed() called at
        all.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_disabled_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 3)
            conn.close()

            embed_mock = mock.Mock(
                side_effect=AssertionError("embed() must not be called when disabled-by-config")
            )
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", embed_mock), \
                 mock.patch.object(
                     core, "_get_embedding_config",
                     return_value={"enabled": False, "model": "x"},
                 ):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()

            embed_mock.assert_not_called()
            flag = core._get_embedding_restore_flag(conn2)
            null_after = conn2.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            conn2.close()
            assert null_after == 3, "disabled-by-config must not embed anything"
            assert flag["not_fully_restored"] is True
            assert flag["null_count"] == 3
            assert flag["cause"] == "disabled-by-config"
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_embedder_unavailable_sets_flag_synchronously(self, capsys):
        """(b) embedder unavailable -> the flag fires SYNCHRONOUSLY (round
        2's judgment call: scheduling an async thread that would provably
        no-op is pointless — see _run_db_one_time_setup's own comment) with
        cause "embedder-unavailable", naming the count; NULL embeddings
        remain (nothing silently marked done). No stderr noise (round 2:
        the diagnostic moved from a stderr print to the durable flag —
        this is also this class's quiet-files regression guard: the six
        previously-noisy test files build graphs under exactly this
        ENGRAM_NO_EMBEDDINGS=1 condition).
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_unavail_")
        try:
            # _fresh_core_in_tmp already sets ENGRAM_NO_EMBEDDINGS=1, making
            # core._embedder.is_available() return False.
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 3)
            conn.close()

            core._db_setup_done_paths.clear()
            capsys.readouterr()
            conn2 = core._get_db()
            captured = capsys.readouterr()

            flag = core._get_embedding_restore_flag(conn2)
            null_after = conn2.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            conn2.close()
            assert null_after == 3, (
                "no embedder available -> nothing should have been embedded"
            )
            assert flag["not_fully_restored"] is True
            assert flag["null_count"] == 3
            assert flag["cause"] == "embedder-unavailable"
            # Round 1's stderr flood is GONE — the flag is the mechanism now.
            assert "NOT FULLY RESTORED" not in captured.err, captured.err
            assert captured.err == "", (
                f"expected zero stderr output for the embedder-unavailable "
                f"case (round 2 moved this to the durable flag), got: "
                f"{captured.err!r}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_partial_failure_async_sets_flag_with_correct_count(self):
        """(c) partial failure (some nodes succeed, some fail) -> the flag
        fires (round 2: asynchronously — this polls for completion) with
        cause "per-node-failures", naming the CORRECTLY failed count (not
        the full node count, not zero)."""
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_partial_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 4)  # claim text 0..3
            conn.close()

            fake_vector = [0.1] * 384

            def _flaky_embed(text, model_name):
                if "claim text 1" in text or "claim text 3" in text:
                    raise RuntimeError("simulated embed failure (test double)")
                return fake_vector

            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", side_effect=_flaky_embed):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()

                settled = self._wait_until(
                    lambda: core._get_embedding_restore_flag(conn2)["cause"]
                    not in ("regeneration-pending", "regeneration-running")
                )
            assert settled, "restore-completeness flag never settled within the timeout"

            flag = core._get_embedding_restore_flag(conn2)
            null_after = conn2.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            conn2.close()
            assert null_after == 2, (
                f"expected exactly 2 nodes (claim text 1, claim text 3) to "
                f"still be NULL, got {null_after}"
            )
            assert flag["not_fully_restored"] is True
            assert flag["null_count"] == 2, (
                f"expected the flag to name the CORRECT remaining-failed "
                f"count (2), got {flag}"
            )
            assert flag["cause"] == "per-node-failures"
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def _insert_empty_text_evidence_node(self, conn, node_id="ev_c5_empty"):
        """(round 3) An evidence node with no source_title: per
        _embedding_text_for_node's own branch (base = row["source_title"]
        or ""), this node's embeddable text is permanently empty. Its
        embedding is NEVER going to become non-NULL, no matter how many
        times regeneration runs -- this is the EXPECTED "skipped" outcome
        EmbeddingOutcome's own docstring names, not a failure."""
        conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, source_title, "
            "status, is_current) "
            "VALUES (?, 'evidence', '2026-01-01T00:00:00Z', NULL, NULL, "
            "'active', 1)",
            (node_id,),
        )
        conn.commit()

    def test_permanent_empty_text_skip_clears_flag_not_per_node_failures(self):
        """(round 3 -- the delta reviewer-fairy's finding on round 2, my
        own independent code-read confirmation before dispatching this
        fix) A graph whose only remaining NULL-embedding node has
        genuinely empty embeddable text (a legitimate, PERMANENT
        EmbeddingOutcome "skipped", per that type's own docstring) must
        NOT leave the restore-completeness flag stuck with cause
        "per-node-failures" -- that cause implies something is wrong and
        offers a remedy ("re-run the tool") that can never help, since
        there was never anything to embed. The flag must CLEAR.

        Can-it-fail: against round 2's code (which derives the cause from
        raw remaining-NULL-count + embedder-availability alone, ignoring
        _reembed_null_nodes's own failed/skipped breakdown), this test
        must FAIL -- the flag would incorrectly stay set with
        cause="per-node-failures". After the round-3 fix (which reads
        counts["failed"] instead), it must PASS.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_permskip_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_empty_text_evidence_node(conn)
            null_before = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            assert null_before == 1
            conn.close()

            fake_vector = [0.1] * 384
            # embed() must never be called for this node -- its text is
            # empty, so _compute_and_store_embedding returns "skipped"
            # before ever reaching the embed() call.
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", return_value=fake_vector):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()

                settled = self._wait_until(
                    lambda: core._get_embedding_restore_flag(conn2)["cause"]
                    not in ("regeneration-pending", "regeneration-running")
                )
            assert settled, "restore-completeness flag never settled within the timeout"

            flag = core._get_embedding_restore_flag(conn2)
            null_after = conn2.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            conn2.close()
            # The node is still NULL (permanently -- nothing to embed) --
            # that is expected and correct.
            assert null_after == 1
            # But the FLAG must read as fully restored: this node's
            # permanent emptiness is not a restore defect.
            assert flag["not_fully_restored"] is False, (
                f"a permanent empty-text skip must clear the flag, not "
                f"leave it stuck with an unhelpful remedy -- got {flag}"
            )
            assert flag["cause"] is None
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_mixed_skip_and_failure_counts_only_the_real_failure(self):
        """(round 3) A graph with BOTH a permanent empty-text skip AND one
        genuine embed() failure: the flag must stay SET (a real failure
        did occur), but its null_count must equal the FAILED count (1),
        not the total remaining-NULL count (2, which would incorrectly
        include the harmless permanent skip)."""
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_mixed_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_empty_text_evidence_node(conn, node_id="ev_c5_empty")
            self._insert_null_embedding_nodes(conn, 1)  # ob_c5_0, "claim text 0"
            conn.close()

            fake_vector = [0.1] * 384

            def _flaky_embed(text, model_name):
                if "claim text 0" in text:
                    raise RuntimeError("simulated embed failure (test double)")
                return fake_vector

            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", side_effect=_flaky_embed):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()

                settled = self._wait_until(
                    lambda: core._get_embedding_restore_flag(conn2)["cause"]
                    not in ("regeneration-pending", "regeneration-running")
                )
            assert settled, "restore-completeness flag never settled within the timeout"

            flag = core._get_embedding_restore_flag(conn2)
            null_after = conn2.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            conn2.close()
            assert null_after == 2, (
                "both the empty-text skip and the failed node stay NULL"
            )
            assert flag["not_fully_restored"] is True
            assert flag["cause"] == "per-node-failures"
            assert flag["null_count"] == 1, (
                f"expected the flag to count only the REAL failure (1), "
                f"not the total remaining-NULL count (2, which conflates "
                f"the harmless permanent skip with the actual failure) -- "
                f"got {flag}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_lock_not_held_during_async_regeneration(self):
        """(new, round 2 — Borges's explicitly-named contract addition) The
        NULL-only regeneration must run OUTSIDE _db_setup_lock. Proven
        directly: a mocked SLOW embed() blocks on a threading.Event this
        test controls; while it is blocked (i.e. the async regeneration is
        genuinely in flight, not merely "not yet started" or "already
        done"), _db_setup_lock must be acquirable by another caller.

        Can-it-fail, demonstrated structurally rather than by literally
        reverting to round 1's code: against round 1's synchronous-inside-
        the-lock implementation, the core._get_db() call below would itself
        DEADLOCK (never return, since the mocked embed() blocks on an Event
        this test never sets before the join-with-timeout) — run in a
        background thread with a bounded join so THIS test fails loudly
        (assertion on thread.is_alive()) instead of hanging the whole suite
        if the regression ever returns.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_lockfree_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 3)
            conn.close()

            release_event = threading.Event()
            fake_vector = [0.1] * 384

            def _blocking_embed(text, model_name):
                release_event.wait(timeout=10)
                return fake_vector

            get_db_result: dict = {}

            def _call_get_db():
                core._db_setup_done_paths.clear()
                get_db_result["conn"] = core._get_db()

            # The mock patch must stay ACTIVE across the whole flow below,
            # not just around _get_db() itself: the actual embed() calls
            # happen on the ASYNC regeneration daemon thread, which outlives
            # _get_db()'s own return (and, until round 2's fix, outlives
            # _call_get_db() returning at all — unpatching here too early
            # would let the background thread call the REAL embedder after
            # release_event.set(), defeating this test's own control).
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", side_effect=_blocking_embed):
                t = threading.Thread(target=_call_get_db, daemon=True)
                t.start()
                t.join(timeout=5)
                assert not t.is_alive(), (
                    "core._get_db() did not return within 5s -- it is "
                    "blocking on the embed() call, meaning the "
                    "regeneration is (still) running SYNCHRONOUSLY inside "
                    "_get_db()/_db_setup_lock, the exact round-1 defect "
                    "this test guards against"
                )

                # _get_db() has returned. The async regeneration thread
                # should still be blocked on release_event (not set yet) --
                # prove the lock is free WHILE that thread is genuinely in
                # flight (NOT merely after _get_db() returns, which
                # discriminates nothing: a synchronous implementation's
                # _get_db() wouldn't have returned above until the embed()
                # unblocked either).
                acquired = core._db_setup_lock.acquire(blocking=False)
                assert acquired, (
                    "_db_setup_lock is still held while the async "
                    "regeneration is in flight -- it must be released "
                    "before the regeneration starts (Borges's design "
                    "ruling)"
                )
                core._db_setup_lock.release()

                # Let the blocked regeneration finish and confirm the flag
                # clears, closing the loop on this test's own setup. A
                # FRESH connection from THIS (the main) thread -- sqlite3
                # connections are thread-affine, and get_db_result["conn"]
                # was created on the background thread above.
                release_event.set()
                poll_conn = sqlite3.connect(str(core.DB_PATH))
                settled = self._wait_until(
                    lambda: not core._get_embedding_restore_flag(poll_conn)["not_fully_restored"]
                )
                poll_conn.close()
                assert settled, "restore-completeness flag never cleared after unblocking"
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_detection_only_permanently_unembeddable_no_flag_no_thread(self):
        """(round 4, Borges's ask (a) on PR#3076 -- "only permanently-
        unembeddable NULLs -> detection does not flag, no thread.") A
        graph whose ONLY NULL-embedding node has genuinely empty
        embeddable text (a legitimate, PERMANENT EmbeddingOutcome
        "skipped", per that type's own docstring) must never even
        SCHEDULE a restore pass -- not a transient flag-then-clear, no
        flag at all, and no async regeneration thread spawned.

        Can-it-fail: round 3's detection site (_run_db_one_time_setup,
        engram_core.py ~:3426) still used a raw `embedding IS NULL`
        COUNT(*), with no awareness of C2's own skip rule -- so THIS
        exact case set the flag transiently (cause="regeneration-
        pending") and spawned a thread that embeds nothing, on every
        single process start, forever. Against round 3's code this test
        must FAIL (the spawn mock gets called). After round 4 (detection
        recounts via the shared _count_missing_embeddings predicate,
        which excludes permanently-unembeddable nodes), it must PASS.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_r4_detect_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_empty_text_evidence_node(conn)
            null_before = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            assert null_before == 1
            conn.close()

            regen_spy = mock.Mock()
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core, "_run_embedding_restore_regeneration", regen_spy):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()
                # Give any (incorrectly) spawned daemon thread a moment to
                # actually invoke the (mocked) target before we assert.
                time.sleep(0.2)

            flag = core._get_embedding_restore_flag(conn2)
            conn2.close()
            regen_spy.assert_not_called()
            assert flag == {
                "not_fully_restored": False, "null_count": 0,
                "cause": None, "updated_at": flag["updated_at"],
            }, (
                f"a graph with only a permanently-unembeddable NULL node "
                f"must read as fully restored -- nothing to schedule, "
                f"nothing to flag, got {flag}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_embedder_unavailable_mid_run_flag_stays_set_not_silently_cleared(self):
        """(round 4, Borges's ask (b) on PR#3076 -- "embeddable NULLs +
        embedder unavailable mid-run -> the flag stays set, cause
        embedder-unavailable" -- this is the round-3 reviewer-fairy's own
        blocker, reproduced directly here via the same real public entry
        point that fairy's round-3 review used.) If the embedder is
        AVAILABLE at the synchronous detection gate (so the async pass
        gets scheduled) but has become UNAVAILABLE by the time the pass
        itself actually runs, every node outcome is "skipped" (not
        "failed"), per C2's own contract -- EmbeddingOutcome.status is
        "skipped" for disabled-config, unavailable-embedder, AND
        empty-text alike (engram_core.py ~:5339). Round 3's
        `counts["failed"] == 0` trusted-pass-tally logic could not tell
        this case apart from "everything is a legitimate permanent skip"
        and silently CLEARED the flag -- while genuinely embeddable
        content stayed un-embedded. Round 4 decides from a fresh
        skip-aware STATE re-count instead of the pass's own history, so
        the still-missing, still-embeddable nodes stay caught regardless
        of WHY every node in the pass was skipped.

        Can-it-fail: against round 3's code this test must FAIL (the
        flag incorrectly clears). After round 4, it must PASS.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_r4_midrun_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 3)  # real embeddable claim text
            null_before = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            assert null_before == 3
            conn.close()

            # Direct call to the real async entry point -- embedder reports
            # UNAVAILABLE for the entire call, simulating "dropped between
            # the synchronous detection gate and the pass actually running"
            # from the pass's own point of view: every node is genuinely
            # skipped (per C2), not failed.
            with mock.patch.object(core._embedder, "is_available", return_value=False):
                core._run_embedding_restore_regeneration(str(core.DB_PATH))

            check_conn = sqlite3.connect(str(core.DB_PATH))
            flag = core._get_embedding_restore_flag(check_conn)
            null_after = check_conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            check_conn.close()

            assert null_after == 3, "nothing could be embedded -- all 3 stay NULL"
            assert flag["not_fully_restored"] is True, (
                f"3 genuinely-embeddable nodes are still NULL -- the flag "
                f"must NOT silently clear just because the pass's own "
                f"tally reported zero 'failed' outcomes, got {flag}"
            )
            assert flag["cause"] == "embedder-unavailable", (
                f"expected cause 'embedder-unavailable' (the CURRENT "
                f"embedder state at decision time), got {flag['cause']!r}"
            )
            assert flag["null_count"] == 3
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_disabled_by_config_mid_run_flag_stays_set_correct_cause(self):
        """(round 4, confirmatory coverage for the round-4 delta reviewer-
        fairy's own suggestion.) Round 3's completion-side cause logic
        collapsed disabled-by-config and embedder-unavailable into a
        single embedder-unavailable check; round 4 restores the same
        3-way branch (disabled-by-config / embedder-unavailable /
        per-node-failures) the detection site already had, but no test
        exercised the disabled-mid-run leg specifically -- only the
        embedder-unavailable-mid-run leg
        (test_embedder_unavailable_mid_run_flag_stays_set_not_silently_cleared,
        above). The fairy traced this branch as already correct by
        reading the code; this test is confirmatory (already GREEN
        against round 4 as shipped, not a can-it-fail RED-first case --
        there was no behavior change here, only a coverage gap).

        Mirrors the mid-run-unavailable test but for embeddings DISABLED
        (not unavailable) mid-run: if config disables embeddings between
        the synchronous detection gate scheduling the async pass and the
        pass itself running, every node is again genuinely "skipped",
        and the flag must stay set with cause "disabled-by-config" (NOT
        "embedder-unavailable" -- the embedder itself is fine, config is
        why nothing embedded).
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_r4_disabled_midrun_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 3)  # real embeddable claim text
            conn.close()

            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(
                     core, "_get_embedding_config",
                     return_value={"enabled": False, "model": "x"},
                 ):
                core._run_embedding_restore_regeneration(str(core.DB_PATH))

            check_conn = sqlite3.connect(str(core.DB_PATH))
            flag = core._get_embedding_restore_flag(check_conn)
            null_after = check_conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            check_conn.close()

            assert null_after == 3, "nothing could be embedded -- all 3 stay NULL"
            assert flag["not_fully_restored"] is True
            assert flag["cause"] == "disabled-by-config", (
                f"expected cause 'disabled-by-config' (config, not the "
                f"embedder itself, is why nothing embedded), got "
                f"{flag['cause']!r}"
            )
            assert flag["null_count"] == 3
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def _insert_null_embedding_prediction_node(self, conn, node_id="pr_c5_pred"):
        """A `prediction` node with NULL `claim` but a real `predicted_event`
        -- _embedding_text_for_node's own prediction branch (`base =
        row["predicted_event"] or row["claim"] or ""`) makes this node
        genuinely repairable, not a permanent skip. Matches Borges's own
        real-graph observation (his 2026-09-20 frozen snapshot's one and
        only NULL-embedding node was exactly this shape: a `prediction`
        with `claim` NULL, repaired via `predicted_event`) -- the node
        type a real restore is most likely to actually hit.
        """
        conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, predicted_event, "
            "status, is_current) "
            "VALUES (?, 'prediction', '2026-01-01T00:00:00Z', NULL, ?, "
            "'active', 1)",
            (node_id, "the predicted event text"),
        )
        conn.commit()

    def test_restore_regen_embeds_prediction_node_via_predicted_event(self):
        """(Borges's ask, ia seq=11645 -- a contract test for the node type
        a real restore is most likely to actually hit, per his own
        2026-09-20 frozen-graph data point.) A `prediction` node with a
        NULL `claim` but a real `predicted_event` must be genuinely
        EMBEDDED (not skipped) by the restore-completeness regeneration
        pass -- confirming _embedding_text_for_node's own
        `predicted_event or claim` fallback is actually exercised
        end-to-end through the real regen pipeline, not just unit-tested
        in isolation (as TestC1RowFactoryIndependence already does for
        the text-computation function alone).
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_prediction_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_prediction_node(conn)
            null_before = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            assert null_before == 1
            conn.close()

            fake_vector = [0.1] * 384
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", return_value=fake_vector):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()
                settled = self._wait_until(
                    lambda: not core._get_embedding_restore_flag(conn2)["not_fully_restored"]
                )
            assert settled, "restore-completeness flag never cleared within the timeout"

            null_after = conn2.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            flag = core._get_embedding_restore_flag(conn2)
            conn2.close()
            assert null_after == 0, (
                "the prediction node must be genuinely embedded via its "
                "predicted_event, not left NULL as if it had no "
                "embeddable text"
            )
            assert flag["not_fully_restored"] is False
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_detection_failure_sets_detection_failed_flag(self):
        """(round 4, Borges's colleague pass required 2 / finding A on
        PR#3076, 2026-09-23) If the DETECTION block itself raises (e.g.
        _count_missing_embeddings blows up), the flag must still be SET
        -- with a distinct "detection-failed" cause naming the exception
        type -- rather than leaving no trace but an unread stderr line.
        The hooks read ONLY the durable flag, and their own read is
        fail-open, so a detection failure that leaves the flag untouched
        is silent to the agent: exactly what Lei's ruling says must be
        loud.

        Can-it-fail: against the pre-required-2 code (bare `print(...)`
        in the except block, no flag write), this test must FAIL -- the
        flag stays at its all-clear default (not_fully_restored=False).
        After the fix, it must PASS.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3075_c5_r4_detectfail_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 1)
            conn.close()

            def _raise(_conn):
                raise RuntimeError("simulated detection failure (test double)")

            with mock.patch.object(core, "_count_missing_embeddings", side_effect=_raise):
                core._db_setup_done_paths.clear()
                conn2 = core._get_db()

            flag = core._get_embedding_restore_flag(conn2)
            conn2.close()
            assert flag["not_fully_restored"] is True, (
                f"a detection FAILURE must still set the flag (best-effort) "
                f"-- silence here is exactly what the hooks' fail-open read "
                f"turns into 'nothing to report', got {flag}"
            )
            assert flag["cause"] is not None and flag["cause"].startswith("detection-failed"), (
                f"expected a 'detection-failed' cause naming the exception "
                f"type, got {flag['cause']!r}"
            )
            assert "RuntimeError" in flag["cause"]
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)
