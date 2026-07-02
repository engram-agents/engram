"""Tests for viz_server live agent re-discovery (TTL-cached rescan, issue #1526).

Uses fake clock injection (unittest.mock.patch on time.monotonic) and
discover_agents mocking so no filesystem I/O happens and timing is deterministic.

Test inventory:
a. Within TTL → _refresh_agents_if_stale is a no-op; discover_agents not called.
b. Past TTL → re-scan fires; a newly-appeared agent shows up in AGENTS.
c. --db single mode (_AGENT_STARTUP_MODE="single_db") → never re-scans regardless
   of elapsed time.
d. A refresh that raises → last-good registry retained; no exception propagated.

Additionally:
e. discover_fallback mode → retry discovery on TTL expiry; newly-appeared agent
   picked up.
f. discover_merge mode → discover + merge fires on TTL expiry.
g. config_only mode → _load_config called on TTL expiry; no discover_agents.
h. Empty string mode ("", not yet seeded by main) → no-op.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "engram"))

import viz_server
from viz_server import _refresh_agents_if_stale


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_entry(name: str, db: str = "/fake/knowledge.db") -> dict:
    return {"name": name, "label": name, "db": db}


def _agents(*names) -> dict:
    return {n: _agent_entry(n) for n in names}


class _RefreshBase(unittest.TestCase):
    """Save/restore all module-level cache state around each test."""

    def setUp(self):
        self._saved_agents = dict(viz_server.AGENTS)
        self._saved_default = viz_server.DEFAULT_AGENT
        self._saved_mode = viz_server._AGENT_STARTUP_MODE
        self._saved_config = viz_server._AGENT_STARTUP_CONFIG
        self._saved_ts = viz_server._AGENT_CACHE_TS

    def tearDown(self):
        viz_server.AGENTS = self._saved_agents
        viz_server.DEFAULT_AGENT = self._saved_default
        viz_server._AGENT_STARTUP_MODE = self._saved_mode
        viz_server._AGENT_STARTUP_CONFIG = self._saved_config
        viz_server._AGENT_CACHE_TS = self._saved_ts


# ---------------------------------------------------------------------------
# a. Within TTL — cache hit, discover_agents NOT called
# ---------------------------------------------------------------------------

class TestCacheHitWithinTTL(_RefreshBase):
    """discover_agents is never called when the cache is still fresh."""

    def test_no_rescan_within_ttl(self):
        viz_server._AGENT_STARTUP_MODE = "discover_only"
        # Fake clock: now=1000.0; last scan was at 990.0 → 10s ago → within 45s TTL.
        viz_server._AGENT_CACHE_TS = 990.0
        viz_server.AGENTS = _agents("existing-agent")
        viz_server.DEFAULT_AGENT = "existing-agent"

        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents") as mock_discover:
                _refresh_agents_if_stale(ttl_seconds=45.0)

        mock_discover.assert_not_called()
        self.assertIn("existing-agent", viz_server.AGENTS)

    def test_exactly_at_ttl_boundary_is_cache_hit(self):
        """now - ts == ttl exactly is within TTL (≤, not <)."""
        viz_server._AGENT_STARTUP_MODE = "discover_only"
        viz_server._AGENT_CACHE_TS = 955.0  # exactly 45s before now=1000
        viz_server.AGENTS = _agents("boundary-agent")
        viz_server.DEFAULT_AGENT = "boundary-agent"

        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents") as mock_discover:
                _refresh_agents_if_stale(ttl_seconds=45.0)

        mock_discover.assert_not_called()


# ---------------------------------------------------------------------------
# b. Past TTL — re-scan fires; new agent appears in AGENTS
# ---------------------------------------------------------------------------

class TestCacheMissPastTTL(_RefreshBase):
    """Past the TTL, discover_agents is called and the new agent is picked up."""

    def test_new_agent_appears_after_ttl(self):
        viz_server._AGENT_STARTUP_MODE = "discover_only"
        # now=1000.0; last scan at 900.0 → 100s ago → past 45s TTL.
        viz_server._AGENT_CACHE_TS = 900.0
        viz_server.AGENTS = _agents("old-agent")
        viz_server.DEFAULT_AGENT = "old-agent"

        new_registry = _agents("old-agent", "new-agent")
        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents", return_value=new_registry):
                with patch.object(viz_server, "_pick_default", return_value="old-agent"):
                    _refresh_agents_if_stale(ttl_seconds=45.0)

        self.assertIn("new-agent", viz_server.AGENTS)
        self.assertIn("old-agent", viz_server.AGENTS)

    def test_rescan_called_exactly_once_past_ttl(self):
        viz_server._AGENT_STARTUP_MODE = "discover_only"
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = _agents("alpha")
        viz_server.DEFAULT_AGENT = "alpha"

        discovered = _agents("alpha", "beta")
        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents", return_value=discovered) as mock_d:
                with patch.object(viz_server, "_pick_default", return_value="alpha"):
                    _refresh_agents_if_stale(ttl_seconds=45.0)

        mock_d.assert_called_once()

    def test_cache_timestamp_updated_after_rescan(self):
        viz_server._AGENT_STARTUP_MODE = "discover_only"
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = _agents("alpha")
        viz_server.DEFAULT_AGENT = "alpha"

        now_val = 12345.0
        discovered = _agents("alpha")
        with patch.object(viz_server.time, "monotonic", return_value=now_val):
            with patch.object(viz_server, "discover_agents", return_value=discovered):
                with patch.object(viz_server, "_pick_default", return_value="alpha"):
                    _refresh_agents_if_stale(ttl_seconds=45.0)

        self.assertEqual(viz_server._AGENT_CACHE_TS, now_val)


# ---------------------------------------------------------------------------
# c. single_db mode — never re-scans regardless of elapsed time
# ---------------------------------------------------------------------------

class TestSingleDbModeNeverRescans(_RefreshBase):
    """In single_db mode, _refresh_agents_if_stale never calls discover_agents."""

    def test_single_db_no_rescan_even_stale(self):
        viz_server._AGENT_STARTUP_MODE = "single_db"
        # Timestamp is zero — maximally stale; TTL=0 forces expiry on any other mode.
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = {"default": _agent_entry("default", "/explicit/knowledge.db")}
        viz_server.DEFAULT_AGENT = "default"

        with patch.object(viz_server.time, "monotonic", return_value=9999.0):
            with patch.object(viz_server, "discover_agents") as mock_discover:
                _refresh_agents_if_stale(ttl_seconds=0.0)

        mock_discover.assert_not_called()
        # Registry unchanged.
        self.assertEqual(
            viz_server.AGENTS["default"]["db"], "/explicit/knowledge.db"
        )

    def test_single_db_registry_unchanged(self):
        original = {"default": _agent_entry("default", "/my/db.db")}
        viz_server._AGENT_STARTUP_MODE = "single_db"
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = original
        viz_server.DEFAULT_AGENT = "default"

        with patch.object(viz_server.time, "monotonic", return_value=99999.0):
            _refresh_agents_if_stale(ttl_seconds=0.0)

        self.assertEqual(viz_server.AGENTS, original)


# ---------------------------------------------------------------------------
# d. Refresh raises — last-good registry retained; no exception out
# ---------------------------------------------------------------------------

class TestRefreshFailureSafe(_RefreshBase):
    """When the refresh throws, the last-good registry is preserved."""

    def test_exception_keeps_last_good_no_raise(self):
        viz_server._AGENT_STARTUP_MODE = "discover_only"
        viz_server._AGENT_CACHE_TS = 0.0
        last_good = _agents("last-good-agent")
        viz_server.AGENTS = last_good
        viz_server.DEFAULT_AGENT = "last-good-agent"

        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(
                viz_server, "discover_agents", side_effect=RuntimeError("disk error")
            ):
                try:
                    _refresh_agents_if_stale(ttl_seconds=0.0)
                except Exception as exc:
                    self.fail(f"_refresh_agents_if_stale raised unexpectedly: {exc}")

        self.assertIn("last-good-agent", viz_server.AGENTS)
        self.assertNotIn("new-agent", viz_server.AGENTS)
        # Anti-hammering: the except block bumps the cache TS so the next retry
        # waits a full TTL. Without this assertion, deleting that bump would
        # leave the test green (the gap the reviewer flagged).
        self.assertEqual(viz_server._AGENT_CACHE_TS, 1000.0)

    def test_exception_in_merge_keeps_last_good(self):
        """Failure in _merge_config_over_discovered also keeps the last-good registry."""
        viz_server._AGENT_STARTUP_MODE = "discover_merge"
        viz_server._AGENT_STARTUP_CONFIG = "/fake/agents.json"
        viz_server._AGENT_CACHE_TS = 0.0
        last_good = _agents("stable-agent")
        viz_server.AGENTS = last_good
        viz_server.DEFAULT_AGENT = "stable-agent"

        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents", return_value=_agents("found")):
                with patch.object(
                    viz_server,
                    "_merge_config_over_discovered",
                    side_effect=ValueError("bad config"),
                ):
                    try:
                        _refresh_agents_if_stale(ttl_seconds=0.0)
                    except Exception as exc:
                        self.fail(f"_refresh_agents_if_stale raised unexpectedly: {exc}")

        self.assertIn("stable-agent", viz_server.AGENTS)
        self.assertEqual(viz_server._AGENT_CACHE_TS, 1000.0)

    def test_empty_discover_in_merge_keeps_last_good_no_merge(self):
        """discover_merge: an empty discover() is a transient scan failure — keep
        last-good, bump TS, and do NOT call _merge_config_over_discovered (which
        would narrow the registry to config-only agents and drop discovered ones)."""
        viz_server._AGENT_STARTUP_MODE = "discover_merge"
        viz_server._AGENT_STARTUP_CONFIG = "/fake/agents.json"
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = _agents("discovered-only-agent")
        viz_server.DEFAULT_AGENT = "discovered-only-agent"

        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents", return_value={}):
                with patch.object(
                    viz_server, "_merge_config_over_discovered"
                ) as mock_merge:
                    _refresh_agents_if_stale(ttl_seconds=0.0)

        mock_merge.assert_not_called()
        self.assertIn("discovered-only-agent", viz_server.AGENTS)
        self.assertEqual(viz_server._AGENT_CACHE_TS, 1000.0)


# ---------------------------------------------------------------------------
# e. discover_fallback mode — retry discovery; new agent picked up
# ---------------------------------------------------------------------------

class TestDiscoverFallbackMode(_RefreshBase):
    """In discover_fallback mode, discovery is retried on TTL expiry."""

    def test_fallback_picks_up_new_agent(self):
        viz_server._AGENT_STARTUP_MODE = "discover_fallback"
        viz_server._AGENT_CACHE_TS = 0.0
        # Simulates the initial fallback: a "default" sentinel agent.
        viz_server.AGENTS = {"default": _agent_entry("default")}
        viz_server.DEFAULT_AGENT = "default"

        newly_found = _agents("ariadne-calvino")
        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents", return_value=newly_found):
                with patch.object(viz_server, "_pick_default", return_value="ariadne-calvino"):
                    _refresh_agents_if_stale(ttl_seconds=0.0)

        self.assertIn("ariadne-calvino", viz_server.AGENTS)
        self.assertEqual(viz_server.DEFAULT_AGENT, "ariadne-calvino")

    def test_fallback_keeps_registry_when_still_nothing_found(self):
        """If discovery still returns empty, the fallback sentinel is preserved."""
        viz_server._AGENT_STARTUP_MODE = "discover_fallback"
        viz_server._AGENT_CACHE_TS = 0.0
        sentinel = {"default": _agent_entry("default")}
        viz_server.AGENTS = sentinel
        viz_server.DEFAULT_AGENT = "default"

        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents", return_value={}):
                _refresh_agents_if_stale(ttl_seconds=0.0)

        self.assertEqual(viz_server.AGENTS, sentinel)
        self.assertEqual(viz_server.DEFAULT_AGENT, "default")


# ---------------------------------------------------------------------------
# f. discover_merge mode — both discover + merge fire on TTL expiry
# ---------------------------------------------------------------------------

class TestDiscoverMergeMode(_RefreshBase):

    def test_discover_and_merge_called_past_ttl(self):
        viz_server._AGENT_STARTUP_MODE = "discover_merge"
        viz_server._AGENT_STARTUP_CONFIG = "/fake/agents.json"
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = _agents("old")
        viz_server.DEFAULT_AGENT = "old"

        merged = _agents("old", "new-merged")
        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents", return_value=_agents("old")) as mock_d:
                with patch.object(
                    viz_server, "_merge_config_over_discovered", return_value=(merged, "old")
                ) as mock_m:
                    _refresh_agents_if_stale(ttl_seconds=0.0)

        mock_d.assert_called_once()
        mock_m.assert_called_once_with(_agents("old"), "/fake/agents.json")
        self.assertIn("new-merged", viz_server.AGENTS)

    def test_discover_not_called_within_ttl_in_merge_mode(self):
        viz_server._AGENT_STARTUP_MODE = "discover_merge"
        viz_server._AGENT_STARTUP_CONFIG = "/fake/agents.json"
        # Cache fresh: 10s ago, TTL=45s.
        viz_server._AGENT_CACHE_TS = 990.0
        viz_server.AGENTS = _agents("old")
        viz_server.DEFAULT_AGENT = "old"

        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents") as mock_d:
                _refresh_agents_if_stale(ttl_seconds=45.0)

        mock_d.assert_not_called()


# ---------------------------------------------------------------------------
# g. config_only mode — _load_config called; discover_agents NOT called
# ---------------------------------------------------------------------------

class TestConfigOnlyMode(_RefreshBase):

    def test_config_only_calls_load_config_not_discover(self):
        viz_server._AGENT_STARTUP_MODE = "config_only"
        viz_server._AGENT_STARTUP_CONFIG = "/my/agents.json"
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = _agents("old")
        viz_server.DEFAULT_AGENT = "old"

        new_reg = _agents("config-reloaded")
        with patch.object(viz_server.time, "monotonic", return_value=1000.0):
            with patch.object(viz_server, "discover_agents") as mock_d:
                with patch.object(
                    viz_server, "_load_config", return_value=(new_reg, "config-reloaded")
                ) as mock_lc:
                    _refresh_agents_if_stale(ttl_seconds=0.0)

        mock_d.assert_not_called()
        mock_lc.assert_called_once_with("/my/agents.json")
        self.assertIn("config-reloaded", viz_server.AGENTS)


# ---------------------------------------------------------------------------
# h. Uninitialized mode ("") — no-op
# ---------------------------------------------------------------------------

class TestUninitializedMode(_RefreshBase):

    def test_empty_mode_is_noop(self):
        viz_server._AGENT_STARTUP_MODE = ""
        viz_server._AGENT_CACHE_TS = 0.0
        viz_server.AGENTS = _agents("existing")
        viz_server.DEFAULT_AGENT = "existing"

        with patch.object(viz_server.time, "monotonic", return_value=9999.0):
            with patch.object(viz_server, "discover_agents") as mock_d:
                _refresh_agents_if_stale(ttl_seconds=0.0)

        mock_d.assert_not_called()
        self.assertIn("existing", viz_server.AGENTS)


if __name__ == "__main__":
    unittest.main()
