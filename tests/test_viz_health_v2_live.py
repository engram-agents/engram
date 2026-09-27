"""Tests for /api/health/v2/live and viz.data.health_v2.get_health_v2_live_data
(engram-alpha#2518, live-score slice -- "live health score on every viz
page load").

Contract-first (repo convention, engram-alpha#3045): these assertions are
written against the spec's stated contract for the six numbered items in
the issue -- engine modes/refusal are covered separately in
tests/test_health_engine.py; this file covers items 3 (no write, ever),
4 (recompute + single-flight), and 6 (fail-honest), plus the route-level
wiring in BOTH viz.server and viz.workbench (item: "the new
/api/health/v2/live route needs a dispatch arm in BOTH files").

Style follows tests/test_viz_health_v2.py (AGENTS-dict fixture convention,
_LiveServerBase HTTPServer pattern) and tests/test_viz_health_workbench.py
(HANDLER_CLASS-parameterized _LiveServerBase, so the SAME route tests run
against both entry points without duplicating the harness).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents  # noqa: E402
from viz import server as viz_server  # noqa: E402
from viz import workbench as viz_workbench  # noqa: E402
from viz.data import health_v2 as viz_health_v2_data  # noqa: E402


# ---------------------------------------------------------------------------
# A real (file-backed, not in-memory) sqlite fixture -- the live route opens
# the agent's DB via `file:...?mode=ro`, which needs an actual file on disk.
# Mirrors tests/test_health_engine.py's _NODES_DDL/_insert helpers (a subset
# of engram_core.py's real nodes/edges columns) closely enough to exercise
# build_snapshot for real, kept minimal since this file is about the ROUTE/
# single-flight/no-write contract, not re-testing the engine's own scoring
# arithmetic (see test_health_engine.py for that).
# ---------------------------------------------------------------------------

_NODES_DDL = """
CREATE TABLE nodes (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    evidence_id     TEXT,
    status          TEXT DEFAULT 'active',
    is_current      INTEGER DEFAULT 1,
    metadata        TEXT DEFAULT '{}',
    recall_summary  TEXT,
    recall_keywords TEXT,
    embedding       TEXT
);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    turn        INTEGER NOT NULL DEFAULT 0,
    action      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    details     TEXT DEFAULT '{}'
);

CREATE TABLE cascade_markers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id                 TEXT NOT NULL REFERENCES nodes(id),
    cascade_event           TEXT NOT NULL,
    root_node               TEXT NOT NULL,
    root_op                 TEXT NOT NULL CHECK (root_op IN ('retract', 'supersede')),
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved')),
    resolution_kind         TEXT,
    resolution_replacement_id TEXT,
    created_at              TEXT NOT NULL,
    resolved_at             TEXT,
    backfill_provenance     TEXT,
    backfill_anomaly        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(node_id, cascade_event)
);
"""


def _make_file_db(db_path: str, with_node: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(_NODES_DDL)
    if with_node:
        conn.execute(
            "INSERT INTO nodes (id, type, created_at, status, is_current, "
            "metadata) VALUES (?, ?, ?, ?, ?, ?)",
            ("obs_1", "observation_factual", "t0", "active", 1, "{}"),
        )
        conn.commit()
    conn.close()


def _mock_no_embeddings():
    """Deterministic embedder-gate mocks for tests that don't care about
    the embedding signal specifically -- same convention
    test_health_engine.py's own tests use."""
    return (
        mock.patch("engram_core._embedder.is_available", return_value=False),
        mock.patch("engram_core._get_embedding_config", return_value={"enabled": False}),
    )


