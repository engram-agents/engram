"""Contract-first tests for #3348: prediction nodes from
`_add_observation_impl` must go through `_stamp_new_node`.

Written BEFORE the implementation, per this repo's contract-first-tests
convention (CLAUDE.md). Every test below is expected to be RED against the
unmodified `origin/dev` tip (ff91358f) -- the prediction-decomposition
block's raw `INSERT INTO nodes` never calls `_stamp_new_node`, so the
prediction node gets no importance stamp, no write-time embedding, and no
#3343 write-path contract -- and GREEN after this PR's implementation lands.

Coverage, matching the contract in issue #3348:

  P1. Created path: `_stamp_new_node` is called for the prediction node,
      inside the same transaction, after the INSERT. Exactly ONE "created"
      edit_history row exists for the prediction (the manual `_log_edit`
      call is removed).
  (linked_to_existing path): no second stamp, no new "created" row.
  P2. A failed prediction embed surfaces in the response naming the
      prediction's node id, and sets the restore flag through the same
      path as every other node.

Round 2 (Kepler's ruling on PR #3352, option B + rider -- see his
ruling comment on that PR): `embedding_failure` keeps its #3343 meaning exactly
(the OBSERVATION node's outcome, always a dict, never a list); the
prediction node's outcome goes in its own `prediction_embedding_failure`
key, present only when the prediction was CREATED this call and its embed
failed. Both dicts carry `node_id` -- `embedding_failure` ALWAYS, with or
without a prediction in play. Each dict's own `message` is prefixed with
its own node id (`f"{node_id}: " + <original message>`); no new top-level
response line.

  Cell (1): observation embed fails, prediction embeds -> `embedding_failure`
      (observation's node_id) only.
  Cell (2): observation embeds, prediction fails -> `prediction_embedding_failure`
      (prediction's node_id) only -- this is the pre-existing embed-failure
      test above, updated to the new key.
  Cell (3): both fail -> both keys, two different node_ids, each message
      self-describing.
  Cell (4): no prediction node created this call (linked_to_existing, or a
      non-predictive observation) and the observation embed fails ->
      `embedding_failure` only, exact key set {status, detail,
      revive_attempted_at, revive_result, next_revive_not_before, message,
      node_id}, no `prediction_embedding_failure`.

Fixture pattern: mirrors tests/test_predicted_by.py's `fresh_server()` /
`_add_predictive_observation()` shape (full real server + schema, not a
hand-built minimal nodes table) -- this exercises the real write path, not
just the engine-level `_stamp_new_node` contract already covered by
tests/test_345_pr3_write_embed_revive.py. The embedder-failure test's
"nothing commits early" check mirrors that file's TestRB1NoEarlyCommit
pattern: a second, independent sqlite3 connection to the same db file reads
state DURING the call (hooked from inside the fake embedder, which runs
mid-transaction) and again after the call returns.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_server(tmp_dir):
    """Full real server wiring, embeddings left ON (not the
    ENGRAM_NO_EMBEDDINGS=1 shortcut tests/test_predicted_by.py uses) --
    this suite needs the real write-time embed path to exercise."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    import server
    server._configure_paths(tmp_dir)
    server._ensure_data_dir()
    os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


def _patch_embedder_ok(server, monkeypatch):
    """Stub the embedder to SUCCEED on every call (a stable 384-d vector),
    no real sentence-transformers model load."""
    monkeypatch.setattr(server._embedder, "is_available", lambda: True)
    monkeypatch.setattr(server._embedder, "_load_model", lambda name: None)
    monkeypatch.setattr(
        server._embedder, "_load_model_local_only", lambda name: None,
    )
    server._embedder._revive_cooldown.clear()
    server._embedder._failed_models.clear()
    monkeypatch.setattr(
        server._embedder, "embed", lambda text, model_name: [0.1] * 384,
    )


