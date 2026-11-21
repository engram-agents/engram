"""Tests for the write-time `low_similarity_hint` on anchoring_advisory
(engram-alpha#4050 part a). Advisory only: below quote-claim similarity 0.2
the advisory gains a `low_similarity_hint` key; at/above 0.2, or when the
similarity is unavailable (None), the key is absent.

The similarity is controlled by patching `engram_observation._quote_claim_similarity`,
the binding `_build_anchoring_advisory` actually calls.
"""

import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_low_sim_4050_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))
TEST_FILE = os.path.join(TEST_DIR, "low_sim_source.md")

EXPECTED_TEXT = (
    "quote-claim similarity {sim} is below 0.2: the quote may not carry this "
    "claim. Widen the quote, narrow the claim to what the quote says, or move "
    "the rest into a derivation with its own premises. Advisory only (#4050)."
)


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
    with open(TEST_FILE, "w") as f:
        f.write("low quote line one\nhigh quote line two\n")
    return server


def _payload(**overrides):
    payload = {
        "url": "https://example.com/low-sim-test",
        "title": "Low similarity test source",
        "quoted_text": "a short quote with no facts",
        "interpretation": "interp text",
        "claim": "claim text",
        "quote_type": "hard_data",
    }
    payload.update(overrides)
    return payload


def _write(server, monkeypatch, sim, reason=None, **overrides):
    obs_mod = sys.modules["engram_observation"]
    monkeypatch.setattr(obs_mod, "_quote_claim_similarity", lambda q, c: (sim, reason))
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(_payload(**overrides))))
    assert r["status"] == "created", r
    return r


def _low(adv):
    """#4847 (D4), changed on purpose: the low-similarity SENTENCE is gone from the reply;
    the threshold crossed is named in `advisory["flags"]` (what the flag means is said once,
    in the engram_add_observation tool description). A cell that asserts the sentence's
    ABSENCE would now pass vacuously, so every 'not fired' cell asserts the flag's absence."""
    return "low_similarity" in adv.get("flags", [])


def test_L1_below_threshold_key_present_exact_text(monkeypatch):
    server = fresh_server()
    adv = _write(server, monkeypatch, 0.19)["anchoring_advisory"]
    assert _low(adv) and "low_similarity_hint" not in adv
    assert adv["quote_claim_similarity"] == 0.19


def test_L2_exactly_threshold_key_absent(monkeypatch):
    server = fresh_server()
    adv = _write(server, monkeypatch, 0.2)["anchoring_advisory"]
    assert not _low(adv)


def test_L3_above_threshold_key_absent(monkeypatch):
    server = fresh_server()
    adv = _write(server, monkeypatch, 0.21)["anchoring_advisory"]
    assert not _low(adv)


def test_L4_none_similarity_never_gives_key(monkeypatch):
    server = fresh_server()
    adv = _write(server, monkeypatch, None, reason="embedder unavailable")["anchoring_advisory"]
    assert not _low(adv)
    assert adv["similarity_status"] == "unavailable: embedder unavailable"


def test_L5_non_hard_data_quote_type_still_gets_key(monkeypatch):
    server = fresh_server()
    adv = _write(server, monkeypatch, 0.05, quote_type="official_statement")["anchoring_advisory"]
    assert _low(adv) and "low_similarity_hint" not in adv
    assert adv["quote_claim_similarity"] == 0.05


def test_L6_independent_of_existing_hint(monkeypatch):
    server = fresh_server()
    kwargs = dict(claim="the record counted 31337 rows and 42000 events",
                  quoted_text="a short quote with no matching facts")
    both = _write(server, monkeypatch, 0.05, **kwargs)["anchoring_advisory"]
    server = fresh_server()
    plain = _write(server, monkeypatch, 0.5, **kwargs)["anchoring_advisory"]
    # #4847 (D4): the two sentences are flags now; the two flags stay independent
    assert "hint" not in both and "low_similarity_hint" not in both
    assert both["flags"] == ["hard_data_unanchored", "low_similarity"], both
    assert plain["flags"] == ["hard_data_unanchored"], plain
    assert not _low(plain)