class TestGetHealthV2LiveDataBasic(unittest.TestCase):
    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_live_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        self.db_path = self.engram_home / "knowledge.db"
        _make_file_db(str(self.db_path))
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.db_path)}
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unknown_agent_returns_has_snapshot_false(self):
        result = viz_health_v2_data.get_health_v2_live_data("nope")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("Unknown agent", result["reason"])

    def test_known_agent_builds_live_and_returns_expected_shape(self):
        m1, m2 = _mock_no_embeddings()
        with m1, m2:
            result = viz_health_v2_data.get_health_v2_live_data("test")
        self.assertTrue(result["has_snapshot"], result)
        for key in ("provenance", "signal_rows", "aspect_tile", "composed",
                    "computed_at", "live_inputs"):
            self.assertIn(key, result)
        self.assertEqual(result["provenance"]["checkpoint_mode"], "live")
        self.assertIn("turn_source", result["live_inputs"])
        self.assertIn("embedding_gate_source", result["live_inputs"])

    def test_no_config_json_defaults_turn_zero(self):
        m1, m2 = _mock_no_embeddings()
        with m1, m2:
            result = viz_health_v2_data.get_health_v2_live_data("test")
        self.assertEqual(result["provenance"]["turn"], 0)

    def test_config_json_current_turn_is_read_per_agent(self):
        (self.engram_home / "config.json").write_text(
            json.dumps({"memory": {"current_turn": 17}})
        )
        m1, m2 = _mock_no_embeddings()
        with m1, m2:
            result = viz_health_v2_data.get_health_v2_live_data("test")
        self.assertEqual(result["provenance"]["turn"], 17)

    def test_two_agents_same_process_each_return_their_own_turn(self):
        """The load-bearing regression test: TWO fixture agents, built
        within ONE process, must each report their OWN current_turn -- the
        process-global bug this slice exists to fix would have collapsed
        both onto whichever agent's config the SERVER process happened to
        default to."""
        (self.engram_home / "config.json").write_text(
            json.dumps({"memory": {"current_turn": 7}})
        )
        other_home = Path(self.tmpdir) / "agent_dir_2"
        other_home.mkdir()
        other_db = other_home / "knowledge.db"
        _make_file_db(str(other_db))
        (other_home / "config.json").write_text(
            json.dumps({"memory": {"current_turn": 99}})
        )
        viz_agents.AGENTS["test2"] = {
            "name": "test2", "label": "Test2", "db": str(other_db),
        }
        m1, m2 = _mock_no_embeddings()
        with m1, m2:
            result_a = viz_health_v2_data.get_health_v2_live_data("test")
            result_b = viz_health_v2_data.get_health_v2_live_data("test2")
        self.assertEqual(result_a["provenance"]["turn"], 7)
        self.assertEqual(result_b["provenance"]["turn"], 99)

    def test_missing_db_file_is_fail_honest(self):
        os.remove(self.db_path)
        result = viz_health_v2_data.get_health_v2_live_data("test")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("Database not found", result["reason"])


class TestResolveEffectiveEmbeddingEnabled(unittest.TestCase):
    """Direct unit coverage of the small combinator the live route uses to
    turn (agent config, cheap process install-check) into ONE effective
    boolean for build_snapshot's embedding_enabled override."""

    def test_missing_embedding_key_defaults_enabled_true(self):
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            effective, source = viz_health_v2_data._resolve_effective_embedding_enabled({})
        self.assertTrue(effective)
        self.assertIn("True", source)

    def test_config_disabled_forces_effective_false_even_if_installed(self):
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            effective, _source = viz_health_v2_data._resolve_effective_embedding_enabled(
                {"embedding": {"enabled": False}}
            )
        self.assertFalse(effective)

    def test_not_installed_forces_effective_false_even_if_config_enabled(self):
        with mock.patch("engram_core._embedder.is_available", return_value=False):
            effective, _source = viz_health_v2_data._resolve_effective_embedding_enabled(
                {"embedding": {"enabled": True}}
            )
        self.assertFalse(effective)

    def test_both_true_is_effective_true(self):
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            effective, _source = viz_health_v2_data._resolve_effective_embedding_enabled(
                {"embedding": {"enabled": True}}
            )
        self.assertTrue(effective)


