"""Tests for viz.data.config write infrastructure (PR-2: PUT /api/config/<key>)
(engram-alpha#2555 PR A retarget -- was viz_server).

Covers:
1.  Valid write round-trips (value survives to disk and is read back correctly).
2.  Invalid type rejected with HTTP 400.
3.  Out-of-range value rejected with HTTP 400.
4.  Unknown key rejected with HTTP 400.
5.  Atomic write leaves no partial/temp file on simulated failure.
6.  Backup created once per session (first write); second write reuses same backup.
7.  restart_required=True for a known restart-key (polarity.enabled).
8.  restart_required=False for a non-restart key (cadence.drowsiness_caution_pct).
9.  Read-only key rejected (editable=False).
10. Body missing 'value' field rejected.
11. Empty body rejected.
12. Unknown agent rejected.
13. HTTP route: PUT /api/config/<key> returns correct status + shape.
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
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import server as viz_server
from viz.data import config as viz_config_data


# ---------------------------------------------------------------------------
# Minimal config.json that passes the schema for common keys under test.
# ---------------------------------------------------------------------------

def _make_config() -> dict:
    return {
        "schema_version": 2,
        "trust_pool": ["reuters.com"],
        "primary_user": "test_user",
        "agent_name": "test_agent",
        "mode": "single",
        "counterparts": [],
        "github": {"enabled": False, "primary_user_login": "", "counterparts_logins": []},
        "embedding": {"enabled": True, "model": "all-MiniLM-L6-v2"},
        "cadence": {
            "drowsiness_caution_pct": 80,
            "drowsiness_urgent_pct": 90,
        },
        "memory": {
            "tier1_max_nodes": 200,
            "tier2_max_nodes": 1000,
            "decay_base": 1.014,
        },
        "polarity": {
            "enabled": False,
            "model": "dleemiller/ModernCE-large-nli",
            "threshold": 0.46,
            "min_similarity_for_check": 0.30,
        },
        "yellow_domains": [],
        "confidence_map": {},
    }


class ConfigWriteBase(unittest.TestCase):
    """Common setUp/tearDown: temp engram dir + AGENTS registry."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self._saved_backups = dict(viz_config_data._CONFIG_WRITE_BACKUPS)
        viz_config_data._CONFIG_WRITE_BACKUPS.clear()

        self.tmpdir = tempfile.mkdtemp(prefix="engram_cfg_write_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        self.db_path = self.engram_home / "knowledge.db"
        self.db_path.write_bytes(b"")
        self.config_path = self.engram_home / "config.json"
        self.config_path.write_text(json.dumps(_make_config(), indent=2))

        viz_agents.AGENTS = {
            "test": {
                "name": "test",
                "label": "Test",
                "db": str(self.db_path),
            }
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        viz_config_data._CONFIG_WRITE_BACKUPS.clear()
        viz_config_data._CONFIG_WRITE_BACKUPS.update(self._saved_backups)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_config(self) -> dict:
        return json.loads(self.config_path.read_text())


# ---------------------------------------------------------------------------
# Unit tests for write_config_key()
# ---------------------------------------------------------------------------

class TestWriteConfigKeyRoundTrip(ConfigWriteBase):
    """Valid writes persist to disk."""

    def test_integer_key_round_trips(self):
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 75
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_value"], 75)
        cfg = self._read_config()
        self.assertEqual(cfg["cadence"]["drowsiness_caution_pct"], 75)

    def test_float_key_round_trips(self):
        result = viz_config_data.write_config_key("test", "memory.decay_base", 1.05)
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(result["validated_value"], 1.05)
        cfg = self._read_config()
        self.assertAlmostEqual(cfg["memory"]["decay_base"], 1.05)

    def test_boolean_key_round_trips(self):
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_enabled", True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_value"], True)
        cfg = self._read_config()
        self.assertEqual(cfg["cadence"]["auto_sleep_enabled"], True)

    def test_string_key_round_trips(self):
        result = viz_config_data.write_config_key("test", "primary_user", "alice")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_value"], "alice")
        cfg = self._read_config()
        self.assertEqual(cfg["primary_user"], "alice")

    def test_list_key_round_trips(self):
        new_pool = ["reuters.com", "apnews.com"]
        result = viz_config_data.write_config_key("test", "trust_pool", new_pool)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_value"], new_pool)
        cfg = self._read_config()
        self.assertEqual(cfg["trust_pool"], new_pool)

    def test_integer_coerced_from_string(self):
        # Browser form inputs send strings — the write path should coerce.
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_urgent_pct", "85"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_value"], 85)

    def test_float_coerced_from_string(self):
        result = viz_config_data.write_config_key("test", "memory.decay_base", "1.020")
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(result["validated_value"], 1.020)

    def test_boolean_coerced_from_string_true(self):
        result = viz_config_data.write_config_key("test", "polarity.enabled", "true")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_value"], True)

    def test_boolean_coerced_from_string_false(self):
        result = viz_config_data.write_config_key("test", "polarity.enabled", "false")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_value"], False)

    def test_bool_coerced_to_int_for_integer_key(self):
        # JSON true / false are valid Python bools; int(True)==1, int(False)==0.
        # The integer coercion path (int(v)) accepts them and stores 1 / 0.
        # This test documents the current behavior — not a contract that it
        # must be accepted, but a record of what the code actually does so
        # any future change to reject bools here is deliberate.
        result_true = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", True
        )
        self.assertTrue(result_true["ok"], result_true)
        self.assertEqual(result_true["validated_value"], 1)

        result_false = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", False
        )
        self.assertTrue(result_false["ok"], result_false)
        self.assertEqual(result_false["validated_value"], 0)


