"""Contract-first tests for engram-alpha #345 PR 1 (c) — the `--json` flag on
tools/engram-regenerate-embeddings.py, consumed by the engram-sleep Phase A
embedding-backfill step (Step 3.9).

Written BEFORE the implementation, per this repo's contract-first-tests
convention (CLAUDE.md). RED against unfixed `dev` (the script has no --json
flag and backfill_embeddings() takes no json_output kwarg); GREEN after the
C1 fix lands.

Coverage, matching the PR1(c) spec's C3 contract exactly:

  (i)   --json emits exactly ONE JSON object to stdout with the four keys
        {before_null, embedded, failed, after_null} and correct counts on a
        fixture DB with N NULL-embedding rows. Without --json, stdout is
        unchanged (empty) from today. A run with nothing to do prints the
        documented all-zero object.
  (ii)  after_null is RE-QUERIED from the DB after the run, not computed by
        subtraction (before_null - embedded). A concurrent-insert fixture
        discriminates the two: a new NULL-embedding row is inserted into the
        DB by a wrapped `_backfill_embeddings_core` AFTER that pass's own
        loop (and its in-memory counts) are finished but BEFORE the tool
        reports its after-run state -- a subtraction-based implementation
        cannot see it (it only has the pre-recorded counts); a genuine
        re-query does.
  (iii) a second run on an already-fully-embedded DB reports before_null==0.

Out of scope (per the PR1(c) spec's DO NOT): the backfill's own selection
logic / batching (untouched); server.py / engram_core.py (untouched); the
dream-master agent file (untouched).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _make_min_nodes_db(path: Path, rows: list[dict]) -> None:
    """Write a minimal `nodes` table (mirrors
    tests/test_3075_embedding_pipeline_restore.py's own `_make_min_nodes_db`
    helper -- duplicated locally, test-only, per this repo's no-cross-test-
    file-import convention, same as that file's own docstring notes for its
    `_fresh_core_in_tmp` helper).
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE nodes (
            id              TEXT PRIMARY KEY,
            type            TEXT,
            claim           TEXT,
            source_title    TEXT,
            predicted_event TEXT,
            recall_summary  TEXT,
            recall_keywords TEXT,
            embedding       TEXT,
            is_current      INTEGER DEFAULT 1
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, source_title, predicted_event, "
            "recall_summary, recall_keywords, embedding, is_current) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r["id"],
                r.get("type", "observation"),
                r.get("claim"),
                r.get("source_title"),
                r.get("predicted_event"),
                r.get("recall_summary"),
                r.get("recall_keywords"),
                r.get("embedding"),
                r.get("is_current", 1),
            ),
        )
    conn.commit()
    conn.close()


