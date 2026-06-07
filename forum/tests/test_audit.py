"""Tests for forum/audit.py — JSONL audit writer, mutations-only enforcement."""

import hashlib
import json
import os
import tempfile
import threading

import pytest

from forum.audit import write_audit


@pytest.fixture
def audit_file(tmp_path):
    return str(tmp_path / "test-audit.jsonl")


class TestValidActions:
    def test_post_writes_single_line(self, audit_file):
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "hello world", path=audit_file)
        lines = open(audit_file).readlines()
        assert len(lines) == 1

    def test_reply_writes_single_line(self, audit_file):
        write_audit("reply", "agent-b", "post", 5, "127.0.0.1", "reply body", path=audit_file)
        lines = open(audit_file).readlines()
        assert len(lines) == 1

    def test_edit_writes_single_line(self, audit_file):
        write_audit("edit", "agent-a", "post", 2, "10.0.0.1", "edited body", path=audit_file)
        lines = open(audit_file).readlines()
        assert len(lines) == 1

    def test_patch_agent_writes_single_line(self, audit_file):
        write_audit("patch_agent", "agent-b", "agent", 3, "192.168.1.5", None, path=audit_file)
        lines = open(audit_file).readlines()
        assert len(lines) == 1


class TestInvalidAction:
    def test_poll_raises_value_error(self, audit_file):
        with pytest.raises(ValueError, match="invalid action"):
            write_audit("poll", "agent-a", "thread", 0, "127.0.0.1", None, path=audit_file)

    def test_get_raises_value_error(self, audit_file):
        with pytest.raises(ValueError):
            write_audit("get", "agent-a", "thread", 0, "127.0.0.1", None, path=audit_file)

    def test_empty_action_raises_value_error(self, audit_file):
        with pytest.raises(ValueError):
            write_audit("", "agent-a", "thread", 0, "127.0.0.1", None, path=audit_file)


class TestBodyHash:
    def test_body_hash_matches_sha256(self, audit_file):
        body = "hello world, this is a test post"
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", body, path=audit_file)
        record = json.loads(open(audit_file).read().strip())
        expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert record["body_hash"] == expected_hash

    def test_body_hash_is_none_for_null_body(self, audit_file):
        write_audit("patch_agent", "agent-b", "agent", 3, "127.0.0.1", None, path=audit_file)
        record = json.loads(open(audit_file).read().strip())
        assert record["body_hash"] is None

    def test_body_hash_unicode_body(self, audit_file):
        body = "ENGRAM ref OB 0124 — testing unicode: 你好世界"
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", body, path=audit_file)
        record = json.loads(open(audit_file).read().strip())
        expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert record["body_hash"] == expected_hash


class TestFileHandling:
    def test_file_created_if_missing(self, tmp_path):
        path = str(tmp_path / "new-dir" / "audit.jsonl")
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "body", path=path)
        assert os.path.exists(path)

    def test_file_appended_not_truncated(self, audit_file):
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "body1", path=audit_file)
        write_audit("reply", "agent-b", "post", 2, "127.0.0.1", "body2", path=audit_file)
        lines = open(audit_file).readlines()
        assert len(lines) == 2

    def test_1000_writes_1000_lines(self, audit_file):
        for i in range(1000):
            write_audit("post", "agent", "thread", i, "127.0.0.1", f"body {i}", path=audit_file)
        lines = open(audit_file).readlines()
        assert len(lines) == 1000


class TestRecordFormat:
    def test_each_line_is_valid_json(self, audit_file):
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "body", path=audit_file)
        write_audit("reply", "agent-b", "post", 2, "127.0.0.1", "reply", path=audit_file)
        for line in open(audit_file):
            json.loads(line)  # raises if invalid

    def test_record_has_required_fields(self, audit_file):
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "body", path=audit_file)
        record = json.loads(open(audit_file).read().strip())
        for field in ["ts", "agent_name", "action", "resource_kind", "resource_id",
                      "source_ip", "body_hash"]:
            assert field in record, f"Missing field: {field}"

    def test_field_order_matches_spec(self, audit_file):
        """Fields appear in the specified order: ts, agent_name, action, ..."""
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "body", path=audit_file)
        line = open(audit_file).read().strip()
        record = json.loads(line)
        keys = list(record.keys())
        expected_order = ["ts", "agent_name", "action", "resource_kind",
                          "resource_id", "source_ip", "body_hash"]
        assert keys == expected_order, f"Field order mismatch: {keys}"

    def test_ts_is_utc_iso_with_z_suffix(self, audit_file):
        write_audit("post", "agent-a", "thread", 1, "127.0.0.1", "body", path=audit_file)
        record = json.loads(open(audit_file).read().strip())
        assert record["ts"].endswith("Z"), f"ts missing Z suffix: {record['ts']}"
        # Should parse as ISO
        from datetime import datetime
        datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))

    def test_agent_name_field(self, audit_file):
        write_audit("post", "agent-a-test", "thread", 1, "127.0.0.1", "body", path=audit_file)
        record = json.loads(open(audit_file).read().strip())
        assert record["agent_name"] == "agent-a-test"

    def test_source_ip_field(self, audit_file):
        write_audit("post", "agent-a", "thread", 1, "192.168.1.42", "body", path=audit_file)
        record = json.loads(open(audit_file).read().strip())
        assert record["source_ip"] == "192.168.1.42"
