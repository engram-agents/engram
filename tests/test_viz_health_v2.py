"""Tests for viz.data.health_v2 (engram-alpha#2518) -- the health-v2 SSoT
loader that builds signal_row.definition/today/provenance and
aspect_tile/composed from a stored health-snapshot JSON file, plus the
/api/health/v2 route in viz/server.py.

Backend-only slice: no template/frontend coverage here (the detail-view
frontend that consumes this endpoint is a separate, not-yet-landed PR).

Style follows tests/test_viz_server_stats.py (AGENTS-dict fixture
convention, empty-state assertions) and
tests/test_viz_health_view_toggle.py (_LiveServerBase HTTPServer pattern).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents  # noqa: E402
from viz import server as viz_server  # noqa: E402
from viz.data import health_v2 as viz_health_v2_data  # noqa: E402


# ---------------------------------------------------------------------------
# A small, realistic (3-signal) fixture snapshot -- enough to exercise every
# branch of _derive_status without needing all 9 real signals:
#
#   - taint_rate: available, admitted (importance>0 in the real model),
#     in an aspect that IS scored -> "scored"
#   - giant_component_fraction: available, but NOT admitted into its
#     (scored) aspect's weighted mean this run -> "measured_not_scored"
#   - uncited_observation_rate: belongs to an aspect with ZERO admitted
#     metrics declared at all (provenance_integrity, structurally) ->
#     "held_out"
#
# A 4th, "open_contradiction_rate", is included purely to exercise the
# "unavailable" branch (signals[name] == "UNAVAILABLE").
# ---------------------------------------------------------------------------

def _make_fixture_snapshot(turn: int, checkpoint_mode: str) -> dict:
    return {
        "health_version": "0.5.0",
        "raw_schema_version": "1",
        "model_version": "1",
        "anchor": "deadbeef",
        "turn": turn,
        "checkpoint_mode": checkpoint_mode,
        "measurements": {
            "claim_bearing_pending_retract_marker": 2,
            "claim_bearing_current": 40,
            "claim_bearing_giant_component_size": 32,
            "observations_without_evidence_id": 1,
            "observations_current": 10,
            "contradictions_open": 0,
            "contradictions_current": 0,
        },
        "signal_defs": {
            "taint_rate": {
                "numerator": "claim_bearing_pending_retract_marker",
                "denominator": "claim_bearing_current",
                "form": "rate",
                "gates": [],
                "formula": "claim_bearing_pending_retract_marker / claim_bearing_current",
            },
            "giant_component_fraction": {
                "numerator": "claim_bearing_giant_component_size",
                "denominator": "claim_bearing_current",
                "form": "rate",
                "gates": [],
                "formula": "claim_bearing_giant_component_size / claim_bearing_current",
            },
            "uncited_observation_rate": {
                "numerator": "observations_without_evidence_id",
                "denominator": "observations_current",
                "form": "one_minus_rate",
                "gates": [],
                "formula": "1 - observations_without_evidence_id / observations_current",
            },
            "open_contradiction_rate": {
                "numerator": "contradictions_open",
                "denominator": "contradictions_current",
                "form": "rate",
                "gates": [],
                "formula": "contradictions_open / contradictions_current",
            },
        },
        "signals": {
            "taint_rate": 0.05,
            "giant_component_fraction": 0.8,
            "uncited_observation_rate": 0.9,
            "open_contradiction_rate": "UNAVAILABLE",
        },
        "config": {
            "aspects": [
                {
                    "name": "epistemic_integrity",
                    "weight": 40.0,
                    "metrics": [
                        {"name": "taint_rate", "scored": True},
                        {"name": "open_contradiction_rate", "scored": True},
                    ],
                },
                {
                    "name": "provenance_integrity",
                    "weight": 25.0,
                    "metrics": [
                        {"name": "uncited_observation_rate", "scored": True},
                    ],
                },
                {
                    "name": "retrievability",
                    "weight": 35.0,
                    "metrics": [
                        {"name": "giant_component_fraction", "scored": True},
                    ],
                },
            ]
        },
        "scores": {
            "metrics": {
                "taint_rate": 0.95,
                "giant_component_fraction": 0.8,
                "uncited_observation_rate": 0.9,
                "open_contradiction_rate": "UNAVAILABLE",
            },
            "aspects": {
                "epistemic_integrity": 0.95,
                "provenance_integrity": "UNAVAILABLE",
                "retrievability": 0.6,
            },
            "composed": 82.5,
            "unscored_aspects": ["provenance_integrity"],
            "points_in_play": 75,
            "composed_coverage": 0.75,
            "aspect_coverage": {
                "epistemic_integrity": {
                    "metrics_in_play": ["taint_rate"],
                    "coverage": 1.0,
                },
                "retrievability": {
                    "metrics_in_play": [],
                    "coverage": 0.0,
                },
            },
        },
    }


class TestResolveHealthSnapshotsDir(unittest.TestCase):
    def setUp(self):
        self._saved_agents = viz_agents.AGENTS

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents

    def test_resolves_to_health_snapshots_dir_for_known_agent(self):
        viz_agents.AGENTS = {
            "alice": {"name": "alice", "label": "Alice", "db": "/home/alice/.engram/knowledge.db"}
        }
        result = viz_health_v2_data._resolve_health_snapshots_dir("alice")
        self.assertEqual(result, "/home/alice/.engram/health-snapshots")

    def test_returns_empty_for_unknown_agent(self):
        viz_agents.AGENTS = {
            "alice": {"name": "alice", "label": "Alice", "db": "/home/alice/.engram/knowledge.db"}
        }
        result = viz_health_v2_data._resolve_health_snapshots_dir("bob")
        self.assertEqual(result, "")


class TestGetHealthV2DataEmptyStates(unittest.TestCase):
    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        (self.engram_home / "knowledge.db").write_bytes(b"")
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.engram_home / "knowledge.db")}
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unknown_agent_returns_empty_state(self):
        result = viz_health_v2_data.get_health_v2_data("nonexistent")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("reason", result)

    def test_no_snapshots_dir_returns_empty_state(self):
        # health-snapshots/ was never created under engram_home
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("reason", result)

    def test_empty_snapshots_dir_returns_empty_state(self):
        (self.engram_home / "health-snapshots").mkdir()
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("reason", result)

    def test_snapshots_dir_with_only_non_matching_files_returns_empty_state(self):
        snap_dir = self.engram_home / "health-snapshots"
        snap_dir.mkdir()
        (snap_dir / "health-snapshot-dream.json").write_text("{}")
        (snap_dir / "5-nap-20260904T120000Z.json.error").write_text("{}")
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("reason", result)

    def test_corrupt_snapshot_file_returns_empty_state(self):
        snap_dir = self.engram_home / "health-snapshots"
        snap_dir.mkdir()
        (snap_dir / "5-nap-20260904T120000Z.json").write_text("not valid json {{")
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertFalse(result["has_snapshot"])
        self.assertIn("reason", result)

    def test_old_format_snapshot_missing_measurements_returns_empty_state(self):
        # A well-formed, parseable JSON snapshot written by an OLDER,
        # pre-health-v2 build of the health engine -- real top-level keys
        # observed on a live seat (engram-alpha#2518 follow-up), critically
        # MISSING `measurements`, `signal_defs`, and `health_version` (all
        # new-format-only keys). Must NOT fall through to has_snapshot=True
        # with a silently-empty signal_rows list.
        old_format_snapshot = {
            "anchor": "deadbeef",
            "checkpoint_mode": "nap",
            "config": {"aspects": []},
            "definitions": {},
            "diagnostics": {},
            "engine_version": "0.3.0",
            "red_flags": [],
            "schema_version": "1",
            "scores": {"composed": 80.0},
            "signals": {},
            "turn": 3,
        }
        snap_dir = self.engram_home / "health-snapshots"
        snap_dir.mkdir()
        (snap_dir / "3-nap-20260904T110000Z.json").write_text(
            json.dumps(old_format_snapshot)
        )
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertFalse(result["has_snapshot"], result)
        self.assertIn("reason", result)
        self.assertIn("old-format", result["reason"].lower())

    def test_higher_turn_old_format_beside_lower_turn_current_format_still_refuses(self):
        # engram-alpha#2587: a stale MCP server process (still running
        # pre-upgrade code) can write an old-format snapshot AFTER a seat
        # has already upgraded and written a current-format one -- landing
        # an old-format file at a HIGHER turn number beside a perfectly
        # usable current-format file at a LOWER turn number. Pin the
        # DELIBERATE behavior: `_latest_snapshot_filename` picks the
        # higher-turn (old-format) file, and the guard refuses rather than
        # falling back to the lower-turn (current-format, usable) one --
        # falling back would show stale data as current, exactly the defect
        # this panel exists to prevent. This is distinct from
        # `test_old_format_snapshot_missing_measurements_returns_empty_state`
        # above, which only ever has ONE (old-format) file on disk -- that
        # test can't distinguish "refuses because it's the only file" from
        # "refuses despite a usable alternative being available"; this test
        # can, because a usable turn-93 alternative is present and must be
        # ignored.
        old_format_snapshot = {
            "anchor": "deadbeef",
            "checkpoint_mode": "nap",
            "config": {"aspects": []},
            "definitions": {},
            "diagnostics": {},
            "engine_version": "0.3.0",
            "red_flags": [],
            "schema_version": "1",
            "scores": {"composed": 80.0},
            "signals": {},
            "turn": 94,
        }
        current_format_snapshot = _make_fixture_snapshot(turn=93, checkpoint_mode="nap")
        snap_dir = self.engram_home / "health-snapshots"
        snap_dir.mkdir()
        (snap_dir / "94-nap-20260904T120000Z.json").write_text(
            json.dumps(old_format_snapshot)
        )
        (snap_dir / "93-nap-20260904T110000Z.json").write_text(
            json.dumps(current_format_snapshot)
        )
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertFalse(result["has_snapshot"], result)
        self.assertIn("reason", result)
        self.assertIn("old-format", result["reason"].lower())
        self.assertIn("94-nap-20260904T120000Z.json", result["reason"])


class TestGetHealthV2DataPopulated(unittest.TestCase):
    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        (self.engram_home / "knowledge.db").write_bytes(b"")
        self.snap_dir = self.engram_home / "health-snapshots"
        self.snap_dir.mkdir()
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.engram_home / "knowledge.db")}
        }
        self.snapshot = _make_fixture_snapshot(turn=5, checkpoint_mode="nap")
        self.filename = "5-nap-20260904T120000Z.json"
        (self.snap_dir / self.filename).write_text(json.dumps(self.snapshot))

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_has_snapshot_true(self):
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertTrue(result["has_snapshot"], result)

    def test_provenance_fields_match_fixture(self):
        result = viz_health_v2_data.get_health_v2_data("test")
        prov = result["provenance"]
        self.assertEqual(prov["snapshot_id"], "5-nap-20260904T120000Z")
        self.assertEqual(prov["turn"], 5)
        self.assertEqual(prov["checkpoint_mode"], "nap")
        self.assertEqual(prov["health_version"], "0.5.0")
        self.assertEqual(prov["raw_schema_version"], "1")
        self.assertEqual(prov["model_version"], "1")
        self.assertEqual(prov["anchor"], "deadbeef")
        # written_at parsed from the filename's own timestamp component
        self.assertEqual(prov["written_at"], "2026-09-04T12:00:00+00:00")

    def test_signal_row_definition_derived_from_fixture(self):
        result = viz_health_v2_data.get_health_v2_data("test")
        rows_by_name = {r["name"]: r for r in result["signal_rows"]}
        self.assertEqual(set(rows_by_name), {
            "taint_rate", "giant_component_fraction",
            "uncited_observation_rate", "open_contradiction_rate",
        })

        taint = rows_by_name["taint_rate"]
        self.assertEqual(
            taint["definition"]["formula"],
            "claim_bearing_pending_retract_marker / claim_bearing_current",
        )
        self.assertEqual(
            taint["definition"]["numerator"],
            {
                "key": "claim_bearing_pending_retract_marker",
                "label": "claim_bearing_pending_retract_marker",
                "value": 2,
            },
        )
        self.assertEqual(
            taint["definition"]["denominator"],
            {
                "key": "claim_bearing_current",
                "label": "claim_bearing_current",
                "value": 40,
            },
        )
        self.assertEqual(taint["definition"]["polarity"], "increasing")
        self.assertEqual(taint["today"]["raw"], 0.05)
        self.assertEqual(taint["today"]["score"], 0.95)
        self.assertIsNone(taint["today"]["curve"])

        coverage_row = rows_by_name["uncited_observation_rate"]
        self.assertEqual(coverage_row["definition"]["polarity"], "decreasing")

    def test_status_derivation_four_states(self):
        result = viz_health_v2_data.get_health_v2_data("test")
        rows_by_name = {r["name"]: r for r in result["signal_rows"]}
        self.assertEqual(rows_by_name["taint_rate"]["status"], "scored")
        self.assertEqual(rows_by_name["giant_component_fraction"]["status"], "measured_not_scored")
        self.assertEqual(rows_by_name["uncited_observation_rate"]["status"], "held_out")
        self.assertEqual(rows_by_name["open_contradiction_rate"]["status"], "unavailable")

    def test_aspect_tile_and_composed(self):
        result = viz_health_v2_data.get_health_v2_data("test")
        tiles_by_name = {t["name"]: t for t in result["aspect_tile"]}
        self.assertEqual(tiles_by_name["epistemic_integrity"]["score"], 0.95)
        self.assertFalse(tiles_by_name["epistemic_integrity"]["unscored"])
        self.assertEqual(tiles_by_name["provenance_integrity"]["score"], "UNAVAILABLE")
        self.assertTrue(tiles_by_name["provenance_integrity"]["unscored"])

        composed = result["composed"]
        self.assertEqual(composed["value"], 82.5)
        self.assertEqual(composed["points_in_play"], 75)
        self.assertEqual(composed["composed_coverage"], 0.75)
        self.assertEqual(composed["unscored_aspects"], ["provenance_integrity"])


class TestGetHealthV2DataEmptySignalDefsRegression(unittest.TestCase):
    """Regression guard for the old-format-detection fix: a genuinely
    NEW-format snapshot (has the `measurements` key) with an empty
    `signal_defs: {}` -- a hypothetical but real-format case of zero
    signals for some other legitimate reason -- must still be treated as
    has_snapshot=True with an empty signal_rows list. The fix must key off
    `measurements`'s absence, not off `signal_defs` being empty."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        (self.engram_home / "knowledge.db").write_bytes(b"")
        self.snap_dir = self.engram_home / "health-snapshots"
        self.snap_dir.mkdir()
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.engram_home / "knowledge.db")}
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_new_format_with_empty_signal_defs_still_has_snapshot(self):
        snapshot = _make_fixture_snapshot(turn=6, checkpoint_mode="nap")
        snapshot["signal_defs"] = {}
        (self.snap_dir / "6-nap-20260904T130000Z.json").write_text(
            json.dumps(snapshot)
        )
        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertTrue(result["has_snapshot"], result)
        self.assertEqual(result["signal_rows"], [])