class _BaseRegenJsonTest:
    """Shared setup/teardown + module loader, mirroring
    tests/test_3075_embedding_pipeline_restore.py's
    TestC4RegenerateEmbeddingsScriptFailsLoud pattern (same repo, same
    script, same fixture approach for the embedder -- a mocked
    `_embedder.embed_batch` driving the script's real backfill logic, not a
    subprocess).
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_345pr1_json_"))
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"

    def _load_regen_module(self):
        regen_script = REPO_ROOT / "tools" / "engram-regenerate-embeddings.py"
        spec = importlib.util.spec_from_file_location(
            "engram_regenerate_embeddings_345pr1_test", str(regen_script)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _patched_server(self, regen_mod, embed_batch_fn):
        """Returns a contextlib.ExitStack patching the real server module +
        the regen module's own server-resolution hooks, the same way
        TestC4RegenerateEmbeddingsScriptFailsLoud does inline -- factored
        here since every test in this file needs the identical stack.
        """
        import server as srv

        srv._configure_paths(self.tmp)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(srv._embedder, "is_available", return_value=True))
        stack.enter_context(mock.patch.object(srv._embedder, "_load_model", return_value=None))
        stack.enter_context(mock.patch.object(srv._embedder, "_model", "fake_model", create=True))
        stack.enter_context(mock.patch.object(srv._embedder, "embed_batch", side_effect=embed_batch_fn))
        stack.enter_context(mock.patch.object(
            regen_mod, "_find_server_py",
            return_value=REPO_ROOT / "src" / "engram" / "server.py",
        ))
        stack.enter_context(mock.patch.object(regen_mod, "_import_server", return_value=srv))
        return stack


# ---------------------------------------------------------------------------
# (i) --json emits the 4 keys with correct counts
# ---------------------------------------------------------------------------

class TestJsonFlagBasicCounts(_BaseRegenJsonTest):

    def test_json_object_has_four_keys_correct_counts(self, capsys):
        db_path = self.tmp / "knowledge.db"
        rows = [{"id": f"ob_{i}", "claim": f"claim text {i}"} for i in range(3)]
        _make_min_nodes_db(db_path, rows)

        regen_mod = self._load_regen_module()
        fake_vector = [0.5] * 384

        def _embed_batch(texts, model_name):
            return [fake_vector for _ in texts]

        with self._patched_server(regen_mod, _embed_batch):
            capsys.readouterr()
            regen_mod.backfill_embeddings(self.tmp, json_output=True)
            captured = capsys.readouterr()

        lines = [l for l in captured.out.splitlines() if l.strip()]
        assert len(lines) == 1, (
            f"expected exactly ONE JSON object on stdout, got {len(lines)} "
            f"line(s): {captured.out!r}"
        )
        obj = json.loads(lines[0])
        assert set(obj.keys()) == {"before_null", "embedded", "failed", "after_null"}
        assert obj == {"before_null": 3, "embedded": 3, "failed": 0, "after_null": 0}

    def test_without_json_flag_stdout_unchanged(self, capsys):
        """Without --json, stdout is unchanged from today (empty -- all of
        the script's own diagnostics go to stderr)."""
        db_path = self.tmp / "knowledge.db"
        rows = [{"id": "ob_0", "claim": "claim text 0"}]
        _make_min_nodes_db(db_path, rows)

        regen_mod = self._load_regen_module()
        fake_vector = [0.5] * 384

        def _embed_batch(texts, model_name):
            return [fake_vector for _ in texts]

        with self._patched_server(regen_mod, _embed_batch):
            capsys.readouterr()
            regen_mod.backfill_embeddings(self.tmp, json_output=False)
            captured = capsys.readouterr()

        assert captured.out == "", (
            f"expected empty stdout without --json, got {captured.out!r}"
        )

    def test_nothing_to_do_prints_all_zero_object(self, capsys):
        """A run with nothing to do (every row already embedded) prints the
        documented all-zero object -- the spec's explicit example."""
        db_path = self.tmp / "knowledge.db"
        rows = [{
            "id": "ob_0", "claim": "already embedded",
            "embedding": json.dumps([0.1] * 384),
        }]
        _make_min_nodes_db(db_path, rows)

        regen_mod = self._load_regen_module()

        def _embed_batch(texts, model_name):
            raise AssertionError("embed_batch must not be called -- nothing to embed")

        with self._patched_server(regen_mod, _embed_batch):
            capsys.readouterr()
            regen_mod.backfill_embeddings(self.tmp, json_output=True)
            captured = capsys.readouterr()

        lines = [l for l in captured.out.splitlines() if l.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj == {"before_null": 0, "embedded": 0, "failed": 0, "after_null": 0}


# ---------------------------------------------------------------------------
# (ii) after_null is RE-QUERIED, not computed by subtraction
# ---------------------------------------------------------------------------

class TestJsonAfterNullIsRequeried(_BaseRegenJsonTest):

    def test_after_null_reflects_a_late_arriving_null_row(self, capsys):
        db_path = self.tmp / "knowledge.db"
        rows = [{"id": f"ob_{i}", "claim": f"claim text {i}"} for i in range(2)]
        _make_min_nodes_db(db_path, rows)

        regen_mod = self._load_regen_module()
        fake_vector = [0.5] * 384

        def _embed_batch(texts, model_name):
            return [fake_vector for _ in texts]

        original_core = regen_mod._backfill_embeddings_core

        def _core_with_concurrent_insert(conn, server, model_name, vec_available):
            counts = original_core(conn, server, model_name, vec_available)
            # Simulate a writer that inserts a new NULL-embedding node AFTER
            # this pass's own loop (and its in-memory counts) are done, but
            # BEFORE the tool reports its after-run state -- exactly the
            # race a subtraction-based "after_null = before_null - embedded"
            # formula cannot observe (it never touches the DB again after
            # recording counts); a genuine re-query must.
            conn.execute(
                "INSERT INTO nodes (id, type, claim, is_current, embedding) "
                "VALUES (?, 'observation', ?, 1, NULL)",
                ("ob_concurrent", "a late-arriving unrelated claim"),
            )
            conn.commit()
            return counts

        with self._patched_server(regen_mod, _embed_batch), \
             mock.patch.object(
                 regen_mod, "_backfill_embeddings_core",
                 side_effect=_core_with_concurrent_insert,
             ):
            capsys.readouterr()
            regen_mod.backfill_embeddings(self.tmp, json_output=True)
            captured = capsys.readouterr()

        lines = [l for l in captured.out.splitlines() if l.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["before_null"] == 2
        assert obj["embedded"] == 2
        assert obj["failed"] == 0
        # Subtraction (before_null - embedded = 0) would MISS the
        # concurrently-inserted row entirely; a genuine re-query sees it.
        assert obj["after_null"] == 1, (
            f"expected after_null to be RE-QUERIED (1, reflecting the "
            f"late-arriving NULL row) rather than computed as "
            f"before_null - embedded (which would wrongly read 0), got "
            f"{obj}"
        )


# ---------------------------------------------------------------------------
# (iii) a second run reports before_null == 0
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# (R3, PR #3332 review round 2) JSON is printed BEFORE the fail-loud exit on
# a genuine partial failure (counts["failed"] > 0), not skipped/swallowed by
# it -- the reviewer's own control (moving the print after sys.exit, or
# skipping it when failed > 0) left all 5 original tests green, meaning the
# suite had no coverage pinning this ordering.
# ---------------------------------------------------------------------------

class TestJsonPrintedBeforeExitOnPartialFailure(_BaseRegenJsonTest):

    def test_json_printed_with_failed_counts_before_sys_exit(self, capsys):
        db_path = self.tmp / "knowledge.db"
        rows = [{"id": f"ob_{i}", "claim": f"claim text {i}"} for i in range(2)]
        _make_min_nodes_db(db_path, rows)

        regen_mod = self._load_regen_module()
        fake_vector = [0.5] * 384

        def _embed_batch(texts, model_name):
            # First node embeds fine; second comes back with an empty
            # vector -- the per-node failure path at
            # _backfill_embeddings_core:200-203 (`if not vector:
            # counts["failed"] += 1; continue`), leaving that row's
            # embedding NULL.
            return [fake_vector, []]

        with self._patched_server(regen_mod, _embed_batch):
            capsys.readouterr()
            with pytest.raises(SystemExit) as exc_info:
                regen_mod.backfill_embeddings(self.tmp, json_output=True)
            captured = capsys.readouterr()

        # Tool's own contract for partial failure
        # (engram-regenerate-embeddings.py:412-432): the JSON object is
        # printed first, THEN `if counts["failed"] > 0: sys.exit(1)` fires.
        # Exit code 1 either way; the thing this test pins is that the JSON
        # line is NOT skipped/moved past that exit.
        assert exc_info.value.code == 1

        lines = [l for l in captured.out.splitlines() if l.strip()]
        assert len(lines) == 1, (
            f"expected the JSON object printed even though this run exits "
            f"non-zero (failed>0); got {len(lines)} stdout line(s): "
            f"{captured.out!r}"
        )
        obj = json.loads(lines[0])
        assert obj["failed"] >= 1
        assert obj["before_null"] == 2
        assert obj["embedded"] == 1
        # One node embedded, one failed (empty vector, never UPDATEd) -> one
        # NULL row remains; after_null is RE-QUERIED, same as the other
        # tests in this file.
        assert obj["after_null"] == 1, (
            f"expected after_null==1 (the failed node's row is still NULL), "
            f"got {obj}"
        )


class TestJsonSecondRunReportsZeroBeforeNull(_BaseRegenJsonTest):

    def test_second_run_before_null_is_zero(self, capsys):
        db_path = self.tmp / "knowledge.db"
        rows = [{"id": f"ob_{i}", "claim": f"claim text {i}"} for i in range(2)]
        _make_min_nodes_db(db_path, rows)

        regen_mod = self._load_regen_module()
        fake_vector = [0.5] * 384

        def _embed_batch(texts, model_name):
            return [fake_vector for _ in texts]

        with self._patched_server(regen_mod, _embed_batch):
            capsys.readouterr()
            regen_mod.backfill_embeddings(self.tmp, json_output=True)
            first_run = capsys.readouterr()

            capsys.readouterr()
            regen_mod.backfill_embeddings(self.tmp, json_output=True)
            second_run = capsys.readouterr()

        first_lines = [l for l in first_run.out.splitlines() if l.strip()]
        assert len(first_lines) == 1
        first_obj = json.loads(first_lines[0])
        assert first_obj == {"before_null": 2, "embedded": 2, "failed": 0, "after_null": 0}

        second_lines = [l for l in second_run.out.splitlines() if l.strip()]
        assert len(second_lines) == 1
        second_obj = json.loads(second_lines[0])
        assert second_obj == {"before_null": 0, "embedded": 0, "failed": 0, "after_null": 0}
