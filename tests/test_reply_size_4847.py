"""Contract cells for the shorter engram_add_observation reply, Option A (#4847).

Written BEFORE the renderer change and red on dev (80366e84) for exactly the
cells that name the change; the cells that say "as today" are green on dev and
guard what must not move. The decisions each cell encodes are Kepler's answers
D1-D5 on the issue and the contract v2 comment (issuecomment-7067501235).

  D1  tier A (duplicate, polarity_alert) keeps its action_hint prose and full claim
  D2  the lead names the action, once, for the three severities that have a lead
      phrase and are present in this reply; same-source and distinct keep
      "nothing needs action" (pinned by test_dedup_tiered_render_4424.py cell c);
      a tier-B match's similarity figure lived only in the action_hint prose and
      goes with it, on purpose (the severity word already encodes the band)
  D3  tier C stays id, severity, keywords (5) until the size cell says otherwise
  D4  a key that says THIS write went wrong or disagrees with what is stored
      survives in full; a key that is the same sentence on every write is
      shortened: anchoring_advisory keeps its numbers and gains `flags`, and the
      sentences `hint` and `low_similarity_hint` go
  D5  `dedup_guidance` leaves the reply and lives once, in the tool description

Everything goes through the real tools, with the same two doubles as
test_dedup_tiered_render_4424.py (the semantic backend and the NLI classifier),
whose helpers this file loads by path.
"""

import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "t4424_helpers", os.path.join(_HERE, "test_dedup_tiered_render_4424.py"))
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

TIER_A, TIER_B, TIER_C = T.TIER_A, T.TIER_B, T.TIER_C

# The tier-B entry as it must render after the change. D2.
TIER_B_KEYS = {"id", "severity", "recall_summary", "confidence"}
TIER_C_KEYS = {"id", "severity", "keywords"}

# The whole-reply size bound of cell (m), in percent of today's, and the
# prediction it was set from. BOTH were written down before the first
# measurement of the new reply: ~/luria-desk/4847-margin-logged-before-first-look.md
# (sha256 952db67f..., 18:46Z on 2026-11-21): predicted ratio 0.70, range 0.65 to
# 0.80, bound 80. A number above the bound keeps cell (m) red and moves tier C to
# counted-only (Kepler's D3 view); a number below 65 sends me to look for
# something dropped that must stay.
SIZE_BOUND_PERCENT = 80

# Whole reply of today's code (dev 80366e84) on the seeded 17-match graph that
# T.write_mixed builds, len(json.dumps(reply)): 3937, the same on 3 runs, measured
# at 18:49Z (`date -u`) on 2026-11-21 AFTER the margin above was logged (18:46Z). Bound 80% = 3149.6 chars.
DEV_WHOLE_REPLY_CHARS = 3937

_FIGURE = re.compile(r"similarity|\(\d\.\d\d\)")


def _reply():
    server = T.fresh_server()
    r, expected, spy = T.write_mixed(server)
    return server, r, expected


def _tier(entries, tier):
    return [e for e in entries if e.get("severity") in tier]


# ── (a) tier B: id, severity, scent, confidence; no action prose, no figure ──
def test_a_tier_b_entry_has_exactly_id_severity_scent_confidence():
    _, r, _ = _reply()
    b = _tier(T._shown(r), TIER_B)
    assert b, "the seeded graph has tier-B matches"
    for e in b:
        assert set(e) == TIER_B_KEYS, e


def test_a_tier_b_entry_says_no_action_and_no_similarity_figure():
    _, r, _ = _reply()
    for e in _tier(T._shown(r), TIER_B):
        text = json.dumps(e)
        assert "action_hint" not in e
        assert not _FIGURE.search(text), (
            "the similarity figure of a tier-B match lived only in the action_hint "
            "prose; it goes with it on purpose (D2): " + text)


# ── (b) the cells that say 'as today' (green on dev; they guard the rest) ────
def test_b_tier_a_entry_keeps_action_hint_prose_and_full_claim():
    _, r, expected = _reply()
    a = _tier(T._shown(r), TIER_A)
    assert a, "the seeded graph has a duplicate"
    for e in a:
        assert e.get("action_hint"), "D1: tier A keeps the prose"
        assert e.get("claim"), "D1: tier A keeps the full claim"