def _log_rows(server):
    p = Path(server.core.DATA_DIR) / "anchoring_advisory.jsonl"
    return [json.loads(x) for x in p.read_text().strip().splitlines()]


@pytest.mark.parametrize("sim,reason,expected", [
    (0.05, None, True),
    (0.5, None, False),
    (None, "embedder unavailable", False),
])
def test_L7_log_row_low_similarity_flag(monkeypatch, sim, reason, expected):
    server = fresh_server()
    _write(server, monkeypatch, sim, reason=reason)
    rows = _log_rows(server)
    assert len(rows) == 1
    assert rows[0]["low_similarity"] is expected
    # existing fields kept
    assert "quote_claim_similarity" in rows[0] and "body_to_quote_ratio" in rows[0]


def test_L8_batch_only_low_item_has_key(monkeypatch):
    server = fresh_server()
    obs_mod = sys.modules["engram_observation"]
    monkeypatch.setattr(
        obs_mod, "_quote_claim_similarity",
        lambda q, c: (0.05, None) if "low" in q else (0.5, None))
    obs = [
        {"quoted_text": "low quote line one", "interpretation": "i1",
         "claim": "claim one", "quote_type": "attributed_analysis"},
        {"quoted_text": "high quote line two", "interpretation": "i2",
         "claim": "claim two", "quote_type": "attributed_analysis"},
    ]
    payload = {"observations_json": json.dumps(obs), "url": f"file://{TEST_FILE}",
               "title": "Low sim batch source"}
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert r.get("created") == 2, r
    advs = [x["anchoring_advisory"] for x in sorted(r["results"], key=lambda x: x["index"])]
    assert _low(advs[0])
    assert not _low(advs[1])


def test_L9_constant_pinned_and_text_derived_from_it(monkeypatch):
    server = fresh_server()
    from engram_anchoring import LOW_QUOTE_CLAIM_SIMILARITY
    assert LOW_QUOTE_CLAIM_SIMILARITY == 0.2
    # #4847 (D4): the sentence that derived its number from the constant is gone; the
    # flag is the constant's only reader in the reply. Pin both sides of it.
    assert _low(_write(server, monkeypatch, 0.01)["anchoring_advisory"])
    assert _low(_write(server, monkeypatch, LOW_QUOTE_CLAIM_SIMILARITY - 1e-6)["anchoring_advisory"])
    assert not _low(_write(server, monkeypatch, LOW_QUOTE_CLAIM_SIMILARITY)["anchoring_advisory"])


def test_L10_display_never_rounds_up_to_threshold(monkeypatch):
    # #4847 (D4): 0.1999 < 0.2 fires; the reply shows the raw number, never a rounded one
    # that could read "0.20 is below 0.2".
    server = fresh_server()
    adv = _write(server, monkeypatch, 0.1999)["anchoring_advisory"]
    assert _low(adv)
    assert adv["quote_claim_similarity"] == 0.1999


def test_L11a_nan_similarity_never_gives_key(monkeypatch):
    server = fresh_server()
    adv = _write(server, monkeypatch, float("nan"))["anchoring_advisory"]
    assert not _low(adv)
    rows = _log_rows(server)
    assert len(rows) == 1
    assert rows[0]["low_similarity"] is False


def test_L11b_raising_similarity_gives_unavailable_advisory(monkeypatch):
    server = fresh_server()
    obs_mod = sys.modules["engram_observation"]

    def boom(q, c):
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr(obs_mod, "_quote_claim_similarity", boom)
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(_payload())))
    assert r["status"] == "created", r
    adv = r["anchoring_advisory"]
    assert adv["status"] == "unavailable"
    assert "embedder exploded" in adv["reason"]
    assert not _low(adv)
