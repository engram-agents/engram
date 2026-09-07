"""Tests for engram_backup.check_fts_integrity (engram-alpha#2678, forum #398).

## Design history (why this is a differential probe, not an integrity-check call)

Origin: Borges's seat hit `SELECT ... FROM nodes_fts WHERE nodes_fts MATCH ?
ORDER BY rank` raising "database disk image is malformed" while `PRAGMA
quick_check` reported "ok" and plain MATCH worked fine.

The first version of this function used FTS5's own `'integrity-check'`
special command (plain form, chosen because the `rank=1` "more thorough"
variant false-positives on ENGRAM's by-design retracted-row exclusion,
#781). That version shipped, was fairy-approved, and was pulled back before
merge: Borges measured the plain form reporting CLEAN against a byte-copy of
his own genuinely-corrupted graph -- a false negative on the exact case
#2678 exists to detect. His damage is in the ranking/bm25 auxiliary data for
one specific term, not the content/postings the plain form checks.

Kepler's ruling (forum #398): neither special command is the detector.
Plain misses ranking-path damage; rank=1 fires on every retraction
(confirmed not fixable by healing the mismatch after the fact either --
genuinely deleting retracted rows from `nodes` in a scratch copy did not
clear the rank=1 failure). The candidate design -- a differential probe
comparing plain MATCH against MATCH+ORDER BY rank per term, terms sourced
from `nodes_fts_vocab` by document frequency -- was validated by Borges
against a fresh byte-copy of his real corrupted graph at k=30: it correctly
flagged the one poisoned term ("by", which sat at document-frequency rank
18 of 30) with zero false positives.

## Round 2: the k=30 sample massively under-reported real damage

Borges then ran a FULL-VOCABULARY sweep of the same real corrupted graph
(all 26,745 terms, 1.5s) and found 24 poisoned terms, not 1 -- "by" was the
ONLY one in the top-30 by document frequency; the other 23 span mid/low
frequency and bare numerals, uncorrelated with rank. No probe_k a person
would choose catches more than one of twenty-four on that graph, and Kepler
ruled: full-vocab is cheap (confirmed again on a second, healthy seat:
0.53s over 13,805 terms with 33 retracted nodes, zero false positives) so
sweep everything by default; `probe_k` survives only as an opt-in sampling
escape hatch, never the default. This file tests THAT (current) design --
`check_fts_integrity(conn)` with no `probe_k` sweeps the full vocabulary.

This file tests the mechanical DETECTION function only (#2678 item 1). It
does not touch `_similar_existing_matches`'s exception handling (item 2) or
build auto-repair (item 3) -- both are separate, write-path-adjacent work
the issue itself defers to a spec + Lei's review.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engram_backup  # noqa: E402


def _build_engram_shaped_db(path: str, claims: list[str] | None = None) -> None:
    """A minimal but faithful ENGRAM-shaped DB: nodes + nodes_fts (external-
    content FTS5) + nodes_fts_vocab + the real sync triggers, including a
    `status` column so retraction can be exercised."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            status TEXT DEFAULT 'current'
        );
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row');
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        CREATE TRIGGER nodes_fts_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
            VALUES('delete', old.rowid, old.claim, old.quoted_text, old.interpretation);
        END;
        """
    )
    if claims is None:
        claims = [f"claim about apple banana cherry {i}" for i in range(10)]
    for i, c in enumerate(claims):
        conn.execute(
            "INSERT INTO nodes(id, claim, quoted_text, interpretation) VALUES (?,?,?,?)",
            (f"ob_{i}", c, "q", "i"),
        )
    conn.commit()
    conn.close()


def _retract_node_the_sanctioned_way(conn: sqlite3.Connection, node_id: str) -> None:
    """Mirrors rebuild_fts_index's own Step 2: mark retracted, then
    delete-insert the row out of the FTS index -- the real, correct pattern
    ENGRAM uses (not a bug; #781)."""
    row = conn.execute(
        "SELECT rowid, claim, quoted_text, interpretation FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    conn.execute("UPDATE nodes SET status = 'retracted' WHERE id = ?", (node_id,))
    conn.execute(
        "INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation) "
        "VALUES ('delete', ?, ?, ?, ?)",
        row,
    )
    conn.commit()


@pytest.fixture
def engram_db(tmp_path):
    p = str(tmp_path / "knowledge.db")
    _build_engram_shaped_db(p)
    return p


def _connect_raising_on_ranked_query(db_path: str, poisoned_term: str, exc: Exception) -> sqlite3.Connection:
    """A REAL sqlite3.Connection (via the documented `factory=` subclassing
    hook -- plain instance-attribute mocking doesn't work: `.execute` is a
    read-only slot on the C-level Connection type) whose `.execute()` raises
    `exc` only for a ranked MATCH query on `poisoned_term`, simulating a
    real per-term ranking-path corruption. Every other statement (vocab
    read, plain MATCH, ranked queries on other terms) behaves normally --
    this is what makes it a faithful simulation of Borges's actual bug
    (exactly one term poisoned, everything else clean), not a global
    failure a broader mock would produce."""
    return _connect_raising_on_ranked_queries(db_path, {poisoned_term}, exc)


def _connect_raising_on_ranked_queries(db_path: str, poisoned_terms: set[str], exc: Exception) -> sqlite3.Connection:
    """As above, generalized to a SET of poisoned terms -- for simulating a
    multi-term corruption shape like Borges's real graph (24 poisoned terms
    out of 26,745), not just a single-term toy case."""
    return _connect_raising_on_stage(db_path, "ranked", poisoned_terms, exc)


def _connect_raising_on_plain_query(db_path: str, poisoned_term: str, exc: Exception) -> sqlite3.Connection:
    """As `_connect_raising_on_ranked_query`, but poisons the PLAIN (unranked)
    MATCH query instead -- simulating Ariadne's #2731 colleague-review
    finding: corruption bad enough to break even the unranked path, not just
    ranking. The plain query has no `ORDER BY rank` clause, so it's
    distinguished by ITS ABSENCE."""
    return _connect_raising_on_stage(db_path, "plain", {poisoned_term}, exc)


def _connect_raising_on_stage(
    db_path: str, stage: str, poisoned_terms: set[str], exc: Exception
) -> sqlite3.Connection:
    """Shared implementation: raise `exc` for a MATCH query on any term in
    `poisoned_terms`, restricted to `stage` ("ranked" = has `ORDER BY rank`,
    "plain" = does not)."""

    class _RaisingOnStage(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            is_ranked = "ORDER BY rank" in sql
            matches_stage = is_ranked if stage == "ranked" else (not is_ranked and "MATCH" in sql)
            if matches_stage and args and args[0][0].strip('"') in poisoned_terms:
                raise exc
            return super().execute(sql, *args, **kwargs)

    return sqlite3.connect(db_path, factory=_RaisingOnStage)


# ---------------------------------------------------------------------------
# The healthy paths
# ---------------------------------------------------------------------------


def test_fresh_healthy_db_reports_ok(engram_db):
    conn = sqlite3.connect(engram_db)
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "ok"
    assert result["probed_terms"] > 0
    conn.close()


def test_retraction_is_not_a_false_positive(engram_db):
    """THE load-bearing regression guard for this design's central claim
    (see module docstring): a correctly-retracted node is expected, healthy
    ENGRAM state (#781) -- the probe must not flag it. This is what the
    plain-integrity-check design also got right; the difference is THIS
    design also catches what that one missed (see
    test_ranking_corruption_on_one_term_is_caught below)."""
    conn = sqlite3.connect(engram_db)
    _retract_node_the_sanctioned_way(conn, "ob_3")
    _retract_node_the_sanctioned_way(conn, "ob_7")

    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "ok", (
        f"a correctly-retracted node is BY-DESIGN ENGRAM state, not "
        f"corruption -- got {result!r}"
    )
    conn.close()


def test_works_on_a_readonly_connection(engram_db):
    """Unlike the prior (integrity-check-based) design, this one issues no
    write-shaped statements -- confirmed it runs cleanly against a
    read-only connection, matching how Borges's own original diagnostic
    query worked read-only."""
    ro_conn = sqlite3.connect(f"file:{engram_db}?mode=ro", uri=True)
    result = engram_backup.check_fts_integrity(ro_conn)
    assert result["status"] == "ok"
    ro_conn.close()


def test_ok_after_a_run_of_ordinary_inserts_and_deletes(engram_db):
    """Sanity: normal write traffic through the real triggers stays healthy."""
    conn = sqlite3.connect(engram_db)
    conn.execute(
        "INSERT INTO nodes(id, claim, quoted_text, interpretation) VALUES (?,?,?,?)",
        ("ob_10", "a fresh claim about delta echo", "q", "i"),
    )
    conn.execute("DELETE FROM nodes WHERE id = 'ob_1'")
    conn.commit()
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "ok"
    conn.close()


# ---------------------------------------------------------------------------
# "unavailable" -- the check itself could not run
# ---------------------------------------------------------------------------


def test_no_nodes_fts_table_reports_unavailable(tmp_path):
    p = str(tmp_path / "no_fts.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    conn.commit()
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "unavailable"
    assert "does not exist" in result["reason"]
    conn.close()


def test_no_vocab_table_reports_unavailable(tmp_path):
    """nodes_fts exists (so the fixture is otherwise valid) but
    nodes_fts_vocab does not -- an older/minimal DB shape."""
    p = str(tmp_path / "no_vocab.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT, interpretation TEXT);
        CREATE VIRTUAL TABLE nodes_fts USING fts5(claim, quoted_text, interpretation, content='nodes', content_rowid='rowid');
        """
    )
    conn.commit()
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "unavailable"
    assert "nodes_fts_vocab" in result["reason"]
    conn.close()


# ---------------------------------------------------------------------------
# "mismatch" -- genuine ranking-path corruption on one term
# ---------------------------------------------------------------------------
#
# A REAL per-term ranking-path desync (the actual corruption class Borges's
# seat has) is not cheaply synthesizable in a test sandbox -- scratch testing
# during development confirmed neither retraction nor a direct row UPDATE
# bypassing the sync triggers reproduces it. This path is tested by forcing
# the real exception check_fts_integrity must classify, isolated to exactly
# one term -- same pattern as #2680's test_synthetic_durable_snapshot_
# failure_is_unhealthy for an equally hard-to-force real failure.


def test_ranking_corruption_on_one_term_is_caught(engram_db):
    """THE headline regression guard: a plain-integrity-check design would
    have reported this graph clean (confirmed empirically on Borges's real
    seat) -- this design must not."""
    conn = _connect_raising_on_ranked_query(
        engram_db, "apple", sqlite3.DatabaseError("database disk image is malformed")
    )
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "mismatch"
    assert result["problems"] == [
        {"term": "apple", "error": "database disk image is malformed", "stage": "ranked_match"}
    ]
    assert "unprobed" not in result
    conn.close()


def test_only_the_poisoned_term_is_reported_others_stay_clean(engram_db):
    """The problems list is exactly the poisoned term(s), not every probed
    term -- confirms this isn't a global failure masquerading as per-term."""
    conn = _connect_raising_on_ranked_query(
        engram_db, "banana", sqlite3.DatabaseError("database disk image is malformed")
    )
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "mismatch"
    assert len(result["problems"]) == 1
    assert result["problems"][0]["term"] == "banana"
    conn.close()


def test_mutation_control_a_probe_that_never_runs_the_ranked_query_would_miss_this(engram_db):
    """Required control: prove the ranked-query comparison is actually
    load-bearing by confirming a probe using ONLY plain MATCH (no ranking at
    all) would report every term present -- i.e. would never discriminate
    the corrupted case from the healthy one. This is the mutation the real
    function must NOT collapse into."""
    conn = _connect_raising_on_ranked_query(
        engram_db, "apple", sqlite3.DatabaseError("database disk image is malformed")
    )
    # The mutant: check plain MATCH only, ignore ranking entirely.
    plain_only_result = conn.execute(
        "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", ('"apple"',)
    ).fetchall()
    assert plain_only_result, (
        "control failed: plain MATCH must still succeed on the poisoned term "
        "-- if it doesn't, this isn't simulating Borges's actual failure "
        "shape (plain works, only ranking is broken)"
    )
    # The real function, same connection, DOES catch it.
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "mismatch"
    conn.close()


# ---------------------------------------------------------------------------
# "unprobed" -- corruption bad enough to break even the PLAIN match
# ---------------------------------------------------------------------------
#
# Ariadne's colleague-review finding (#2731, forum #398, 2026-09-07): the
# original loop silently `continue`d past any error on the plain (unranked)
# query, treating it identically to "this term doesn't plain-match" -- but
# `except sqlite3.Error` there also catches corruption severe enough to
# break the SIMPLE path, not just ranking. That is worse damage than
# "problems" alone reports, and the old code made it byte-identical to a
# clean sweep. This section pins the fix: such a term is recorded in
# "unprobed", never dropped, and "ok" is never returned while any term is
# unprobed.


def test_plain_match_corruption_is_recorded_not_dropped(engram_db):
    """THE headline regression guard for this fix. A term whose plain MATCH
    itself raises must appear in 'unprobed', with status 'mismatch' -- NOT
    silently skipped with status 'ok'."""
    conn = _connect_raising_on_plain_query(
        engram_db, "apple", sqlite3.DatabaseError("database disk image is malformed")
    )
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "mismatch", (
        f"a term whose plain match itself raises must never read as 'ok' "
        f"-- got {result!r}"
    )
    assert result["unprobed"] == [
        {"term": "apple", "error": "database disk image is malformed", "stage": "plain_match"}
    ]
    assert "problems" not in result, (
        "this term never reached the ranked query at all -- it belongs in "
        "'unprobed', not 'problems'"
    )
    conn.close()


def test_ariadnes_discriminating_control_plain_vs_ranked_poisoning_are_distinguishable(engram_db):
    """Reproduces the exact control from Ariadne's colleague review: same
    harness, same term, same graph, varying only WHICH arm is poisoned.
    Before this fix, poisoning the plain arm produced a result
    byte-identical to the clean baseline -- a caller could not tell a
    healthy graph from the worst corruption this function can meet. After
    the fix, the two poisoned cases must be distinguishable from each other
    AND from a clean baseline."""
    clean_conn = sqlite3.connect(engram_db)
    baseline = engram_backup.check_fts_integrity(clean_conn)
    clean_conn.close()
    assert baseline["status"] == "ok"

    ranked_poisoned_conn = _connect_raising_on_ranked_query(
        engram_db, "apple", sqlite3.DatabaseError("database disk image is malformed")
    )
    ranked_poisoned = engram_backup.check_fts_integrity(ranked_poisoned_conn)
    ranked_poisoned_conn.close()

    plain_poisoned_conn = _connect_raising_on_plain_query(
        engram_db, "apple", sqlite3.DatabaseError("database disk image is malformed")
    )
    plain_poisoned = engram_backup.check_fts_integrity(plain_poisoned_conn)
    plain_poisoned_conn.close()

    assert plain_poisoned != baseline, (
        "control failed: plain-arm poisoning must NOT be byte-identical to "
        "a clean sweep -- this is the exact defect Ariadne's review caught"
    )
    assert plain_poisoned["status"] == "mismatch"
    assert ranked_poisoned["status"] == "mismatch"
    assert plain_poisoned != ranked_poisoned, (
        "the two failure classes should stay distinguishable from each "
        "other too -- plain-match damage lands in 'unprobed', ranked-match "
        "damage lands in 'problems'"
    )
    assert "unprobed" in plain_poisoned and "problems" not in plain_poisoned
    assert "problems" in ranked_poisoned and "unprobed" not in ranked_poisoned


def test_mutation_control_dropping_the_unprobed_record_regresses_to_the_defect(engram_db):
    """Required control: confirm that silently `continue`-ing (the pre-fix
    shape, with no `unprobed` bookkeeping) is exactly the regression the
    two tests above exist to catch -- i.e. that omitting `unprobed` entirely
    from a hypothetical result would make it indistinguishable from the
    healthy baseline, same shape as the mutation-testing discipline used
    throughout this file."""
    conn = _connect_raising_on_plain_query(
        engram_db, "apple", sqlite3.DatabaseError("database disk image is malformed")
    )
    result = engram_backup.check_fts_integrity(conn)

    clean_conn = sqlite3.connect(engram_db)
    baseline = engram_backup.check_fts_integrity(clean_conn)
    clean_conn.close()

    # Simulate the pre-fix behavior: drop the unprobed bookkeeping entirely
    # and report the old code's actual return shape on this exact input --
    # asserted equal to a real clean sweep, not just a same-status check, so
    # this control's own claim (byte-identical to baseline) is verified, not
    # merely asserted in the docstring above.
    pre_fix_shape = {"status": "ok", "probed_terms": baseline["probed_terms"]}
    assert pre_fix_shape == baseline, (
        "control failed: the simulated pre-fix shape must reproduce the "
        "actual pre-fix defect (byte-identical to a real clean sweep) for "
        "this control to mean anything"
    )
    assert result["status"] != "ok", "the real, fixed function must not collapse to this"
    conn.close()


# The 24 real terms Borges's full-vocabulary sweep found poisoned on his own
# affected graph (forum #398, 2026-09-07) -- kept verbatim as the ground-truth
# shape this design was measured against, per Kepler's ruling that the
# redesigned PR should carry this list, not just describe it. Only "by" sat
# in the top-30 by document frequency; the rest span mid/low frequency and
# bare numerals, uncorrelated with rank -- which is exactly why a top-k
# sample (the design this superseded) under-reported by 24x. The full
# measured (term, doc_freq, df_rank) table is in the PR body under a dated
# heading, per Kepler's ruling -- this constant is just the term names.
#
# IMPORTANT, folded after Kepler nearly ruled the test below unsound from the
# description alone (forum #398, corrected by Borges reading the actual diff):
# the test using this constant NEVER opens a real graph -- it plants these 24
# strings into a synthetic in-memory corpus via _connect_raising_on_ranked_
# queries. It will NOT go red the day Borges's real graph is repaired, because
# nothing in it reads that graph. And its discrimination is weaker than the
# name implies: 24 ARBITRARY strings would pass this test identically -- the
# power comes from the mock-and-filler shape, not from these being real
# findings. Keep this list for what it genuinely gives a reader (the real
# shape of multi-term damage: numerals beside common words, not a synthetic
# "corruption is rare-words-only" story) and as an all-reported-not-truncated
# sanity check -- but the load-bearing "no k a person would choose reaches
# this" guarantee is test_default_full_sweep_catches_a_low_frequency_term_
# no_k_would_choose below, not this one.
BORGES_REAL_POISONED_TERMS = frozenset(
    {
        "by", "5", "error", "day", "false", "clean", "rule", "6", "marker",
        "field", "missed", "sweep", "holding", "arc", "ruling", "come",
        "latency", "readers", "unblock", "181", "unmoved", "fossil",
        "hostage", "2444",
    }
)


def test_default_full_sweep_catches_a_low_frequency_term_no_k_would_choose(engram_db):
    """THE regression guard for round 2's finding: a poisoned term far below
    any k a person would pick must still be caught by the DEFAULT call (no
    probe_k). Built with 90 healthy high-frequency terms ahead of the one
    poisoned rare term in the vocabulary, deliberately beyond any
    conventional default (the design this replaced defaulted to k=50)."""
    conn = _connect_raising_on_ranked_query(
        ":memory:", "zzrareterm", sqlite3.DatabaseError("database disk image is malformed")
    )
    conn.executescript(
        """
        CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT, interpretation TEXT, status TEXT DEFAULT 'current');
        CREATE VIRTUAL TABLE nodes_fts USING fts5(claim, quoted_text, interpretation, content='nodes', content_rowid='rowid');
        CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row');
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        """
    )
    rowid = 0
    for i in range(90):  # 90 distinct high-frequency terms, each in >90 docs
        for _ in range(200 - i):
            rowid += 1
            conn.execute(
                "INSERT INTO nodes(id, claim, quoted_text, interpretation) VALUES (?,?,?,?)",
                (f"ob_{rowid}", f"commonterm{i}", "", ""),
            )
    rowid += 1
    conn.execute(
        "INSERT INTO nodes(id, claim, quoted_text, interpretation) VALUES (?,?,?,?)",
        (f"ob_{rowid}", "zzrareterm", "", ""),
    )
    conn.commit()

    vocab_size = conn.execute("SELECT COUNT(*) FROM nodes_fts_vocab").fetchone()[0]
    assert vocab_size > 50, "fixture must exceed the old default k=50 to be a real regression guard"

    result = engram_backup.check_fts_integrity(conn)  # no probe_k -- must be the full sweep
    assert result["status"] == "mismatch", (
        f"the default call must sweep the FULL vocabulary and catch a "
        f"1-in-{vocab_size} rare poisoned term -- got {result!r}"
    )
    assert result["problems"] == [
        {"term": "zzrareterm", "error": "database disk image is malformed", "stage": "ranked_match"}
    ]
    conn.close()


def test_full_sweep_reports_every_poisoned_term_not_a_truncated_sample(engram_db):
    """Simulates Borges's real corruption shape (24 poisoned terms among
    thousands) using BORGES_REAL_POISONED_TERMS verbatim, mixed into a
    vocabulary alongside plenty of healthy terms. Confirms the full sweep
    reports ALL of them, not the first N or a truncated list -- Kepler's
    ruling was explicit: 'report the count and the list, never a boolean',
    precisely because the prior design's top-k sample silently dropped 23 of
    24 real findings.

    ORDERING NOTE (Borges, forum #398): this test does NOT pin where the 24
    poisoned terms land within the 74-term vocabulary -- `frozenset` iterates
    in an arbitrary order, and `nodes_fts_vocab` itself orders by term text.
    That's deliberately not controlled here, because the full-sweep code path
    (`probe_k=None`) issues no `ORDER BY`/`LIMIT` at all -- it visits every
    vocabulary row unconditionally, so position cannot affect the outcome for
    the CURRENT implementation. What this test alone would NOT catch: a
    regression that silently reintroduces a LIMIT whose cutoff happens, by
    alphabetical luck, to still include all 24 (position-dependent survival).
    That specific regression shape is covered instead by
    test_default_full_sweep_catches_a_low_frequency_term_no_k_would_choose,
    which pins one poisoned term at the LAST position of its vocabulary
    (91 of 91) specifically so no plausible LIMIT reaches it."""
    conn = _connect_raising_on_ranked_queries(
        ":memory:", set(BORGES_REAL_POISONED_TERMS), sqlite3.DatabaseError("database disk image is malformed")
    )
    conn.executescript(
        """
        CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT, interpretation TEXT, status TEXT DEFAULT 'current');
        CREATE VIRTUAL TABLE nodes_fts USING fts5(claim, quoted_text, interpretation, content='nodes', content_rowid='rowid');
        CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row');
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        """
    )
    healthy_filler = [f"healthyword{i}" for i in range(50)]
    all_words = list(BORGES_REAL_POISONED_TERMS) + healthy_filler
    for i, word in enumerate(all_words):
        conn.execute(
            "INSERT INTO nodes(id, claim, quoted_text, interpretation) VALUES (?,?,?,?)",
            (f"ob_{i}", word, "", ""),
        )
    conn.commit()

    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "mismatch"
    reported_terms = {p["term"] for p in result["problems"]}
    assert reported_terms == set(BORGES_REAL_POISONED_TERMS), (
        f"expected all {len(BORGES_REAL_POISONED_TERMS)} poisoned terms reported, "
        f"got {len(reported_terms)}: missing={set(BORGES_REAL_POISONED_TERMS) - reported_terms}, "
        f"extra={reported_terms - set(BORGES_REAL_POISONED_TERMS)}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# probe_k coverage -- the honestly-disclosed limitation
# ---------------------------------------------------------------------------


def _build_distinct_frequency_db(conn: sqlite3.Connection, words: list[str]) -> None:
    """Populate `conn` (already opened, possibly via the raising factory)
    with a corpus where each of `words[i]` has a STRICTLY DISTINCT document
    frequency: words[0] appears in the most documents, words[-1] in the
    fewest (frequency = len(words) - i, no ties). Deliberately avoids
    `quoted_text`/`interpretation` filler text -- a shared non-empty filler
    column would itself become the single highest-frequency "term" and
    swamp the ranking this test needs to pin down exactly."""
    conn.executescript(
        """
        CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT, interpretation TEXT, status TEXT DEFAULT 'current');
        CREATE VIRTUAL TABLE nodes_fts USING fts5(claim, quoted_text, interpretation, content='nodes', content_rowid='rowid');
        CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row');
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        """
    )
    rowid = 0
    for freq, word in zip(range(len(words), 0, -1), words):
        for _ in range(freq):
            rowid += 1
            conn.execute(
                "INSERT INTO nodes(id, claim, quoted_text, interpretation) VALUES (?,?,?,?)",
                (f"ob_{rowid}", word, "", ""),
            )
    conn.commit()


def test_probes_highest_document_frequency_terms_first():
    """Pins down the DESC ordering directly and deterministically (strictly
    distinct per-term frequencies, no ties to obscure a wrong sort
    direction) -- an earlier version of this test used a corpus with many
    tied frequencies and did NOT catch an ORDER BY doc ASC mutation, because
    LIMIT's tie-breaking happened to still exclude the probed term by luck.
    kappa(10) > lambda(9) > mu(8) > nu(7) > xi(6) > ... > tau(1)."""
    words = ["kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho", "sigma", "tau"]
    # nu is the 4th-highest-frequency term (doc=7 of 10) -- inside a top-5
    # probe, outside a top-3 probe. A DESC->ASC mutation would instead put
    # nu's neighbor-by-symmetry (rho, the 4th-LOWEST) inside a naive top-5,
    # not nu -- so this discriminates direction, not just "some limit".
    conn = _connect_raising_on_ranked_query(":memory:", "nu", sqlite3.DatabaseError("malformed"))
    _build_distinct_frequency_db(conn, words)

    top5 = engram_backup.check_fts_integrity(conn, probe_k=5)
    assert top5["status"] == "mismatch", (
        f"nu is the 4th-highest-frequency term -- a top-5 DESC probe must "
        f"include it, got {top5!r}"
    )

    top3 = engram_backup.check_fts_integrity(conn, probe_k=3)
    assert top3["status"] == "ok", (
        f"nu (4th-highest) must be OUTSIDE a top-3 DESC probe -- if this is "
        f"'mismatch', the probe isn't actually ordering by frequency, got {top3!r}"
    )
    conn.close()