class TestLiveNeverPersists(unittest.TestCase):
    """Contract item 3: the live route opens the DB read-only and never
    writes a snapshot file, the dream projection, or anything else under
    health-snapshots/ -- assert the snapshot dir, dream-projection path,
    and the DB file are ALL byte/mtime-unchanged after the call."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_live_nowrite_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        self.db_path = self.engram_home / "knowledge.db"
        _make_file_db(str(self.db_path))
        self.snap_dir = self.engram_home / "health-snapshots"
        self.snap_dir.mkdir()
        self.existing_snapshot = self.snap_dir / "3-nap-20260904T090000Z.json"
        self.existing_snapshot.write_text(json.dumps({"turn": 3, "checkpoint_mode": "nap"}))
        self.dream_path = self.snap_dir / "health-snapshot-dream.json"
        self.dream_path.write_text(json.dumps({"turn": 3}))
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.db_path)}
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _snapshot(self):
        """(sorted file listing, {name: mtime_ns}) under the snapshots dir,
        plus the DB file's own mtime -- the full "byte/mtime-unchanged"
        surface named in the contract."""
        names = sorted(os.listdir(self.snap_dir))
        mtimes = {n: os.stat(self.snap_dir / n).st_mtime_ns for n in names}
        db_mtime = os.stat(self.db_path).st_mtime_ns
        return names, mtimes, db_mtime

    def test_live_call_leaves_everything_byte_mtime_unchanged(self):
        before_names, before_mtimes, before_db_mtime = self._snapshot()
        m1, m2 = _mock_no_embeddings()
        with m1, m2:
            result = viz_health_v2_data.get_health_v2_live_data("test")
        self.assertTrue(result["has_snapshot"], result)
        after_names, after_mtimes, after_db_mtime = self._snapshot()
        self.assertEqual(before_names, after_names)
        self.assertEqual(before_mtimes, after_mtimes)
        self.assertEqual(before_db_mtime, after_db_mtime)

    def test_discriminating_check_swap_in_a_real_write_and_confirm_it_is_caught(self):
        """THE DISCRIMINATING CHECK named in the spec: 'swap in
        write_snapshot in place of the non-persisting path and confirm
        this test goes red.'

        JUDGMENT CALL (flagged in the handoff): write_snapshot ITSELF
        refuses checkpoint_mode="live" (contract item 1), so a literal
        substitution would just raise before ever writing -- proving
        nothing about whether THIS test's file-listing assertions can
        detect a real write. Interpreted instead as: patch
        health.engine.build_snapshot with a substitute that performs a
        REAL file write into the same snapshots dir (mirroring what
        write_snapshot itself would persist, computed via the REAL,
        unpatched build_snapshot -- captured BEFORE the patch, since
        write_snapshot's OWN internal call to build_snapshot would
        otherwise recurse into this same patch), and confirms the
        no-write assertions above catch it."""
        from health import engine as health_engine

        real_build_snapshot = health_engine.build_snapshot

        def _fake_build_snapshot_that_writes(conn, checkpoint_mode, *args, **kwargs):
            snap = real_build_snapshot(conn, "nap")
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            path = self.snap_dir / f"{snap['turn']}-nap-{ts}.json"
            path.write_text(json.dumps(snap))
            return snap

        m1, m2 = _mock_no_embeddings()
        with m1, m2, mock.patch(
            "health.engine.build_snapshot", side_effect=_fake_build_snapshot_that_writes
        ):
            before_names, _before_mtimes, _before_db_mtime = self._snapshot()
            viz_health_v2_data.get_health_v2_live_data("test")
            after_names, _after_mtimes, _after_db_mtime = self._snapshot()
        self.assertNotEqual(
            before_names, after_names,
            "the substitute write did not add a new file -- the "
            "discriminating check itself is broken, not just uninformative",
        )


class TestLiveSingleFlight(unittest.TestCase):
    """Contract item 4: N concurrent requests for the SAME agent trigger
    exactly ONE build; a fresh request after the previous one completed
    always triggers a new build."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_live_flight_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        self.db_path = self.engram_home / "knowledge.db"
        _make_file_db(str(self.db_path))
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.db_path)}
        }
        # Clear any leftover in-flight state from a prior (failed) test run
        # in the same process -- module-level state, so tests must not leak
        # into one another.
        viz_health_v2_data._live_build_inflight.clear()

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        viz_health_v2_data._live_build_inflight.clear()

    def test_concurrent_requests_trigger_exactly_one_build(self):
        from health import engine as health_engine

        call_count = {"n": 0}
        lock = threading.Lock()
        real_build_snapshot = health_engine.build_snapshot

        def _counting_build_snapshot(*args, **kwargs):
            with lock:
                call_count["n"] += 1
            time.sleep(0.15)  # widen the overlap window for concurrent callers
            return real_build_snapshot(*args, **kwargs)

        results = []
        results_lock = threading.Lock()

        def _worker():
            r = viz_health_v2_data.get_health_v2_live_data("test")
            with results_lock:
                results.append(r)

        # The embedder mocks are entered ONCE, wrapping the whole concurrent
        # section -- NOT one mock.patch pair per thread. mock.patch restores
        # its captured "before" value on __exit__ regardless of whether a
        # DIFFERENT patch of the same target is still notionally "active",
        # so N independent per-thread patches of the SAME attribute race:
        # whichever thread finishes (and exits) first can un-patch the
        # attribute out from under a still-running thread. One shared patch
        # for the whole section has no such race.
        m1, m2 = _mock_no_embeddings()
        with m1, m2, mock.patch("health.engine.build_snapshot", side_effect=_counting_build_snapshot):
            threads = [threading.Thread(target=_worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(call_count["n"], 1, "N concurrent requests must trigger exactly ONE build")
        self.assertEqual(len(results), 8)
        for r in results:
            self.assertTrue(r["has_snapshot"], r)
            # All 8 callers must have received the SAME (single) build's result.
            self.assertEqual(r["computed_at"], results[0]["computed_at"])

    def test_a_fresh_request_after_completion_always_rebuilds(self):
        from health import engine as health_engine

        call_count = {"n": 0}
        real_build_snapshot = health_engine.build_snapshot

        def _counting_build_snapshot(*args, **kwargs):
            call_count["n"] += 1
            return real_build_snapshot(*args, **kwargs)

        m1, m2 = _mock_no_embeddings()
        with m1, m2, mock.patch("health.engine.build_snapshot", side_effect=_counting_build_snapshot):
            viz_health_v2_data.get_health_v2_live_data("test")
            viz_health_v2_data.get_health_v2_live_data("test")
            viz_health_v2_data.get_health_v2_live_data("test")

        self.assertEqual(call_count["n"], 3, "each sequential call (no overlap) must be a fresh build")


class TestLiveFailHonest(unittest.TestCase):
    """Contract item 6: on any raise from the live build, the SAME
    {has_snapshot: False, reason: ...} shape -- never a stale stored
    snapshot dressed up as live."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_live_fail_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        self.db_path = self.engram_home / "knowledge.db"
        _make_file_db(str(self.db_path))
        self.snap_dir = self.engram_home / "health-snapshots"
        self.snap_dir.mkdir()
        # A real, valid STORED snapshot exists -- if the live path ever
        # fell back to serving it, this test would not catch a bare
        # {has_snapshot: True} from the stored file; it must specifically
        # be the fail-honest shape.
        self.snap_dir.joinpath("3-nap-20260904T090000Z.json").write_text(
            json.dumps({
                "turn": 3, "checkpoint_mode": "nap", "measurements": {},
                "signal_defs": {}, "scores": {}, "config": {},
            })
        )
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.db_path)}
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_snapshot_exception_folds_into_fail_honest_shape(self):
        with mock.patch("health.engine.build_snapshot", side_effect=RuntimeError("boom")):
            result = viz_health_v2_data.get_health_v2_live_data("test")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("boom", result["reason"])
        # Never silently swap in the stored snapshot on failure.
        self.assertNotIn("provenance", result)

    def test_build_snapshot_exception_caught_by_the_INNER_builder_directly(self):
        """Calls ``_build_live_health_v2_payload`` DIRECTLY, not through
        ``get_health_v2_live_data``'s single-flight wrapper -- that wrapper
        carries its OWN defense-in-depth ``except Exception`` (documented
        as defense-in-depth ONLY, since the inner builder is "documented to
        never raise"). A test that only ever goes through the wrapper
        cannot tell "the inner builder itself folds the exception" from
        "the inner builder lets it propagate and the OUTER wrapper happens
        to catch it anyway" -- verified directly: removing the inner
        ``except Exception as exc: return {...}`` in
        ``_build_live_health_v2_payload`` (restoring the exception's
        propagation) leaves ``test_build_snapshot_exception_folds_into_
        fail_honest_shape`` above GREEN (the outer wrapper masks it) while
        THIS test goes red -- see the handoff for the confirmed mutation
        run."""
        with mock.patch("health.engine.build_snapshot", side_effect=RuntimeError("boom")):
            result = viz_health_v2_data._build_live_health_v2_payload(
                "test", str(self.db_path)
            )
        self.assertFalse(result["has_snapshot"])
        self.assertIn("boom", result["reason"])
        self.assertNotIn("provenance", result)


class _LiveServerMixin:
    """HANDLER_CLASS-parameterized harness -- same convention
    test_viz_health_workbench.py uses, so the SAME route tests run
    against both viz.server (:5001) and viz.workbench (:5021).

    Deliberately NOT a unittest.TestCase subclass itself (a plain mixin) --
    only the concrete per-handler classes below are, each combining this
    mixin with unittest.TestCase. A shared base that itself extends
    TestCase AND carries test_* methods gets collected and RUN directly by
    pytest with HANDLER_CLASS unset (None), which crashes the HTTPServer
    constructor rather than being silently skipped -- caught by running
    this file, not by inspection."""

    HANDLER_CLASS = None

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), self.HANDLER_CLASS)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get_json(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_live_http_")
        cls.engram_home = Path(cls.tmpdir) / "agent_dir"
        cls.engram_home.mkdir()
        cls.db_path = cls.engram_home / "knowledge.db"
        _make_file_db(str(cls.db_path))
        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test", "db": str(cls.db_path)}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

    @classmethod
    def tearDownClass(cls):
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_unknown_agent_returns_404(self):
        status, body = self._get_json("/api/health/v2/live?agent=nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(body["agent"], "nope")

    def test_known_agent_returns_200_with_expected_shape(self):
        m1, m2 = _mock_no_embeddings()
        with m1, m2:
            status, body = self._get_json("/api/health/v2/live?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertEqual(body["agent"], "test_agent")
        self.assertTrue(body["has_snapshot"], body)
        self.assertIn("computed_at", body)
        self.assertIn("live_inputs", body)


class TestLiveRouteOnVizServer(_LiveServerMixin, unittest.TestCase):
    HANDLER_CLASS = viz_server.Handler


class TestLiveRouteOnVizWorkbench(_LiveServerMixin, unittest.TestCase):
    HANDLER_CLASS = viz_workbench.Handler


if __name__ == "__main__":
    unittest.main()
