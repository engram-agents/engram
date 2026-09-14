"""Tests for the #2753 epic WS2 tripwire render (D3 point 1) --
engram-lesson-tripwire-hook.py's additive "[triggered principles]" section.

Covers:
  1. load_tripwires/check_command shape change (pattern, nudge, node_id) and
     check_command's hits-as-dicts contract.
  2. render_triggered_principles: a real 2-3 rung chain renders correctly
     (middle rungs as recall_summary one-liners, head's claim in FULL).
  3. Cross-hit head-dedupe within one fire: 2 hits sharing a head merge into
     ONE block, not two.
  4. No-ancestor hit (bare lesson/cornerstone, no generalizes parent) renders
     nothing extra.
  5. Byte-identical existing-nudge-block regression guard: the pre-existing
     "[lesson-tripwire] Action-moment pattern matched" block is unchanged by
     the new additive section, whether or not a chain exists.
  6. Hook-never-blocks: generalization_chain raising inside the new code path
     never suppresses the existing nudge (T2 "never blocks" invariant).
  7. Superseded-rung note: generalization_chain() itself pre-resolves every
     rung to its current version (WS1's own test_generalizes_edge.py already
     controls this at the chain layer) -- this file's render tests don't
     duplicate that control; they only confirm the render path correctly
     surfaces whatever generalization_chain() already returned.
"""

import atexit
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))  # #2565: append, not insert(0) -- see test_lesson_tripwire.py


def _import_hook():
    p = PROJECT_ROOT / "src/engram/hooks/claude/engram-lesson-tripwire-hook.py"
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_ws2", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_hook = _import_hook()
load_tripwires = _hook.load_tripwires
check_command = _hook.check_command
render_triggered_principles = _hook.render_triggered_principles


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_generalizes_tripwire_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


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
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    return server


def _db_path() -> Path:
    return Path(TEST_DIR) / "knowledge.db"


