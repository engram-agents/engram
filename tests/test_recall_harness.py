"""Tests for tools/recall_harness — the ported recall-measurement harness
(engram-alpha #TBD).

Scope: this repo carries the harness's CODE only. The golden set and any
snapshot .db are per-seat private artifacts (never shipped here — see
tools/recall_harness/README-scope note in the PR description) — so these
tests exercise:

  1. Every env-var-absent path fails LOUDLY, naming the var to set, rather
     than silently guessing a path (golden_loader, runner, cli).
  2. The report records the REAL snapshot path actually opened, threaded
     from the runner — never a literal, and never a default-fallback value
     when the runner couldn't say what it opened.
  3. `open_snapshot`'s missing-file guard fires (load-bearing: without it,
     sqlite3.connect silently creates an empty database at a missing path).

No golden-set / snapshot data is read or referenced by these tests — all
fixtures are synthetic, constructed in-process.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from tools.recall_harness import golden_loader, runner
from tools.recall_harness.rru import RankingResultUnit, RankedNode


# ---------------------------------------------------------------------------
# golden_loader — env-var-absent path
# ---------------------------------------------------------------------------


class TestGoldenLoaderEnvVar:
    def test_load_golden_set_raises_loudly_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_RECALL_GOLDEN_DIR", raising=False)
        with pytest.raises(RuntimeError, match="ENGRAM_RECALL_GOLDEN_DIR"):
            golden_loader.load_golden_set()

    def test_iter_golden_raises_loudly_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_RECALL_GOLDEN_DIR", raising=False)
        with pytest.raises(RuntimeError, match="ENGRAM_RECALL_GOLDEN_DIR"):
            next(golden_loader.iter_golden())

    def test_load_golden_set_uses_env_var_when_set(self, monkeypatch, tmp_path):
        """Sanity check the happy path too: a real env var + real files load."""
        golden_dir = tmp_path / "ground-truth"
        golden_dir.mkdir()
        (golden_dir / "q_0001.json").write_text(json.dumps({
            "query_id": "q_0001",
            "query_text": "hello world",
            "ranked_candidates": [
                {"id": "ob_1", "grading": {"relevance": 3}},
            ],
            "primary_category": "test",
        }))
        monkeypatch.setenv("ENGRAM_RECALL_GOLDEN_DIR", str(golden_dir))
        loaded = golden_loader.load_golden_set()
        assert set(loaded) == {"q_0001"}
        assert loaded["q_0001"].metadata["primary_category"] == "test"

    def test_load_golden_set_explicit_arg_does_not_need_env_var(self, monkeypatch, tmp_path):
        """Passing golden_dir explicitly must not require the env var at all."""
        monkeypatch.delenv("ENGRAM_RECALL_GOLDEN_DIR", raising=False)
        golden_dir = tmp_path / "ground-truth"
        golden_dir.mkdir()
        (golden_dir / "q_0002.json").write_text(json.dumps({
            "query_id": "q_0002",
            "query_text": "explicit dir",
            "ranked_candidates": [],
        }))
        loaded = golden_loader.load_golden_set(golden_dir=str(golden_dir))
        assert set(loaded) == {"q_0002"}


# ---------------------------------------------------------------------------
# runner — snapshot path resolution + the load-bearing missing-file guard
# ---------------------------------------------------------------------------


class TestSnapshotPathResolution:
    def test_resolve_snapshot_path_raises_loudly_when_unset(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_RECALL_SNAPSHOT", raising=False)
        with pytest.raises(RuntimeError, match="ENGRAM_RECALL_SNAPSHOT"):
            runner.resolve_snapshot_path(None)

    def test_resolve_snapshot_path_uses_env_var(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_RECALL_SNAPSHOT", "/tmp/some-snapshot.db")
        assert runner.resolve_snapshot_path(None) == "/tmp/some-snapshot.db"

    def test_resolve_snapshot_path_explicit_arg_wins_over_env_var(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_RECALL_SNAPSHOT", "/tmp/env-snapshot.db")
        assert runner.resolve_snapshot_path("/tmp/explicit.db") == "/tmp/explicit.db"

    def test_open_snapshot_raises_loudly_when_unset(self, monkeypatch):
        monkeypatch.delenv("ENGRAM_RECALL_SNAPSHOT", raising=False)
        with pytest.raises(RuntimeError, match="ENGRAM_RECALL_SNAPSHOT"):
            runner.open_snapshot(None)

    def test_open_snapshot_missing_file_guard_fires(self, tmp_path):
        """Load-bearing guard (per CLAUDE.md/spec): sqlite3.connect on a
        missing path silently CREATES an empty database and later queries
        fail with "no such table: nodes" -- measuring an empty graph
        instead of erroring. open_snapshot's `os.path.exists` check must
        fire FIRST, as a FileNotFoundError, for a path that does not exist.
        """
        missing = tmp_path / "does-not-exist.db"
        assert not missing.exists()
        with pytest.raises(FileNotFoundError, match=str(missing)):
            runner.open_snapshot(str(missing))
        # The guard must not have created the file as a side effect either
        # (sqlite3.connect would, if it were ever reached) -- confirms the
        # existence check runs BEFORE sqlite3.connect, not after.
        assert not missing.exists()

    def test_open_snapshot_missing_file_guard_control_can_fail(self, tmp_path):
        """Mutation control for the guard test above: point at a file that
        DOES exist (but is not a valid sqlite db) and confirm we get PAST
        the FileNotFoundError guard -- i.e. the guard is discriminating on
        existence, not unconditionally raising FileNotFoundError."""
        present = tmp_path / "present-but-not-a-db.db"
        present.write_text("not a real sqlite database")
        # Existence check passes; sqlite3 itself will raise on the garbage
        # content once a real query is attempted, but sqlite3.connect()
        # alone does not validate content eagerly, so this should NOT raise
        # FileNotFoundError.
        conn = runner.open_snapshot(str(present))
        conn.close()

    def test_run_all_reports_the_path_it_actually_resolved(self, monkeypatch):
        """run_all's SECOND return value must be the path it actually
        resolved, not the argument it was handed. With the snapshot supplied
        ONLY via the env var the argument is None and the resolved path is the
        env value -- so an implementation returning the raw argument is caught
        here. Pins runner.py's own invariant; the TestCliSnapshotProvenance
        tests monkeypatch run_all away and therefore cannot reach it.
        """
        monkeypatch.setenv("ENGRAM_RECALL_SNAPSHOT", "/tmp/env-only-snapshot.db")
        monkeypatch.setattr(runner, "open_snapshot",
                            lambda p: type("C", (), {"close": lambda s: None})())
        monkeypatch.setattr(runner, "load_golden_set", lambda: {})
        _out, resolved = runner.run_all()
        assert resolved == "/tmp/env-only-snapshot.db"


class TestEngramAlphaDirResolution:
    def test_env_var_override_wins(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_ALPHA_DIR", "/some/override/dir")
        assert runner._resolve_engram_alpha_dir() == "/some/override/dir"

    def test_walk_parents_finds_src_engram_server(self, monkeypatch):
        """No override set: must resolve to the real src/engram directory
        (the dev-checkout layout) by walking up from this file's location,
        since server.py is not tracked at the repo root itself."""
        monkeypatch.delenv("ENGRAM_ALPHA_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        resolved = runner._resolve_engram_alpha_dir()
        assert os.path.isfile(os.path.join(resolved, "server.py"))
        assert resolved.endswith(os.path.join("src", "engram"))

    def test_no_home_directory_paths_anywhere_in_package(self):
        """Static check that NO .py file anywhere under the
        tools/recall_harness/ PACKAGE (not just runner.py) contains a
        hardcoded home-directory path -- the whole point of this port.

        Widened from a single-module check (was: `inspect.getsource(runner)`)
        because the package ships several other modules (cli.py, report.py,
        rru.py, golden_loader.py, metrics/*.py) any of which could carry a
        seat-specific `/home/` literal invisible to a runner-only scan. The
        file list is DISCOVERED via os.walk, not hardcoded, so a file added
        later to the package is covered automatically rather than silently
        falling outside this guard the way the module-scoped version did.

        Reads each file's TEXT directly (not `inspect.getsource` per
        imported module) -- this avoids importing every module just to scan
        it, and covers files regardless of whether anything in this test
        process ever imports them.

        Not covered: non-`.py` files in the package (none currently exist),
        and anything outside `tools/recall_harness/` itself.

        Completeness (PR #2806 round-7): a NAMED SUBSET can never verify
        walk completeness -- four prior rounds each widened the named set
        (one function -> one module -> a floor equal to the top-level file
        count -> four named files) and each was defeated the same way: a
        file OUTSIDE the named set can silently drop out of the walk
        (excluded, or the walk stops recursing) without any named-subset
        assertion noticing, because those assertions only ever check that
        the files THEY name are present. Confirmed live on this package:
        with `golden_loader.py` and `report.py` dropped from the walk (and
        no other change), the previous 4-file named-subset version of this
        test still passed -- a real leak in either of those files would
        have gone undetected.

        The fix: compare the walked set against an INDEPENDENTLY derived
        `expected` set (via `pathlib.rglob`, a different traversal
        mechanism than the `os.walk` loop above) for EXACT equality. A file
        added to the package later is then covered automatically and
        completely -- no list to update, and nothing to name.
        """
        import os as _os
        from pathlib import Path as _Path

        package_dir = _os.path.dirname(_os.path.abspath(runner.__file__))
        checked = []
        for dirpath, dirnames, filenames in _os.walk(package_dir):
            # Explicit, visible exclusion rule: skip __pycache__ directories.
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                full_path = _os.path.join(dirpath, filename)
                checked.append(full_path)
                with open(full_path, encoding="utf-8") as f:
                    src = f.read()
                assert "/home/" not in src, (
                    f"hardcoded home-directory path found in {full_path}"
                )

        # Independently-derived expected set -- pathlib.rglob is a
        # DIFFERENT traversal mechanism than the os.walk loop above. The
        # independence is the point: two instruments walking the same tree
        # by different means must land on the same answer, or one of them
        # is wrong. Same __pycache__ exclusion rule as above, made visible
        # here rather than assumed identical.
        expected = {
            str(p)
            for p in _Path(package_dir).rglob("*.py")
            if "__pycache__" not in p.parts
        }
        checked_set = set(checked)

        # 1. COMPLETENESS: the walked set and the independently-derived set
        # must be EXACTLY equal. This catches a walk that LOSES files (a
        # filter excluding one, or a lost subdirectory) and one that PICKS
        # UP files it shouldn't -- without naming any of them, and without
        # needing an update when a file is added to or removed from the
        # package.
        missing_from_walk = expected - checked_set
        extra_in_walk = checked_set - expected
        assert not missing_from_walk and not extra_in_walk, (
            "walked set != independently-derived set -- "
            f"missing from walk: {sorted(missing_from_walk)}; "
            f"extra in walk (not expected): {sorted(extra_in_walk)}"
        )

        # 2. ADDRESS SANITY: set equality above passes vacuously if BOTH
        # instruments were pointed at the wrong root (e.g. package_dir
        # resolves wrong) -- two empty sets are trivially equal. These
        # assertions guard against that specifically: the set must be
        # non-empty, and a known-permanent module (runner.py -- the very
        # module package_dir is derived from) must be present. Equality
        # (above) verifies completeness; these two verify the address is
        # right. Neither subsumes the other.
        assert checked_set, "no .py files found -- package_dir likely wrong"
        assert any(_os.path.basename(p) == "runner.py" for p in checked), (
            "runner.py not found by the walk -- package_dir likely "
            "resolves to the wrong location"
        )

    def test_walk_parents_rejects_decoy_server_without_search_nodes(
        self, monkeypatch, tmp_path
    ):
        """A same-named server.py in an unrelated ancestor directory must
        NOT be picked as the repo root -- only accepted if it looks like
        ENGRAM's own server.py (exposes `_search_nodes`). Without that
        content check, the walk would return the decoy's directory the
        moment it finds it; with the check, it skips the decoy, finds
        nothing else within the walk's ancestor limit (the decoy tree is
        isolated under tmp_path, with no real server.py above it), and
        fails loudly instead of silently returning the wrong root.
        """
        monkeypatch.delenv("ENGRAM_ALPHA_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        decoy_root = tmp_path / "decoy_root"
        nested = decoy_root / "nested"
        nested.mkdir(parents=True)
        (decoy_root / "server.py").write_text(
            "# not ENGRAM's server.py -- no _search_nodes here\n"
        )
        fake_module_file = nested / "fake_runner_location.py"
        fake_module_file.write_text("# stand-in for runner.py's own __file__\n")

        monkeypatch.setattr(runner, "__file__", str(fake_module_file))
        with pytest.raises(RuntimeError, match="Could not locate"):
            runner._resolve_engram_alpha_dir()


# ---------------------------------------------------------------------------
# cli — snapshot provenance must be the REAL threaded path, never a literal
# and never a default-fallback value
# ---------------------------------------------------------------------------


def _make_synthetic_golden_and_test():
    golden = {
        "q_synthetic_1": RankingResultUnit(
            query_id="q_synthetic_1",
            query_text="synthetic test query",
            ranked_nodes=[
                RankedNode(id="ob_synthetic_1", rank=1, grading={"relevance": 3}),
            ],
            metadata={"primary_category": "synthetic"},
        ),
    }
    test = {
        "q_synthetic_1": RankingResultUnit(
            query_id="q_synthetic_1",
            query_text="synthetic test query",
            ranked_nodes=[
                RankedNode(id="ob_synthetic_1", rank=1, signals={}),
            ],
            metadata={
                "algo": "fake",
                "top_k": 20,
                "include_superseded": False,
                "use_semantic": True,
                "keyword_matches": 1,
                "semantic_matches": 0,
                "semantic_available": False,
                "query_debug": None,
            },
        ),
    }
    return golden, test


FAKE_SNAPSHOT_PATH = "/tmp/definitely-not-the-old-literal-snapshot.db"


def _run_cli_with_fakes(monkeypatch, tmp_path, argv_extra=None):
    """Run cli.main() with load_golden_set/run_all monkeypatched to
    synthetic in-process fixtures, and return the parsed JSON report."""
    from tools.recall_harness import cli as cli_mod

    golden, test = _make_synthetic_golden_and_test()

    monkeypatch.setattr(cli_mod, "load_golden_set", lambda: golden)
    monkeypatch.setattr(
        cli_mod, "run_all",
        lambda snapshot_path=None, top_k=20, include_superseded=False: (test, FAKE_SNAPSHOT_PATH),
    )

    out_path = tmp_path / "report.md"
    argv = ["cli.py", "--out", str(out_path)] + (argv_extra or [])
    monkeypatch.setattr(sys, "argv", argv)

    cli_mod.main()

    json_path = str(out_path).replace(".md", ".json")
    with open(json_path) as f:
        return json.load(f), str(out_path)


class TestCliSnapshotProvenance:
    def test_report_records_actual_snapshot_path_not_a_literal(self, monkeypatch, tmp_path):
        data, out_path = _run_cli_with_fakes(monkeypatch, tmp_path)
        assert data["snapshot"] == FAKE_SNAPSHOT_PATH
        # The old hardcoded literal must be gone.
        assert data["snapshot"] != "phase3_pre_curation_20260518T140535Z.db"
        # Markdown report also carries the real path, not the literal.
        with open(out_path) as f:
            md = f.read()
        assert FAKE_SNAPSHOT_PATH in md
        assert "phase3_pre_curation_20260518T140535Z.db" not in md

    def test_out_dir_env_var_absent_and_no_flag_raises_loudly(self, monkeypatch, tmp_path):
        from tools.recall_harness import cli as cli_mod
        monkeypatch.delenv("ENGRAM_RECALL_EVAL_RUNS_DIR", raising=False)
        monkeypatch.setattr(sys, "argv", ["cli.py"])
        with pytest.raises(RuntimeError, match="ENGRAM_RECALL_EVAL_RUNS_DIR"):
            cli_mod.main()

    def test_out_dir_env_var_used_when_no_flag(self, monkeypatch, tmp_path):
        from tools.recall_harness import cli as cli_mod
        golden, test = _make_synthetic_golden_and_test()
        monkeypatch.setattr(cli_mod, "load_golden_set", lambda: golden)
        monkeypatch.setattr(
            cli_mod, "run_all",
            lambda snapshot_path=None, top_k=20, include_superseded=False: (test, FAKE_SNAPSHOT_PATH),
        )
        eval_dir = tmp_path / "eval_runs"
        monkeypatch.setenv("ENGRAM_RECALL_EVAL_RUNS_DIR", str(eval_dir))
        monkeypatch.setattr(sys, "argv", ["cli.py"])
        cli_mod.main()
        written = list(eval_dir.glob("*.json"))
        assert len(written) == 1

    def test_cli_fails_loudly_if_runner_cannot_say_which_snapshot(self, monkeypatch, tmp_path):
        """Amendment (spec addendum): a provenance field that can only ever
        be PRESENT is not a pin. If run_all ever reports it doesn't know
        which snapshot it opened (falsy resolved path), the CLI must FAIL
        rather than write a report with a defaulted/guessed value.

        In the real implementation run_all can never actually return a
        falsy path (resolve_snapshot_path raises first) — this test forces
        the condition via a fake run_all, to pin the belt-and-suspenders
        guard in cli.main() itself as a second, independent line of
        defense (not merely inherited from runner.py's own invariant).

        What actually pins run_all's own invariant (that the returned path
        is the one it resolved, not the argument it was handed) is
        TestSnapshotPathResolution.test_run_all_reports_the_path_it_actually_resolved.
        """
        from tools.recall_harness import cli as cli_mod
        golden, test = _make_synthetic_golden_and_test()
        monkeypatch.setattr(cli_mod, "load_golden_set", lambda: golden)
        monkeypatch.setattr(
            cli_mod, "run_all",
            lambda snapshot_path=None, top_k=20, include_superseded=False: (test, None),
        )
        out_path = tmp_path / "report.md"
        monkeypatch.setattr(sys, "argv", ["cli.py", "--out", str(out_path)])
        with pytest.raises(RuntimeError, match="did not report which snapshot"):
            cli_mod.main()
        # And no report was written -- a failure must not leave a
        # half-written report with bad provenance behind.
        assert not out_path.exists()


# ---------------------------------------------------------------------------
# run_one -- signals["composite"] must be the row's `composite_score`
# ---------------------------------------------------------------------------


class TestRunOneCompositeSignal:
    def test_harness_composite_equals_row_composite_score_on_real_db(
        self, monkeypatch, tmp_path
    ):
        """On a real database, signals["composite"] for each ranked node
        equals that row's `composite_score` as returned by `_search_nodes`.

        Contract: a row whose composite_score is None produces NO
        "composite" key in its signals. (A bypass row is not easy to produce
        in this FTS-only fixture, so only the equality side is asserted
        here.)"""
        monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        import server

        server._configure_paths(str(tmp_path / "engram_data"))
        server._ensure_data_dir()
        for claim in (
            "xylophone rhythm instrument unique term",
            "xylophone tuning bars resonate",
            "xylophone mallets strike wooden bars",
        ):
            r = json.loads(server.engram_add_observation(payload_json=json.dumps({
                "quoted_text": "test quote",
                "interpretation": "test interpretation",
                "claim": claim,
                "quote_type": "personal_communication",
                "url": "https://example.com/test",
                "title": "Test",
            })))
            assert "error" not in r, r

        conn = server._get_db()
        monkeypatch.setattr(runner, "engram_server", server)

        golden = RankingResultUnit(
            query_id="q_composite", query_text="xylophone", ranked_nodes=[],
        )
        test = runner.run_one(conn, golden, top_k=5, use_semantic=False)
        rows = server._search_nodes(
            conn, query="xylophone", top_k=5, use_semantic=False,
        )["results"]

        assert len(rows) >= 2
        by_id = {n.id: n for n in test.ranked_nodes}
        assert set(by_id) == {row["id"] for row in rows}
        for row in rows:
            assert isinstance(row["composite_score"], float)
            assert by_id[row["id"]].signals["composite"] == row["composite_score"]

    def test_row_with_none_composite_score_yields_no_composite_key(
        self, monkeypatch
    ):
        """Stub cell (no database) pinning the None side of the contract: a
        row whose composite_score is None produces NO "composite" key, while a
        sibling row with a real score still carries it, and a row scoring
        exactly 0.0 keeps it (presence, not truthiness)."""

        class _StubServer:
            @staticmethod
            def _search_nodes(conn, **kwargs):
                return {
                    "results": [
                        {"id": "stub_a", "composite_score": 0.42},
                        {"id": "stub_b", "composite_score": None},
                        {"id": "stub_c", "composite_score": 0.0},
                    ],
                    "debug": {},
                }

        monkeypatch.setattr(runner, "engram_server", _StubServer)
        golden = RankingResultUnit(
            query_id="q_stub", query_text="anything", ranked_nodes=[],
        )
        test = runner.run_one(None, golden, top_k=5, use_semantic=False)

        by_id = {n.id: n for n in test.ranked_nodes}
        assert by_id["stub_a"].signals["composite"] == 0.42
        # stub_b has no other signal either, so run_one stores signals=None;
        # a regression that leaked the None would give {"composite": None}.
        assert by_id["stub_b"].signals is None
        # stub_c carries a legitimate composite of exactly 0.0: it must be
        # kept (presence check), not dropped by a truthiness test.
        assert "composite" in by_id["stub_c"].signals
        assert by_id["stub_c"].signals["composite"] == 0.0
        assert by_id["stub_c"].signals["composite"] is not None


# ---------------------------------------------------------------------------
# MUTATION CONTROLS (documentation only -- run by hand, not in CI, since they
# mutate real source on disk). See the handoff's "Verification" section for
# the actual captured run of both.
#
# Mutation 1 (restore the hardcoded literal): in cli.py, replace
#   `"snapshot": snapshot_used,`   ->   `"snapshot": "phase3_pre_curation_20260518T140535Z.db",`
# Expected: ONLY test_report_records_actual_snapshot_path_not_a_literal fails
# (assert data["snapshot"] == FAKE_SNAPSHOT_PATH no longer holds).
#
# Mutation 2 (the more valuable control per the spec amendment -- weaken the
# explicit "fail rather than fall back" guard into a silent default, the
# version a careful-but-wrong reader would most plausibly write): in cli.py,
# replace the belt-and-suspenders guard's raise with a default-fallback
# assignment:
#   if not snapshot_used:
#       raise RuntimeError(...)
#   ->
#   snapshot_used = snapshot_used or "unknown"
# Expected: ONLY test_cli_fails_loudly_if_runner_cannot_say_which_snapshot
# fails (no RuntimeError is raised; a report gets written with
# snapshot="unknown" instead) -- demonstrating the guard, not just the
# literal, is what's actually pinned.
#
# Mutation 3 (the one that pins runner.py itself, not cli.py): in runner.py,
# replace run_all's own return statement --
#   return out, resolved_path
#   ->
#   return out, snapshot_path
# -- returning the raw ARGUMENT instead of the RESOLVED path. Mutations 1 and
# 2 both pin cli.py; this is the only one that pins run_all's own contract
# (the second return value is the path actually opened, not the argument it
# was handed) -- the invariant TestCliSnapshotProvenance's tests explicitly
# inherit rather than test, since they monkeypatch run_all away entirely
# (their own docstrings say so).
# Expected: ONLY test_run_all_reports_the_path_it_actually_resolved fails
# (assert resolved == "/tmp/env-only-snapshot.db" no longer holds --
# resolved comes back None instead). Observed 2026-09-10: applying the
# mutation and running the full file produced exactly "1 failed, 18 passed,
# 1 skipped", and the one failure was
# test_run_all_reports_the_path_it_actually_resolved.
@pytest.mark.skip(reason="documentation-only placeholder; run by hand, see comment above")
def test_mutation_control_placeholder():
    pass
