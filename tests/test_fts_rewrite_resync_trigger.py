"""Tests for nodes_fts_rewrite_resync — issue #2742.

Background (forum #398, 2026-09-07): engram-alpha#2678 shipped a detector
for FTS5 ranking-path corruption (`engram_backup.check_fts_integrity`) and
ran it fleet-wide. 4 of 5 measured seats came back corrupted. Kepler traced
the mechanism: none of the four PRE-EXISTING nodes_fts sync triggers
(insert, delete, retract-remove, supersede-remove) fire on a PLAIN content
edit that leaves a node current and non-retracted — most commonly a
`engram_set_recall_summaries` tightening pass (`_set_recall_summaries_impl`,
engram_recall_summaries.py), which can drop tokens the original claim
carried. The row's rowid stays indexed with STALE postings for whatever the
edit removed, invisible to `PRAGMA quick_check`, a plain `MATCH`, and even
FTS5's own plain `'integrity-check'` special command -- only ranking a
dropped token (`MATCH ... ORDER BY rank`, which `_similar_existing_matches`'s
write-time dedup gate and `engram_query` both use) touches the stale
postings and raises SQLITE_CORRUPT_VTAB.

Fix: a fifth trigger, `nodes_fts_rewrite_resync`
(`AFTER UPDATE OF claim, quoted_text, interpretation, trigger_text,
recall_summary, recall_keywords ON nodes`), issues the same delete+reinsert
external-content FTS5 pattern the insert/delete triggers already use.
Guarded (`WHEN OLD.is_current = 1 AND NEW.is_current = 1 AND
COALESCE(NEW.status, '') != 'retracted'`) to compose safely with the two
existing removal triggers: a supersede or retract sharing the SAME UPDATE
statement as a text edit must not ALSO fire this trigger, or the FTS5
external-content 'delete' magic-insert double-fires on a rowid the sibling
trigger already removed -- raising "database disk image is malformed",
real corruption, not a wrong answer (same failure class as
test_fts_retract_trigger.py's own OLD.is_current=1 guard tests).

Coverage (matching Kepler's spec, forum #398 reply #43):
    1. test_rewrite_then_supersede_leaves_zero_orphans -- rewrite a node's
       claim (dropping a token), then supersede it. Zero remaining
       instances of the dropped token in nodes_fts_vocab.
    2. test_rewrite_then_retract_leaves_zero_orphans -- same, via retract
       instead of supersede (Aleph's both-paths requirement).
    3. test_rewrite_alone_keeps_index_in_step -- a plain content rewrite
       (node stays current) removes the dropped token from the index
       entirely: zero nodes_fts_vocab instances, AND a real
       `MATCH ... ORDER BY rank` query for the dropped token returns
       cleanly (0 rows) rather than raising.
    3b. test_recall_summary_tightening_triggers_resync -- the SAME
        property via the actual production code path Kepler traced the
        root cause to (_set_recall_summaries_impl), not a raw UPDATE.
    4. test_composed_supersede_and_rewrite_in_one_statement_does_not_
       double_delete -- the guard's own reason to exist: a single UPDATE
       that touches both `is_current` and a text column must not double-
       delete. Mutation-checked: with the guard's NEW.is_current=1 clause
       removed, this test goes red with "database disk image is
       malformed".
    4b. test_composed_restore_and_rewrite_in_one_statement_does_not_raise
        -- the WHEN clause's OTHER half (OLD.is_current=1): a row going
        NON-current back to current (a "restore"), combined with a text
        edit in the same UPDATE, must not fire this trigger either -- its
        OLD.* values are no longer what the index actually holds (the
        supersede/retract trigger already removed them), so a 'delete'
        magic-insert against them would raise the same corruption. Not
        reachable via any writer today (Borges, PR #2745 review round 2:
        grepped for any is_current 0->1 transition, found none -- both
        _supersede_impl and _retract_impl only ever set it to 0), but
        guarded defensively, matching this trigger's own posture toward
        the supersede/retract composition case above. Mutation-checked
        below with its own dedicated control.
    5. Existing supersede/retract trigger tests stay green (verified
       separately via the full collateral suite, not duplicated here).

Import-pattern contract (#948 wave 0 pilot — R1, matching
test_fts_retract_trigger.py's own established convention): this file
imports engram_revision, engram_observation, engram_recall_summaries, and
engram_core directly and contains no ``import server``.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core
import engram_observation
import engram_recall_summaries
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_fts_rewrite_resync_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Same pattern as test_fts_retract_trigger.py's own _make_fresh_mods.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    rs = importlib.import_module("engram_recall_summaries")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, rs, core


def _add_observation(obs, claim: str, quoted_text: str = None, url: str = "https://example.com/fts2742") -> str:
    qt = quoted_text if quoted_text is not None else claim[:80]
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="FTS2742 Test Source",
        quoted_text=qt,
        interpretation="Test interpretation for rewrite-resync trigger tests.",
        claim=claim,
        quote_type="hard_data",
    ))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _db_path() -> str:
    return os.path.join(TEST_DIR, "knowledge.db")


def _fts_match_rowids(token: str) -> set:
    """Real MATCH query (no ranking) -- presence in the index, per
    test_fts_retract_trigger.py's own established idiom/caveat about
    contentless-table rowid semantics."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (token,)).fetchall()
    conn.close()
    return {r["rowid"] for r in rows}