class TestWriteConfigKeyValidationRejection(ConfigWriteBase):
    """Invalid inputs return ok=False without touching disk."""

    def test_wrong_type_integer_rejected(self):
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", "not-a-number"
        )
        self.assertFalse(result["ok"])
        self.assertIn("integer", result["error"].lower())

    def test_wrong_type_float_rejected(self):
        result = viz_config_data.write_config_key("test", "memory.decay_base", "banana")
        self.assertFalse(result["ok"])
        self.assertIn("float", result["error"].lower())

    def test_wrong_type_string_for_list_rejected(self):
        result = viz_config_data.write_config_key("test", "trust_pool", "reuters.com")
        self.assertFalse(result["ok"])
        self.assertIn("list", result["error"].lower())

    def test_out_of_range_min_rejected(self):
        # cadence.drowsiness_caution_pct min=0; send -1
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", -1
        )
        self.assertFalse(result["ok"])
        self.assertIn("minimum", result["error"].lower())

    def test_out_of_range_max_rejected(self):
        # cadence.drowsiness_caution_pct max=100; send 101
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 101
        )
        self.assertFalse(result["ok"])
        self.assertIn("maximum", result["error"].lower())

    def test_out_of_range_float_rejected(self):
        # memory.decay_base min=1.0 max=1.5; send 2.0
        result = viz_config_data.write_config_key("test", "memory.decay_base", 2.0)
        self.assertFalse(result["ok"])
        self.assertIn("maximum", result["error"].lower())

    def test_unknown_key_rejected(self):
        result = viz_config_data.write_config_key(
            "test", "nonexistent.totally_fake_key", 42
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown", result["error"].lower())

    def test_readonly_key_rejected(self):
        # embedding.model is editable=False
        result = viz_config_data.write_config_key(
            "test", "embedding.model", "some-new-model"
        )
        self.assertFalse(result["ok"])
        self.assertIn("read-only", result["error"].lower())

    def test_disk_not_touched_on_validation_error(self):
        mtime_before = os.path.getmtime(str(self.config_path))
        viz_config_data.write_config_key("test", "cadence.drowsiness_caution_pct", 999)
        mtime_after = os.path.getmtime(str(self.config_path))
        self.assertEqual(mtime_before, mtime_after)

    def test_unknown_agent_rejected(self):
        result = viz_config_data.write_config_key(
            "no_such_agent", "cadence.drowsiness_caution_pct", 75
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown agent", result["error"].lower())


class TestWriteConfigKeyBackup(ConfigWriteBase):
    """Backup created exactly once per session per agent."""

    def test_backup_created_on_first_write(self):
        self.assertEqual(len(viz_config_data._CONFIG_WRITE_BACKUPS), 0)
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 70
        )
        self.assertTrue(result["ok"])
        self.assertIn("test", viz_config_data._CONFIG_WRITE_BACKUPS)
        backup_path = viz_config_data._CONFIG_WRITE_BACKUPS["test"]
        self.assertTrue(os.path.exists(backup_path), "Backup file should exist")

    def test_backup_contains_last_good_value_after_write(self):
        # After a successful write (80 → 70), the backup is refreshed to the
        # written value (70) so a later rollback lands on last-good, not session-start.
        # (#380 backup-refresh fix)
        viz_config_data.write_config_key("test", "cadence.drowsiness_caution_pct", 70)
        backup_path = viz_config_data._CONFIG_WRITE_BACKUPS["test"]
        backup_cfg = json.loads(Path(backup_path).read_text())
        self.assertEqual(backup_cfg["cadence"]["drowsiness_caution_pct"], 70)

    def test_backup_created_only_once(self):
        viz_config_data.write_config_key("test", "cadence.drowsiness_caution_pct", 70)
        backup_path_1 = viz_config_data._CONFIG_WRITE_BACKUPS["test"]

        viz_config_data.write_config_key("test", "cadence.drowsiness_urgent_pct", 85)
        backup_path_2 = viz_config_data._CONFIG_WRITE_BACKUPS["test"]

        self.assertEqual(backup_path_1, backup_path_2, "Same backup for second write")

    def test_backup_path_includes_timestamp(self):
        viz_config_data.write_config_key("test", "cadence.drowsiness_caution_pct", 70)
        backup_path = viz_config_data._CONFIG_WRITE_BACKUPS["test"]
        self.assertIn(".bak.", backup_path)
        # Should end with something like 20260524T120000Z
        self.assertTrue(Path(backup_path).name.endswith("Z"),
                        f"Unexpected backup name: {Path(backup_path).name}")

    def test_backup_refreshed_after_successful_write(self):
        """After write A succeeds, backup must reflect A's value (not session-start).

        Regression test for #380: backup was written only once per session at the
        START of the session, so a later rollback would restore to session-start
        instead of last-good.
        """
        # Session-start value for drowsiness_caution_pct is 80 (_make_config).
        # Write A: set to 70.
        result_a = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 70
        )
        self.assertTrue(result_a["ok"], result_a)

        # After successful write A, backup must contain A's value (70), not 80.
        backup_path = viz_config_data._CONFIG_WRITE_BACKUPS["test"]
        backup_cfg = json.loads(Path(backup_path).read_text())
        self.assertEqual(
            backup_cfg["cadence"]["drowsiness_caution_pct"], 70,
            "Backup must refresh to last-good (70) after successful write; "
            "found session-start value instead.",
        )

    def test_rollback_after_second_write_restores_last_good(self):
        """Torn write #2 rolls back to last-good (write #1), not session-start.

        Without the backup-refresh fix (#380): rollback of write #2 would land on
        session-start (80), discarding write #1's value (70).
        With the fix: backup is refreshed after write #1 succeeds, so rollback of
        write #2 correctly restores 70.
        """
        # Write A succeeds: caution_pct 80 → 70.
        result_a = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 70
        )
        self.assertTrue(result_a["ok"], result_a)

        # Write B torn: mock shutil.copyfileobj to simulate a mid-copy disk error
        # on the second invocation (matching test_rollback_restores_config_on_torn_write
        # pattern from PR #376).
        real_copyfileobj = shutil.copyfileobj
        state = {"calls": 0}

        def torn_then_real(src, dst, *args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                dst.write('{"torn')  # leave genuinely invalid JSON, then fail
                raise OSError("simulated disk-full mid-copy")
            return real_copyfileobj(src, dst, *args, **kwargs)

        shutil.copyfileobj = torn_then_real
        try:
            result_b = viz_config_data.write_config_key(
                "test", "cadence.drowsiness_urgent_pct", 95
            )
        finally:
            shutil.copyfileobj = real_copyfileobj

        self.assertFalse(result_b["ok"])
        self.assertIn("Write failed", result_b["error"])

        # Rollback of write B must restore write A's value (70), NOT session-start (80).
        cfg = self._read_config()
        self.assertEqual(
            cfg["cadence"]["drowsiness_caution_pct"], 70,
            "Rollback after torn write B must restore last-good (write A = 70), "
            "not session-start (80). Backup-refresh fix #380 is missing.",
        )


class TestWriteConfigKeyRestartRequired(ConfigWriteBase):
    """restart_required flag reflects schema metadata."""

    def test_restart_required_true_for_polarity_enabled(self):
        result = viz_config_data.write_config_key("test", "polarity.enabled", True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["restart_required"])

    def test_restart_required_true_for_polarity_model(self):
        result = viz_config_data.write_config_key(
            "test", "polarity.model", "some-model/name"
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["restart_required"])

    def test_restart_required_false_for_drowsiness_caution_pct(self):
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 75
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["restart_required"])

    def test_restart_required_false_for_trust_pool(self):
        result = viz_config_data.write_config_key(
            "test", "trust_pool", ["reuters.com"]
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["restart_required"])


