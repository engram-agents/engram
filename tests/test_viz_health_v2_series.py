"""Tests for /api/health/v2/series and viz.data.health_v2.get_health_v2_series
(engram-alpha#2575 PR C1).

Style follows tests/test_viz_health_v2.py's `_LiveServerBase` HTTPServer
pattern, reusing tests/test_health_series.py's own scratch-repo/snapshot
fixtures to build a real `series.db` via ingest + backfill + derive.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.append(str(REPO_ROOT / "src" / "engram"))  # #2565: append, not insert(0)

from viz import agents as viz_agents  # noqa: E402
from viz import server as viz_server  # noqa: E402
from viz.data import health_v2 as viz_health_v2_data  # noqa: E402

import test_health_series as ths  # noqa: E402
from health import series  # noqa: E402


class _LiveServerBase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())


class TestHealthV2SeriesRoute(_LiveServerBase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="engram_health_v2_series_http_test_"))
        cls.engram_home = cls.tmpdir / "agent_dir"
        cls.engram_home.mkdir()
        cls.db_path = cls.engram_home / "knowledge.db"
        cls.db_path.write_bytes(b"")
        cls.snap_dir = cls.engram_home / "health-snapshots"
        cls.snap_dir.mkdir()

        cls.repo_dir = cls.tmpdir / "repo"
        ths._init_repo(cls.repo_dir)
        t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        cls.commit_sha = ths._commit_knowledge(cls.repo_dir, "-- k1", ths._committer_iso(t0))

        snap = ths._build_populated_v2_snapshot(anchor=None, turn=5)
        ts = t0.strftime("%Y%m%dT%H%M%SZ")
        ths._write_snapshot_file(cls.snap_dir, 5, "nap", ts, snap)
        cls.snap = snap

        cls.series_db = cls.snap_dir / "series.db"
        series.ingest(cls.snap_dir, cls.repo_dir, cls.series_db)
        series.derive(cls.series_db, snapshots_dir=cls.snap_dir)

        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test", "db": str(cls.db_path)},
            "no_series_agent": {"name": "no_series_agent", "label": "NoSeries", "db": str(cls.db_path) + ".other"},
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

        # A second agent with a real snapshots dir but NO series.db yet.
        cls.other_home = cls.tmpdir / "other_agent_dir"
        cls.other_home.mkdir()
        (cls.other_home / "knowledge.db").write_bytes(b"")
        (cls.other_home / "health-snapshots").mkdir()
        viz_agents.AGENTS["no_series_agent"]["db"] = str(cls.other_home / "knowledge.db")

    @classmethod
    def tearDownClass(cls):
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_unknown_agent_returns_404(self):
        status, body = self._get("/api/health/v2/series?agent=nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(body["agent"], "nope")

    def test_known_agent_no_db_returns_200_reason_no_db(self):
        status, body = self._get("/api/health/v2/series?agent=no_series_agent")
        self.assertEqual(status, 200)
        self.assertEqual(body["reason"], "no_db")

    def test_known_agent_with_series_db_returns_signals(self):
        status, body = self._get("/api/health/v2/series?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertEqual(body["agent"], "test_agent")
        self.assertIn("signals", body)
        self.assertEqual(set(body["signals"].keys()), set(self.snap["signal_defs"].keys()))
        point = body["signals"]["taint_rate"][0]
        self.assertEqual(point["commit"], self.commit_sha)
        self.assertEqual(point["source"], "stored")

    def test_signal_filter(self):
        status, body = self._get("/api/health/v2/series?agent=test_agent&signal=taint_rate")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["signals"].keys()), {"taint_rate"})

    def test_version_absent(self):
        status, body = self._get("/api/health/v2/series?agent=test_agent&version=health-v9.9")
        self.assertEqual(status, 200)
        self.assertEqual(body["reason"], "version_absent")


class TestGetHealthV2SeriesDirect(unittest.TestCase):
    def test_unknown_agent(self):
        result = viz_health_v2_data.get_health_v2_series("definitely-not-an-agent")
        self.assertEqual(result["reason"], "no_db")


if __name__ == "__main__":
    unittest.main()