def _fts_rank_query_ok(token: str) -> tuple:
    """The REAL production symptom this whole design history is about: a
    ranked MATCH query. Returns (raised: bool, row_count_or_None)."""
    conn = sqlite3.connect(_db_path())
    try:
        rows = conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY rank", (token,)
        ).fetchall()
        return False, len(rows)
    except sqlite3.Error:
        return True, None
    finally:
        conn.close()


def _fts_hit_count(token: str) -> int:
    """Number of rows matching `token` via plain MATCH -- this test
    harness's _add_observation_impl path does not create nodes_fts_vocab
    (ensure_vocab_table silently no-ops here; verified via direct
    debug -- an environment characteristic of this test harness, not
    something #2742 introduced), so this file uses the SAME portable
    presence/absence observable test_fts_retract_trigger.py's own
    established tests already rely on (_fts_match_rowids), rather than
    Kepler's suggested "fts5vocab instance check" example verbatim. Same
    verification GOAL (zero remaining instances of a dropped/removed
    token), different but equally direct mechanism."""
    return len(_fts_match_rowids(token))


def _get_node_rowid(node_id: str) -> int:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT rowid FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    assert row is not None, f"Node {node_id} not found in DB"
    return row["rowid"]


# ---------------------------------------------------------------------------
# 1. rewrite-then-supersede leaves zero orphans
# ---------------------------------------------------------------------------