class TestWriteConfigKeyAtomicity(ConfigWriteBase):
    """Atomic write: no partial file left on disk after failure."""

    def test_no_temp_files_after_successful_write(self):
        viz_config_data.write_config_key("test", "cadence.drowsiness_caution_pct", 70)
        # Scan for leftover .tmp files in the config dir
        tmp_files = list(self.engram_home.glob(".config_write_*.tmp"))
        self.assertEqual(tmp_files, [], f"Leftover temp files: {tmp_files}")

    def test_config_json_remains_valid_after_write(self):
        viz_config_data.write_config_key("test", "cadence.drowsiness_caution_pct", 70)
        # config.json must still be valid JSON after write
        cfg = json.loads(self.config_path.read_text())
        self.assertIsInstance(cfg, dict)

    def test_rollback_on_verify_failure(self):
        """Simulate verify failure by patching _get_nested to return _MISSING."""
        original_get_nested = viz_config_data._get_nested

        call_count = [0]

        def fake_get_nested(config, key):
            call_count[0] += 1
            return viz_config_data._MISSING  # always fail verify

        viz_config_data._get_nested = fake_get_nested
        try:
            result = viz_config_data.write_config_key(
                "test", "cadence.drowsiness_caution_pct", 70
            )
        finally:
            viz_config_data._get_nested = original_get_nested
            # Clear backup tracking so tearDown works cleanly
            viz_config_data._CONFIG_WRITE_BACKUPS.pop("test", None)

        self.assertFalse(result["ok"])
        self.assertIn("verify failed", result["error"])
        self.assertIn("rolled back", result["error"])

        # Config should have been rolled back to original value (80)
        cfg = self._read_config()
        self.assertEqual(cfg["cadence"]["drowsiness_caution_pct"], 80)

    def test_rollback_restores_config_on_torn_write(self):
        """#372 regression: the in-place write is not crash-atomic — if the
        copy into config.json's existing inode tears the file mid-write, the
        write-block OSError handler must restore it from backup so the agent
        never inherits a corrupt/unreadable config (the lockout class #372 is
        about). Without that rollback, config.json would be left as invalid
        JSON and self._read_config() (json.loads) would raise.

        Note: we fail only the FIRST copyfileobj call (the in-place commit)
        and delegate the rest to the real implementation, because the rollback
        path (shutil.copyfile) may itself call copyfileobj internally — a
        blanket patch would break the very rollback under test."""
        real_copyfileobj = shutil.copyfileobj
        state = {"calls": 0}

        def torn_then_real(src, dst, *args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                dst.write('{"torn')  # leave genuinely invalid JSON, then fail
                raise OSError("simulated disk-full mid-copy")
            return real_copyfileobj(src, dst, *args, **kwargs)

        shutil.copyfileobj = torn_then_real
        try:
            result = viz_config_data.write_config_key(
                "test", "cadence.drowsiness_caution_pct", 99
            )
        finally:
            shutil.copyfileobj = real_copyfileobj
            viz_config_data._CONFIG_WRITE_BACKUPS.pop("test", None)

        self.assertFalse(result["ok"])
        self.assertIn("Write failed", result["error"])
        # config.json must be valid JSON again, restored to the original value.
        cfg = self._read_config()
        self.assertEqual(cfg["cadence"]["drowsiness_caution_pct"], 80)


class TestWriteConfigKeyPreservesInode(ConfigWriteBase):
    """Regression test for #372: write must preserve config.json's inode
    (hence owner + mode). os.rename swapped inodes → stripped agent ownership
    → operator:operator 0600 lockout. In-place write preserves the inode."""

    def test_inode_preserved_across_write(self):
        st_before = os.stat(str(self.config_path))
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 70
        )
        self.assertTrue(result["ok"], result)
        st_after = os.stat(str(self.config_path))
        self.assertEqual(
            st_before.st_ino, st_after.st_ino,
            "config.json inode changed — write swapped the file (os.rename "
            "regression); in-place write must preserve the inode so the "
            "owning agent keeps ownership (#372)",
        )

    def test_mode_preserved_across_write(self):
        os.chmod(str(self.config_path), 0o600)
        st_before = os.stat(str(self.config_path))
        result = viz_config_data.write_config_key(
            "test", "cadence.drowsiness_caution_pct", 71
        )
        self.assertTrue(result["ok"], result)
        st_after = os.stat(str(self.config_path))
        self.assertEqual(
            st_before.st_mode, st_after.st_mode,
            "config.json mode changed across write (#372)",
        )

    def test_value_still_persists_with_inplace_write(self):
        viz_config_data.write_config_key("test", "cadence.drowsiness_caution_pct", 72)
        cfg = self._read_config()
        self.assertEqual(cfg["cadence"]["drowsiness_caution_pct"], 72)