def _seed_incident(server) -> str:
    payload = {
        "quoted_text": "I shipped without reading the error.",
        "interpretation": "Missed contextual cue from stderr.",
        "claim": "Skipped reading error output before hypothesizing root cause.",
        "quote_type": "personal_communication",
        "url": "https://example.com/ws2-tripwire-incident",
        "title": "Test incident",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _seed_cornerstone(server, title, recall_summary=None, generalized_by=""):
    payload = {"tag": "test-axis", "title": title, "new_frame": f"frame-{title}"}
    if generalized_by:
        payload["generalized_by"] = generalized_by
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    cs_id = r["cornerstone_id"]
    if recall_summary:
        rs = json.loads(server.engram_set_recall_summaries(payload_json=json.dumps({
            "summaries": [{"node_id": cs_id, "recall_summary": recall_summary, "recall_keywords": ["k1", "k2", "k3"]}]
        })))
        assert rs.get("applied") == 1, rs
    return cs_id


def _seed_lesson(server, *, pattern, nudge, recall_summary=None, generalized_by="", claim=None):
    inc_id = _seed_incident(server)
    payload = {
        "claim": claim or f"claim for {nudge}",
        "incident_ids": inc_id,
        "scaffolding_nudge": nudge,
        "logical_chain": "chain",
        "situation_pattern": pattern,
    }
    if recall_summary:
        payload["recall_summary"] = recall_summary
    if generalized_by:
        payload["generalized_by"] = generalized_by
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["lesson_id"]


def _hit(node_id: str, nudge: str = "nudge") -> dict:
    return {"nudge": nudge, "node_id": node_id}


# ---------------------------------------------------------------------------
# 1. load_tripwires / check_command shape
# ---------------------------------------------------------------------------

class TestTripwireShapeCarriesNodeId(unittest.TestCase):

    def test_load_tripwires_returns_three_tuples(self):
        server = fresh_server()
        ls1 = _seed_lesson(server, pattern=r"rm -rf", nudge="careful")
        tripwires = load_tripwires(db_path=_db_path())
        self.assertEqual(len(tripwires), 1)
        pattern, nudge, node_id = tripwires[0]
        self.assertEqual(pattern, r"rm -rf")
        self.assertEqual(nudge, "careful")
        self.assertEqual(node_id, ls1)

    def test_check_command_hits_are_dicts_with_node_id(self):
        server = fresh_server()
        ls1 = _seed_lesson(server, pattern=r"rm -rf", nudge="careful")
        tripwires = load_tripwires(db_path=_db_path())
        hits = check_command("rm -rf /tmp/x", tripwires)
        # #2858 added a 3rd "pattern" key to each hit (the firing
        # situation_pattern itself, used by main() to order hits by
        # specificity before rendering) -- assert the full dict shape
        # check_command's own docstring documents, not just the two keys
        # this test predates that change.
        self.assertEqual(hits, [{"nudge": "careful", "node_id": ls1, "pattern": r"rm -rf"}])


# ---------------------------------------------------------------------------
# 2. render_triggered_principles -- a real chain renders correctly
# ---------------------------------------------------------------------------

class TestRenderTriggeredPrinciplesChain(unittest.TestCase):

    def test_two_rung_chain_renders_middle_summary_and_head_claim_in_full(self):
        server = fresh_server()
        cs_head = _seed_cornerstone(
            server, "HeadCS", recall_summary="head summary",
        )
        ls1 = _seed_lesson(
            server, pattern=r"rm -rf", nudge="careful",
            recall_summary="lesson summary", generalized_by=cs_head,
        )

        block = render_triggered_principles([_hit(ls1)], db_path=_db_path())
        self.assertIn("[triggered principles]", block)
        # Middle rung (the lesson itself) renders as a recall_summary one-liner.
        self.assertIn(f"↑ {ls1} — lesson summary", block)
        # Head renders its CLAIM in full (title text, per engram_add_cornerstone's
        # "title ... used as the claim text").
        self.assertIn(f"↑ {cs_head} — HeadCS", block)

    def test_no_ancestor_hit_renders_nothing(self):
        """A lesson with no generalized_by parent (already a head with
        nothing above it) -- D3: render nothing extra for it."""
        server = fresh_server()
        ls1 = _seed_lesson(server, pattern=r"rm -rf", nudge="careful")
        block = render_triggered_principles([_hit(ls1)], db_path=_db_path())
        self.assertEqual(block, "")

    def test_missing_db_returns_empty_string(self):
        block = render_triggered_principles(
            [_hit("ls_nonexistent")], db_path=Path(TEST_DIR) / "does-not-exist.db",
        )
        self.assertEqual(block, "")

    def test_empty_hits_returns_empty_string(self):
        self.assertEqual(render_triggered_principles([], db_path=_db_path()), "")


# ---------------------------------------------------------------------------
# 3. Cross-hit head-dedupe within one fire
# ---------------------------------------------------------------------------

class TestCrossHitHeadDedupe(unittest.TestCase):

    def test_two_hits_sharing_a_head_merge_into_one_block(self):
        server = fresh_server()
        cs_head = _seed_cornerstone(server, "SharedHeadCS", recall_summary="shared head summary")
        ls1 = _seed_lesson(
            server, pattern=r"pattern-one", nudge="nudge one",
            recall_summary="lesson one summary", generalized_by=cs_head,
        )
        ls2 = _seed_lesson(
            server, pattern=r"pattern-two", nudge="nudge two",
            recall_summary="lesson two summary", generalized_by=cs_head,
        )

        hits = [_hit(ls1, "nudge one"), _hit(ls2, "nudge two")]
        block = render_triggered_principles(hits, db_path=_db_path())

        # Exactly ONE "[triggered principles]" header, one head claim line --
        # not two separate blocks.
        self.assertEqual(block.count("[triggered principles]"), 1)
        self.assertEqual(block.count(f"↑ {cs_head} — SharedHeadCS"), 1)
        # Both routes' middle rungs are present in the merged subgraph.
        self.assertIn(f"↑ {ls1} — lesson one summary", block)
        self.assertIn(f"↑ {ls2} — lesson two summary", block)
        # Convergence line names the merge.
        self.assertIn(f"2 of these 2 matches sit under {cs_head}", block)

    def test_single_hit_per_head_no_convergence_line(self):
        """When only one hit reaches a given head, no 'N of these M matches'
        line is emitted for it (the line is additive only on real merges)."""
        server = fresh_server()
        cs_head = _seed_cornerstone(server, "SoloHeadCS", recall_summary="solo head summary")
        ls1 = _seed_lesson(
            server, pattern=r"pattern-solo", nudge="nudge solo",
            recall_summary="lesson solo summary", generalized_by=cs_head,
        )
        block = render_triggered_principles([_hit(ls1, "nudge solo")], db_path=_db_path())
        self.assertNotIn("matches sit under", block)


# ---------------------------------------------------------------------------
# 5 + 6. main() end-to-end: byte-identical existing nudge + never-blocks
# ---------------------------------------------------------------------------

def _run_hook_main(hook_input: dict, *, tripwires=None, db_path=None) -> str:
    output_buf = io.StringIO()
    patches = [
        unittest.mock.patch.object(_hook, "load_tripwires", return_value=tripwires or []),
        unittest.mock.patch.object(_hook, "load_prevention_lessons", return_value=[]),
        unittest.mock.patch.object(_hook, "check_in_turn_recall", return_value=""),
        unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
        unittest.mock.patch("sys.stdout", output_buf),
    ]
    if db_path is not None:
        patches.append(unittest.mock.patch.object(_hook, "DB_PATH", db_path))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            _hook.main()
        except SystemExit:
            pass
    return output_buf.getvalue()


class TestExistingNudgeBlockByteIdentical(unittest.TestCase):
    """The pre-existing "[lesson-tripwire] Action-moment pattern matched"
    block must render byte-identically whether or not a principles chain
    exists for the hit -- the additive section must never alter it."""

    def test_nudge_block_unchanged_with_chain_present(self):
        server = fresh_server()
        cs_head = _seed_cornerstone(server, "NudgeCS", recall_summary="nudge cs summary")
        ls1 = _seed_lesson(
            server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            recall_summary="lesson summary", generalized_by=cs_head,
        )
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [(r"rm -rf", "Think twice before rm -rf.", ls1)]

        out = _run_hook_main(hook_input, tripwires=tripwires, db_path=_db_path())
        response = json.loads(out)
        additional_context = response["hookSpecificOutput"]["additionalContext"]

        expected_nudge_block = (
            "[lesson-tripwire] Action-moment pattern matched — remember:\n"
            "  • Think twice before rm -rf."
        )
        self.assertIn(expected_nudge_block, additional_context)
        # And the additive section is present too, appended AFTER it.
        self.assertIn("[triggered principles]", additional_context)
        self.assertGreater(
            additional_context.index("[triggered principles]"),
            additional_context.index(expected_nudge_block),
        )

    def test_nudge_block_unchanged_with_no_chain(self):
        """Same nudge block text, byte-identical, when the hit has NO
        generalizes ancestor at all (no additive section rendered)."""
        server = fresh_server()
        ls1 = _seed_lesson(server, pattern=r"rm -rf", nudge="Think twice before rm -rf.")
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [(r"rm -rf", "Think twice before rm -rf.", ls1)]

        out_with_db = _run_hook_main(hook_input, tripwires=tripwires, db_path=_db_path())
        out_without_db_arg = _run_hook_main(hook_input, tripwires=tripwires)  # real (possibly absent) DB_PATH

        response = json.loads(out_with_db)
        additional_context = response["hookSpecificOutput"]["additionalContext"]
        expected_nudge_block = (
            "[lesson-tripwire] Action-moment pattern matched — remember:\n"
            "  • Think twice before rm -rf."
        )
        self.assertIn(expected_nudge_block, additional_context)
        self.assertNotIn("[triggered principles]", additional_context)

        # Same nudge block text regardless of which DB backs the (absent-chain) lookup.
        response2 = json.loads(out_without_db_arg)
        self.assertIn(expected_nudge_block, response2["hookSpecificOutput"]["additionalContext"])


class TestHookNeverBlocksOnChainFailure(unittest.TestCase):
    """T2 'never blocks' invariant: generalization_chain() raising inside
    the new render path must never suppress the existing nudge, and must
    never turn into a permissionDecision:deny or a hook crash."""

    def test_generalization_chain_raising_does_not_suppress_nudge(self):
        server = fresh_server()
        cs_head = _seed_cornerstone(server, "CrashCS")
        ls1 = _seed_lesson(
            server, pattern=r"rm -rf", nudge="Think twice before rm -rf.",
            generalized_by=cs_head,
        )
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [(r"rm -rf", "Think twice before rm -rf.", ls1)]

        def _boom(*_a, **_kw):
            raise RuntimeError("simulated generalization_chain failure")

        with unittest.mock.patch.object(_hook, "_import_generalization_chain", return_value=_boom):
            out = _run_hook_main(hook_input, tripwires=tripwires, db_path=_db_path())

        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])
        self.assertNotIn("[triggered principles]", hso["additionalContext"])

    def test_render_triggered_principles_raising_inside_main_does_not_crash(self):
        """Even a failure OUTSIDE render_triggered_principles's own internal
        guard (simulated by monkeypatching the function itself to raise)
        must not break main() -- the call site in main() wraps it too."""
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [(r"rm -rf", "Think twice before rm -rf.", "ls_whatever")]

        with unittest.mock.patch.object(
            _hook, "render_triggered_principles", side_effect=RuntimeError("boom"),
        ):
            out = _run_hook_main(hook_input, tripwires=tripwires)

        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])


