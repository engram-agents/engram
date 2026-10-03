"""Contract-first tests for #345 PR3 (part b): store, flag, report loudly,
revive, on a write-time embed failure.

Written BEFORE the implementation, per this repo's contract-first-tests
convention (CLAUDE.md) and Kepler's binding ruling on issue #345
("Contract-first on all of it"). Every test below is expected to be RED
against the unmodified `aleph/issue-345-pr2-retire-enabled` tip (no
_compute_and_store_embedding_with_revive, no EmbeddingManager.revive(), no
EmbeddingOutcome revive fields, _stamp_new_node discards its outcome) and
GREEN after this PR's implementation lands.

Coverage, matching the spec's B1-B5 contract (alpha-345-pr3-write-embed-
revive.md) and Kepler's three riders exactly:

  B1. _stamp_new_node RETURNS the EmbeddingOutcome.
  B2. In-process revive on a failed outcome, once per write, never
      touching the surface daemon.
      Rider 1: cooldown after a still-failing revive (EMBED_REVIVE_COOLDOWN_S).
  B3. The restore flag is set (respect_lease=True) when the outcome is
      still failed after B2, with cause carrying T/R/N.
      Rider 3: the flag is cleared on ANY later successful write (not only
      at the next sleep backfill), but ONLY when its cause starts with
      "write-embed-failed" -- never any other cause.
  B4. The tool response gains an `embedding_failure` block + a one-line
      message, ONLY when the final status is "failed". Skipped/embedded
      add nothing. (Round 2, R1: the key is `embedding_failure`, not
      `embedding` -- the latter collides with `_AGENT_STRIP_KEYS`, which
      globally strips any key literally named "embedding" from every
      @mcp.tool response.)
      Rider 2: the message names the operator's own revive (/mcp).
  B5. Fault-injected embedder, no real model -- same stub pattern as
      tests/test_3075_embedding_pipeline_restore.py and
      tests/test_prewarm_embeddings.py (monkeypatch engram_core._embedder's
      is_available/embed/_load_model; never a real SentenceTransformer).

Known environment gap on this Mac (per the spec; not chased here): this
Python build has no sqlite3.enable_load_extension, so sqlite-vec mirror
writes never engage in this process. Every test below stays on the JSON
nodes.embedding column, which is unaffected.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# conftest.py adds src/engram to sys.path.
import engram_core  # noqa: E402

# M2's hooks-reader contract cell loads BOTH hooks the same way
# tests/test_3075_c5_embedding_restore_hooks.py does -- hyphenated
# filenames, not importable via a plain `import` statement.
_SURFACE_HOOK_PATH = (
    REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
)
_SESSION_START_HOOK_PATH = (
    REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
)


def _load_surface_hook_module():
    sys.modules.pop("engram_surface_hook", None)
    spec = importlib.util.spec_from_file_location(
        "engram_surface_hook", str(_SURFACE_HOOK_PATH),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_session_start_hook_module():
    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location(
        "engram_session_start_hook", str(_SESSION_START_HOOK_PATH),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared helpers (deliberately NOT imported from test_3075_*.py -- no
# cross-test-file import convention exists in this repo; duplicated
# locally, same as that file's own _fresh_core_in_tmp precedent).
# ---------------------------------------------------------------------------

_NODES_SCHEMA = """
    CREATE TABLE nodes (
        id                TEXT PRIMARY KEY,
        type              TEXT,
        claim             TEXT,
        source_title      TEXT,
        predicted_event   TEXT,
        recall_summary    TEXT,
        recall_keywords   TEXT,
        embedding         TEXT,
        is_current        INTEGER DEFAULT 1,
        confidence        REAL,
        importance_base   REAL,
        importance_score  REAL,
        recall_turn       INTEGER
    )