def test_b_tier_c_entry_is_id_severity_keywords_as_today():
    _, r, _ = _reply()
    c = _tier(T._shown(r), TIER_C)
    assert c
    for e in c:
        assert set(e) == TIER_C_KEYS, e
        assert len(e["keywords"]) <= 5


# ── (c) the lead names the action, once, for severities present (D2) ────────
def test_c_lead_names_the_action_for_each_present_lead_severity():
    _, r, _ = _reply()
    lead = r["similar_existing_lead"]
    assert "duplicate candidate" in lead and "corroboration candidate" in lead
    # the action, said in the lead (it is no longer said in a tier-B entry)
    assert "inductive_generalization" in lead, lead
    assert "retract" in lead, lead


def test_c_lead_names_the_action_for_a_polarity_alert():
    # Found by the reviewer-fairy round: emptying the polarity action left every cell green.
    server = T.fresh_server(polarity_nli=0.60)
    server._hits = []
    first = T.add_obs(server, "Topic Z is X.", "https://example.com/pa")["observation_id"]
    server._hits = [T.hit(server, first, 0.70)]
    r = T.add_obs(server, "Topic Z is NOT X.", "https://example.com/pb")
    lead = r["similar_existing_lead"]
    assert "polarity alert" in lead and first in lead
    assert "contradict" in lead, lead


def _only(server, severity_ids, claim, url):
    """Reply to a fresh write whose similar block holds only the given ids."""
    keep = [h for h in server._hits if h["id"] in severity_ids]
    server._hits = keep
    return T.add_obs(server, claim, url)


def test_c_lead_does_not_name_an_action_for_a_severity_that_is_absent():
    server = T.fresh_server()
    expected = T._seed_mixed(server)
    dup = [i for i, s in expected.items() if s == "duplicate"]
    r = _only(server, set(dup), "A claim about quartz again.", T.SAME)
    lead = r["similar_existing_lead"]
    assert "duplicate candidate" in lead
    assert "inductive_generalization" not in lead, lead


def test_c_same_source_and_distinct_only_still_says_nothing_needs_action():
    server = T.fresh_server()
    expected = T._seed_mixed(server)
    ids = {i for i, s in expected.items() if s == "distinct_from_same_source"}
    r = _only(server, ids, "A claim about quartz once more.", T.SAME)
    assert r["similar_existing_lead"] == "nothing needs action"
    for e in T._shown(r):
        assert "action_hint" not in e, "no action text anywhere for these two (D2)"


# ── (d) dedup_guidance: gone from the reply, once in the tool description ────
def test_d_dedup_guidance_is_absent_from_the_reply():
    _, r, _ = _reply()
    assert "dedup_guidance" not in r


def _reading_paragraph(doc):
    start = doc.find("Reading the reply.")
    end = doc.find("Worked examples", start)
    assert start != -1 and end != -1, "the description has a 'Reading the reply.' paragraph"
    return doc[start:end]


def test_d_tool_description_carries_the_guidance_once_and_stands_alone():
    server = T.fresh_server()
    doc = server.engram_add_observation.__doc__ or ""
    assert doc.count("similar_existing") >= 1
    assert len(re.findall(r"similarity-graded", doc)) == 1, doc
    # the whole paragraph, read away from a reply: no word that points at one
    para = _reading_paragraph(doc)
    for deictic in ("this reply", "the reply above", "above", "below"):
        assert deictic not in para, (deictic, para)


def test_d_tool_description_states_the_corrected_content():
    # Found by the reviewer-fairy round: a docstring keeping only the word "similarity-graded"
    # passed. The paragraph must carry what D5 and the CHANGELOG promise.
    server = T.fresh_server()
    para = _reading_paragraph(server.engram_add_observation.__doc__ or "")
    assert "0.85" in para and "0.92" in para, "both duplicate thresholds"
    assert "conversation or a" in para and "file" in para, "duplicate also fires for long-running sources"
    for needle in ("hard_data_unanchored", "low_similarity", "person_mentions", "engram_link_about",
                   "similar_not_shown", "weak_match"):
        assert needle in para, needle