# ---------------------------------------------------------------------------
# 8. cycle_detected (WS2 round 2, #2753 -- WS1 5e14f83b's new field)
# ---------------------------------------------------------------------------

class TestCycleDetectedRendersMalformedNotice(unittest.TestCase):
    """WS1's cycle-guard fix (5e14f83b) added `cycle_detected` to
    generalization_chain()'s return dict: True only when the walk completed
    un-truncated over a non-empty node set and found NO head -- structurally
    impossible in a valid DAG. A hit whose chain reports it must render a
    visible malformed-data notice -- never the normal head/rung structure
    (there is no head to render) and never silent omission (an omitted
    section would misreport "no chain exists", which is false -- a chain
    WAS found, it's just corrupt).

    Stubs `_import_generalization_chain` (the round-1 chain-fetch import
    point) rather than constructing a real cyclic graph, per the round-2
    spec -- WS1's own write-time guard should make a real cycle unreachable
    through the guarded engram_add_* / engram_supersede tools this test
    module's fixtures use, so a stub is the only way to exercise this path
    at all. WS1's test_generalizes_edge.py builds real cyclic fixtures, but
    only by reaching past the guard with its own low-level `_new_conn` /
    `_add_edge` helpers (not exposed here, and out of this round's scope);
    see `test_cycle_detected_is_false_on_a_healthy_chain` and its siblings
    there for the chain-layer control this render-layer test does not
    duplicate.
    """

    def _stub_chain(self, cycle_detected_ids, normal_chains=None):
        """A minimal fake generalization_chain(conn, node_id) -- ids in
        cycle_detected_ids come back cycle_detected=True/heads=[]; ids in
        normal_chains return the caller-supplied dict verbatim; anything
        else returns an "already a head, no ancestor" chain (the D3
        no-op case), matching real generalization_chain's documented shape
        in each case."""
        cycle_detected_ids = set(cycle_detected_ids)
        normal_chains = normal_chains or {}

        def _chain(conn, node_id):
            if node_id in cycle_detected_ids:
                return {
                    "start_id": node_id,
                    "start_resolved_id": node_id,
                    "roots": [node_id],
                    "nodes": {node_id: {"original_id": node_id, "parents": [node_id], "depth": 0}},
                    "heads": [],
                    "truncated": False,
                    "cycle_detected": True,
                }
            if node_id in normal_chains:
                return normal_chains[node_id]
            return {
                "start_id": node_id,
                "start_resolved_id": node_id,
                "roots": [node_id],
                "nodes": {node_id: {"original_id": node_id, "parents": [], "depth": 0}},
                "heads": [node_id],
                "truncated": False,
                "cycle_detected": False,
            }
        return _chain

    def test_cycle_detected_hit_renders_warning_not_normal_chain(self):
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_cyclic"}),
        ):
            block = render_triggered_principles([_hit("ls_cyclic")], db_path=_db_path())

        self.assertIn("[triggered principles]", block)
        self.assertIn("ls_cyclic", block)
        self.assertIn("malformed", block.lower())
        # Never the normal per-rung "↑ <id> — <text>" form for this node.
        self.assertNotIn("↑ ls_cyclic —", block)

    def test_cycle_detected_hit_is_not_silently_omitted(self):
        """The section must be emitted at all (never "") when the ONLY hit
        is cycle-detected -- omission would misreport "no chain exists"."""
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_cyclic"}),
        ):
            block = render_triggered_principles([_hit("ls_cyclic")], db_path=_db_path())
        self.assertNotEqual(block, "")

    def test_cycle_detected_hit_alongside_a_healthy_chain_hit_renders_both(self):
        """A cycle-detected hit does not crowd out, merge into, or get
        crowded out by a normal chain-bearing hit within the same fire."""
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        healthy_chain = {
            "start_id": "ls_healthy",
            "start_resolved_id": "ls_healthy",
            "roots": ["ls_healthy"],
            "nodes": {
                "cs_head": {
                    "original_id": "cs_head", "parents": [], "depth": 1,
                    "recall_summary": "head summary", "claim": "HeadClaim",
                },
                "ls_healthy": {
                    "original_id": "ls_healthy", "parents": ["cs_head"], "depth": 0,
                    "recall_summary": "healthy lesson summary",
                },
            },
            "heads": ["cs_head"],
            "truncated": False,
            "cycle_detected": False,
        }
        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_cyclic"}, normal_chains={"ls_healthy": healthy_chain}),
        ):
            block = render_triggered_principles(
                [_hit("ls_cyclic"), _hit("ls_healthy")], db_path=_db_path(),
            )

        self.assertIn("malformed", block.lower())
        self.assertIn("ls_cyclic", block)
        self.assertIn("↑ cs_head — HeadClaim", block)
        self.assertIn("↑ ls_healthy — healthy lesson summary", block)

    def test_cycle_detected_never_raises_never_blocks(self):
        """Same T2 'never blocks' posture as every other path here: a
        cycle-detected chain must never propagate as an exception or a
        permissionDecision through main()."""
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [(r"rm -rf", "Think twice before rm -rf.", "ls_cyclic")]

        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_cyclic"}),
        ):
            out = _run_hook_main(hook_input, tripwires=tripwires, db_path=_db_path())

        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])
        self.assertIn("malformed", hso["additionalContext"].lower())