# ---------------------------------------------------------------------------
# HTTP integration tests for PUT /api/config/<key>
# ---------------------------------------------------------------------------

def _start_test_server() -> tuple:
    """Start viz_server on an ephemeral port. Returns (server, port, thread)."""
    server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port, t


class TestPutRouteHTTP(ConfigWriteBase):
    """End-to-end HTTP tests for PUT /api/config/<key>."""

    def setUp(self):
        super().setUp()
        self._server, self._port, self._thread = _start_test_server()

    def tearDown(self):
        self._server.shutdown()
        super().tearDown()

    def _put(self, key: str, value, agent: str = "test") -> tuple:
        """Send PUT request. Returns (status_code, parsed_json)."""
        body = json.dumps({"value": value}).encode()
        url = f"http://127.0.0.1:{self._port}/api/config/{key}?agent={agent}"
        req = Request(url, data=body, method="PUT",
                      headers={"Content-Type": "application/json",
                               "Content-Length": str(len(body))})
        try:
            resp = urlopen(req)
            data = json.loads(resp.read())
            return resp.status, data
        except HTTPError as e:
            data = json.loads(e.read())
            return e.code, data

    def test_valid_write_returns_200_ok(self):
        status, data = self._put("cadence.drowsiness_caution_pct", 70)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["validated_value"], 70)
        self.assertIn("restart_required", data)

    def test_invalid_type_returns_400(self):
        status, data = self._put("cadence.drowsiness_caution_pct", "banana")
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("error", data)

    def test_out_of_range_returns_400(self):
        status, data = self._put("cadence.drowsiness_caution_pct", 999)
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_unknown_key_returns_400(self):
        status, data = self._put("totally.fake.key", 42)
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_unknown_agent_returns_400(self):
        status, data = self._put("cadence.drowsiness_caution_pct", 70,
                                 agent="no_such_agent")
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_restart_required_true_in_response(self):
        status, data = self._put("polarity.enabled", True)
        self.assertEqual(status, 200)
        self.assertTrue(data["restart_required"])

    def test_restart_required_false_in_response(self):
        status, data = self._put("cadence.drowsiness_caution_pct", 70)
        self.assertEqual(status, 200)
        self.assertFalse(data["restart_required"])

    def test_value_persists_on_disk(self):
        self._put("cadence.drowsiness_caution_pct", 65)
        cfg = self._read_config()
        self.assertEqual(cfg["cadence"]["drowsiness_caution_pct"], 65)

    def test_missing_value_field_returns_400(self):
        body = json.dumps({"not_value": 70}).encode()
        url = f"http://127.0.0.1:{self._port}/api/config/cadence.drowsiness_caution_pct?agent=test"
        req = Request(url, data=body, method="PUT",
                      headers={"Content-Type": "application/json",
                               "Content-Length": str(len(body))})
        try:
            resp = urlopen(req)
            data = json.loads(resp.read())
            status = resp.status
        except HTTPError as e:
            data = json.loads(e.read())
            status = e.code
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_empty_body_returns_400(self):
        url = f"http://127.0.0.1:{self._port}/api/config/cadence.drowsiness_caution_pct?agent=test"
        req = Request(url, data=b"", method="PUT",
                      headers={"Content-Type": "application/json",
                               "Content-Length": "0"})
        try:
            resp = urlopen(req)
            data = json.loads(resp.read())
            status = resp.status
        except HTTPError as e:
            data = json.loads(e.read())
            status = e.code
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_options_preflight_returns_204(self):
        """CORS preflight should return 204 with allow headers."""
        from urllib.request import urlopen
        url = f"http://127.0.0.1:{self._port}/api/config/cadence.drowsiness_caution_pct"
        req = Request(url, method="OPTIONS")
        try:
            resp = urlopen(req)
            self.assertEqual(resp.status, 204)
        except HTTPError as e:
            self.fail(f"OPTIONS returned error: {e.code}")