# ── (e) anchoring_advisory: numbers stay, `flags` names the threshold, ───────
#        the two sentences go (D4)
def _advisory_for(server, quote_type, quoted_text, claim):
    payload = {
        "url": "https://example.com/anchor-" + quote_type,
        "title": "T anchor",
        "quoted_text": quoted_text,
        "interpretation": "interp",
        "claim": claim,
        "quote_type": quote_type,
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    return r["anchoring_advisory"]


def test_e_hard_data_with_an_unanchored_token_gets_the_flag_not_the_sentence():
    server = T.fresh_server()
    adv = _advisory_for(server, "hard_data", "the pier has some mooring points",
                        "the pier has 1066 mooring points")
    assert "hint" not in adv, adv
    assert "hard_data_unanchored" in adv.get("flags", []), adv
    for k in ("hard_tokens", "anchored", "ratio", "unanchored",
              "body_to_quote_ratio", "quote_claim_similarity"):
        assert k in adv, k


def test_e_flag_absent_when_every_token_is_anchored():
    server = T.fresh_server()
    adv = _advisory_for(server, "hard_data", "the pier has 1066 mooring points",
                        "the pier has 1066 mooring points")
    assert "hard_data_unanchored" not in adv.get("flags", [])
    assert "hint" not in adv


def test_e_low_similarity_gets_the_flag_not_the_sentence(monkeypatch):
    server = T.fresh_server()
    # the similarity itself is stubbed exactly as test_low_similarity_hint_4050.py does
    monkeypatch.setattr(sys.modules["engram_observation"], "_quote_claim_similarity",
                        lambda q, c: (0.05, None))
    adv = _advisory_for(server, "attributed_analysis",
                        "completely different words about harbours and ships",
                        "quartz crystals form slowly under pressure")
    assert "low_similarity_hint" not in adv, adv
    assert "low_similarity" in adv.get("flags", []), adv


def test_e_low_similarity_flag_absent_at_or_above_the_threshold(monkeypatch):
    # Found by a mutant: with the flag forced on, no cell of this file went red, because
    # only the hard_data flag had an absence cell here. (test_low_similarity_hint_4050.py
    # carries the boundary cells; this one makes this file discriminate on its own.)
    server = T.fresh_server()
    sim = {"v": 0.5}
    monkeypatch.setattr(sys.modules["engram_observation"], "_quote_claim_similarity",
                        lambda q, c: (sim["v"], None))
    for v in (0.2, 0.5):
        sim["v"] = v
        adv = _advisory_for(server, "attributed_analysis", "words about harbours", "words about harbours")
        assert "low_similarity" not in adv.get("flags", []), (v, adv)


# ── (f) what stays in full: keys that say THIS write went wrong (D4) ───────
def test_f_signals_that_fire_on_the_seeded_write_survive_in_full():
    _, r, _ = _reply()
    # both fire here only because of the test doubles / fake URL, and survive by rule
    assert r.get("embedding_failure"), r.keys()
    assert r.get("quote_provenance_warning"), r.keys()


def test_f_recall_summary_overrun_survives():
    server = T.fresh_server()
    payload = {"url": "https://example.com/overrun", "title": "T overrun", "quoted_text": "body of text here",
               "interpretation": "interp", "claim": "A claim about overrun.", "quote_type": "attributed_analysis",
               "recall_summary": "x" * 150}
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    assert "150" in r.get("recall_summary_overrun", ""), r.get("recall_summary_overrun")


def test_f_person_mention_keeps_id_and_name_and_loses_the_message():
    server = T.fresh_server()
    p = json.loads(server.engram_add_person(payload_json=json.dumps({"name": "Valentina", "relation": "colleague"})))
    assert "error" not in p, p
    r = T.add_obs(server, "Valentina shared an insight about calibration", "https://example.com/pm")
    pms = r.get("person_mentions")
    assert pms and pms[0]["person_name"] == "Valentina" and pms[0]["person_id"].startswith("pn_"), r
    assert set(pms[0]) == {"person_id", "person_name"}, pms[0]


# ── (m) the whole reply, size (written last; the bound is logged above) ──────
def test_m_whole_reply_is_at_most_the_bound_percent_of_todays():
    assert DEV_WHOLE_REPLY_CHARS is not None, (
        "fill DEV_WHOLE_REPLY_CHARS from this cell's first run against the untouched base")
    _, r, _ = _reply()
    now = len(json.dumps(r))
    assert now * 100 <= SIZE_BOUND_PERCENT * DEV_WHOLE_REPLY_CHARS, (
        now, DEV_WHOLE_REPLY_CHARS, SIZE_BOUND_PERCENT)
