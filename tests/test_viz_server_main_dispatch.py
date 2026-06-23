"""Tests for viz_server.main() CLI-argument dispatch.

PR #1109 unit-tested discover_agents() and _merge_config_over_discovered()
directly. This file covers the three-path agent-registry build wired inside
main(), which was previously uncovered.

Seven dispatch paths exercised:

1. --db <dir>      → single-agent, directory auto-appended with knowledge.db
2. --db <file.db>  → single-agent, direct .db path used as-is
3. --no-autodetect --config <path>  → explicit-config-only (_load_config called)
4. --no-autodetect (alone)          → single-agent default (DEFAULT_DB)
5. (default) + discovery returns agents → pure auto-detect (_pick_default used)
6. (default) + --config             → auto-detect base + config merge
7. (default) + discovery empty + no config → single-agent fallback (DEFAULT_DB)

Plus:

8. --config + --db together         → parser.error (mutual-exclusion guard)

All tests mock discover_agents, _load_config, _merge_config_over_discovered,
and HTTPServer so nothing touches the filesystem beyond what the test controls
and no port is ever bound.

AGENTS and DEFAULT_AGENT module globals are saved and restored around each
test so tests are fully isolated from each other and from the real environment.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Insert the src/engram path so `import viz_server` resolves cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "engram"))

import viz_server
from viz_server import main, DEFAULT_DB


# ---------------------------------------------------------------------------
# Base class: save/restore module globals + minimal argv scaffolding
# ---------------------------------------------------------------------------

class _MainDispatchBase(unittest.TestCase):
    """Common setUp/tearDown for all main() dispatch tests.

    Saves AGENTS and DEFAULT_AGENT before each test and restores them after,
    so tests are isolated even when main() sets them as module globals.
    """

    def setUp(self):
        self._saved_agents = dict(viz_server.AGENTS)
        self._saved_default_agent = viz_server.DEFAULT_AGENT

    def tearDown(self):
        viz_server.AGENTS = self._saved_agents
        viz_server.DEFAULT_AGENT = self._saved_default_agent

    # ------------------------------------------------------------------
    # Convenience: run main() with controlled argv + a mocked HTTPServer.
    # Returns the mock server instance so callers can assert on it if needed.
    # ------------------------------------------------------------------

    def _run_main(self, argv_tail: list, extra_patches: dict | None = None):
        """Run main() with sys.argv = ['viz_server'] + argv_tail.

        Always patches HTTPServer so no port is bound and serve_forever does
        not block. Passes extra_patches as additional context-managed mocks
        into the viz_server module namespace.

        Returns the mock HTTPServer *instance* (the one returned by the
        patched constructor).
        """
        mock_server_instance = MagicMock()
        mock_server_instance.serve_forever.side_effect = KeyboardInterrupt()

        patches = {
            "HTTPServer": MagicMock(return_value=mock_server_instance),
        }
        if extra_patches:
            patches.update(extra_patches)

        with patch.object(sys, "argv", ["viz_server"] + argv_tail):
            with unittest.mock.patch.multiple("viz_server", **patches):
                try:
                    main()
                except KeyboardInterrupt:
                    pass  # serve_forever raises KeyboardInterrupt → normal exit path

        return mock_server_instance


# ---------------------------------------------------------------------------
# 1 & 2. --db PATH: single-agent (directory + direct .db file)
# ---------------------------------------------------------------------------

class TestMainDbFlag(unittest.TestCase):
    """--db flag forces single-agent mode regardless of autodetect."""

    def setUp(self):
        self._saved_agents = dict(viz_server.AGENTS)
        self._saved_default_agent = viz_server.DEFAULT_AGENT
        self._tmpdir = Path(tempfile.mkdtemp(prefix="engram_main_db_"))

    def tearDown(self):
        viz_server.AGENTS = self._saved_agents
        viz_server.DEFAULT_AGENT = self._saved_default_agent
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _run_main_with_db(self, argv_tail):
        mock_server_instance = MagicMock()
        mock_server_instance.serve_forever.side_effect = KeyboardInterrupt()

        with patch.object(sys, "argv", ["viz_server"] + argv_tail):
            with patch("viz_server.HTTPServer", return_value=mock_server_instance):
                try:
                    main()
                except KeyboardInterrupt:
                    pass

    def test_db_directory_appends_knowledge_db(self):
        """--db <dir> must resolve to <dir>/knowledge.db in AGENTS."""
        db_dir = self._tmpdir / "myagent"
        db_dir.mkdir()

        self._run_main_with_db(["--db", str(db_dir)])

        self.assertIn("default", viz_server.AGENTS)
        self.assertEqual(viz_server.DEFAULT_AGENT, "default")
        expected_db = str(db_dir / "knowledge.db")
        self.assertEqual(viz_server.AGENTS["default"]["db"], expected_db)

    def test_db_direct_file_used_as_is(self):
        """--db <file.db> (non-directory path) must be used directly."""
        db_file = self._tmpdir / "custom.db"
        # Write it so the path resolves cleanly; isdir() returns False for files.
        db_file.write_bytes(b"")

        self._run_main_with_db(["--db", str(db_file)])

        self.assertIn("default", viz_server.AGENTS)
        self.assertEqual(viz_server.DEFAULT_AGENT, "default")
        self.assertEqual(viz_server.AGENTS["default"]["db"], str(db_file))

    def test_db_single_agent_has_correct_shape(self):
        """AGENTS['default'] must have name, label, and db keys."""
        db_dir = self._tmpdir / "shaped"
        db_dir.mkdir()

        self._run_main_with_db(["--db", str(db_dir)])

        entry = viz_server.AGENTS["default"]
        self.assertEqual(entry["name"], "default")
        self.assertEqual(entry["label"], "Default")
        self.assertIn("db", entry)

    def test_db_only_one_agent_registered(self):
        """--db must register exactly one agent ('default')."""
        db_dir = self._tmpdir / "only"
        db_dir.mkdir()

        self._run_main_with_db(["--db", str(db_dir)])

        self.assertEqual(len(viz_server.AGENTS), 1)


# ---------------------------------------------------------------------------
# 3. --no-autodetect --config PATH → _load_config called
# ---------------------------------------------------------------------------

class TestMainNoAutodetectWithConfig(_MainDispatchBase):
    """--no-autodetect --config: _load_config is the sole source of truth."""

    def test_load_config_called_with_config_path(self):
        """_load_config must be called with the provided --config path."""
        fake_agents = {
            "alice": {"name": "alice", "label": "Alice", "db": "/fake/alice.db"},
        }
        fake_default = "alice"
        load_config_mock = MagicMock(return_value=(fake_agents, fake_default))

        self._run_main(
            ["--no-autodetect", "--config", "/tmp/agents.json"],
            extra_patches={"_load_config": load_config_mock},
        )

        load_config_mock.assert_called_once_with("/tmp/agents.json")

    def test_agents_and_default_from_load_config(self):
        """AGENTS and DEFAULT_AGENT must reflect _load_config's return values."""
        fake_agents = {
            "alice": {"name": "alice", "label": "Alice", "db": "/fake/alice.db"},
            "bob": {"name": "bob", "label": "Bob", "db": "/fake/bob.db"},
        }
        fake_default = "bob"
        load_config_mock = MagicMock(return_value=(fake_agents, fake_default))

        self._run_main(
            ["--no-autodetect", "--config", "/tmp/agents.json"],
            extra_patches={"_load_config": load_config_mock},
        )

        self.assertEqual(viz_server.AGENTS, fake_agents)
        self.assertEqual(viz_server.DEFAULT_AGENT, fake_default)

    def test_discover_agents_not_called(self):
        """When --no-autodetect is set, discover_agents must not be called."""
        fake_agents = {"alice": {"name": "alice", "label": "Alice", "db": "/fake/a.db"}}
        discover_mock = MagicMock(return_value={})
        load_config_mock = MagicMock(return_value=(fake_agents, "alice"))

        self._run_main(
            ["--no-autodetect", "--config", "/tmp/agents.json"],
            extra_patches={
                "discover_agents": discover_mock,
                "_load_config": load_config_mock,
            },
        )

        discover_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. --no-autodetect (alone) → single-agent default (DEFAULT_DB)
