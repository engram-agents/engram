"""Tests for the write-time anchoring_advisory attached to every
engram_add_observation response (engram-alpha#2602 facet 2).

Coverage (per the build spec's test group D2-D4):
  - positive control: a hard_data write with unanchored claim tokens
    returns `hint` + those tokens in `unanchored`.
  - negative control: the same claim with a widened quote returns
    `unanchored == []` and NO `hint`.
  - a personal_communication write with unanchored tokens returns the
    tokens but no `hint` (hint is hard_data-only).
  - JSONL: one row per write, with the documented fields, under the
    test's own temp DATA_DIR (never ~/.engram).
  - failure path: a broken extractor still lets the write succeed, with
    the advisory reporting {"status": "unavailable", "reason": ...}.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_anchoring_advisory_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _payload(**overrides):
    payload = {
        "url": "https://example.com/anchoring-test",
        "title": "Anchoring test source",
        "quoted_text": "a short quote with no facts",
        "interpretation": "interp text",
        "claim": "claim text",
        "quote_type": "hard_data",
    }
    payload.update(overrides)
    return payload


# ── positive / negative controls ────────────────────────────────────────────


def test_hard_data_advisory_hint_present_when_unanchored():
    server = fresh_server()
    payload = _payload(
        claim="the record cites ob_9999 and counted 42000 events",
        quoted_text="a short quote with no matching facts",
    )
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r["status"] == "created", r
    adv = r["anchoring_advisory"]
    assert "ob_9999" in adv["unanchored"]
    assert "42000" in adv["unanchored"]
    assert "hint" in adv
    assert "hard_data" in adv["hint"]


def test_hard_data_advisory_no_hint_when_fully_anchored():
    server = fresh_server()
    payload = _payload(
        claim="the record cites ob_9999 and counted 42000 events",
        quoted_text="the record cites ob_9999 and counted 42000 events exactly",
    )
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r["status"] == "created", r
    adv = r["anchoring_advisory"]
    assert adv["unanchored"] == []
    assert "hint" not in adv


def test_non_hard_data_advisory_reports_but_never_hints():
    server = fresh_server()
    payload = _payload(
        quote_type="personal_communication",
        claim="the record cites ob_9999 and counted 42000 events",
        quoted_text="a short quote with no matching facts",
    )
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r["status"] == "created", r
    adv = r["anchoring_advisory"]
    # The unanchored tokens are still reported (advisory, not gated)...
    assert "ob_9999" in adv["unanchored"]
    # ...but the hint is hard_data-only.
    assert "hint" not in adv


# ── similarity column, embeddings disabled in test fixtures ────────────────


def test_similarity_null_with_reason_when_embeddings_disabled():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(_payload())))
    adv = r["anchoring_advisory"]
    assert adv["quote_claim_similarity"] is None
    assert "similarity_status" in adv
    assert adv["similarity_status"].startswith("unavailable:")


# ── JSONL log ────────────────────────────────────────────────────────────


def test_jsonl_one_row_per_write_with_documented_fields():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(_payload(
        claim="claim with ob_1111",
    ))))
    node_id = r["observation_id"]

    log_path = Path(server.core.DATA_DIR) / "anchoring_advisory.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])

    for key in (
        "ts", "node_id", "quote_type", "source_class", "confidence",
        "hard_tokens", "anchored", "ratio", "unanchored", "quote_len",
        "claim_len", "interp_len", "body_to_quote_ratio",
        "quote_claim_similarity",
    ):
        assert key in entry, f"missing {key!r} in {entry}"
    assert entry["node_id"] == node_id
    assert entry["quote_type"] == "hard_data"


def test_jsonl_appends_one_line_per_additional_write():
    server = fresh_server()
    for i in range(3):
        server.engram_add_observation(payload_json=json.dumps(_payload(
            claim=f"claim number {i}",
        )))
    log_path = Path(server.core.DATA_DIR) / "anchoring_advisory.jsonl"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 3


# ── failure path: loud, never fatal ─────────────────────────────────────────


def test_advisory_failure_path_write_still_succeeds(monkeypatch):
    server = fresh_server()
    obs_mod = sys.modules["engram_observation"]

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom: extractor exploded")

    monkeypatch.setattr(obs_mod, "_anchor_claim", _raise)

    r = json.loads(server.engram_add_observation(payload_json=json.dumps(_payload())))
    assert r["status"] == "created", r
    adv = r["anchoring_advisory"]
    assert adv == {"status": "unavailable", "reason": "boom: extractor exploded"}
