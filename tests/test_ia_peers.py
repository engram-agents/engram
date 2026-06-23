"""Tests for the `ia peers` subcommand and scan_peers() helper.

Part 2 of issue #1266: scan_peers() classifies peers co-host vs cross-host,
excludes self, marks stale by age, and skips malformed files.

The ia CLI integration test verifies:
  - `ia peers` renders a table in human format.
  - `ia peers --format json` returns the scan_peers() list as JSON.
  - Mode gate is exempt (peers works in single-agent mode too).
"""

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Import the module under test directly (no subprocess for unit tests)
_REPO_ROOT = Path(__file__).resolve().parents[1]
_IA_PATH = _REPO_ROOT / "src" / "engram" / "tools" / "ia.py"

import importlib.util

_spec = importlib.util.spec_from_file_location("ia", _IA_PATH)
_ia = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ia)

scan_peers = _ia.scan_peers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWN_HOSTNAME = socket.gethostname()
OTHER_HOSTNAME = f"other-host-{OWN_HOSTNAME}"

_NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)
_RECENT = (_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
_STALE = (_NOW - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_reg(directory: Path, agent_name: str, hostname: str, registered_at: str, pid: int = 12345) -> None:
    """Write a well-formed registration JSON file to the registry directory."""
    record = {
        "agent_name": agent_name,
        "hostname": hostname,
        "pid": pid,
        "registered_at": registered_at,
    }
    (directory / f"{agent_name}.json").write_text(json.dumps(record))


# ---------------------------------------------------------------------------
# scan_peers unit tests
# ---------------------------------------------------------------------------


class TestScanPeers:
    def test_co_host_classification(self, tmp_path):
        """A peer with the same hostname is classified as co-host."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert len(peers) == 1
        assert peers[0]["agent_name"] == "ariadne"
        assert peers[0]["topology"] == "co-host"

    def test_cross_host_classification(self, tmp_path):
        """A peer with a different hostname is classified as cross-host."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "sol", OTHER_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert len(peers) == 1
        assert peers[0]["topology"] == "cross-host"

    def test_excludes_self(self, tmp_path):
        """The own agent's registration file is excluded from results."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "borges", OWN_HOSTNAME, _RECENT)
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        names = [p["agent_name"] for p in peers]
        assert "borges" not in names
        assert "ariadne" in names

    def test_self_exclusion_case_insensitive(self, tmp_path):
        """Self exclusion is case-insensitive."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "Borges", OWN_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert len(peers) == 0

    def test_marks_stale_by_age(self, tmp_path):
        """Entries older than the stale threshold are marked stale."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _STALE)

        peers = scan_peers("borges", registry_dir=str(registry), stale_hours=24, _now=_NOW)
        assert len(peers) == 1
        assert peers[0]["stale"] is True

    def test_recent_entry_not_stale(self, tmp_path):
        """Entries within the stale threshold are NOT marked stale."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), stale_hours=24, _now=_NOW)
        assert len(peers) == 1
        assert peers[0]["stale"] is False

    def test_skips_malformed_json(self, tmp_path):
        """Malformed JSON files are skipped; other entries still returned."""
        registry = tmp_path / "agents"
        registry.mkdir()
        (registry / "bad.json").write_text("this is not json {{{{")
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        # bad.json skipped; ariadne still returned
        names = [p["agent_name"] for p in peers]
        assert "ariadne" in names

    def test_skips_missing_required_fields(self, tmp_path):
        """Entries without agent_name or hostname are skipped."""
        registry = tmp_path / "agents"
        registry.mkdir()
        # Missing agent_name
        (registry / "no-name.json").write_text(json.dumps({"hostname": OWN_HOSTNAME, "pid": 1, "registered_at": _RECENT}))
        # Missing hostname
        (registry / "no-host.json").write_text(json.dumps({"agent_name": "ghost", "pid": 1, "registered_at": _RECENT}))
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        names = [p["agent_name"] for p in peers]
        assert names == ["ariadne"], f"expected only ariadne, got {names}"

    def test_mixed_topology(self, tmp_path):
        """Mixed co-host and cross-host peers are both returned correctly."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)
        _write_reg(registry, "sol", OTHER_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        topologies = {p["agent_name"]: p["topology"] for p in peers}
        assert topologies["ariadne"] == "co-host"
        assert topologies["sol"] == "cross-host"

    def test_empty_registry_dir(self, tmp_path):
        """An empty registry dir returns an empty list."""
        registry = tmp_path / "agents"
        registry.mkdir()

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert peers == []

    def test_missing_registry_dir(self, tmp_path):
        """A non-existent registry dir returns an empty list without raising."""
        registry = tmp_path / "nonexistent"
        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert peers == []

    def test_sorted_by_agent_name(self, tmp_path):
        """Results are sorted alphabetically by agent_name."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "zephyr", OWN_HOSTNAME, _RECENT)
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)
        _write_reg(registry, "mira", OTHER_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        names = [p["agent_name"] for p in peers]
        assert names == sorted(names, key=str.lower)

    def test_age_seconds_populated(self, tmp_path):
        """age_seconds is populated from registered_at."""
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert peers[0]["age_seconds"] is not None
        # ~1 hour = 3600s; allow ±5s for rounding
        assert abs(peers[0]["age_seconds"] - 3600) < 5

    def test_missing_registered_at_ok(self, tmp_path):
        """An entry with no registered_at is returned with age_seconds=None."""
        registry = tmp_path / "agents"
        registry.mkdir()
        record = {"agent_name": "ariadne", "hostname": OWN_HOSTNAME, "pid": 99}
        (registry / "ariadne.json").write_text(json.dumps(record))

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert len(peers) == 1
        assert peers[0]["age_seconds"] is None
        assert peers[0]["stale"] is False

    def test_invalid_registered_at_ok(self, tmp_path):
        """An entry with an invalid registered_at is returned with age_seconds=None."""
        registry = tmp_path / "agents"
        registry.mkdir()
        record = {"agent_name": "ariadne", "hostname": OWN_HOSTNAME, "pid": 99,
                  "registered_at": "not-a-date"}
        (registry / "ariadne.json").write_text(json.dumps(record))

        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        assert len(peers) == 1
        assert peers[0]["age_seconds"] is None

    def test_non_numeric_pid_does_not_crash(self, tmp_path):
        """A structurally-valid JSON with a non-numeric pid must NOT crash scan_peers.

        Regression test for the blocker identified in PR review: the pid coercion
        `int(pid)` was outside the try/except guarding JSON parsing, so a record
        like {"pid": "corrupted"} raised an uncaught ValueError that aborted the
        whole scan. Other valid peers must still be returned.
        """
        registry = tmp_path / "agents"
        registry.mkdir()
        # Structurally-valid JSON, but pid is a non-numeric string
        bad_record = {
            "agent_name": "ariadne",
            "hostname": OWN_HOSTNAME,
            "pid": "bad",
            "registered_at": _RECENT,
        }
        (registry / "ariadne.json").write_text(json.dumps(bad_record))
        # A second, fully valid peer to confirm scan continues
        _write_reg(registry, "sol", OTHER_HOSTNAME, _RECENT)

        # Must not raise; must return peers (at minimum sol, possibly ariadne with pid=None)
        peers = scan_peers("borges", registry_dir=str(registry), _now=_NOW)
        names = [p["agent_name"] for p in peers]
        # sol (valid) must still be present
        assert "sol" in names, f"valid peer 'sol' missing after non-numeric pid; got {names}"
        # ariadne should be present (skipping the bad pid, not the whole record)
        assert "ariadne" in names, f"ariadne dropped entirely; expected pid=None, got {names}"
        # ariadne's pid should have degraded gracefully to None
        ariadne = next(p for p in peers if p["agent_name"] == "ariadne")
        assert ariadne["pid"] is None, f"expected pid=None for non-numeric pid, got {ariadne['pid']}"


# ---------------------------------------------------------------------------
# ia CLI integration tests for `ia peers`
# ---------------------------------------------------------------------------


def _run_ia(
    engram_home: Path,
    inter_agent_dir: Path,
    *args: str,
    env_overrides: dict | None = None,
) -> tuple:
    """Run the ia CLI as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["INTER_AGENT_DIR"] = str(inter_agent_dir)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(_IA_PATH)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(engram_home: Path, mode: str, agent_name: str = "borges") -> None:
    cfg = {"mode": mode, "agent_name": agent_name}
    (engram_home / "config.json").write_text(json.dumps(cfg))


class TestIaPeersCLI:
    def test_peers_empty_registry(self, tmp_path):
        """ia peers returns exit 0 with empty registry message."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        inter_agent_dir = tmp_path / "inter-agent"
        inter_agent_dir.mkdir()
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_config(engram_home, "multi", "borges")

        rc, out, err = _run_ia(
            engram_home, inter_agent_dir,
            "peers", "--registry-dir", str(registry),
        )
        assert rc == 0
        assert "0" in out or "no peer" in out.lower()

    def test_peers_json_format(self, tmp_path):
        """ia peers --format json returns valid JSON list."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        inter_agent_dir = tmp_path / "inter-agent"
        inter_agent_dir.mkdir()
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_config(engram_home, "multi", "borges")
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)

        rc, out, err = _run_ia(
            engram_home, inter_agent_dir,
            "peers", "--format", "json", "--registry-dir", str(registry),
        )
        assert rc == 0, f"stderr: {err}"
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["agent_name"] == "ariadne"
        assert data[0]["topology"] == "co-host"

    def test_peers_human_format_shows_table(self, tmp_path):
        """ia peers --format human renders a table with header row."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        inter_agent_dir = tmp_path / "inter-agent"
        inter_agent_dir.mkdir()
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_config(engram_home, "multi", "borges")
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _RECENT)
        _write_reg(registry, "sol", OTHER_HOSTNAME, _RECENT)

        rc, out, err = _run_ia(
            engram_home, inter_agent_dir,
            "peers", "--registry-dir", str(registry),
        )
        assert rc == 0, f"stderr: {err}"
        assert "ariadne" in out
        assert "sol" in out
        assert "co-host" in out
        assert "cross-host" in out

    def test_peers_exempt_from_mode_gate_single_mode(self, tmp_path):
        """ia peers works in single-agent mode (exempt from mode gate)."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        inter_agent_dir = tmp_path / "inter-agent"
        inter_agent_dir.mkdir()
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_config(engram_home, "single", "borges")  # single-agent mode

        rc, out, err = _run_ia(
            engram_home, inter_agent_dir,
            "peers", "--registry-dir", str(registry),
        )
        # Should succeed (exit 0), not exit 3 (mode gate)
        assert rc == 0, f"peers should not be gated; got rc={rc}, stderr: {err}"

    def test_peers_stale_marker_in_output(self, tmp_path):
        """Stale entries show (stale) marker in human output."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        inter_agent_dir = tmp_path / "inter-agent"
        inter_agent_dir.mkdir()
        registry = tmp_path / "agents"
        registry.mkdir()
        _write_config(engram_home, "multi", "borges")
        _write_reg(registry, "ariadne", OWN_HOSTNAME, _STALE)

        rc, out, err = _run_ia(
            engram_home, inter_agent_dir,
            "peers", "--registry-dir", str(registry),
        )
        assert rc == 0
        assert "stale" in out.lower()