class _FailOnTextEmbed:
    """Fake embed() that fails ONLY when the text passed in matches (is
    equal to, or contains) `fail_marker` -- every other text embeds fine.
    Used to make the PREDICTION node's embed fail while the paired
    OBSERVATION node's embed succeeds, since
    `_embedding_text_for_node` embeds `predicted_event` for a prediction
    and `claim` for an observation (measured fact in the spec)."""

    def __init__(self, fail_marker, on_fail=None):
        self.fail_marker = fail_marker
        self.on_fail = on_fail
        self.calls = []

    def __call__(self, text, model_name):
        self.calls.append(text)
        if self.fail_marker in text:
            if self.on_fail is not None:
                self.on_fail()
            return None
        return [0.1] * 384


def _add_predictive_observation(server, claim, predicted_event, url):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Predictive source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "attributed_analysis",
        "is_predictive": True,
        "predicted_event": predicted_event,
    })))
    return r


def _add_observation(server, claim, url):
    """Non-predictive observation -- round 2 cell (4b)'s second half."""
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Non-predictive source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "attributed_analysis",
    })))
    return r


def _prime_fail_embedder(server, monkeypatch, fail_marker):
    """Round 2: shared embedder-priming + `_FailOnTextEmbed` wiring, factored
    out of the cell-2 test above (`test_prediction_embed_failure_surfaces_and_
    commits_atomically`) so cells 1/3/4 can reuse the same setup against a
    different `fail_marker`."""
    monkeypatch.setattr(server._embedder, "is_available", lambda: True)
    monkeypatch.setattr(server._embedder, "_load_model", lambda name: None)
    monkeypatch.setattr(
        server._embedder, "_load_model_local_only", lambda name: None,
    )
    server._embedder._revive_cooldown.clear()
    server._embedder._failed_models.clear()
    fake_embed = _FailOnTextEmbed(fail_marker)
    monkeypatch.setattr(server._embedder, "embed", fake_embed)
    return fake_embed