class TestLatestSnapshotIntegerSort(unittest.TestCase):
    """Multiple snapshot files with different turn numbers, including one
    where lexical string-sort would pick the WRONG file (turns 9 and 10) --
    confirm the highest-turn one is picked via integer sort."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_sort_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        (self.engram_home / "knowledge.db").write_bytes(b"")
        self.snap_dir = self.engram_home / "health-snapshots"
        self.snap_dir.mkdir()
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.engram_home / "knowledge.db")}
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_turn_10_beats_turn_9_despite_lexical_order(self):
        snap9 = _make_fixture_snapshot(turn=9, checkpoint_mode="nap")
        snap10 = _make_fixture_snapshot(turn=10, checkpoint_mode="pre_sleep")
        (self.snap_dir / "9-nap-20260904T090000Z.json").write_text(json.dumps(snap9))
        (self.snap_dir / "10-pre_sleep-20260904T100000Z.json").write_text(json.dumps(snap10))

        # Sanity: lexical string sort WOULD pick "9-..." over "10-..." since
        # "9" > "1" as characters -- this is exactly the bug the integer
        # sort exists to avoid.
        names = os.listdir(self.snap_dir)
        self.assertEqual(sorted(names)[-1].split("-")[0], "9")

        result = viz_health_v2_data.get_health_v2_data("test")
        self.assertEqual(result["provenance"]["turn"], 10)
        self.assertEqual(result["provenance"]["checkpoint_mode"], "pre_sleep")

    def test_latest_snapshot_filename_helper_directly(self):
        (self.snap_dir / "2-nap-20260904T020000Z.json").write_text("{}")
        (self.snap_dir / "15-nap-20260904T150000Z.json").write_text("{}")
        (self.snap_dir / "3-nap-20260904T030000Z.json").write_text("{}")
        picked = viz_health_v2_data._latest_snapshot_filename(str(self.snap_dir))
        self.assertEqual(picked, "15-nap-20260904T150000Z.json")


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


class TestHealthV2Route(_LiveServerBase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_health_v2_http_test_")
        cls.engram_home = Path(cls.tmpdir) / "agent_dir"
        cls.engram_home.mkdir()
        cls.db_path = cls.engram_home / "knowledge.db"
        cls.db_path.write_bytes(b"")
        cls.snap_dir = cls.engram_home / "health-snapshots"
        cls.snap_dir.mkdir()
        snapshot = _make_fixture_snapshot(turn=5, checkpoint_mode="nap")
        (cls.snap_dir / "5-nap-20260904T120000Z.json").write_text(json.dumps(snapshot))

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
        status, body = self._get("/api/health/v2?agent=nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(body["agent"], "nope")

    def test_known_agent_returns_200_with_expected_shape(self):
        status, body = self._get("/api/health/v2?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertEqual(body["agent"], "test_agent")
        self.assertTrue(body["has_snapshot"])
        self.assertIn("provenance", body)
        self.assertIn("signal_rows", body)
        self.assertIn("aspect_tile", body)
        self.assertIn("composed", body)
        self.assertEqual(body["provenance"]["turn"], 5)


if __name__ == "__main__":
    unittest.main()