class TestAutoSleepTimePatternValidation(ConfigWriteBase):
    """cadence.auto_sleep_time enforces HH:MM 24h format via schema pattern."""

    def test_valid_hhmm_accepted(self):
        """'03:00' is a valid HH:MM value and must be accepted."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "03:00")
        self.assertTrue(result["ok"], f"Expected ok=True for '03:00'; got: {result}")
        self.assertEqual(result["validated_value"], "03:00")

    def test_midnight_accepted(self):
        """'00:00' (midnight) is a valid HH:MM value."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "00:00")
        self.assertTrue(result["ok"], f"Expected ok=True for '00:00'; got: {result}")

    def test_end_of_day_accepted(self):
        """'23:59' (last minute of day) is a valid HH:MM value."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "23:59")
        self.assertTrue(result["ok"], f"Expected ok=True for '23:59'; got: {result}")

    def test_am_suffix_rejected(self):
        """'03 am' (12h with suffix) must be rejected — not HH:MM 24h format."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "03 am")
        self.assertFalse(result["ok"], "Expected ok=False for '03 am'")
        self.assertIn("HH:MM", result.get("error", ""),
                      f"Error should mention HH:MM format; got: {result.get('error')}")

    def test_24h_format_rejected(self):
        """'3:00' (single-digit hour) must be rejected — zero-padding required."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "3:00")
        self.assertFalse(result["ok"], "Expected ok=False for '3:00' (no zero-pad)")

    def test_invalid_hour_rejected(self):
        """'25:00' (hour > 23) must be rejected."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "25:00")
        self.assertFalse(result["ok"], "Expected ok=False for '25:00'")

    def test_invalid_minute_rejected(self):
        """'12:60' (minute > 59) must be rejected."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "12:60")
        self.assertFalse(result["ok"], "Expected ok=False for '12:60'")

    def test_free_text_rejected(self):
        """'bedtime' (arbitrary string) must be rejected."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "bedtime")
        self.assertFalse(result["ok"], "Expected ok=False for 'bedtime'")

    def test_value_persists_on_valid_write(self):
        """Valid write persists to config.json at cadence.auto_sleep_time."""
        result = viz_config_data.write_config_key("test", "cadence.auto_sleep_time", "22:30")
        self.assertTrue(result["ok"], result)
        cfg = self._read_config()
        self.assertEqual(cfg["cadence"]["auto_sleep_time"], "22:30")


class TestSelfLineagePatternValidation(ConfigWriteBase):
    """self_lineage pattern-mismatch error must NOT say 'HH:MM' (issue #990)."""

    def test_valid_lineage_accepted(self):
        """'anthropic:opus' is a valid provider:family value."""
        result = viz_config_data.write_config_key("test", "self_lineage", "anthropic:opus")
        self.assertTrue(result["ok"], result)

    def test_empty_lineage_accepted(self):
        """Empty string is valid (disables the feature)."""
        result = viz_config_data.write_config_key("test", "self_lineage", "")
        self.assertTrue(result["ok"], result)

    def test_invalid_lineage_rejected(self):
        """An invalid value must be rejected and the error must not mention 'HH:MM'."""
        result = viz_config_data.write_config_key("test", "self_lineage", "invalid!value")
        self.assertFalse(result["ok"], "Expected ok=False for 'invalid!value'")
        error = result.get("error", "")
        self.assertNotIn("HH:MM", error,
                         f"Error must not reference HH:MM for self_lineage; got: {error!r}")
        self.assertIn("provider", error.lower(),
                      f"Error should describe lineage format; got: {error!r}")


if __name__ == "__main__":
    unittest.main()