"""


def _make_min_nodes_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(_NODES_SCHEMA)
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, source_title, predicted_event, "
            "recall_summary, recall_keywords, embedding, is_current, confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                r["id"],
                r.get("type", "observation"),
                r.get("claim", "some claim text"),
                r.get("source_title"),
                r.get("predicted_event"),
                r.get("recall_summary"),
                r.get("recall_keywords"),
                r.get("embedding"),
                r.get("is_current", 1),
                r.get("confidence", 0.5),
            ),
        )
    conn.commit()
    conn.close()


def _open_row_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


class _FaultyEmbed:
    """Fault-injected `embed()` replacement -- no real model (B5).

    `fail_times=None, persistent=True` -> every call fails (returns None).
    `fail_times=N` -> the first N calls fail, every call after succeeds.
    """

    def __init__(self, *, persistent: bool = False, fail_times: int = 0):
        self.persistent = persistent
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, text, model_name):
        self.calls += 1
        if self.persistent:
            return None
        if self.calls <= self.fail_times:
            return None
        return [0.1] * 384


class _FrozenClock:
    """Swaps in for engram_core.datetime inside
    _compute_and_store_embedding_with_revive -- only `.now()` and
    `.fromisoformat()` are used by that function, both forwarded here so
    the cooldown math can be driven deterministically without sleeping.
    """

    def __init__(self, start: datetime):
        self.current = start

    def now(self, tz=None):
        return self.current

    def fromisoformat(self, s):
        return datetime.fromisoformat(s)

    def advance(self, seconds: float):
        self.current = self.current + timedelta(seconds=seconds)


@pytest.fixture
def frozen_clock(monkeypatch):
    clock = _FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(engram_core, "datetime", clock)
    return clock


def _patch_embedder_common(monkeypatch, model="test-model"):
    """Shared monkeypatch setup: config + is_available + no real reload.

    `_load_model` AND `_load_model_local_only` (round 4, M4 -- the latter
    is revive()'s OWN reload path as of this round, no longer `_load_model`
    itself) are both patched to a no-op so revive() never attempts a real
    sentence-transformers import/download (B5: no real model) -- the
    recovered/still-failing outcome is driven entirely by the `embed`
    fake's own call-counter, never by either method's actual effect on
    `self._model`. (TestM4ReviveLocalFilesOnlyNoOnlineFallback below
    deliberately does NOT use this helper -- it exercises the real
    _load_model_local_only codepath on purpose.)
    """
    monkeypatch.setattr(
        engram_core, "_get_embedding_config", lambda: {"model": model},
    )
    monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
    monkeypatch.setattr(engram_core._embedder, "_load_model", lambda name: None)
    monkeypatch.setattr(
        engram_core._embedder, "_load_model_local_only", lambda name: None,
    )
    # Every test starts from a clean slate on the shared singleton --
    # _embedder is a module-level object, so a prior test's cooldown/
    # failed-model state must not leak across tests.
    engram_core._embedder._revive_cooldown.clear()
    engram_core._embedder._failed_models.clear()
    engram_core._embedder._model = None
    engram_core._embedder._model_name = None


class _TestBase:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_345_pr3_"))
        self.db_path = self.tmp / "fixture.db"

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self, rows):
        _make_min_nodes_db(self.db_path, rows)
        return _open_row_conn(self.db_path)

    def _embedding_of(self, conn, node_id):
        return conn.execute(
            "SELECT embedding FROM nodes WHERE id=?", (node_id,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# B1 -- _stamp_new_node returns the EmbeddingOutcome
# ---------------------------------------------------------------------------

class TestB1StampNewNodeReturnsOutcome(_TestBase):

    def test_returns_embedded_outcome_on_success(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome is not None, (
            "_stamp_new_node must RETURN the EmbeddingOutcome (B1), not "
            "discard it as bare None"
        )
        assert outcome.status == "embedded"
        assert outcome.node_id == "n1"
        conn.close()

    def test_returns_failed_outcome_on_persistent_fault(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "failed"
        conn.close()


# ---------------------------------------------------------------------------
# R2 (round 2) -- failure STAGE: only "embed" may revive/cooldown/claim
# "embedder down". "text"/"store" stage failures are not an embedder-
# health signal.
# ---------------------------------------------------------------------------

class _FailingUpdateConn:
    """Wraps a real sqlite3 connection; the embedding-column UPDATE raises
    sqlite3.OperationalError("database is locked") every time, letting
    every OTHER statement (the importance UPDATE, the edit_history INSERT,
    any SELECT) pass through to the real connection untouched -- isolates
    the "store" stage (R2) without touching the embedder at all."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.update_calls = 0

    def execute(self, sql, params=()):
        if sql.strip().startswith("UPDATE nodes SET embedding"):
            self.update_calls += 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestR2FailureStage(_TestBase):

    def test_store_stage_failure_no_revive_no_embedder_down_message(
        self, monkeypatch,
    ):
        """An UPDATE that raises sqlite3.OperationalError (R2's named
        example, "database is locked") must: NOT call revive(), NOT
        mention "embedder down" in the message, and flag the restore
        cause with the "store" stage -- the vector computed fine; only
        persisting it failed."""
        real_conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        revive_calls = []
        monkeypatch.setattr(
            engram_core._embedder, "revive",
            lambda model_name: revive_calls.append(model_name),
        )

        wrapped = _FailingUpdateConn(real_conn)
        outcome = engram_core._stamp_new_node(wrapped, "n1")

        assert outcome.status == "failed", outcome
        assert outcome.stage == "store", outcome
        assert revive_calls == [], (
            f"a store-stage failure must never call revive() -- the "
            f"embedder is not implicated, got calls: {revive_calls}"
        )
        assert outcome.revive_result is None, outcome
        assert outcome.revive_attempted_at is None, outcome

        block = engram_core._embedding_response_block(outcome)
        assert block is not None
        assert "embedder down" not in block["message"], block
        assert "store error" in block["message"], block

        flag = engram_core._get_embedding_restore_flag(real_conn)
        assert flag["cause"].startswith("write-embed-failed: store error:"), flag
        real_conn.close()


# ---------------------------------------------------------------------------
# B2 / B5(i,iv) -- in-process revive, once per write
# ---------------------------------------------------------------------------

class TestB2Revive(_TestBase):

    def test_b5_i_persistent_fault_leaves_node_unembedded(self, monkeypatch):
        """B5 (i): a persistent fault leaves the node persisted with a
        NULL embedding -- the write itself still succeeds (no rollback)."""
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "failed"
        assert self._embedding_of(conn, "n1") is None
        # The node row itself is still there and still stamped (the write
        # succeeded) -- not rolled back.
        row = conn.execute(
            "SELECT importance_base FROM nodes WHERE id='n1'"
        ).fetchone()
        assert row is not None and row[0] is not None
        conn.close()

    def test_b5_iv_one_shot_fault_recovers(self, monkeypatch):
        """B5 (iv): a one-shot fault (first load fails, reload works)
        gives revive_result == 'recovered' and the embedding present."""
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        fake = _FaultyEmbed(fail_times=1)
        monkeypatch.setattr(engram_core._embedder, "embed", fake)
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "embedded", outcome
        assert outcome.revive_result == "recovered", outcome
        assert outcome.revive_attempted_at is not None
        assert self._embedding_of(conn, "n1") is not None
        # Exactly one retry beyond the initial failing attempt.
        assert fake.calls == 2, fake.calls
        conn.close()

    def test_revive_never_touches_surface_daemon(self, monkeypatch):
        """B2: revive is IN-PROCESS ONLY -- no socket/pid/kill/restart
        action on any other process. There is no daemon-facing API
        reachable from this module at all; this test documents the
        invariant by asserting revive()'s only observable side effects
        are on the EmbeddingManager's own in-process state."""
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        before = set(dir(engram_core._embedder))
        engram_core._stamp_new_node(conn, "n1")
        after = set(dir(engram_core._embedder))
        assert before == after, "revive() must not add new attributes/methods"
        conn.close()


# ---------------------------------------------------------------------------
# B3 / B5(ii,iii) -- flag set on a still-failed outcome
# ---------------------------------------------------------------------------