class _TestBase:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_3348_")

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _node_row(self, server, node_id):
        conn = server._get_db()
        try:
            row = conn.execute(
                "SELECT importance_base, importance_score, recall_turn, "
                "embedding, confidence FROM nodes WHERE id = ?",
                (node_id,),
            ).fetchone()
        finally:
            conn.close()
        return row

    def _created_rows(self, server, node_id):
        conn = server._get_db()
        try:
            rows = conn.execute(
                "SELECT action, node_type, details FROM edit_history "
                "WHERE node_id = ? AND action = 'created'",
                (node_id,),
            ).fetchall()
        finally:
            conn.close()
        return rows

    def _fresh_conn(self, server):
        """A SEPARATE sqlite3 connection to the same db file -- the only
        way to observe what is actually DURABLE, independent of the
        server's own connection (mirrors test_345_pr3_write_embed_revive.py
        TestRB1NoEarlyCommit's `_node_ids_via_fresh_conn` idiom)."""
        import engram_core as core
        conn = sqlite3.connect(str(core.DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn


# ---------------------------------------------------------------------------
# P1 -- created path: prediction node is stamped, exactly one "created" row
# ---------------------------------------------------------------------------

class TestCreatedPathStamped(_TestBase):

    def test_prediction_node_gets_importance_and_embedding(self, monkeypatch):
        server = _fresh_server(self.tmp)
        import engram_core as core  # AFTER _fresh_server -- it deletes and
        # re-imports engram_core as a NEW module object; importing before
        # would bind `core` to the stale pre-reload module (wrong CONFIG_PATH).
        _patch_embedder_ok(server, monkeypatch)

        # Force the current turn away from its 0 default BEFORE the write --
        # the raw INSERT's schema-level DEFAULTs (importance_base=0.5,
        # importance_score=0.5, recall_turn=0) are "not None" too, so a bare
        # not-None check does not discriminate "stamped" from "defaulted" on
        # a freshly booted test DB at turn 0 (measured: dev's pre-fix row
        # reads importance_base=0.5, importance_score=0.5, recall_turn=0 --
        # identical in TYPE to a real stamp, not just absent). Asserting the
        # EXACT value _stamp_new_node(confidence=0.5, surprise=0.5) would
        # compute, at a turn where the default and the real value provably
        # differ, is the actual discriminator.
        server._set_current_turn(3)
        expected_turn = core._get_current_turn()
        assert expected_turn == 3

        r = _add_predictive_observation(
            server, "X predicts Y will happen.", "Y occurs within 30 days.",
            "https://example.com/3348-a",
        )
        assert "error" not in r, r
        assert r.get("prediction_status") == "created", r
        pred_id = r["prediction_id"]

        row = self._node_row(server, pred_id)
        assert row is not None, "prediction node must exist"

        expected_base = core._importance_base_for_node(0.5, 0.5)
        expected_score = core._compute_importance(expected_base, expected_turn)
        assert row["importance_base"] == expected_base, (
            f"prediction node {pred_id} must have importance_base == "
            f"{expected_base} (the value _stamp_new_node(confidence=0.5, "
            f"surprise=0.5) computes), not the schema DEFAULT -- got row: "
            f"{dict(row)}"
        )
        assert row["importance_score"] == expected_score, (
            f"prediction node {pred_id} must have importance_score == "
            f"{expected_score}, got row: {dict(row)}"
        )
        assert row["recall_turn"] == expected_turn, (
            f"prediction node {pred_id} must have recall_turn == "
            f"{expected_turn} (the real current turn, not the schema "
            f"DEFAULT of 0), got row: {dict(row)}"
        )
        assert row["embedding"] is not None, (
            f"prediction node {pred_id} must have a non-NULL embedding "
            f"at write time (#3348), got row: {dict(row)}"
        )

    def test_prediction_node_has_exactly_one_created_row(self, monkeypatch):
        server = _fresh_server(self.tmp)
        _patch_embedder_ok(server, monkeypatch)

        r = _add_predictive_observation(
            server, "A predicts B will happen.", "B occurs within 10 days.",
            "https://example.com/3348-b",
        )
        assert "error" not in r, r
        pred_id = r["prediction_id"]

        rows = self._created_rows(server, pred_id)
        assert len(rows) == 1, (
            f"expected exactly ONE 'created' edit_history row for prediction "
            f"{pred_id} (the manual _log_edit call must be removed so "
            f"_stamp_new_node's own 'created' row is the only one), "
            f"got {len(rows)}: {[dict(row) for row in rows]}"
        )


# ---------------------------------------------------------------------------
# linked_to_existing path -- no second stamp, no new "created" row
# ---------------------------------------------------------------------------

class TestLinkedToExistingPathNotStamped(_TestBase):

    def test_second_observation_links_without_restamping(self, monkeypatch):
        server = _fresh_server(self.tmp)
        _patch_embedder_ok(server, monkeypatch)

        predicted_event = "Z occurs within 60 days."
        r1 = _add_predictive_observation(
            server, "First predicts Z will happen.", predicted_event,
            "https://example.com/3348-c1",
        )
        assert "error" not in r1, r1
        assert r1.get("prediction_status") == "created", r1
        pred_id = r1["prediction_id"]

        row_after_first = self._node_row(server, pred_id)
        rows_after_first = self._created_rows(server, pred_id)
        assert len(rows_after_first) == 1, [dict(row) for row in rows_after_first]

        r2 = _add_predictive_observation(
            server, "Second also predicts Z will happen.", predicted_event,
            "https://example.com/3348-c2",
        )
        assert "error" not in r2, r2
        assert r2.get("prediction_status") == "linked_to_existing", r2
        assert r2["prediction_id"] == pred_id, r2

        rows_after_second = self._created_rows(server, pred_id)
        assert len(rows_after_second) == 1, (
            f"linking to an existing prediction must not add a new "
            f"'created' row for it, got {len(rows_after_second)}: "
            f"{[dict(row) for row in rows_after_second]}"
        )

        row_after_second = self._node_row(server, pred_id)
        assert row_after_second["importance_score"] == row_after_first["importance_score"], (
            "linking to an existing prediction must not re-stamp "
            f"(importance_score changed: {dict(row_after_first)} -> "
            f"{dict(row_after_second)})"
        )


# ---------------------------------------------------------------------------
# P2 -- failed prediction embed surfaces in the response, names the
# prediction id, sets the restore flag, nothing commits early
# ---------------------------------------------------------------------------

class TestCreatedPathEmbedFailsForPredictionOnly(_TestBase):

    def test_prediction_embed_failure_surfaces_and_commits_atomically(
        self, monkeypatch,
    ):
        server = _fresh_server(self.tmp)
        monkeypatch.setattr(server._embedder, "is_available", lambda: True)
        monkeypatch.setattr(server._embedder, "_load_model", lambda name: None)
        monkeypatch.setattr(
            server._embedder, "_load_model_local_only", lambda name: None,
        )
        server._embedder._revive_cooldown.clear()
        server._embedder._failed_models.clear()

        predicted_event = "Q occurs within 5 days (3348 fail marker)."
        claim = "P predicts Q will happen (3348 fail marker observation)."

        mid_call_snapshot = {}

        def _snapshot_mid_transaction():
            """Called from INSIDE the fake embed(), at the moment the
            prediction's embed is attempted -- mid-transaction, before
            _add_observation_impl's own conn.commit(). A second, separate
            connection to the same db file must NOT see the observation
            node yet if nothing has committed early."""
            fresh = self._fresh_conn(server)
            try:
                row = fresh.execute(
                    "SELECT id FROM nodes WHERE claim = ?", (claim,),
                ).fetchone()
                mid_call_snapshot["observation_visible"] = row is not None
            finally:
                fresh.close()

        fake_embed = _FailOnTextEmbed(
            predicted_event, on_fail=_snapshot_mid_transaction,
        )
        monkeypatch.setattr(server._embedder, "embed", fake_embed)

        r = _add_predictive_observation(
            server, claim, predicted_event, "https://example.com/3348-d",
        )

        assert "error" not in r, r
        assert r.get("status") == "created", r
        assert r.get("prediction_status") == "created", r
        pred_id = r["prediction_id"]

        assert mid_call_snapshot.get("observation_visible") is False, (
            "the observation node (created earlier in the SAME transaction "
            "as the prediction) must not be visible to a separate connection "
            "while the prediction's embed is still being attempted -- "
            "nothing may commit early"
        )

        # Round 2 (cell 2 of Kepler's ruling): the prediction's own outcome
        # goes in its own "prediction_embedding_failure" key, never in
        # "embedding_failure" -- the observation's own embed succeeded (no
        # marker in `claim`), so "embedding_failure" must be ABSENT entirely.
        assert "embedding_failure" not in r, (
            f"the observation's embed succeeded here -- 'embedding_failure' "
            f"must not appear (prediction-only failures go in "
            f"'prediction_embedding_failure'), got: {r}"
        )
        assert "prediction_embedding_failure" in r, (
            f"a failed prediction embed must surface a "
            f"'prediction_embedding_failure' block naming the prediction's "
            f"node id, got: {r}"
        )
        failure_block = r["prediction_embedding_failure"]
        assert failure_block.get("status") == "failed", failure_block
        assert failure_block.get("node_id") == pred_id, (
            f"the prediction_embedding_failure block must name the "
            f"prediction's node id ({pred_id}), got: {failure_block}"
        )
        assert failure_block.get("message", "").startswith(f"{pred_id}: "), (
            f"R4: the message must be prefixed with the prediction's own "
            f"node id, got: {failure_block.get('message')!r}"
        )

        # After the call returns, a fresh connection must see everything
        # as durable -- the eventual single commit succeeded.
        fresh = self._fresh_conn(server)
        try:
            obs_row = fresh.execute(
                "SELECT id FROM nodes WHERE claim = ?", (claim,),
            ).fetchone()
            assert obs_row is not None, "observation must be durable post-call"
            pred_row = fresh.execute(
                "SELECT id, embedding FROM nodes WHERE id = ?", (pred_id,),
            ).fetchone()
            assert pred_row is not None, "prediction must be durable post-call"
            assert pred_row["embedding"] is None, (
                "the prediction's embedding must still be NULL (it stayed "
                "unembedded after the failed write-time attempt)"
            )
            flag_row = fresh.execute(
                "SELECT not_fully_restored, null_count, cause FROM "
                "embedding_restore_state WHERE singleton_key = 1"
            ).fetchone()
            assert flag_row is not None and flag_row["not_fully_restored"] == 1, (
                f"the restore flag must be set through the same path as "
                f"every other node, got: {dict(flag_row) if flag_row else None}"
            )
            # The existing (pre-#3348, unrelated) duplicate
            # `_stamp_new_node(conn, node_id, ...)` call later in
            # `_add_observation_impl` re-embeds the MAIN observation node a
            # second time in the same transaction -- its success triggers
            # Rider 3's recount-on-recovery (M2), rewriting the cause from
            # "write-embed-failed: ..." to "per-node-failures (write-embed-
            # failed recovered at <T>)" with null_count=1 (the prediction,
            # still NULL) BEFORE this call even returns. Both strings are
            # legitimate outcomes of the SAME flag-setting path every other
            # node uses (_set_embedding_restore_flag /
            # _maybe_clear_write_embed_failed_flag); the exact wording
            # depends on that pre-existing duplicate call, which #3348 does
            # not touch -- so this checks the substring, not an exact prefix.
            assert "write-embed-failed" in flag_row["cause"], flag_row["cause"]
            assert flag_row["null_count"] == 1, (
                f"exactly one node (the prediction) should still be missing "
                f"an embedding, got null_count={flag_row['null_count']}"
            )
        finally:
            fresh.close()


# ---------------------------------------------------------------------------
# Round 2 (Kepler's ruling on PR #3352, option B + rider) -- contract cells
# (1)-(4). Full contract text: Kepler's ruling comment on PR #3352.
# ---------------------------------------------------------------------------

class TestCell1ObservationFailsPredictionEmbeds(_TestBase):
    """Cell (1): observation embed fails, prediction embeds ->
    `embedding_failure` with the observation's node_id; NO
    `prediction_embedding_failure`."""

    def test_observation_fails_prediction_ok(self, monkeypatch):
        server = _fresh_server(self.tmp)
        claim = "P1 predicts outcome will occur (3348r2-cell1-faildoc)."
        predicted_event = "Outcome occurs within 14 days (3348r2-cell1)."
        _prime_fail_embedder(server, monkeypatch, "3348r2-cell1-faildoc")

        r = _add_predictive_observation(
            server, claim, predicted_event, "https://example.com/3348r2-cell1",
        )
        assert "error" not in r, r
        assert r.get("prediction_status") == "created", r
        obs_id = r["observation_id"]

        assert "prediction_embedding_failure" not in r, (
            f"the prediction's embed succeeded here -- "
            f"'prediction_embedding_failure' must be absent, got: {r}"
        )
        assert "embedding_failure" in r, (
            f"the observation's embed failed here -- 'embedding_failure' "
            f"must be present, got: {r}"
        )
        block = r["embedding_failure"]
        assert block.get("status") == "failed", block
        assert block.get("node_id") == obs_id, (
            f"embedding_failure must name the observation's own node_id "
            f"({obs_id}), got: {block}"
        )
        assert block.get("message", "").startswith(f"{obs_id}: "), (
            f"R4: message must be prefixed with the observation's own "
            f"node id, got: {block.get('message')!r}"
        )


class TestCell3BothFail(_TestBase):
    """Cell (3): both embeds fail -> both keys present, two different
    node_ids, each message self-describing (per-key, not one shared line)."""

    def test_both_observation_and_prediction_fail(self, monkeypatch):
        server = _fresh_server(self.tmp)
        claim = "P3 predicts outcome will occur (3348r2-cell3-faildoc)."
        predicted_event = "Outcome occurs within 21 days (3348r2-cell3-faildoc)."
        _prime_fail_embedder(server, monkeypatch, "3348r2-cell3-faildoc")

        r = _add_predictive_observation(
            server, claim, predicted_event, "https://example.com/3348r2-cell3",
        )
        assert "error" not in r, r
        assert r.get("prediction_status") == "created", r
        obs_id = r["observation_id"]
        pred_id = r["prediction_id"]
        assert obs_id != pred_id, r

        assert "embedding_failure" in r, r
        assert "prediction_embedding_failure" in r, r
        obs_block = r["embedding_failure"]
        pred_block = r["prediction_embedding_failure"]

        # R1: embedding_failure is ALWAYS a dict, never a list, even in the
        # both-fail case (Kepler's ruling, option B -- the shape that was
        # explicitly rejected).
        assert isinstance(obs_block, dict), (
            f"embedding_failure must stay a dict, not a list, even when "
            f"both nodes fail, got: {type(obs_block).__name__}: {obs_block!r}"
        )
        assert isinstance(pred_block, dict), pred_block

        assert obs_block.get("node_id") == obs_id, obs_block
        assert pred_block.get("node_id") == pred_id, pred_block
        assert obs_block.get("status") == "failed", obs_block
        assert pred_block.get("status") == "failed", pred_block

        # R4: two self-describing messages, one per key -- no new top-level
        # "message" line naming both.
        assert "message" not in r, (
            f"no new top-level 'message' line naming both nodes -- each "
            f"block keeps its own nested message, got top-level keys: "
            f"{list(r.keys())}"
        )
        assert obs_block.get("message", "").startswith(f"{obs_id}: "), (
            obs_block.get("message")
        )
        assert pred_block.get("message", "").startswith(f"{pred_id}: "), (
            pred_block.get("message")
        )


class TestCell4NoPredictionNodeCreated(_TestBase):
    """Cell (4): no prediction node is created THIS call (either the
    linked_to_existing path, or a plain non-predictive observation), and the
    observation embed fails -> `embedding_failure` only, with the exact key
    set {status, detail, revive_attempted_at, revive_result,
    next_revive_not_before, message, node_id}; no
    `prediction_embedding_failure`."""

    _EXPECTED_KEYS = {
        "status", "detail", "revive_attempted_at", "revive_result",
        "next_revive_not_before", "message", "node_id",
    }

    def test_linked_to_existing_observation_fails(self, monkeypatch):
        server = _fresh_server(self.tmp)
        predicted_event = "Z3 occurs within 60 days (3348r2-cell4a)."

        # First call: create the prediction cleanly (embedder healthy).
        _patch_embedder_ok(server, monkeypatch)
        r1 = _add_predictive_observation(
            server, "First predicts Z3 will happen.", predicted_event,
            "https://example.com/3348r2-cell4a-1",
        )
        assert "error" not in r1, r1
        assert r1.get("prediction_status") == "created", r1

        # Second call: SAME predicted_event -> links to the existing
        # prediction (no new _stamp_new_node call for it, so
        # _pred_emb_outcome stays None); the observation's own embed fails.
        claim2 = "Second also predicts Z3 (3348r2-cell4a-faildoc)."
        _prime_fail_embedder(server, monkeypatch, "3348r2-cell4a-faildoc")
        r2 = _add_predictive_observation(
            server, claim2, predicted_event, "https://example.com/3348r2-cell4a-2",
        )
        assert "error" not in r2, r2
        assert r2.get("prediction_status") == "linked_to_existing", r2
        obs_id = r2["observation_id"]

        assert "prediction_embedding_failure" not in r2, (
            f"no prediction node was created THIS call -- "
            f"'prediction_embedding_failure' must be absent, got: {r2}"
        )
        assert "embedding_failure" in r2, r2
        block = r2["embedding_failure"]
        assert set(block.keys()) == self._EXPECTED_KEYS, (
            f"exact key set required, got: {sorted(block.keys())}"
        )
        assert block["node_id"] == obs_id, block
        assert block["message"].startswith(f"{obs_id}: "), block["message"]

    def test_non_predictive_observation_fails(self, monkeypatch):
        server = _fresh_server(self.tmp)
        claim = "A plain non-predictive claim (3348r2-cell4b-faildoc)."
        _prime_fail_embedder(server, monkeypatch, "3348r2-cell4b-faildoc")

        r = _add_observation(
            server, claim, "https://example.com/3348r2-cell4b",
        )
        assert "error" not in r, r
        obs_id = r["observation_id"]

        assert "prediction_id" not in r, r
        assert "prediction_embedding_failure" not in r, r
        assert "embedding_failure" in r, r
        block = r["embedding_failure"]
        assert set(block.keys()) == self._EXPECTED_KEYS, (
            f"exact key set required, got: {sorted(block.keys())}"
        )
        assert block["node_id"] == obs_id, block
        assert block["message"].startswith(f"{obs_id}: "), block["message"]
