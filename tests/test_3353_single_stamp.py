"""Contract-first tests for #3353: `_add_observation_impl` must stamp each
observation node exactly ONCE, not twice.

Written BEFORE the implementation of the fix. Every test below is expected to
be RED against the unmodified base (the #3352/#3348 branch tip) --
`_add_observation_impl` calls `core._stamp_new_node` for the observation node
TWICE (once right after the evidence `cites` edge, once again after the
prediction-decomposition block), producing two "created" edit_history rows
for the observation and reassigning `_emb_outcome` to the SECOND call's
outcome. After the fix lands, only the FIRST call runs.

Coverage, matching the contract in issue #3353:

  S1a. A plain non-predictive observation write produces exactly ONE
       "created" edit_history row for the observation, and
       `_stamp_new_node` is called exactly ONCE for the observation's node
       id.
  S1b. A predictive observation write that creates a NEW prediction node
       produces exactly ONE "created" row each for the observation AND the
       prediction (and `_stamp_new_node` is called exactly once for each).

Fixture pattern mirrors tests/test_3348_prediction_stamp.py's
`_fresh_server()` / `_patch_embedder_ok()` / `_TestBase._created_rows()`
shape -- full real server + schema, not a hand-built minimal nodes table.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_server(tmp_dir):
    """Full real server wiring, embeddings left ON."""
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


def _add_observation(server, claim, url):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Non-predictive source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "attributed_analysis",
    })))
    return r


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


def _spy_on_stamp_new_node(core, monkeypatch):
    """Wrap `core._stamp_new_node` with a call-recording spy that still
    delegates to the real implementation -- mirrors the `_spy_ctor` idiom in
    tests/test_345_pr3_write_embed_revive.py. Patched on the `engram_core`
    module object itself (not a rebinding of a name already imported
    elsewhere) so every caller that does `core._stamp_new_node(...)` via a
    module-attribute lookup (e.g. engram_observation.py's
    `import engram_core as core`) picks up the spy."""
    calls = []
    real_stamp = core._stamp_new_node

    def _spy(conn, node_id, **kwargs):
        calls.append(node_id)
        return real_stamp(conn, node_id, **kwargs)

    monkeypatch.setattr(core, "_stamp_new_node", _spy)
    return calls


class _TestBase:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_test_3353_")

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

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


# ---------------------------------------------------------------------------
# S1a -- plain observation: one stamp call, one "created" row
# ---------------------------------------------------------------------------

class TestPlainObservationStampedOnce(_TestBase):

    def test_stamp_called_once_and_one_created_row(self, monkeypatch):
        server = _fresh_server(self.tmp)
        import engram_core as core
        _patch_embedder_ok(server, monkeypatch)
        stamp_calls = _spy_on_stamp_new_node(core, monkeypatch)

        r = _add_observation(
            server, "A plain observation claim (3353-plain).",
            "https://example.com/3353-plain",
        )
        assert "error" not in r, r
        obs_id = r["observation_id"]

        obs_stamp_calls = [c for c in stamp_calls if c == obs_id]
        assert len(obs_stamp_calls) == 1, (
            f"_stamp_new_node must be called exactly ONCE for the "
            f"observation node {obs_id}, got {len(obs_stamp_calls)} calls "
            f"out of all calls {stamp_calls}"
        )

        rows = self._created_rows(server, obs_id)
        assert len(rows) == 1, (
            f"expected exactly ONE 'created' edit_history row for "
            f"observation {obs_id}, got {len(rows)}: "
            f"{[dict(row) for row in rows]}"
        )


# ---------------------------------------------------------------------------
# S1b -- predictive observation (new prediction): one stamp call each, one
# "created" row each
# ---------------------------------------------------------------------------

class TestPredictiveObservationStampedOnceEach(_TestBase):

    def test_stamp_called_once_each_for_observation_and_prediction(
        self, monkeypatch,
    ):
        server = _fresh_server(self.tmp)
        import engram_core as core
        _patch_embedder_ok(server, monkeypatch)
        stamp_calls = _spy_on_stamp_new_node(core, monkeypatch)

        r = _add_predictive_observation(
            server, "A predicts B will happen (3353-pred).",
            "B occurs within 10 days (3353-pred).",
            "https://example.com/3353-pred",
        )
        assert "error" not in r, r
        assert r.get("prediction_status") == "created", r
        obs_id = r["observation_id"]
        pred_id = r["prediction_id"]
        assert obs_id != pred_id, r

        obs_stamp_calls = [c for c in stamp_calls if c == obs_id]
        pred_stamp_calls = [c for c in stamp_calls if c == pred_id]
        assert len(obs_stamp_calls) == 1, (
            f"_stamp_new_node must be called exactly ONCE for the "
            f"observation node {obs_id}, got {len(obs_stamp_calls)} calls "
            f"out of all calls {stamp_calls}"
        )
        assert len(pred_stamp_calls) == 1, (
            f"_stamp_new_node must be called exactly ONCE for the "
            f"prediction node {pred_id}, got {len(pred_stamp_calls)} calls "
            f"out of all calls {stamp_calls}"
        )

        obs_rows = self._created_rows(server, obs_id)
        pred_rows = self._created_rows(server, pred_id)
        assert len(obs_rows) == 1, (
            f"expected exactly ONE 'created' row for observation {obs_id}, "
            f"got {len(obs_rows)}: {[dict(row) for row in obs_rows]}"
        )
        assert len(pred_rows) == 1, (
            f"expected exactly ONE 'created' row for prediction {pred_id}, "
            f"got {len(pred_rows)}: {[dict(row) for row in pred_rows]}"
        )
