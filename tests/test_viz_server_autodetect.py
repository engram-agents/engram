"""Tests for viz_server auto-detect (discover_agents + merge).

Uses tmp_path to build fake home directories — never touches real homes.

Test inventory:
1. Detect: two fake roots with .engram/{config.json, knowledge.db} → both
   returned; names come from config (NOT dir names — dir name differs from
   agent_name to prove config-derived).
2. .engram-gemini variant detected; .bak and baseline dirs skipped.
3. Graceful skip: missing config.json → skipped, no raise; missing agent_name
   → skipped, no raise; unreadable dir (chmod 000) → skipped, no raise.
4. Merge/override: discovery base + explicit config that (a) overrides a label,
   (b) sets default, (c) excludes one, (d) adds a non-detected agent with db.
5. db-derivation: registered db == <engram_dir>/knowledge.db.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "engram"))

import viz_server
from viz_server import discover_agents, _merge_config_over_discovered


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engram_dir(root: Path, dir_name: str, agent_name: str) -> Path:
    """Create <root>/<dir_name>/{config.json, knowledge.db} and return the dir."""
    engram_dir = root / dir_name
    engram_dir.mkdir(parents=True)
    (engram_dir / "knowledge.db").write_bytes(b"")
    (engram_dir / "config.json").write_text(
        json.dumps({"agent_name": agent_name, "schema_version": 2})
    )
    return engram_dir


# ---------------------------------------------------------------------------
# 1. Basic detection: two roots, names from config (NOT dir/username)
# ---------------------------------------------------------------------------

class TestDiscoverBasic(unittest.TestCase):
    """discover_agents returns both agents and derives names from config.json."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp(prefix="engram_disc_basic_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_two_roots_both_detected(self):
        home_a = self.tmpdir / "home_a"
        home_b = self.tmpdir / "home_b"
        home_a.mkdir()
        home_b.mkdir()
        # Dir name intentionally differs from agent_name to prove config-derived.
        _make_engram_dir(home_a, ".engram", "ariadne-calvino")
        _make_engram_dir(home_b, ".engram", "borges-agent")

        result = discover_agents(scan_roots=[home_a, home_b])

        self.assertIn("ariadne-calvino", result, "ariadne-calvino not discovered")
        self.assertIn("borges-agent", result, "borges-agent not discovered")
        self.assertEqual(len(result), 2)

    def test_name_from_config_not_dir(self):
        """The agent name must come from config.json, not the directory name."""
        home = self.tmpdir / "home_x"
        home.mkdir()
        # dir name = ".engram-kepler"; agent_name = "kepler-prime" — they differ.
        _make_engram_dir(home, ".engram-kepler", "kepler-prime")

        result = discover_agents(scan_roots=[home])

        self.assertIn("kepler-prime", result,
                      "agent name should be 'kepler-prime' from config, not '.engram-kepler'")
        self.assertNotIn(".engram-kepler", result)

    def test_db_path_points_to_knowledge_db(self):
        home = self.tmpdir / "home_db"
        home.mkdir()
        engram_dir = _make_engram_dir(home, ".engram", "mira-agent")

        result = discover_agents(scan_roots=[home])

        self.assertIn("mira-agent", result)
        expected_db = str(engram_dir / "knowledge.db")
        self.assertEqual(result["mira-agent"]["db"], expected_db)


# ---------------------------------------------------------------------------
# 2. Variant dir names: .engram-gemini detected; .bak / baseline skipped
# ---------------------------------------------------------------------------