# ---------------------------------------------------------------------------

class TestMainNoAutodetectAlone(_MainDispatchBase):
    """--no-autodetect without --config falls back to single-agent DEFAULT_DB."""

    def test_default_db_used(self):
        """AGENTS['default']['db'] must equal DEFAULT_DB."""
        self._run_main(["--no-autodetect"])

        self.assertIn("default", viz_server.AGENTS)
        self.assertEqual(viz_server.AGENTS["default"]["db"], DEFAULT_DB)

    def test_default_agent_is_default(self):
        """DEFAULT_AGENT must be 'default'."""
        self._run_main(["--no-autodetect"])

        self.assertEqual(viz_server.DEFAULT_AGENT, "default")

    def test_only_one_agent_registered(self):
        """Exactly one agent ('default') must be registered."""
        self._run_main(["--no-autodetect"])

        self.assertEqual(len(viz_server.AGENTS), 1)

    def test_discover_agents_not_called(self):
        """discover_agents must not be called when --no-autodetect is set."""
        discover_mock = MagicMock(return_value={})

        self._run_main(
            ["--no-autodetect"],
            extra_patches={"discover_agents": discover_mock},
        )

        discover_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Default (no flags) + discovery returns agents → pure auto-detect
# ---------------------------------------------------------------------------