class TestB3Flag(_TestBase):

    def test_b5_iii_flag_cause_contains_t_and_r(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["cause"].startswith("write-embed-failed:"), flag
        assert outcome.revive_attempted_at in flag["cause"], flag
        assert outcome.revive_result in flag["cause"], flag
        assert flag["not_fully_restored"] is True
        conn.close()

    def test_lease_row_not_overwritten_by_failing_write(self, monkeypatch):
        """B5: a lease row (regeneration-running) is not overwritten by a
        failing write -- respect_lease=True must hold the WHERE guard."""
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        acquired = engram_core._try_acquire_regeneration_lease(conn, null_count=3)
        assert acquired is True

        engram_core._stamp_new_node(conn, "n1")

        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["cause"] == "regeneration-running", (
            f"a failing write must not clobber a fresh regeneration lease, got {flag}"
        )
        conn.close()

    def test_skipped_outcome_adds_no_embedding_key_or_flag_write(self, monkeypatch):
        """B5: a skipped outcome (ENGRAM_NO_EMBEDDINGS / empty text) adds
        no `embedding` key and does not touch the restore flag at all."""
        conn = self._db([{"id": "n1", "claim": None, "type": "evidence",
                           "source_title": None}])
        _patch_embedder_common(monkeypatch)
        embed_mock = _FaultyEmbed(persistent=True)
        monkeypatch.setattr(engram_core._embedder, "embed", embed_mock)
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "skipped"
        assert engram_core._embedding_response_block(outcome) is None
        assert embed_mock.calls == 0, "embed() must not be called for empty text"
        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["cause"] is None and flag["not_fully_restored"] is False
        conn.close()


# ---------------------------------------------------------------------------
# B4 / Rider 2 -- the response block + message
# ---------------------------------------------------------------------------

class TestB4ResponseBlock(_TestBase):

    def test_failed_outcome_gets_response_block(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")
        block = engram_core._embedding_response_block(outcome)

        assert block is not None
        for key in ("status", "detail", "revive_attempted_at",
                    "revive_result", "next_revive_not_before", "message"):
            assert key in block, f"missing {key} in {block}"
        assert block["status"] == "failed"
        conn.close()

    def test_rider2_message_contains_mcp_revive_instruction(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")
        block = engram_core._embedding_response_block(outcome)

        assert "/mcp" in block["message"], block["message"]
        assert "backfill" in block["message"].lower(), block["message"]
        conn.close()

    def test_embedded_outcome_adds_no_block(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert engram_core._embedding_response_block(outcome) is None
        conn.close()

    def test_recovered_outcome_adds_no_block(self, monkeypatch):
        """A recovered (one-shot-fault) outcome still ends at status ==
        'embedded' -- B4 says ONLY a still-failed outcome gets the block."""
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(fail_times=1),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "embedded"
        assert engram_core._embedding_response_block(outcome) is None
        conn.close()


# ---------------------------------------------------------------------------
# Rider 1 -- cooldown
# ---------------------------------------------------------------------------

class TestRider1Cooldown(_TestBase):

    def test_two_failing_writes_within_cooldown_revive_once(
        self, monkeypatch, frozen_clock,
    ):
        conn = self._db([{"id": "n1"}, {"id": "n2"}])
        _patch_embedder_common(monkeypatch)
        fake = _FaultyEmbed(persistent=True)
        monkeypatch.setattr(engram_core._embedder, "embed", fake)
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome1 = engram_core._stamp_new_node(conn, "n1")
        assert outcome1.revive_result == "still-failing", outcome1
        calls_after_first_write = fake.calls  # 1 initial + 1 retry = 2

        frozen_clock.advance(5)  # well within EMBED_REVIVE_COOLDOWN_S
        outcome2 = engram_core._stamp_new_node(conn, "n2")

        assert outcome2.revive_result == "cooldown", outcome2
        assert outcome2.next_revive_not_before == outcome1.next_revive_not_before
        # Second write's failing attempt itself still calls embed() once
        # (the initial try), but must NOT trigger a second reload/retry.
        assert fake.calls == calls_after_first_write + 1, (
            f"expected exactly one extra embed() call (the initial try, "
            f"no retry) during cooldown, got {fake.calls - calls_after_first_write}"
        )
        conn.close()

    def test_write_after_cooldown_revives_again(self, monkeypatch, frozen_clock):
        conn = self._db([{"id": "n1"}, {"id": "n2"}])
        _patch_embedder_common(monkeypatch)
        fake = _FaultyEmbed(persistent=True)
        monkeypatch.setattr(engram_core._embedder, "embed", fake)
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome1 = engram_core._stamp_new_node(conn, "n1")
        assert outcome1.revive_result == "still-failing"
        calls_after_first_write = fake.calls

        frozen_clock.advance(engram_core.EMBED_REVIVE_COOLDOWN_S + 1)
        outcome2 = engram_core._stamp_new_node(conn, "n2")

        assert outcome2.revive_result == "still-failing", (
            "cooldown expired -- a new revive must be attempted, not 'cooldown'"
        )
        assert fake.calls == calls_after_first_write + 2, (
            "expected a fresh initial-try + retry pair after cooldown expiry"
        )
        conn.close()


# ---------------------------------------------------------------------------
# Rider 3 -- clear on recovery
# ---------------------------------------------------------------------------

class TestRider3ClearOnRecovery(_TestBase):

    def test_flag_cleared_by_later_recovered_write(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        engram_core._set_embedding_restore_flag(
            conn, null_count=2,
            cause="write-embed-failed: revive T -> still-failing; next revive not before N",
        )

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "embedded"
        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["cause"] is None and flag["not_fully_restored"] is False, flag

    def test_per_node_failures_cause_not_cleared_by_successful_write(
        self, monkeypatch,
    ):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        engram_core._set_embedding_restore_flag(
            conn, null_count=1, cause="per-node-failures",
        )

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "embedded"
        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["cause"] == "per-node-failures", (
            f"a successful write must NEVER clear a non-write-embed-failed "
            f"cause, got {flag}"
        )


# ---------------------------------------------------------------------------
# End-to-end wiring: one representative real tool-impl call site
# ---------------------------------------------------------------------------

class TestEndToEndOneCallSite:
    """Exercises _add_evidence_impl (engram_observation.py:980, the
    simplest single-stamp B1 call site) through the real server wiring,
    to prove the response-propagation pattern (B4) actually reaches a
    real tool's JSON response -- not just the engine-level contract
    tested above. Uses fresh_server()'s own established pattern (see
    tests/test_add_evidence_payload.py) rather than hand-building a full
    schema, since the durability read-back and other call-site plumbing
    depend on the real schema.
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_345_pr3_e2e_"))

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fresh_server(self):
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        import server
        server._configure_paths(str(self.tmp))
        server._ensure_data_dir()
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        return server

    def test_add_evidence_propagates_failed_embedding_into_response(
        self, monkeypatch,
    ):
        server = self._fresh_server()
        monkeypatch.setattr(server._embedder, "is_available", lambda: True)
        monkeypatch.setattr(server._embedder, "_load_model", lambda name: None)
        monkeypatch.setattr(
            server._embedder, "_load_model_local_only", lambda name: None,
        )
        server._embedder._revive_cooldown.clear()
        server._embedder._failed_models.clear()
        monkeypatch.setattr(
            server._embedder, "embed", _FaultyEmbed(persistent=True),
        )

        test_file = self.tmp / "evidence_source.md"
        test_file.write_text("Test source content.\n")
        payload = {"url": f"file://{test_file}", "title": "Test source"}

        r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))

        assert r.get("status") == "created", r
        assert "embedding_failure" in r, (
            f"a failed write-time embed must surface an 'embedding_failure' "
            f"block on the tool response (B4; R1 renamed the key from "
            f"'embedding' to avoid the _AGENT_STRIP_KEYS collision), got {r}"
        )
        assert r["embedding_failure"]["status"] == "failed"
        assert "/mcp" in r["embedding_failure"]["message"]

    def test_add_evidence_embedded_outcome_adds_no_embedding_key(
        self, monkeypatch,
    ):
        server = self._fresh_server()
        monkeypatch.setattr(server._embedder, "is_available", lambda: True)
        monkeypatch.setattr(server._embedder, "_load_model", lambda name: None)
        monkeypatch.setattr(
            server._embedder, "_load_model_local_only", lambda name: None,
        )
        server._embedder._revive_cooldown.clear()
        server._embedder._failed_models.clear()
        monkeypatch.setattr(server._embedder, "embed", _FaultyEmbed())

        test_file = self.tmp / "evidence_source2.md"
        test_file.write_text("Test source content 2.\n")
        payload = {"url": f"file://{test_file}", "title": "Test source 2"}

        r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))

        assert r.get("status") == "created", r
        assert "embedding_failure" not in r, r


# ---------------------------------------------------------------------------
# R1 (round 2): response-key collision with _AGENT_STRIP_KEYS
# ---------------------------------------------------------------------------

class TestR1ResponseKeyCollision:
    """_strip_agent_facing (engram_core._AGENT_STRIP_KEYS) removes every key
    literally named "embedding" from an @mcp.tool response -- including
    recursively, from every nested dict/list. engram_add_observation_batch
    calls _strip_agent_facing on its WHOLE batch_result (including the
    per-observation `results` list), so a B4 block still named "embedding"
    inside one item is silently discarded at the batch level even though
    the single-observation tool's own (unrelated) strip-then-add ordering
    survives it. R1 renames the key to "embedding_failure", which is not in
    _AGENT_STRIP_KEYS, so it survives any strip at any nesting depth.

    Mutation (per spec): renaming the key back to "embedding" at the
    _embedding_response_block call sites turns (a) red (the batch item's
    block disappears) while leaving every other PR3 test green.
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_345_pr3_r1_"))

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fresh_server(self):
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        import server
        server._configure_paths(str(self.tmp))
        server._ensure_data_dir()
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
        return server

    def test_batch_tool_propagates_embedding_failure_for_failed_item(
        self, monkeypatch,
    ):
        """(a) engram_add_observation_batch, with one observation whose
        write-time embed fails persistently, still returns that item's
        'embedding_failure' block -- not silently dropped by the batch's
        own outer _strip_agent_facing(batch_result) call."""
        server = self._fresh_server()
        monkeypatch.setattr(server._embedder, "is_available", lambda: True)
        monkeypatch.setattr(server._embedder, "_load_model", lambda name: None)
        monkeypatch.setattr(
            server._embedder, "_load_model_local_only", lambda name: None,
        )
        server._embedder._revive_cooldown.clear()
        server._embedder._failed_models.clear()
        monkeypatch.setattr(
            server._embedder, "embed", _FaultyEmbed(persistent=True),
        )

        test_file = self.tmp / "batch_source.md"
        test_file.write_text("Batch quote text appears verbatim here.\n")
        observations = [{
            "quoted_text": "Batch quote text appears verbatim here.",
            "interpretation": "Batch interpretation.",
            "claim": "A batch claim for the R1 collision test.",
            "quote_type": "hard_data",
        }]
        payload = {
            "url": f"file://{test_file}",
            "title": "Batch test source",
            "observations_json": json.dumps(observations),
        }

        r = json.loads(
            server.engram_add_observation_batch(payload_json=json.dumps(payload))
        )

        assert r.get("status") == "batch_complete", r
        assert r.get("created") == 1, r
        item = r["results"][0]
        assert item.get("status") == "created", item
        assert "embedding_failure" in item, (
            f"a failed write-time embed on a batch item must still surface "
            f"its 'embedding_failure' block after the batch's own outer "
            f"_strip_agent_facing(batch_result) call, got {item}"
        )
        assert item["embedding_failure"]["status"] == "failed"
        assert "/mcp" in item["embedding_failure"]["message"]

    def test_strip_agent_facing_leaves_embedding_failure_key_untouched(self):
        """(b) guard test: a dict containing an 'embedding_failure' block
        survives _strip_agent_facing unchanged, at any nesting depth --
        only the literal 'embedding' key (raw vector payload) is stripped."""
        import engram_core as core

        obj = {
            "status": "created",
            "embedding": [0.1, 0.2, 0.3],  # raw vector payload -- stripped
            "embedding_failure": {         # B4 block -- must survive
                "status": "failed",
                "detail": "embed() returned empty/None",
                "revive_attempted_at": "2026-10-03T00:00:00+00:00",
                "revive_result": "still-failing",
                "next_revive_not_before": "2026-10-03T00:05:00+00:00",
                "message": "embedder down ... reconnect the MCP server (/mcp)",
            },
            "results": [
                {"embedding": [0.4], "embedding_failure": {"status": "failed"}},
            ],
        }

        out = core._strip_agent_facing(obj)

        assert "embedding" not in out, out
        assert "embedding_failure" in out, out
        assert out["embedding_failure"] == obj["embedding_failure"]
        assert "embedding" not in out["results"][0], out
        assert "embedding_failure" in out["results"][0], out


# ---------------------------------------------------------------------------
# K2 (round 3, Kepler's rider): each failure stage is a contract cell --
# revive-called?, cooldown-recorded?, flag-cause-names-the-stage, and
# message-tone must all vary correctly together, parametrized across all
# three stages in one place (round 2 only directed "store" explicitly).
# ---------------------------------------------------------------------------

class TestK2StageContractCells(_TestBase):
    """One parametrized test per stage (embed/store/text), asserting the
    full stage-contract cell in one place."""

    @pytest.mark.parametrize("stage", ["embed", "store", "text"])
    def test_stage_contract_cell(self, monkeypatch, stage):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        revive_calls = []
        monkeypatch.setattr(
            engram_core._embedder, "revive",
            lambda model_name: revive_calls.append(model_name),
        )

        call_conn = conn
        if stage == "embed":
            monkeypatch.setattr(
                engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
            )
        elif stage == "store":
            monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
            call_conn = _FailingUpdateConn(conn)
        elif stage == "text":
            monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
            original_text_fn = engram_core._embedding_text_for_node
            calls = {"n": 0}

            def _text_fn(c, node_id):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ValueError("bad text extraction")
                return original_text_fn(c, node_id)

            monkeypatch.setattr(engram_core, "_embedding_text_for_node", _text_fn)

        outcome = engram_core._stamp_new_node(call_conn, "n1")

        assert outcome.status == "failed", outcome
        assert outcome.stage == stage, outcome

        if stage == "embed":
            assert revive_calls == ["test-model"], (
                f"an embed-stage failure MUST call revive() exactly once, "
                f"got {revive_calls}"
            )
            assert outcome.revive_result == "still-failing", outcome
            assert outcome.next_revive_not_before is not None, outcome
        else:
            assert revive_calls == [], (
                f"a {stage}-stage failure must NEVER call revive() -- the "
                f"embedder is not implicated, got calls: {revive_calls}"
            )
            assert outcome.revive_result is None, outcome
            assert outcome.next_revive_not_before is None, outcome

        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["cause"].startswith("write-embed-failed:"), flag
        if stage == "embed":
            assert "revive" in flag["cause"], flag
        else:
            assert f"{stage} error" in flag["cause"], flag

        block = engram_core._embedding_response_block(outcome)
        assert block is not None
        if stage == "embed":
            assert "embedder down" in block["message"], block
            assert "/mcp" in block["message"], block
        else:
            assert "embedder down" not in block["message"], (
                f"a {stage}-stage failure must never claim the embedder is "
                f"down -- got {block['message']!r}"
            )
            assert "/mcp" not in block["message"], block
            assert stage in block["message"], (
                f"a {stage}-stage failure's message must name its own "
                f"stage -- got {block['message']!r}"
            )
        conn.close()


# ---------------------------------------------------------------------------
# R-B1 (round 3, BLOCKER, reviewer comment 5972004666): the restore-flag
# helpers each call conn.commit() unconditionally, including when called
# from inside _stamp_new_node's caller's own open write transaction -- so a
# failed embed (or a clear-on-recovery) silently committed the caller's
# ENTIRE pending transaction, not just the flag row. Fixed by a `commit`
# kwarg on both helpers, defaulting True (every pre-existing caller
# unchanged), with the write-path call sites passing commit=False.
# ---------------------------------------------------------------------------

class TestRB1NoEarlyCommit(_TestBase):

    def _flag_cause_via_fresh_conn(self):
        """Read the flag's cause through a SEPARATE sqlite3 connection to
        the same file -- the only way to observe what is actually DURABLE,
        independent of what `conn` itself (mid-transaction) can see of its
        own uncommitted writes. Returns None if the row (or even the
        table) isn't durable yet -- both are valid "not visible" states,
        since the table itself is created inside the same transaction on
        a DB that never had this flag before."""
        fresh = sqlite3.connect(str(self.db_path))
        try:
            row = fresh.execute(
                "SELECT cause FROM embedding_restore_state WHERE singleton_key=1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        fresh.close()
        return row[0] if row else None

    def _node_ids_via_fresh_conn(self):
        fresh = sqlite3.connect(str(self.db_path))
        ids = {r[0] for r in fresh.execute("SELECT id FROM nodes").fetchall()}
        fresh.close()
        return ids

    def test_failed_write_does_not_commit_callers_transaction(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        conn.execute(
            "INSERT INTO nodes (id, type, claim, is_current, confidence) "
            "VALUES ('n2_pending', 'observation', 'pending claim', 1, 0.5)"
        )
        assert conn.in_transaction is True

        outcome = engram_core._stamp_new_node(conn, "n1")
        assert outcome.status == "failed", outcome

        assert conn.in_transaction is True, (
            "a failed write-time embed must NOT commit the caller's open "
            "transaction (R-B1) -- only the caller's own conn.commit() may"
        )
        assert "n2_pending" not in self._node_ids_via_fresh_conn(), (
            "the caller's uncommitted pending row must not be durable "
            "before the caller's own commit"
        )
        assert self._flag_cause_via_fresh_conn() is None, (
            "the restore-flag write must not be durable before the "
            "caller's own commit either"
        )

        conn.commit()

        ids = self._node_ids_via_fresh_conn()
        assert {"n1", "n2_pending"} <= ids, ids
        cause = self._flag_cause_via_fresh_conn()
        assert cause is not None and cause.startswith("write-embed-failed"), cause
        conn.close()

    def test_failed_write_rollback_leaves_nothing_durable(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        conn.execute(
            "INSERT INTO nodes (id, type, claim, is_current, confidence) "
            "VALUES ('n2_pending', 'observation', 'pending claim', 1, 0.5)"
        )
        engram_core._stamp_new_node(conn, "n1")
        conn.rollback()

        assert "n2_pending" not in self._node_ids_via_fresh_conn()
        assert self._flag_cause_via_fresh_conn() is None, (
            "after a rollback, neither the node nor the flag may be durable"
        )
        conn.close()

    def test_recovery_clear_does_not_commit_callers_transaction(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        # Pre-existing write-embed-failed flag, durably committed on its
        # own (this call keeps the default commit=True -- it is NOT inside
        # any caller transaction, matching every pre-existing caller).
        engram_core._set_embedding_restore_flag(
            conn, null_count=2,
            cause="write-embed-failed: revive T -> still-failing; next revive not before N",
        )
        assert conn.in_transaction is False

        # NULL claim (not "pending claim") -- round 4 (M2) RECOUNTS on a
        # recovered write, and _embedding_text_for_node treats an empty
        # claim as no embeddable text (same rule _count_missing_
        # embeddings already applies), so this pending row does NOT
        # contribute to the recount. That keeps this test's ORIGINAL
        # purpose (atomicity of the flag write, not M2's own recount
        # logic, which has its own dedicated contract-cell tests below)
        # intact under the new Rider 3 behaviour.
        conn.execute(
            "INSERT INTO nodes (id, type, claim, is_current, confidence) "
            "VALUES ('n2_pending', 'observation', NULL, 1, 0.5)"
        )
        assert conn.in_transaction is True

        outcome = engram_core._stamp_new_node(conn, "n1")
        assert outcome.status == "embedded", outcome

        assert conn.in_transaction is True, (
            "a recovery flag-CLEAR must not commit the caller's open "
            "transaction either (R-B1)"
        )
        assert "n2_pending" not in self._node_ids_via_fresh_conn()
        cause_before_commit = self._flag_cause_via_fresh_conn()
        assert cause_before_commit is not None and \
            cause_before_commit.startswith("write-embed-failed"), (
            f"the OLD flag must still read as set to a separate connection "
            f"until the caller commits, got {cause_before_commit!r}"
        )

        conn.commit()

        assert "n2_pending" in self._node_ids_via_fresh_conn()
        assert self._flag_cause_via_fresh_conn() is None, (
            "after the caller commits, the recovery clear must be visible too"
        )
        conn.close()

    def test_recovery_clear_rollback_leaves_nothing_durable(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        engram_core._set_embedding_restore_flag(
            conn, null_count=2,
            cause="write-embed-failed: revive T -> still-failing; next revive not before N",
        )
        conn.execute(
            "INSERT INTO nodes (id, type, claim, is_current, confidence) "
            "VALUES ('n2_pending', 'observation', 'pending claim', 1, 0.5)"
        )
        engram_core._stamp_new_node(conn, "n1")
        conn.rollback()

        assert "n2_pending" not in self._node_ids_via_fresh_conn()
        cause_after_rollback = self._flag_cause_via_fresh_conn()
        assert cause_after_rollback is not None and \
            cause_after_rollback.startswith("write-embed-failed"), (
            f"a rollback must revert the clear too -- the ORIGINAL "
            f"still-failing flag must still read as set, got "
            f"{cause_after_rollback!r}"
        )
        conn.close()


# ---------------------------------------------------------------------------
# R-S2 (round 3, suggestion): the vec-mirror block must tolerate catching
# ANY exception, not only sqlite3.OperationalError -- a non-OperationalError
# raised by the vec backend (e.g. _sqlite_vec.serialize_float32) must still
# degrade to vec_mirror_ok=False / status="embedded" rather than falling to
# the outer catch-all (which defaults stage="embed" and wrongly implies the
# embedder itself, not the vec mirror, is unhealthy).
# ---------------------------------------------------------------------------

class _RaisingSqliteVec:
    """Fake `_sqlite_vec` module stand-in: `serialize_float32` raises a
    non-OperationalError exception (ValueError, per the spec's own named
    example), to prove the vec-mirror catch widens beyond
    sqlite3.OperationalError."""

    @staticmethod
    def serialize_float32(vector):
        raise ValueError("fake vec serialization failure")


class TestRS2VecMirrorCatchesAnyException(_TestBase):

    def test_non_operational_error_in_vec_mirror_still_embeds(self, monkeypatch):
        conn = self._db([{"id": "n1"}])
        # A plain stand-in for the real vec0 virtual table -- MUST exist so
        # the DELETE statement succeeds and execution actually reaches the
        # INSERT (where serialize_float32 is called). Without this table
        # the DELETE itself raises sqlite3.OperationalError ("no such
        # table: vec_nodes"), which the EXISTING narrow catch already
        # handles -- that would mask the very path this test exists to
        # exercise, giving a false pass for the wrong reason.
        conn.execute("CREATE TABLE vec_nodes (node_id TEXT PRIMARY KEY, embedding BLOB)")
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(),
        )  # first call succeeds -> a real 384-dim vector
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)
        monkeypatch.setattr(engram_core, "_VEC_BACKEND_AVAILABLE", True)
        monkeypatch.setattr(engram_core, "_sqlite_vec", _RaisingSqliteVec())

        revive_calls = []
        monkeypatch.setattr(
            engram_core._embedder, "revive",
            lambda model_name: revive_calls.append(model_name),
        )

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "embedded", (
            f"a non-OperationalError vec-mirror failure must still be a "
            f"TOLERATED degradation (status='embedded'), got {outcome}"
        )
        assert outcome.vec_mirror_ok is False, outcome
        assert revive_calls == [], (
            f"a vec-mirror-only failure must never trigger a revive -- "
            f"the embedder and the canonical write both succeeded, got "
            f"{revive_calls}"
        )
        # The canonical JSON column is still written -- the vec mirror is
        # a best-effort secondary index, not the source of truth.
        assert self._embedding_of(conn, "n1") is not None
        conn.close()


# ---------------------------------------------------------------------------
# ROUND 4, M1 (BLOCKER, Mira's security read on #3343): a PERSISTENT
# _embedding_text_for_node fault escapes the write wrapper entirely.
# _count_missing_embeddings re-runs the SAME always-raising function over
# every NULL-embedding node -- including the one THIS write just failed on
# -- while the wrapper is building the restore flag's null_count, so the
# exception propagates out of _stamp_new_node: no flag is ever set, nothing
# is reported, and the caller sees a raw exception instead of a flagged,
# stored-unembedded node. The existing K2 parametrized "text" stage test
# (TestK2StageContractCells) uses a ONE-SHOT fault (only the first call
# raises; the SECOND call, made by _count_missing_embeddings' own scan,
# succeeds) -- it stays exactly as it is and does not exercise this bug at
# all. This class adds the PERSISTENT case Mira actually reproduced.
# ---------------------------------------------------------------------------

class TestM1NonThrowingNullCountOnPersistentTextFault(_TestBase):

    def test_persistent_text_fault_does_not_escape_and_still_flags(
        self, monkeypatch,
    ):
        conn = self._db([{"id": "n1"}])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        def _always_raise(c, node_id):
            raise ValueError("persistent text extraction failure")

        monkeypatch.setattr(engram_core, "_embedding_text_for_node", _always_raise)

        # Must return normally -- this IS the bug: the unmodified wrapper
        # lets the second internal call (inside _count_missing_embeddings)
        # raise straight out of _stamp_new_node.
        outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "failed", outcome
        assert outcome.stage == "text", outcome

        # The node row exists with a NULL embedding -- the write itself
        # was never rolled back by the count-path's own failure.
        row = conn.execute(
            "SELECT embedding FROM nodes WHERE id='n1'"
        ).fetchone()
        assert row is not None, "the node row must still exist"
        assert row[0] is None, "the embedding column must stay NULL"

        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["not_fully_restored"] is True, flag
        assert flag["cause"] is not None, flag
        assert flag["cause"].startswith("write-embed-failed: text error:"), flag

        block = engram_core._embedding_response_block(outcome)
        assert block is not None, (
            "a persistent text-stage fault must still surface an "
            "embedding_failure response block, not be silently dropped"
        )
        conn.close()


# ---------------------------------------------------------------------------
# ROUND 4, M3 (Mira's own regression on her round-3 R-S2 WARNING): on a
# seat where sqlite-vec IS actually available but the test's own minimal
# fixture has no vec_nodes table (the long-tolerated #3075 contract), R-S2's
# widened `except Exception` logs a WARNING for a plain missing-table
# OperationalError too -- turning two previously-silent #3075 tests red.
# Splits the catch back into sqlite3.OperationalError (silent, old
# contract) vs any OTHER exception (WARNING, R-S2's own fix, preserved).
# ---------------------------------------------------------------------------

class _SilentStubVec:
    """Fake `_sqlite_vec` stand-in whose serialize_float32 never raises --
    isolates the OperationalError-from-a-missing-table path (M3 case a)
    without also exercising R-S2's non-OperationalError path."""

    @staticmethod
    def serialize_float32(vector):
        return b"stub-blob"


class TestM3VecMirrorCatchSplitByExceptionType(_TestBase):

    def test_a_missing_vec_nodes_table_no_warning(self, monkeypatch, caplog):
        """Simulates sqlite-vec being available (per the spec: monkeypatch
        _VEC_BACKEND_AVAILABLE=True + a stub _sqlite_vec) with NO vec_nodes
        table at all -- exactly the #3075 minimal-fixture gap Mira found on
        a vec-capable Linux seat. Must stay SILENT (no WARNING) and still
        report 'embedded' -- the pre-R-S2, pre-M3 contract for this exact
        exception type."""
        conn = self._db([{"id": "n1"}])
        # Deliberately NO `CREATE TABLE vec_nodes`.
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)
        monkeypatch.setattr(engram_core, "_VEC_BACKEND_AVAILABLE", True)
        monkeypatch.setattr(engram_core, "_sqlite_vec", _SilentStubVec())

        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "embedded", outcome
        assert outcome.vec_mirror_ok is False, outcome
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == [], (
            "a missing vec_nodes table (sqlite3.OperationalError) must stay "
            "SILENT -- the long-tolerated #3075 contract, not a new WARNING"
        )
        assert self._embedding_of(conn, "n1") is not None
        conn.close()

    def test_b_serialize_raising_value_error_one_warning(self, monkeypatch, caplog):
        """The R-S2 case (non-OperationalError from the vec backend) must
        still log exactly ONE WARNING and still report 'embedded' -- this
        is the behaviour M3 must NOT regress while fixing case (a)."""
        conn = self._db([{"id": "n1"}])
        conn.execute(
            "CREATE TABLE vec_nodes (node_id TEXT PRIMARY KEY, embedding BLOB)"
        )
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(engram_core._embedder, "embed", _FaultyEmbed())
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)
        monkeypatch.setattr(engram_core, "_VEC_BACKEND_AVAILABLE", True)
        monkeypatch.setattr(engram_core, "_sqlite_vec", _RaisingSqliteVec())

        with caplog.at_level(logging.WARNING, logger="engram_core"):
            outcome = engram_core._stamp_new_node(conn, "n1")

        assert outcome.status == "embedded", outcome
        assert outcome.vec_mirror_ok is False, outcome
        warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "vec_nodes mirror write failed" in r.message
        ]
        assert len(warnings) == 1, [r.message for r in caplog.records]
        conn.close()


# ---------------------------------------------------------------------------
# ROUND 4, M2 (RULED by Kepler, seq 6540, replacing the earlier PENDING
# block): a recovered write RECOUNTS rather than unconditionally clearing
# the write-embed-failed flag. Mira's round-4 finding: the old unconditional
# clear made the flag's completeness attestation lie -- a successful write
# on NODE A cleared the row even while NODE B (an unrelated, earlier
# failure) stayed NULL, so both hooks reported all-clear while real recall
# debt was still outstanding.
# ---------------------------------------------------------------------------

class TestM2RiderThreeRecountsNotJustClears(_TestBase):

    def _embed_succeeds_except_for(self, *bad_texts):
        """Returns an embed() stand-in that fails (returns None) only for
        the given exact text(s) -- lets one node's write genuinely fail
        while another node's write genuinely succeeds, in the same test,
        without touching _embedding_text_for_node at all."""
        bad = set(bad_texts)

        def _fn(text, model_name):
            if text in bad:
                return None
            return [0.1] * 384

        return _fn

    def test_a_fail_node1_then_embed_node2_rewrites_as_per_node_failures(
        self, monkeypatch,
    ):
        conn = self._db([
            {"id": "n1", "claim": "n1 claim text"},
            {"id": "n2", "claim": "n2 claim text"},
        ])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed",
            self._embed_succeeds_except_for("n1 claim text"),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome1 = engram_core._stamp_new_node(conn, "n1")
        assert outcome1.status == "failed", outcome1
        flag_after_n1 = engram_core._get_embedding_restore_flag(conn)
        assert flag_after_n1["cause"].startswith("write-embed-failed:"), flag_after_n1

        outcome2 = engram_core._stamp_new_node(conn, "n2")
        assert outcome2.status == "embedded", outcome2

        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["null_count"] == 1, flag
        assert flag["not_fully_restored"] is True, flag
        assert flag["cause"].startswith(
            "per-node-failures (write-embed-failed recovered at"
        ), flag

        # node 2's OWN response must carry no embedding_failure block --
        # its own write succeeded; the rewritten flag describes node 1's
        # debt, not node 2's outcome.
        block2 = engram_core._embedding_response_block(outcome2)
        assert block2 is None, block2

        conn.commit()

        # engram-surface-hook.py's _check_embedding_restore_flag takes a
        # DIRECT db path -- reads self.db_path as-is.
        surface_mod = _load_surface_hook_module()
        surface_ok, surface_msg = surface_mod._check_embedding_restore_flag(
            str(self.db_path)
        )
        assert surface_ok is False, (
            f"surface hook: a row rewritten to 'per-node-failures (...)' "
            f"must NOT read as all-clear, got ok={surface_ok!r} "
            f"msg={surface_msg!r}"
        )
        assert surface_msg is not None

        # engram-session-start-hook.py's own version hardcodes
        # os.path.join(engram_home, "knowledge.db") -- this fixture's
        # file is named fixture.db, so give it a knowledge.db-named copy
        # of the SAME committed data to read.
        knowledge_db_path = self.tmp / "knowledge.db"
        shutil.copyfile(self.db_path, knowledge_db_path)
        session_mod = _load_session_start_hook_module()
        session_ok, session_msg = session_mod._check_embedding_restore_flag(
            str(self.tmp)
        )
        assert session_ok is False, (
            f"session-start hook: a row rewritten to "
            f"'per-node-failures (...)' must NOT read as all-clear, got "
            f"ok={session_ok!r} msg={session_msg!r}"
        )
        assert session_msg is not None
        conn.close()

    def test_b_fail_node1_backfill_embeds_it_then_node2_clears_row(
        self, monkeypatch,
    ):
        conn = self._db([
            {"id": "n1", "claim": "n1 claim text"},
            {"id": "n2", "claim": "n2 claim text"},
        ])
        _patch_embedder_common(monkeypatch)
        monkeypatch.setattr(
            engram_core._embedder, "embed",
            self._embed_succeeds_except_for("n1 claim text"),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        outcome1 = engram_core._stamp_new_node(conn, "n1")
        assert outcome1.status == "failed", outcome1

        # "run the backfill path": the embedder has genuinely recovered
        # (every text now succeeds) and #3075's own backfill calls the
        # BARE _compute_and_store_embedding directly, never this write
        # path's wrapper -- same call shape as the real backfill tool /
        # sleep skill.
        monkeypatch.setattr(
            engram_core._embedder, "embed", self._embed_succeeds_except_for(),
        )
        backfill_outcome = engram_core._compute_and_store_embedding(conn, "n1")
        assert backfill_outcome.status == "embedded", backfill_outcome

        outcome2 = engram_core._stamp_new_node(conn, "n2")
        assert outcome2.status == "embedded", outcome2

        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["cause"] is None and flag["not_fully_restored"] is False, flag
        conn.close()


# ---------------------------------------------------------------------------
# ROUND 4, M4 (RULED by Kepler, seq 6544, from Mira's finding seq 6539):
# revive() must not risk a NETWORK download under the writer lock --
# _load_model's existing online-fallback branch is fine for server start
# (outside any write transaction), but revive() runs INSIDE the caller's
# open write transaction, so it must stay LOCAL-FILES-ONLY, no exception,
# ever, with no second online attempt.
# ---------------------------------------------------------------------------

class TestM4ReviveLocalFilesOnlyNoOnlineFallback(_TestBase):

    def test_cache_miss_spied_constructor_called_once_local_files_only(
        self, monkeypatch,
    ):
        import sentence_transformers as st_module

        conn = self._db([{"id": "n1"}])
        # Deliberately NOT _patch_embedder_common -- that helper patches
        # _load_model_local_only to a no-op (hermetic default for every
        # OTHER test in this file), which would defeat the one thing this
        # test exists to exercise: the REAL _load_model_local_only
        # codepath, with the SentenceTransformer constructor spied below.
        monkeypatch.setattr(
            engram_core, "_get_embedding_config", lambda: {"model": "test-model"},
        )
        monkeypatch.setattr(engram_core._embedder, "is_available", lambda: True)
        engram_core._embedder._revive_cooldown.clear()
        engram_core._embedder._failed_models.clear()
        engram_core._embedder._model = None
        engram_core._embedder._model_name = None
        monkeypatch.setattr(
            engram_core._embedder, "embed", _FaultyEmbed(persistent=True),
        )
        monkeypatch.setattr(engram_core, "_get_current_turn", lambda: 0)

        spy_calls = []

        def _spy_ctor(*args, **kwargs):
            spy_calls.append((args, kwargs))
            raise OSError("not found in local cache (simulated cache miss)")

        monkeypatch.setattr(st_module, "SentenceTransformer", _spy_ctor)

        outcome = engram_core._stamp_new_node(conn, "n1")

        assert len(spy_calls) == 1, (
            f"revive() must call the SentenceTransformer constructor "
            f"EXACTLY once (no online-fallback second attempt), got "
            f"{spy_calls}"
        )
        args, kwargs = spy_calls[0]
        assert kwargs.get("local_files_only") is True, spy_calls

        assert outcome.status == "failed", outcome
        assert outcome.revive_result == "still-failing", outcome
        flag = engram_core._get_embedding_restore_flag(conn)
        assert flag["not_fully_restored"] is True, flag

        # The caller's write must still commit normally -- a local-cache
        # failure inside revive() must never raise out of the wrapper or
        # leave the transaction in a broken state.
        conn.commit()
        row = conn.execute(
            "SELECT embedding FROM nodes WHERE id='n1'"
        ).fetchone()
        assert row is not None and row[0] is None
        conn.close()