def test_rewrite_then_supersede_leaves_zero_orphans():
    """A token dropped by a rewrite, on a node later superseded, must have
    zero remaining nodes_fts_vocab instances -- not just "the node is gone"
    (which the pre-existing supersede trigger already guaranteed for
    whatever the node's LATEST content is), but genuinely zero orphaned
    postings from the DROPPED token specifically."""
    rev, obs, _rs, core = _make_fresh_mods()

    dropped_token = "fts2742rewritesupersede_zebra"
    kept_token = "fts2742rewritesupersede_giraffe"

    a_id = _add_observation(
        obs, claim=f"A claim about {dropped_token} migration patterns.",
        quoted_text="q", url="https://example.com/fts2742-a1",
    )
    assert _fts_hit_count(dropped_token) == 1

    # Rewrite: drop the token entirely, replacing it with something else.
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "UPDATE nodes SET claim = ? WHERE id = ?",
        (f"A totally different claim about {kept_token}.", a_id),
    )
    conn.commit()
    conn.close()
    assert _fts_hit_count(dropped_token) == 0, (
        "rewrite alone should already have zeroed the dropped token"
    )

    # Deliberately does NOT mention dropped_token anywhere in its own text --
    # an earlier draft of this test wrote "unrelated to {dropped_token}",
    # which self-poisons the index with the very token being checked (the
    # tokenizer has no notion of English negation) and produced a false
    # "orphaned" failure that had nothing to do with the trigger under test.
    b_id = _add_observation(
        obs, claim="Superseder claim about an entirely separate topic.",
        quoted_text="q", url="https://example.com/fts2742-b1",
    )
    sup = json.loads(rev._supersede_impl(
        a_id, b_id, supersede_reason="FTS rewrite-resync test 1 — supersede after rewrite",
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    assert _fts_hit_count(dropped_token) == 0, (
        f"'{dropped_token}' must have zero nodes_fts_vocab instances after "
        f"rewrite-then-supersede -- any nonzero count means it orphaned "
        f"rather than resynced"
    )
    assert _fts_hit_count(kept_token) == 0, (
        "the superseded node's LATEST content must also be gone after "
        "supersede (pre-existing guarantee, sanity-checked here too)"
    )


# ---------------------------------------------------------------------------
# 2. rewrite-then-retract leaves zero orphans (Aleph's both-paths requirement)
# ---------------------------------------------------------------------------


def test_rewrite_then_retract_leaves_zero_orphans():
    """Same property as test 1, via retract instead of supersede."""
    rev, obs, _rs, core = _make_fresh_mods()

    dropped_token = "fts2742rewriteretract_zebra"
    kept_token = "fts2742rewriteretract_giraffe"

    a_id = _add_observation(
        obs, claim=f"A claim about {dropped_token} migration patterns.",
        quoted_text="q", url="https://example.com/fts2742-a2",
    )
    assert _fts_hit_count(dropped_token) == 1

    conn = sqlite3.connect(_db_path())
    conn.execute(
        "UPDATE nodes SET claim = ? WHERE id = ?",
        (f"A totally different claim about {kept_token}.", a_id),
    )
    conn.commit()
    conn.close()
    assert _fts_hit_count(dropped_token) == 0

    ret = json.loads(rev._retract_impl(
        node_id=a_id, error_type="other",
        reason="FTS rewrite-resync test 2 — retract after rewrite",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert ret.get("status") == "retracted", f"retract failed: {ret}"

    assert _fts_hit_count(dropped_token) == 0, (
        f"'{dropped_token}' must have zero nodes_fts_vocab instances after "
        f"rewrite-then-retract"
    )
    assert _fts_hit_count(kept_token) == 0, (
        "the retracted node's LATEST content must also be gone after retract"
    )


# ---------------------------------------------------------------------------
# 3. rewrite alone keeps the index in step
# ---------------------------------------------------------------------------


def test_rewrite_alone_keeps_index_in_step():
    """THE headline regression guard: a rewrite on a node that stays
    current must resync the index, not just leave it stale-but-undetected.
    Checked two ways: the vocab-instance count (Kepler's named observable)
    AND a real ranked MATCH query for the dropped token (the actual
    production symptom -- must return 0 rows cleanly, not raise)."""
    rev, obs, _rs, core = _make_fresh_mods()

    dropped_token = "fts2742rewritealone_zebra"
    kept_token = "fts2742rewritealone_giraffe"

    a_id = _add_observation(
        obs, claim=f"A claim about {dropped_token} migration patterns.",
        quoted_text="q", url="https://example.com/fts2742-a3",
    )
    rowid_a = _get_node_rowid(a_id)
    assert rowid_a in _fts_match_rowids(dropped_token)
    assert _fts_hit_count(dropped_token) == 1

    conn = sqlite3.connect(_db_path())
    conn.execute(
        "UPDATE nodes SET claim = ? WHERE id = ?",
        (f"A totally different claim about {kept_token}.", a_id),
    )
    conn.commit()
    conn.close()

    assert _fts_hit_count(dropped_token) == 0, (
        f"'{dropped_token}' must be gone from nodes_fts_vocab after the "
        f"rewrite dropped it -- a nonzero count is exactly the orphan "
        f"class #2742 exists to fix"
    )
    assert rowid_a in _fts_match_rowids(kept_token), (
        "the node's NEW content must be findable after the rewrite"
    )

    raised, count = _fts_rank_query_ok(dropped_token)
    assert not raised, (
        f"ranking a dropped token must not raise SQLITE_CORRUPT_VTAB -- "
        f"this is the actual production symptom (forum #398) the fix exists "
        f"to prevent"
    )
    assert count == 0


def test_recall_summary_tightening_triggers_resync():
    """The SAME property as test 3, but via the actual production code
    path Kepler traced the fleet-wide root cause to
    (_set_recall_summaries_impl / engram_set_recall_summaries) rather than
    a raw UPDATE -- confirms the fix covers the real-world trigger, not
    just a synthetic claim edit.

    The dropped token must live in recall_summary/recall_keywords, NOT in
    claim -- an earlier draft of this test put it in `claim`, which the
    tightening call never touches, so the trigger's NEW.* re-insert simply
    carried the (unchanged) claim text forward and the token never actually
    left the row. Real tightening passes narrow an existing recall_summary;
    modeled here as two _set_recall_summaries_impl calls -- the first
    establishes a summary that mentions the token (and is itself the thing
    that puts it in the index, since the token is never in claim at all),
    the second is the tightening pass that drops it.
    """
    rev, obs, rs, core = _make_fresh_mods()

    dropped_token = "fts2742tightening_platypus"
    a_id = _add_observation(
        obs,
        claim="A claim about migration patterns, unrelated to the summary text below.",
        quoted_text="q", url="https://example.com/fts2742-tighten",
    )
    assert _fts_hit_count(dropped_token) == 0

    verbose_payload = json.dumps({
        "summaries": [{
            "node_id": a_id,
            "recall_summary": f"A verbose summary that mentions {dropped_token} explicitly.",
            "recall_keywords": ["verbose", "summary", dropped_token],
        }]
    })
    verbose_result = json.loads(rs._set_recall_summaries_impl(verbose_payload))
    assert "error" not in verbose_result or not verbose_result.get("error"), (
        f"initial (verbose) recall-summary call failed: {verbose_result}"
    )
    assert _fts_hit_count(dropped_token) == 1, (
        "the verbose recall_summary/recall_keywords must have put the "
        "token INTO the index -- otherwise the tightening pass below "
        "proves nothing"
    )

    tight_payload = json.dumps({
        "summaries": [{
            "node_id": a_id,
            "recall_summary": "A short summary with nothing zoological at all.",
            "recall_keywords": ["short", "summary", "tightened"],
        }]
    })
    result = json.loads(rs._set_recall_summaries_impl(tight_payload))
    assert "error" not in result or not result.get("error"), f"tightening call failed: {result}"

    assert _fts_hit_count(dropped_token) == 0, (
        f"a recall_summary tightening pass that drops '{dropped_token}' "
        f"from the node's indexed columns must resync the index, not "
        f"orphan the term -- this is the exact real-world mechanism the "
        f"fleet-wide corruption (forum #398) traced back to"
    )
    raised, _count = _fts_rank_query_ok(dropped_token)
    assert not raised


# ---------------------------------------------------------------------------
# 4. Composition safety: a combined supersede+rewrite in ONE statement
#    must not double-delete (the guard's own reason to exist).
# ---------------------------------------------------------------------------


def test_composed_supersede_and_rewrite_in_one_statement_does_not_double_delete():
    """Reaches the DB transition the guard exists for directly in SQL
    (same idiom as test_fts_retract_trigger.py's own test 6): a single
    UPDATE that sets is_current=0 AND a text column together must fire
    ONLY the supersede-remove trigger, not also nodes_fts_rewrite_resync
    -- a second FTS5 'delete' magic-insert on a rowid already removed by
    the first raises "database disk image is malformed".

    Not reachable via _supersede_impl as production code stands today
    (verified: its own UPDATE only ever sets is_current + superseded_by,
    never touches text columns in the same statement) -- this test
    exercises the guard defensively, matching #2742's own spec
    ("guarded to compose... so a supersede/retract that also touches
    text does not double-delete") and this trigger design's sibling
    guard in nodes_fts_supersede_remove, which defends against retract's
    OWN real combined status+is_current UPDATE the same way.
    """
    _rev, obs, _rs, core = _make_fresh_mods()

    token = "fts2742composedsupersede_orangutan"
    a_id = _add_observation(
        obs, claim=f"A claim about {token} for the composition test.",
        quoted_text="q", url="https://example.com/fts2742-composed",
    )

    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            "UPDATE nodes SET is_current = 0, claim = 'rewritten during supersede' "
            "WHERE id = ?",
            (a_id,),
        )
        conn.commit()
    except sqlite3.Error as e:
        raise AssertionError(
            f"a combined is_current+claim UPDATE must not raise -- the "
            f"rewrite-resync guard should have stayed silent here, "
            f"deferring to the supersede trigger. Got: {e!r}"
        )
    finally:
        conn.close()

    assert _fts_hit_count(token) == 0, (
        "the original token must be gone after the combined update "
        "(removed by the supersede trigger, exactly once)"
    )


def test_mutation_control_removing_the_new_is_current_guard_causes_the_double_delete():
    """Required control (matches test_fts_retract_trigger.py's own
    mutation-checked discipline for this exact failure class): confirm
    that WITHOUT the NEW.is_current=1 guard clause, the combined-statement
    scenario above genuinely does raise "database disk image is
    malformed" -- proving the guard is load-bearing, not a no-op.

    Covers the NEW.is_current=1 half of the WHEN clause specifically; the
    OLD.is_current=1 half has its own sibling control below
    (test_mutation_control_removing_the_old_is_current_guard_...), added
    after Borges's colleague review found this control exercised only one
    of the two guard-clause halves (PR #2745 review round 2)."""
    _rev, obs, _rs, core = _make_fresh_mods()

    token = "fts2742mutationguard_narwhal"
    a_id = _add_observation(
        obs, claim=f"A claim about {token} for the mutation-control test.",
        quoted_text="q", url="https://example.com/fts2742-mutation",
    )

    conn = sqlite3.connect(_db_path())
    # Drop the real (guarded) trigger and install an UNGUARDED version --
    # same shape, minus "AND NEW.is_current = 1" -- to prove the guard
    # clause specifically is what prevents the double-delete, not some
    # other property of the trigger.
    conn.execute("DROP TRIGGER IF EXISTS nodes_fts_rewrite_resync")
    conn.execute(
        """
        CREATE TRIGGER nodes_fts_rewrite_resync
        AFTER UPDATE OF claim, quoted_text, interpretation, trigger_text, recall_summary, recall_keywords ON nodes
        WHEN OLD.is_current = 1 AND COALESCE(NEW.status, '') != 'retracted'
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, trigger_text, recall_summary, recall_keywords)
            VALUES ('delete', OLD.rowid, OLD.claim, OLD.quoted_text, OLD.interpretation, OLD.trigger_text, OLD.recall_summary, OLD.recall_keywords);
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, trigger_text, recall_summary, recall_keywords)
            VALUES (NEW.rowid, NEW.claim, NEW.quoted_text, NEW.interpretation, NEW.trigger_text, NEW.recall_summary, NEW.recall_keywords);
        END
        """
    )
    conn.commit()

    raised = False
    try:
        conn.execute(
            "UPDATE nodes SET is_current = 0, claim = 'rewritten during supersede' "
            "WHERE id = ?",
            (a_id,),
        )
        conn.commit()
    except sqlite3.Error as e:
        raised = True
        assert "malformed" in str(e).lower() or "disk image" in str(e).lower(), (
            f"expected the double-delete corruption error, got a different "
            f"error: {e!r}"
        )
    finally:
        conn.close()

    assert raised, (
        "control failed: removing the NEW.is_current=1 guard must "
        "reproduce the double-delete corruption for this control to mean "
        "anything -- if this doesn't raise, the guard was never what was "
        "preventing it"
    )


# ---------------------------------------------------------------------------
# 4b. Composition safety, the OTHER half: OLD.is_current=1. A row going
#     NON-current back to CURRENT ("restore"), combined with a rewrite in
#     the same statement, must not double-delete either -- added after
#     Borges's colleague review (PR #2745 round 2) found the mutation
#     control above exercised only the NEW.is_current=1 half; dropping
#     OLD.is_current=1 instead left all 6 original tests green.
# ---------------------------------------------------------------------------


def test_composed_restore_and_rewrite_in_one_statement_does_not_raise():
    """Mirrors test_composed_supersede_and_rewrite_in_one_statement_does_
    not_double_delete, for the WHEN clause's other guard half.

    Not reachable via production code today (verified: neither
    _supersede_impl nor _retract_impl ever sets is_current 0->1; grepped
    the whole tree for any 0->1 transition and found none -- Borges's
    finding, PR #2745 review round 2) -- exercised here directly in SQL,
    matching this file's own established idiom for the supersede/retract
    composition case above.

    Why this specific combination would corrupt without the guard: once a
    row goes non-current, the supersede/retract removal trigger has
    already delete-excluded it from nodes_fts -- its OLD.* values are no
    longer what the index holds. A restore-plus-rewrite UPDATE reaching
    this trigger would issue a 'delete' magic-insert against content
    that was already removed, which FTS5's external-content contract
    treats as corruption, not a no-op (same failure class as every other
    mutation control in this file).
    """
    rev, obs, _rs, core = _make_fresh_mods()

    token = "fts2742restorerewrite_pangolin"
    a_id = _add_observation(
        obs, claim=f"A claim about {token} for the restore-composition test.",
        quoted_text="q", url="https://example.com/fts2742-restore",
    )
    b_id = _add_observation(
        obs, claim="Superseder claim about an entirely separate topic.",
        quoted_text="q", url="https://example.com/fts2742-restore-b",
    )
    sup = json.loads(rev._supersede_impl(
        a_id, b_id, supersede_reason="FTS rewrite-resync test 4b — setup for restore composition",
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"
    assert _fts_hit_count(token) == 0, (
        "sanity check: the supersede must already have removed the token "
        "before the restore attempt below"
    )

    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            "UPDATE nodes SET is_current = 1, "
            "claim = 'restored claim, still mentions the original topic' "
            "WHERE id = ?",
            (a_id,),
        )
        conn.commit()
    except sqlite3.Error as e:
        raise AssertionError(
            f"a combined is_current(0->1)+claim UPDATE ('restore') must "
            f"not raise -- the rewrite-resync guard's OLD.is_current=1 "
            f"half should have stayed silent here, since the row's OLD.* "
            f"content is no longer what the index holds. Got: {e!r}"
        )
    finally:
        conn.close()


def test_mutation_control_removing_the_old_is_current_guard_causes_the_double_delete():
    """Required control for test 4b, matching the discipline of the
    NEW.is_current=1 control above: confirm that WITHOUT the
    OLD.is_current=1 guard clause, the restore-composition scenario
    genuinely does raise "database disk image is malformed" -- proving
    that guard half is load-bearing too, not merely unreached.

    Origin: Borges's colleague review on PR #2745 round 2 -- he verified
    independently (dropping OLD.is_current=1 from a reconstructed
    in-memory trigger) that all 6 of this file's original tests stayed
    green, meaning nothing existing discriminated this half. This control
    closes that gap."""
    rev, obs, _rs, core = _make_fresh_mods()

    token = "fts2742mutationguard_okapi"
    a_id = _add_observation(
        obs, claim=f"A claim about {token} for the OLD.is_current mutation-control test.",
        quoted_text="q", url="https://example.com/fts2742-mutation-old",
    )
    b_id = _add_observation(
        obs, claim="Superseder claim about an entirely separate topic.",
        quoted_text="q", url="https://example.com/fts2742-mutation-old-b",
    )
    sup = json.loads(rev._supersede_impl(
        a_id, b_id, supersede_reason="FTS rewrite-resync mutation-control test — setup for restore composition",
    ))
    assert sup.get("status") == "superseded", f"supersede failed: {sup}"

    conn = sqlite3.connect(_db_path())
    # Drop the real (guarded) trigger and install an UNGUARDED version --
    # same shape, minus "OLD.is_current = 1 AND" -- to prove that guard
    # clause specifically is what prevents the double-delete on the
    # restore path, not some other property of the trigger.
    conn.execute("DROP TRIGGER IF EXISTS nodes_fts_rewrite_resync")
    conn.execute(
        """
        CREATE TRIGGER nodes_fts_rewrite_resync
        AFTER UPDATE OF claim, quoted_text, interpretation, trigger_text, recall_summary, recall_keywords ON nodes
        WHEN NEW.is_current = 1 AND COALESCE(NEW.status, '') != 'retracted'
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, trigger_text, recall_summary, recall_keywords)
            VALUES ('delete', OLD.rowid, COALESCE(OLD.claim, ''), COALESCE(OLD.quoted_text, ''), COALESCE(OLD.interpretation, ''), COALESCE(OLD.trigger_text, ''), COALESCE(OLD.recall_summary, ''), COALESCE(OLD.recall_keywords, ''));
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, trigger_text, recall_summary, recall_keywords)
            VALUES (NEW.rowid, NEW.claim, NEW.quoted_text, NEW.interpretation, NEW.trigger_text, NEW.recall_summary, NEW.recall_keywords);
        END
        """
    )
    conn.commit()

    raised = False
    try:
        conn.execute(
            "UPDATE nodes SET is_current = 1, "
            "claim = 'restored claim, still mentions the original topic' "
            "WHERE id = ?",
            (a_id,),
        )
        conn.commit()
    except sqlite3.Error as e:
        raised = True
        assert "malformed" in str(e).lower() or "disk image" in str(e).lower(), (
            f"expected the double-delete corruption error, got a different "
            f"error: {e!r}"
        )
    finally:
        conn.close()

    assert raised, (
        "control failed: removing the OLD.is_current=1 guard must "
        "reproduce the double-delete corruption on the restore path for "
        "this control to mean anything -- if this doesn't raise, that "
        "guard half was never what was preventing it"
    )