class TestDiscoverDirVariants(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp(prefix="engram_disc_variants_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_engram_gemini_variant_detected(self):
        home = self.tmpdir / "home_gemini"
        home.mkdir()
        _make_engram_dir(home, ".engram-gemini", "gemini-agent")

        result = discover_agents(scan_roots=[home])

        self.assertIn("gemini-agent", result)

    def test_bak_dir_skipped(self):
        home = self.tmpdir / "home_bak"
        home.mkdir()
        _make_engram_dir(home, ".engram.first-session-baseline.bak", "should-be-skipped")

        result = discover_agents(scan_roots=[home])

        self.assertNotIn("should-be-skipped", result,
                         ".bak dirs must be skipped")
        self.assertEqual(len(result), 0)

    def test_baseline_dir_skipped(self):
        home = self.tmpdir / "home_baseline"
        home.mkdir()
        _make_engram_dir(home, ".engram-baseline-backup", "also-skipped")

        result = discover_agents(scan_roots=[home])

        self.assertNotIn("also-skipped", result,
                         "baseline-containing dirs must be skipped")
        self.assertEqual(len(result), 0)

    def test_valid_and_bak_same_root(self):
        """A valid .engram alongside a .bak — only valid is detected."""
        home = self.tmpdir / "home_mixed"
        home.mkdir()
        _make_engram_dir(home, ".engram", "valid-agent")
        _make_engram_dir(home, ".engram.bak", "backup-agent")

        result = discover_agents(scan_roots=[home])

        self.assertIn("valid-agent", result)
        self.assertNotIn("backup-agent", result)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# 3. Graceful skip: various failure modes must not raise
# ---------------------------------------------------------------------------

class TestDiscoverGracefulSkip(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp(prefix="engram_disc_skip_"))

    def tearDown(self):
        import shutil
        # Restore permissions before cleanup so shutil can delete.
        for root, dirs, files in os.walk(self.tmpdir):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_config_json_skipped(self):
        home = self.tmpdir / "home_noconfig"
        home.mkdir()
        engram_dir = home / ".engram"
        engram_dir.mkdir()
        (engram_dir / "knowledge.db").write_bytes(b"")
        # No config.json — must be silently skipped.

        result = discover_agents(scan_roots=[home])

        self.assertEqual(result, {}, "dir without config.json must be skipped")

    def test_missing_knowledge_db_skipped(self):
        home = self.tmpdir / "home_nodb"
        home.mkdir()
        engram_dir = home / ".engram"
        engram_dir.mkdir()
        (engram_dir / "config.json").write_text(
            json.dumps({"agent_name": "no-db-agent"})
        )
        # No knowledge.db — must be silently skipped.

        result = discover_agents(scan_roots=[home])

        self.assertEqual(result, {}, "dir without knowledge.db must be skipped")

    def test_missing_agent_name_skipped(self):
        home = self.tmpdir / "home_noname"
        home.mkdir()
        engram_dir = home / ".engram"
        engram_dir.mkdir()
        (engram_dir / "knowledge.db").write_bytes(b"")
        (engram_dir / "config.json").write_text(
            json.dumps({"schema_version": 2})  # no agent_name
        )

        result = discover_agents(scan_roots=[home])

        self.assertEqual(result, {}, "config without agent_name must be skipped")

    def test_blank_agent_name_skipped(self):
        home = self.tmpdir / "home_blank"
        home.mkdir()
        engram_dir = home / ".engram"
        engram_dir.mkdir()
        (engram_dir / "knowledge.db").write_bytes(b"")
        (engram_dir / "config.json").write_text(
            json.dumps({"agent_name": ""})
        )

        result = discover_agents(scan_roots=[home])

        self.assertEqual(result, {}, "blank agent_name must be skipped")

    def test_malformed_json_config_skipped(self):
        home = self.tmpdir / "home_badjson"
        home.mkdir()
        engram_dir = home / ".engram"
        engram_dir.mkdir()
        (engram_dir / "knowledge.db").write_bytes(b"")
        (engram_dir / "config.json").write_bytes(b"not-valid-json{{{")

        result = discover_agents(scan_roots=[home])

        self.assertEqual(result, {}, "malformed config.json must be skipped without raise")

    def test_unreadable_dir_skipped_no_raise(self):
        """An unreadable engram dir must be skipped without raising (unless root)."""
        if os.getuid() == 0:
            self.skipTest("Running as root — chmod 000 has no effect")

        home = self.tmpdir / "home_unreadable"
        home.mkdir()
        engram_dir = home / ".engram"
        engram_dir.mkdir()
        (engram_dir / "knowledge.db").write_bytes(b"")
        (engram_dir / "config.json").write_text(
            json.dumps({"agent_name": "locked-agent"})
        )
        os.chmod(str(engram_dir), 0o000)

        try:
            result = discover_agents(scan_roots=[home])
        except Exception as exc:
            self.fail(f"discover_agents raised on unreadable dir: {exc}")

        self.assertNotIn("locked-agent", result)

    def test_unreadable_root_skipped_no_raise(self):
        """An unreadable home root must be skipped without raising."""
        if os.getuid() == 0:
            self.skipTest("Running as root — chmod 000 has no effect")

        home = self.tmpdir / "home_locked"
        home.mkdir()
        os.chmod(str(home), 0o000)

        try:
            result = discover_agents(scan_roots=[home])
        except Exception as exc:
            self.fail(f"discover_agents raised on unreadable root: {exc}")

        self.assertEqual(result, {})

    def test_partial_results_with_one_bad_dir(self):
        """One bad dir must not prevent discovering good dirs in other roots."""
        home_good = self.tmpdir / "home_good"
        home_bad = self.tmpdir / "home_bad"
        home_good.mkdir()
        home_bad.mkdir()

        _make_engram_dir(home_good, ".engram", "good-agent")

        # Bad dir: has .engram but no config.json.
        engram_bad = home_bad / ".engram"
        engram_bad.mkdir()
        (engram_bad / "knowledge.db").write_bytes(b"")

        result = discover_agents(scan_roots=[home_good, home_bad])

        self.assertIn("good-agent", result)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# 4. Merge / override semantics
# ---------------------------------------------------------------------------

class TestMergeConfig(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp(prefix="engram_disc_merge_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_discovered(self) -> dict:
        """Build a fake discovered dict with two agents."""
        home_a = self.tmpdir / "home_a"
        home_b = self.tmpdir / "home_b"
        home_a.mkdir(exist_ok=True)
        home_b.mkdir(exist_ok=True)
        _make_engram_dir(home_a, ".engram", "alpha-agent")
        _make_engram_dir(home_b, ".engram", "beta-agent")
        return discover_agents(scan_roots=[home_a, home_b])

    def _write_config(self, cfg: dict) -> str:
        path = self.tmpdir / "agents.json"
        path.write_text(json.dumps(cfg))
        return str(path)

    def test_label_override(self):
        discovered = self._make_discovered()
        cfg_path = self._write_config({
            "agents": [{"name": "alpha-agent", "label": "Alpha (pretty)"}],
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        self.assertEqual(agents["alpha-agent"]["label"], "Alpha (pretty)")
        # beta-agent label unchanged.
        self.assertEqual(agents["beta-agent"]["label"], "beta-agent")

    def test_explicit_default_wins(self):
        discovered = self._make_discovered()
        cfg_path = self._write_config({
            "agents": [],
            "default": "beta-agent",
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        self.assertEqual(default, "beta-agent")

    def test_merge_does_not_mutate_caller_discovered(self):
        # The merge must deep-copy inner dicts: overriding label/db on the
        # merged result must NOT leak back into the caller's discovered dict.
        discovered = self._make_discovered()
        original_label = discovered["alpha-agent"]["label"]
        original_db = discovered["alpha-agent"]["db"]
        cfg_path = self._write_config({
            "agents": [{"name": "alpha-agent", "label": "Mutated",
                        "db": "/elsewhere/knowledge.db"}],
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        # Merged result reflects the override...
        self.assertEqual(agents["alpha-agent"]["label"], "Mutated")
        # ...but the caller's discovered dict is untouched.
        self.assertEqual(discovered["alpha-agent"]["label"], original_label)
        self.assertEqual(discovered["alpha-agent"]["db"], original_db)

    def test_exclude_removes_agent(self):
        discovered = self._make_discovered()
        cfg_path = self._write_config({
            "exclude": ["alpha-agent"],
            "agents": [],
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        self.assertNotIn("alpha-agent", agents)
        self.assertIn("beta-agent", agents)

    def test_add_non_detected_agent_with_db(self):
        """An entry with db that was not auto-detected is added (back-compat)."""
        discovered = self._make_discovered()
        extra_db = self.tmpdir / "extra_knowledge.db"
        extra_db.write_bytes(b"")
        cfg_path = self._write_config({
            "agents": [{"name": "gamma-agent", "db": str(extra_db), "label": "Gamma"}],
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        self.assertIn("gamma-agent", agents)
        self.assertEqual(agents["gamma-agent"]["label"], "Gamma")
        self.assertEqual(agents["gamma-agent"]["db"], str(extra_db))

    def test_non_detected_no_db_skipped(self):
        """An entry without db that is not auto-detected is skipped with a warning."""
        discovered = self._make_discovered()
        cfg_path = self._write_config({
            "agents": [{"name": "ghost-agent", "label": "Ghost"}],
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        self.assertNotIn("ghost-agent", agents)

    def test_merged_registry_preserves_both_detected(self):
        discovered = self._make_discovered()
        cfg_path = self._write_config({
            "agents": [{"name": "alpha-agent", "label": "Alpha (pretty)"}],
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        self.assertIn("alpha-agent", agents)
        self.assertIn("beta-agent", agents)

    def test_default_sorted_first_when_no_explicit_default(self):
        """Without explicit default, first agent by sorted name is the default."""
        discovered = self._make_discovered()
        cfg_path = self._write_config({"agents": []})

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        # 'alpha-agent' sorts before 'beta-agent'.
        self.assertEqual(default, "alpha-agent")

    def test_invalid_default_raises(self):
        discovered = self._make_discovered()
        cfg_path = self._write_config({
            "agents": [],
            "default": "nonexistent-agent",
        })

        with self.assertRaises(ValueError):
            _merge_config_over_discovered(discovered, cfg_path)

    def test_all_four_overrides_combined(self):
        """All four operations (label override, default, exclude, add) in one config."""
        home_a = self.tmpdir / "home_a"
        home_b = self.tmpdir / "home_b"
        home_c = self.tmpdir / "home_c"
        for h in [home_a, home_b, home_c]:
            h.mkdir(exist_ok=True)
        _make_engram_dir(home_a, ".engram", "alpha-agent")
        _make_engram_dir(home_b, ".engram", "beta-agent")
        _make_engram_dir(home_c, ".engram", "gamma-agent")

        discovered = discover_agents(scan_roots=[home_a, home_b, home_c])

        extra_db = self.tmpdir / "delta_knowledge.db"
        extra_db.write_bytes(b"")

        cfg_path = self._write_config({
            "exclude": ["gamma-agent"],
            "default": "beta-agent",
            "agents": [
                {"name": "alpha-agent", "label": "Alpha (renamed)"},
                {"name": "delta-agent", "db": str(extra_db), "label": "Delta"},
            ],
        })

        agents, default = _merge_config_over_discovered(discovered, cfg_path)

        # a) label override
        self.assertEqual(agents["alpha-agent"]["label"], "Alpha (renamed)")
        # b) explicit default
        self.assertEqual(default, "beta-agent")
        # c) exclude
        self.assertNotIn("gamma-agent", agents)
        # d) add non-detected
        self.assertIn("delta-agent", agents)
        self.assertEqual(agents["delta-agent"]["db"], str(extra_db))


# ---------------------------------------------------------------------------
# 5. db-derivation: registered db is always <engram_dir>/knowledge.db
# ---------------------------------------------------------------------------

class TestDbDerivation(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp(prefix="engram_disc_db_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_db_is_knowledge_db_under_engram_dir(self):
        home = self.tmpdir / "home_db"
        home.mkdir()
        engram_dir = _make_engram_dir(home, ".engram", "test-agent")

        result = discover_agents(scan_roots=[home])

        self.assertIn("test-agent", result)
        expected = str(engram_dir / "knowledge.db")
        self.assertEqual(result["test-agent"]["db"], expected)

    def test_db_absolute_path(self):
        home = self.tmpdir / "home_abs"
        home.mkdir()
        engram_dir = _make_engram_dir(home, ".engram", "abs-agent")

        result = discover_agents(scan_roots=[home])

        db_path = result["abs-agent"]["db"]
        self.assertTrue(os.path.isabs(db_path),
                        f"db path should be absolute, got: {db_path}")

    def test_engram_gemini_db_path(self):
        home = self.tmpdir / "home_gemini"
        home.mkdir()
        engram_dir = _make_engram_dir(home, ".engram-gemini", "gemini-test")

        result = discover_agents(scan_roots=[home])

        self.assertIn("gemini-test", result)
        expected = str(engram_dir / "knowledge.db")
        self.assertEqual(result["gemini-test"]["db"], expected)


# ---------------------------------------------------------------------------
# 6. Collision handling: first-wins on duplicate agent_name
# ---------------------------------------------------------------------------

class TestCollisionHandling(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp(prefix="engram_disc_coll_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_wins_on_same_agent_name(self):
        """If two candidates have the same agent_name, first one registered wins."""
        home_a = self.tmpdir / "home_a"
        home_b = self.tmpdir / "home_b"
        home_a.mkdir()
        home_b.mkdir()
        dir_a = _make_engram_dir(home_a, ".engram", "shared-name")
        _make_engram_dir(home_b, ".engram", "shared-name")

        result = discover_agents(scan_roots=[home_a, home_b])

        self.assertEqual(len(result), 1)
        self.assertIn("shared-name", result)
        # home_a was passed first → its db should win.
        expected_db = str(dir_a / "knowledge.db")
        self.assertEqual(result["shared-name"]["db"], expected_db)


if __name__ == "__main__":
    unittest.main()