class TestMainPureAutodetect(_MainDispatchBase):
    """Default mode: discover_agents returns agents → AGENTS set from discovery."""

    def _fake_agents(self, *names):
        return {n: {"name": n, "label": n, "db": f"/fake/{n}.db"} for n in names}

    def test_agents_set_from_discovery(self):
        """AGENTS must be the dict returned by discover_agents."""
        fake_discovered = self._fake_agents("ariadne", "borges")
        discover_mock = MagicMock(return_value=fake_discovered)

        with patch.object(viz_server, "_own_agent_name", return_value=""):
            self._run_main(
                [],
                extra_patches={"discover_agents": discover_mock},
            )

        self.assertEqual(viz_server.AGENTS, fake_discovered)

    def test_default_agent_is_sorted_first_when_own_absent(self):
        """When _own_agent_name returns '', DEFAULT_AGENT is sorted-first agent."""
        fake_discovered = self._fake_agents("zara", "alpha", "beta")
        discover_mock = MagicMock(return_value=fake_discovered)

        with patch.object(viz_server, "_own_agent_name", return_value=""):
            self._run_main(
                [],
                extra_patches={"discover_agents": discover_mock},
            )

        self.assertEqual(viz_server.DEFAULT_AGENT, "alpha")

    def test_default_agent_is_own_when_present(self):
        """When _own_agent_name returns a name in discovered, it wins as default."""
        fake_discovered = self._fake_agents("ariadne", "borges", "kepler")
        discover_mock = MagicMock(return_value=fake_discovered)

        with patch.object(viz_server, "_own_agent_name", return_value="kepler"):
            self._run_main(
                [],
                extra_patches={"discover_agents": discover_mock},
            )

        self.assertEqual(viz_server.DEFAULT_AGENT, "kepler")

    def test_merge_not_called_when_no_config(self):
        """_merge_config_over_discovered must not be called when no --config given."""
        fake_discovered = self._fake_agents("ariadne")
        discover_mock = MagicMock(return_value=fake_discovered)
        merge_mock = MagicMock()

        with patch.object(viz_server, "_own_agent_name", return_value=""):
            self._run_main(
                [],
                extra_patches={
                    "discover_agents": discover_mock,
                    "_merge_config_over_discovered": merge_mock,
                },
            )

        merge_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Default + --config → auto-detect base + config merge
# ---------------------------------------------------------------------------

class TestMainAutodetectWithConfig(_MainDispatchBase):
    """Default mode + --config: discover_agents is base, merge layered on top."""

    def _fake_agents(self, *names):
        return {n: {"name": n, "label": n, "db": f"/fake/{n}.db"} for n in names}

    def test_discover_agents_called(self):
        """discover_agents must be called even when --config is given (it is the base)."""
        fake_discovered = self._fake_agents("ariadne")
        discover_mock = MagicMock(return_value=fake_discovered)
        merged_agents = self._fake_agents("ariadne", "extra-agent")
        merge_mock = MagicMock(return_value=(merged_agents, "ariadne"))

        self._run_main(
            ["--config", "/tmp/agents.json"],
            extra_patches={
                "discover_agents": discover_mock,
                "_merge_config_over_discovered": merge_mock,
            },
        )

        discover_mock.assert_called_once()

    def test_merge_called_with_discovered_and_config_path(self):
        """_merge_config_over_discovered must be called with (discovered, config_path)."""
        fake_discovered = self._fake_agents("ariadne")
        discover_mock = MagicMock(return_value=fake_discovered)
        merged_agents = self._fake_agents("ariadne")
        merge_mock = MagicMock(return_value=(merged_agents, "ariadne"))

        self._run_main(
            ["--config", "/tmp/agents.json"],
            extra_patches={
                "discover_agents": discover_mock,
                "_merge_config_over_discovered": merge_mock,
            },
        )

        merge_mock.assert_called_once_with(fake_discovered, "/tmp/agents.json")

    def test_agents_and_default_from_merge(self):
        """AGENTS and DEFAULT_AGENT must reflect the merge return values."""
        fake_discovered = self._fake_agents("ariadne")
        discover_mock = MagicMock(return_value=fake_discovered)
        merged_agents = self._fake_agents("ariadne", "extra-agent")
        fake_default = "extra-agent"
        merge_mock = MagicMock(return_value=(merged_agents, fake_default))

        self._run_main(
            ["--config", "/tmp/agents.json"],
            extra_patches={
                "discover_agents": discover_mock,
                "_merge_config_over_discovered": merge_mock,
            },
        )

        self.assertEqual(viz_server.AGENTS, merged_agents)
        self.assertEqual(viz_server.DEFAULT_AGENT, fake_default)

    def test_load_config_not_called_in_default_mode(self):
        """_load_config must NOT be called in default (non-no-autodetect) mode."""
        fake_discovered = self._fake_agents("ariadne")
        discover_mock = MagicMock(return_value=fake_discovered)
        merged_agents = self._fake_agents("ariadne")
        merge_mock = MagicMock(return_value=(merged_agents, "ariadne"))
        load_config_mock = MagicMock()

        self._run_main(
            ["--config", "/tmp/agents.json"],
            extra_patches={
                "discover_agents": discover_mock,
                "_merge_config_over_discovered": merge_mock,
                "_load_config": load_config_mock,
            },
        )

        load_config_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Default + discovery empty + no config → single-agent fallback