# ---------------------------------------------------------------------------
# 9. truncated (WS2 round 3, #2753 -- present since WS1's original commit,
#    read by render_triggered_principles for the first time this round)
# ---------------------------------------------------------------------------

class TestTruncatedRendersPartialNotice(unittest.TestCase):
    """A hit whose chain reports `heads: []`, `cycle_detected: False`, and
    `truncated: True` has real partial ancestry -- max_depth was hit before
    a head was reached, not a malformed graph. Round 1+2 fell this case
    into the exact same silent skip as a genuinely head-less/malformed
    node (reviewer-fairy's non-blocking round-2 suggestion on PR #2814).
    This class confirms the fix: a visible, distinctly-worded notice
    (never the cycle-detected `⚠`/"malformed" wording, never the normal
    per-rung "↑ <id> — <text>" form, never silent omission).
    """

    def _stub_chain(self, truncated_ids, normal_chains=None):
        """A minimal fake generalization_chain(conn, node_id) -- ids in
        truncated_ids come back heads=[]/truncated=True/cycle_detected=False;
        ids in normal_chains return the caller-supplied dict verbatim;
        anything else returns an "already a head, no ancestor" chain (the
        D3 no-op case)."""
        truncated_ids = set(truncated_ids)
        normal_chains = normal_chains or {}

        def _chain(conn, node_id):
            if node_id in truncated_ids:
                return {
                    "start_id": node_id,
                    "start_resolved_id": node_id,
                    "roots": [node_id],
                    "nodes": {node_id: {"original_id": node_id, "parents": [], "depth": 0}},
                    "heads": [],
                    "truncated": True,
                    "cycle_detected": False,
                }
            if node_id in normal_chains:
                return normal_chains[node_id]
            return {
                "start_id": node_id,
                "start_resolved_id": node_id,
                "roots": [node_id],
                "nodes": {node_id: {"original_id": node_id, "parents": [], "depth": 0}},
                "heads": [node_id],
                "truncated": False,
                "cycle_detected": False,
            }
        return _chain

    def test_truncated_hit_renders_notice_not_normal_chain_not_cycle_wording(self):
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}),
        ):
            block = render_triggered_principles([_hit("ls_truncated")], db_path=_db_path())

        self.assertIn("[triggered principles]", block)
        self.assertIn("ls_truncated", block)
        self.assertIn("truncated", block.lower())
        # Never the normal per-rung "↑ <id> — <text>" form for this node.
        self.assertNotIn("↑ ls_truncated —", block)
        # Never the cycle-detected wording -- these are distinct states.
        self.assertNotIn("malformed", block.lower())
        self.assertNotIn("⚠", block)

    def test_truncated_hit_is_not_silently_omitted(self):
        """The section must be emitted at all (never "") when the ONLY hit
        is truncated-with-no-head -- omission would misreport "no chain
        exists" for a hit with real (if unreached) ancestry."""
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}),
        ):
            block = render_triggered_principles([_hit("ls_truncated")], db_path=_db_path())
        self.assertNotEqual(block, "")

    def test_truncated_hit_alongside_a_healthy_chain_hit_renders_both(self):
        """A truncated-with-no-head hit does not crowd out, merge into, or
        get crowded out by a normal chain-bearing hit within the same
        fire."""
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        healthy_chain = {
            "start_id": "ls_healthy",
            "start_resolved_id": "ls_healthy",
            "roots": ["ls_healthy"],
            "nodes": {
                "cs_head": {
                    "original_id": "cs_head", "parents": [], "depth": 1,
                    "recall_summary": "head summary", "claim": "HeadClaim",
                },
                "ls_healthy": {
                    "original_id": "ls_healthy", "parents": ["cs_head"], "depth": 0,
                    "recall_summary": "healthy lesson summary",
                },
            },
            "heads": ["cs_head"],
            "truncated": False,
            "cycle_detected": False,
        }
        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}, normal_chains={"ls_healthy": healthy_chain}),
        ):
            block = render_triggered_principles(
                [_hit("ls_truncated"), _hit("ls_healthy")], db_path=_db_path(),
            )

        self.assertIn("truncated", block.lower())
        self.assertIn("ls_truncated", block)
        self.assertIn("↑ cs_head — HeadClaim", block)
        self.assertIn("↑ ls_healthy — healthy lesson summary", block)

    def test_truncated_never_raises_never_blocks(self):
        """Same T2 'never blocks' posture as every other path here: a
        truncated-with-no-head chain must never propagate as an exception
        or a permissionDecision through main()."""
        server = fresh_server()
        _seed_lesson(server, pattern=r"unused", nudge="unused")  # forces the DB file to exist
        hook_input = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
        tripwires = [(r"rm -rf", "Think twice before rm -rf.", "ls_truncated")]

        with unittest.mock.patch.object(
            _hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}),
        ):
            out = _run_hook_main(hook_input, tripwires=tripwires, db_path=_db_path())

        response = json.loads(out)
        hso = response["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("Think twice before rm -rf.", hso["additionalContext"])
        self.assertIn("truncated", hso["additionalContext"].lower())


if __name__ == "__main__":
    unittest.main()
