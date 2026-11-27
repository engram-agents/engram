"""Contract cells for the tiered dedup block (#4424).

engram_add_observation (`similar_existing`) used to return every match as a
full JSON entry. It now renders by severity tier (engram_add_lesson keeps its
block and reply keys exactly as before: cell f), lead with one line saying what needs
action, and COUNT what they do not show:

  tier A  duplicate, polarity_alert                       full entry
  tier B  corroborate, keyword_overlap_same_source,
          distinct_from_same_source                       id, severity, recall_summary,
                                                          confidence, action_hint
  tier C  related, keyword_overlap_other_source           id, severity, keywords (<=5);
                                                          at most 5 entries shown
  tier D  weak_match                                      counted only

Everything goes through the real tools. The only stubs are the semantic
backend (to control similarity) and the NLI classifier (polarity), the same
doubles test_action_hints.py / test_polarity_nli.py use. A spy on
core._render_similar_block records what the callers hand the renderer, so the
per-match severity and the builder's per-category totals can be read.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_dedup_tiered_4424_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

# Length of json.dumps(...) (default separators) of the `similar_existing` list
# ONLY, as dev (02a158e8) returned it on the seeded 17-match graph that
# _seed_mixed() builds. Measured by running this file's seeding against the
# untouched base, not estimated. Cell (g) compares it against the head's
# lead + list + not_shown together (the stricter comparison).
DEV_BLOCK_CHARS = 5015

TIER_A = {"duplicate", "polarity_alert"}
TIER_B = {"corroborate", "keyword_overlap_same_source", "distinct_from_same_source"}
TIER_C = {"related", "keyword_overlap_other_source"}
TIER_D = {"weak_match"}

LONG_CLAIM = (
    "The harbour survey of the northern pier recorded forty seven distinct "
    "mooring points and the surveyors noted that every one of them showed "
    "corrosion consistent with a decade of exposure to brackish water."
)
assert len(LONG_CLAIM) > 140


class _Spy:
    """Records every entries list the callers hand to the renderer."""

    def __init__(self):
        self.calls = []


def fresh_server(polarity_nli=None):
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    import server
    import engram_core
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()

    server._hits = []

    class _StubEmb:
        def __init__(self_):
            self_._revive_cooldown = {}

        def is_available(self_):
            return True

        def revive(self_, model_name):
            pass

    server._embedder = _StubEmb()
    engram_core._embedder = server._embedder
    engram_core._get_embedding_config = lambda: {"enabled": True}

    def stub(conn, query_text, **kwargs):
        return list(server._hits)

    engram_core._semantic_search = stub

    if polarity_nli is not None:
        os.environ.pop("ENGRAM_NO_POLARITY", None)
        cfg_path = engram_core.CONFIG_PATH
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg["polarity"] = {
            "enabled": True,
            "model": "dleemiller/ModernCE-large-nli",
            "threshold": 0.46,
            "min_similarity_for_check": 0.30,
        }
        cfg_path.write_text(json.dumps(cfg))

        class _StubNLI:
            _loaded = True

            def available(self_):
                return True

            def score(self_, prem, hyp, model_name):
                return polarity_nli

            def score_batch(self_, pairs, model_name):
                return [polarity_nli] * len(pairs)

        server._nli_classifier = _StubNLI()
        engram_core._nli_classifier = server._nli_classifier
    return server


def install_spy(server):
    """Wrap core._render_similar_block; keep real behaviour, record inputs."""
    spy = _Spy()
    real = server.core._render_similar_block

    def wrapped(entries, *a, **kw):
        spy.calls.append([dict(e) for e in entries])
        return real(entries, *a, **kw)

    server.core._render_similar_block = wrapped
    return spy


def add_obs(server, claim, url, recall_summary="", recall_keywords=None):
    payload = {
        "url": url,
        "title": "T " + url,
        "quoted_text": "body of " + claim[:30],
        "interpretation": "interp",
        "claim": claim,
        "quote_type": "attributed_analysis",
    }
    if recall_summary:
        payload["recall_summary"] = recall_summary
    if recall_keywords:
        payload["recall_keywords"] = recall_keywords
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    return r


def hit(server, node_id, sim):
    import sqlite3
    c = sqlite3.connect(str(server.core.DB_PATH))
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT claim, confidence FROM nodes WHERE id=?", (node_id,)).fetchone()
    c.close()
    return {
        "id": node_id, "type": "observation_factual", "claim": row["claim"],
        "confidence": row["confidence"], "similarity": sim,
        "quote_type": "attributed_analysis", "importance_score": 1.0,
    }


SAME = "https://example.com/same-source"
NEW_CLAIM = "A wholly unrelated sentence about quartz."


def _seed_mixed(server, new_url=SAME):
    """17 matches: 1 duplicate, 1 distinct_from_same_source, 3 corroborate,
    7 related, 5 weak_match. Returns {id: expected_severity}, hits set."""
    server._hits = []
    expected = {}
    sims = {}
    # same source as the new observation (document source -> sim>=0.85 DUPLICATE)
    r = add_obs(server, "Same-source claim one about alpha topic.", SAME,
                recall_summary="same source one", recall_keywords=["a", "b"])
    expected[r["observation_id"]] = "duplicate"; sims[r["observation_id"]] = 0.88
    r = add_obs(server, "Same-source claim two about beta topic.", SAME,
                recall_summary="same source two", recall_keywords=["a", "b"])
    expected[r["observation_id"]] = "distinct_from_same_source"; sims[r["observation_id"]] = 0.60
    # independent sources
    for i, s in enumerate([0.80, 0.72, 0.66]):
        claim = LONG_CLAIM if i == 0 else f"Corroborating claim number {i} about gamma topic."
        rs = "" if i == 0 else f"corroborating summary {i}"
        r = add_obs(server, claim, f"https://example.com/corr-{i}",
                    recall_summary=rs, recall_keywords=["k1", "k2"])
        expected[r["observation_id"]] = "corroborate"; sims[r["observation_id"]] = s
    for i, s in enumerate([0.64, 0.62, 0.60, 0.58, 0.56, 0.54, 0.52]):
        r = add_obs(server, f"Related claim {i} about delta topic.",
                    f"https://example.com/rel-{i}",
                    recall_summary=f"related summary {i}",
                    recall_keywords=["w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8"])
        expected[r["observation_id"]] = "related"; sims[r["observation_id"]] = s
    for i, s in enumerate([0.49, 0.47, 0.45, 0.43, 0.41]):
        r = add_obs(server, f"Weak claim {i} about epsilon topic.",
                    f"https://example.com/weak-{i}",
                    recall_summary=f"weak summary {i}", recall_keywords=["x"])
        expected[r["observation_id"]] = "weak_match"; sims[r["observation_id"]] = s
    server._hits = [hit(server, nid, s) for nid, s in
                    sorted(sims.items(), key=lambda kv: -kv[1])]
    return expected


def write_mixed(server):
    expected = _seed_mixed(server)
    spy = install_spy(server)
    r = add_obs(server, NEW_CLAIM, SAME)
    return r, expected, spy


def _category_from_hint(hint):
    word = hint.split(" ")[0]
    if word.startswith("POSSIBLE_DUPLICATE"):
        return "duplicate"
    return word.lower()


def _shown(r):
    return r.get("similar_existing", [])


# ── (a) severity golden table ──────────────────────────────────────────────


def test_a_severity_equals_hint_category_per_match_semantic_mix():
    server = fresh_server()
    r, expected, spy = write_mixed(server)
    assert len(spy.calls) == 1
    entries = spy.calls[0]
    assert len(entries) == 17
    for m in entries:
        assert m["severity"] == expected[m["id"]], m
        assert m["severity"] == _category_from_hint(m["action_hint"]), m
    assert set(m["severity"] for m in entries) == {
        "duplicate", "distinct_from_same_source", "corroborate", "related", "weak_match"}


def test_a_severity_keyword_branches():
    server = fresh_server()
    server._hits = []
    same = add_obs(server, "quokkaword same source note.", SAME)["observation_id"]
    other = add_obs(server, "quokkaword other source note.",
                    "https://example.com/other")["observation_id"]
    spy = install_spy(server)
    add_obs(server, "quokkaword", SAME)
    entries = {m["id"]: m for m in spy.calls[0]}
    assert entries[same]["severity"] == "keyword_overlap_same_source"
    assert entries[other]["severity"] == "keyword_overlap_other_source"
    for m in entries.values():
        assert m["severity"] == _category_from_hint(m["action_hint"])


def test_a_severity_possible_duplicate_is_duplicate():
    server = fresh_server()
    server._hits = []
    url = "https://example.com/convo"
    first = add_obs(server, "Convo claim about marmots.", url)["observation_id"]
    conn_url_type = __import__("sqlite3").connect(str(server.core.DB_PATH))
    conn_url_type.execute(
        "UPDATE nodes SET source_type='conversation' WHERE id IN "
        "(SELECT evidence_id FROM nodes WHERE id=?)", (first,))
    conn_url_type.commit(); conn_url_type.close()
    server._hits = [hit(server, first, 0.95)]
    spy = install_spy(server)
    add_obs(server, "Convo claim about marmots again.", url)
    m = spy.calls[0][0]
    assert m["action_hint"].startswith("POSSIBLE_DUPLICATE")
    assert m["severity"] == "duplicate"


def test_a_severity_polarity_alert_prefix():
    server = fresh_server(polarity_nli=0.60)
    server._hits = []
    first = add_obs(server, "Topic Z is X.", "https://example.com/pa")["observation_id"]
    server._hits = [hit(server, first, 0.70)]
    spy = install_spy(server)
    r = add_obs(server, "Topic Z is NOT X.", "https://example.com/pb")
    m = spy.calls[0][0]
    assert m["action_hint"].startswith("POLARITY_ALERT")
    assert m["severity"] == "polarity_alert"
    shown = _shown(r)
    assert shown[0]["severity"] == "polarity_alert"
    assert "claim" in shown[0]  # tier A: full entry
    assert "polarity alert" in r["similar_existing_lead"]
    assert first in r["similar_existing_lead"]


# ── (b) tier contents ──────────────────────────────────────────────────────


def test_b_tier_a_keeps_full_claim():
    server = fresh_server()
    r, expected, _ = write_mixed(server)
    dup = [e for e in _shown(r) if e["severity"] == "duplicate"]
    assert len(dup) == 1
    e = dup[0]
    assert e["claim"] == "Same-source claim one about alpha topic."
    for k in ("id", "confidence", "action_hint", "evidence_id", "same_evidence"):
        assert k in e, (k, e)
    assert "match_type" not in e and "similarity" not in e
    assert "recall_summary" not in e and "recall_keywords" not in e


def test_b_tier_b_uses_recall_summary_never_full_claim():
    server = fresh_server()
    r, expected, _ = write_mixed(server)
    corr = [e for e in _shown(r) if e["severity"] == "corroborate"]
    assert len(corr) == 3
    for e in corr:
        assert set(e) == {"id", "severity", "recall_summary", "confidence", "action_hint"}, e
    by_id = {e["id"]: e for e in corr}
    summaries = sorted(e["recall_summary"] for e in corr)
    assert "corroborating summary 1" in summaries and "corroborating summary 2" in summaries
    assert LONG_CLAIM not in json.dumps(_shown(r))
    distinct = [e for e in _shown(r) if e["severity"] == "distinct_from_same_source"]
    assert len(distinct) == 1 and distinct[0]["recall_summary"] == "same source two"
    assert "claim" not in distinct[0]


def test_b_tier_b_fallback_is_140_char_cut_of_claim():
    server = fresh_server()
    r, expected, _ = write_mixed(server)
    corr = [e for e in _shown(r) if e["severity"] == "corroborate"]
    fallback = [e for e in corr if not e["recall_summary"].startswith("corroborating summary")]
    assert len(fallback) == 1
    rs = fallback[0]["recall_summary"]
    assert len(rs) <= 140
    assert rs.endswith("…")
    assert LONG_CLAIM.startswith(rs[:-1])
    assert rs != LONG_CLAIM


def test_b_tier_c_keywords_at_most_5_and_entries_at_most_5():
    server = fresh_server()
    r, expected, _ = write_mixed(server)
    c = [e for e in _shown(r) if e["severity"] in TIER_C]
    assert len(c) == 5  # 7 related exist, 5 shown
    for e in c:
        assert set(e) == {"id", "severity", "keywords"}, e
        assert e["keywords"] == ["w1", "w2", "w3", "w4", "w5"]


def test_b_tier_d_absent_from_block():
    server = fresh_server()
    r, expected, _ = write_mixed(server)
    weak_ids = {i for i, s in expected.items() if s == "weak_match"}
    assert len(weak_ids) == 5
    assert not weak_ids & {e["id"] for e in _shown(r)}
    assert not any(e["severity"] in TIER_D for e in _shown(r))


def test_b_tier_d_absent_even_when_tier_c_has_room():
    """With only 2 related matches the tier-C cap leaves room, so a weak match
    wrongly rendered as tier C would show up in the block."""
    server = fresh_server()
    server._hits = []
    rel = [add_obs(server, f"Room related {i}.", f"https://example.com/rr{i}")["observation_id"]
           for i in range(2)]
    weak = [add_obs(server, f"Room weak {i}.", f"https://example.com/rw{i}")["observation_id"]
            for i in range(2)]
    server._hits = ([hit(server, n, 0.55) for n in rel] + [hit(server, n, 0.45) for n in weak])
    r = add_obs(server, NEW_CLAIM, "https://example.com/room")
    assert [e["id"] for e in _shown(r)] == rel
    assert r["similar_not_shown"]["weak_match"] == 2
    assert r["similar_count"] == 4


def test_b_tier_c_keywords_drop_null_and_empty():
    """A recall_keywords list holding null or "" must not render "None" or empty
    strings; they are filtered before the 5-keyword cap."""
    import sqlite3
    server = fresh_server()
    server._hits = []
    nid = add_obs(server, "Keyword hygiene claim.", "https://example.com/kw")["observation_id"]
    c = sqlite3.connect(str(server.core.DB_PATH))
    c.execute("UPDATE nodes SET recall_keywords = ? WHERE id = ?",
              (json.dumps(["a", None, "", "b"]), nid))
    c.commit()
    c.close()
    server._hits = [hit(server, nid, 0.55)]
    r = add_obs(server, NEW_CLAIM, "https://example.com/kw2")
    assert _shown(r) == [{"id": nid, "severity": "related", "keywords": ["a", "b"]}]


# ── (c) leading line ───────────────────────────────────────────────────────


def test_c_only_c_and_d_says_nothing_needs_action():
    server = fresh_server()
    server._hits = []
    rel = add_obs(server, "Related only claim.", "https://example.com/r1")["observation_id"]
    weak = add_obs(server, "Weak only claim.", "https://example.com/w1")["observation_id"]
    server._hits = [hit(server, rel, 0.55), hit(server, weak, 0.45)]
    r = add_obs(server, NEW_CLAIM, "https://example.com/n1")
    assert r["similar_existing_lead"] == "nothing needs action"


def test_c_same_source_and_distinct_do_not_need_action():
    server = fresh_server()
    server._hits = []
    a = add_obs(server, "Same source thing.", SAME)["observation_id"]
    server._hits = [hit(server, a, 0.60)]
    r = add_obs(server, NEW_CLAIM, SAME)
    assert _shown(r)[0]["severity"] == "distinct_from_same_source"
    assert r["similar_existing_lead"] == "nothing needs action"


def test_c_duplicate_polarity_corroborate_each_named():
    server = fresh_server()
    r, expected, _ = write_mixed(server)
    lead = r["similar_existing_lead"]
    dup_id = [i for i, s in expected.items() if s == "duplicate"][0]
    assert "1 duplicate candidate" in lead and dup_id in lead
    assert "corroboration candidates" in lead
    for i, s in expected.items():
        if s == "corroborate":
            assert i in lead
    # polarity named in its own test above; here: never weak
    for i, s in expected.items():
        if s == "weak_match":
            assert i not in lead


def test_c_lead_never_says_weak_match_and_caps_ids_at_3():
    server = fresh_server()
    server._hits = []
    ids = []
    for i in range(5):
        ids.append(add_obs(server, f"Corr claim {i}.",
                           f"https://example.com/c{i}")["observation_id"])
    server._hits = [hit(server, n, 0.80 - i * 0.01) for i, n in enumerate(ids)]
    r = add_obs(server, NEW_CLAIM, "https://example.com/new")
    lead = r["similar_existing_lead"]
    assert "WEAK_MATCH" not in lead and "weak" not in lead.lower()
    assert lead.startswith("5 corroboration candidates: ")
    assert sum(1 for n in ids if n in lead) == 3
    assert lead.endswith("and 2 more")
    r2, expected, _ = write_mixed(fresh_server())
    assert "WEAK_MATCH" not in json.dumps(r2["similar_existing_lead"])
    assert "WEAK_MATCH" not in r2["similar_not_shown"]["see"]


# ── (d) counted, not dropped ───────────────────────────────────────────────


def test_d_per_category_shown_plus_counted_equals_returned():
    server = fresh_server()
    r, expected, spy = write_mixed(server)
    returned = Counter(m["severity"] for m in spy.calls[0])
    shown = Counter(e["severity"] for e in _shown(r))
    ns = r["similar_not_shown"]
    for cat, n in returned.items():
        assert shown.get(cat, 0) + ns.get(cat, 0) == n, (cat, shown, ns, returned)
    assert ns["related"] == 2 and ns["weak_match"] == 5
    assert r["similar_count"] == 17
    assert returned["weak_match"] == 5 and returned["related"] == 7


def test_d_see_names_engram_query_and_human_line():
    server = fresh_server()
    r, _, _ = write_mixed(server)
    ns = r["similar_not_shown"]
    assert "engram_query" in ns["see"]
    assert "engram_query" in ns["summary"]
    assert "2 more related" in ns["summary"] and "5 weak matches" in ns["summary"]
    assert not any(v == 0 for k, v in ns.items() if isinstance(v, int))


def test_d_nothing_hidden_means_no_not_shown_key():
    server = fresh_server()
    server._hits = []
    a = add_obs(server, "Lonely claim.", "https://example.com/x")["observation_id"]
    server._hits = [hit(server, a, 0.80)]
    r = add_obs(server, NEW_CLAIM, "https://example.com/y")
    assert "similar_not_shown" not in r
    assert r["similar_count"] == 1


# ── (e) key order ──────────────────────────────────────────────────────────


def test_e_key_order_observation():
    server = fresh_server()
    r, _, _ = write_mixed(server)
    keys = list(r)
    assert keys.index("similar_existing_lead") + 1 == keys.index("similar_existing")
    assert keys.index("similar_existing") < keys.index("similar_not_shown")


# ── (f) both call sites share one shape ────────────────────────────────────


def _mint_lessons(n):
    server = fresh_server()
    inc = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "I shipped without reading the error.",
        "interpretation": "Missed cue.",
        "claim": "Skipped reading error output before hypothesizing.",
        "quote_type": "personal_communication",
        "url": "https://example.com/incident", "title": "inc"})))["observation_id"]
    spy = install_spy(server)
    replies = []
    for i in range(n):
        r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
            "claim": "Zorblat rule: always read stderr before guessing.",
            "incident_ids": inc,
            "scaffolding_nudge": "Stop. Read the error output first.",
            "logical_chain": "single incident",
        })))
        assert "error" not in r, r
        replies.append(r)
    return server, spy, replies


# Measured: the lesson reply's keys, in order, from the lesson site as on dev
# (src/engram/engram_epistemic.py identical to origin/dev at 02a158e8).
LESSON_REPLY_KEYS_DEV = [
    "status", "lesson_id", "claim", "reasoning_type", "reasoning_class",
    "scaffolding_nudge", "prevention_type", "tripwire_priority",
    "incident_nodes", "context_nodes", "importance_anchored",
    "similar_existing_lessons", "consolidation_hint", "embedding_failure",
]


def test_f_lesson_reply_is_byte_identical_to_dev():
    """The lesson site keeps today's block and today's reply keys exactly:
    no severity, no lead, no not_shown, no renderer call (lessons were not
    measured and the block is tiny)."""
    server, spy, replies = _mint_lessons(4)
    last = replies[-1]
    assert spy.calls == [], "the lesson site must not call the renderer"
    assert "similar_existing_lead" not in last and "similar_not_shown" not in last
    assert list(last) == LESSON_REPLY_KEYS_DEV, list(last)
    block = last["similar_existing_lessons"]
    assert len(block) == 3
    for e in block:
        # today's entry: builder record minus match_type/similarity
        assert list(e) == ["id", "claim", "confidence", "scaffolding_nudge"], e
        assert e["claim"] == "Zorblat rule: always read stderr before guessing."
        assert e["scaffolding_nudge"] == "Stop. Read the error output first."
    import sqlite3
    c = sqlite3.connect(str(server.core.DB_PATH))
    c.row_factory = sqlite3.Row
    expected = server.core._strip_similar_block(server.core._similar_existing_matches(
        c, "Zorblat rule: always read stderr before guessing.",
        type_filter={"lesson"}, extra_metadata_keys=("scaffolding_nudge",)))
    c.close()
    # the builder now also sees the 4th lesson itself; the first 3 are the block
    assert json.dumps(block) == json.dumps([x for x in expected if x["id"] != last["lesson_id"]])


def test_f2_entry_without_severity_renders_as_tier_a():
    server = fresh_server()
    lead, shown, not_shown = server.core._render_similar_block([
        {"id": "ob_1", "claim": "No severity here.", "confidence": 0.8,
         "match_type": "keyword", "recall_summary": "rs", "recall_keywords": "[\"k\"]"},
    ])
    assert shown == [{"id": "ob_1", "claim": "No severity here.", "confidence": 0.8}]
    assert not_shown == {}


# ── (g) the reply is smaller ───────────────────────────────────────────────


def test_g_block_at_most_half_of_dev():
    server = fresh_server()
    r, _, _ = write_mixed(server)
    block = {"lead": r["similar_existing_lead"], "block": r["similar_existing"],
             "not_shown": r["similar_not_shown"]}
    size = len(json.dumps(block))
    assert size * 2 <= DEV_BLOCK_CHARS, (size, DEV_BLOCK_CHARS)


# ── (h) no setting ─────────────────────────────────────────────────────────


def test_h_renderer_reads_no_config_or_env():
    import inspect
    server = fresh_server()
    src = inspect.getsource(server.core._render_similar_block)
    for needle in ("environ", "getenv", "CONFIG", "_get_thresholds_config", "config"):
        assert needle not in src, needle