# ---------------------------------------------------------------------------

class TestMainAutodetectFallback(_MainDispatchBase):
    """Default mode + empty discovery + no --config → DEFAULT_DB single-agent."""

    def test_fallback_to_default_db(self):
        """AGENTS['default']['db'] must equal DEFAULT_DB when discovery returns nothing."""
        discover_mock = MagicMock(return_value={})

        self._run_main(
            [],
            extra_patches={"discover_agents": discover_mock},
        )

        self.assertIn("default", viz_server.AGENTS)
        self.assertEqual(viz_server.AGENTS["default"]["db"], DEFAULT_DB)

    def test_default_agent_is_default(self):
        """DEFAULT_AGENT must be 'default' in the fallback path."""
        discover_mock = MagicMock(return_value={})

        self._run_main(
            [],
            extra_patches={"discover_agents": discover_mock},
        )

        self.assertEqual(viz_server.DEFAULT_AGENT, "default")

    def test_only_one_agent_registered(self):
        """Exactly one agent ('default') must be registered in the fallback path."""
        discover_mock = MagicMock(return_value={})

        self._run_main(
            [],
            extra_patches={"discover_agents": discover_mock},
        )

        self.assertEqual(len(viz_server.AGENTS), 1)

    def test_agent_entry_shape(self):
        """AGENTS['default'] must have the correct name, label, and db keys."""
        discover_mock = MagicMock(return_value={})

        self._run_main(
            [],
            extra_patches={"discover_agents": discover_mock},
        )

        entry = viz_server.AGENTS["default"]
        self.assertEqual(entry["name"], "default")
        self.assertEqual(entry["label"], "Default")
        self.assertEqual(entry["db"], DEFAULT_DB)


# ---------------------------------------------------------------------------
# 8. --config + --db together → parser.error (mutual-exclusion guard)
# ---------------------------------------------------------------------------

class TestMainMutualExclusionGuard(unittest.TestCase):
    """--config and --db together must call parser.error (they are mutually exclusive)."""

    def setUp(self):
        self._saved_agents = dict(viz_server.AGENTS)
        self._saved_default_agent = viz_server.DEFAULT_AGENT

    def tearDown(self):
        viz_server.AGENTS = self._saved_agents
        viz_server.DEFAULT_AGENT = self._saved_default_agent

    def test_parser_error_raised_when_both_config_and_db_given(self):
        """Providing both --config and --db must trigger parser.error."""
        # argparse.ArgumentParser.error raises SystemExit (exit code 2) after
        # printing to stderr. We intercept it via SystemExit so the test does
        # not abort and so we can assert the exit happened.
        with patch.object(sys, "argv",
                          ["viz_server", "--config", "/tmp/a.json", "--db", "/tmp/b.db"]):
            with self.assertRaises(SystemExit) as cm:
                main()
        # argparse.error() exits with code 2 by convention.
        self.assertEqual(cm.exception.code, 2)

    def test_agents_not_set_when_mutual_exclusion_fires(self):
        """AGENTS must remain at its pre-call value when parser.error fires."""
        original_agents = dict(viz_server.AGENTS)
        with patch.object(sys, "argv",
                          ["viz_server", "--config", "/tmp/a.json", "--db", "/tmp/b.db"]):
            try:
                main()
            except SystemExit:
                pass

        self.assertEqual(viz_server.AGENTS, original_agents)


if __name__ == "__main__":
    unittest.main()
