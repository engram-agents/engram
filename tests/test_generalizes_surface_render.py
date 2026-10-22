"""Tests for the #2753 epic WS2 surface-hook render (D3 point 2) --
engram-surface-hook.py's additive "Triggered principles" section.

Follows the sibling patterns already established in this test suite:
  - test_recall_injection_budget.py for the hyphenated-filename module
    loader, the _claim_entry / _special_entry / _make_result fixture
    builders, and calling format_nudge(result, session_id=..., stats=...)
    directly.
  - test_generalizes_edge.py / test_lesson_tripwire.py for the real-server
    fresh_server() fixture pattern used to mint a genuine `generalizes`
    chain (cornerstone -> lesson / cornerstone -> cornerstone) backing the
    render.

Coverage:
  1. A chain-bearing Specials/Top-claims node gets a "Triggered principles"
     section appended, with the head's recall_summary rendered in full.
  2. The pre-existing "Specials:"/"Top claims:" block renders BYTE-IDENTICAL
     whether or not the additive section fires (regression guard -- the
     worst failure mode named by the WS2 spec).
  3. Render-budget cap: 5 distinct chain-bearing recalled nodes -> exactly 2
     heads render in FULL (with recall_summary), the rest as bare IDs.
  4. Cross-node head-dedupe within one prompt: 2 Specials/Top-claims nodes
     sharing a head merge into ONE head line, not two.
  5. A node with no generalizes ancestor at all renders no additive section.
  6. Hook-never-breaks-render: generalization_chain raising inside the new
     code path never affects the existing Specials/Top-claims output.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename
    (mirrors test_recall_injection_budget.py's _load_hook_module)."""
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook_ws2_test", str(hook_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_generalizes_surface_")
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
        "url": "https://example.com/ws2-surface-incident",
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


def _seed_lesson(server, *, recall_summary=None, generalized_by="", claim=None):
    inc_id = _seed_incident(server)
    payload = {
        "claim": claim or "test lesson claim",
        "incident_ids": inc_id,
        "scaffolding_nudge": "nudge",
        "logical_chain": "chain",
    }
    if recall_summary:
        payload["recall_summary"] = recall_summary
    if generalized_by:
        payload["generalized_by"] = generalized_by
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["lesson_id"]


# ---------------------------------------------------------------------------
# Fixture builders (mirrors test_recall_injection_budget.py's style)
# ---------------------------------------------------------------------------

def _special_entry(*, node_id, claim="an axiom", ntype="lesson", confidence=None,
                    recall_summary=None, recall_keywords=None):
    return {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "confidence": confidence,
        "recall_summary": recall_summary,
        "recall_keywords": recall_keywords or ["kw"],
        "created_ago": "2d ago",
    }


def _make_result(*, special_nodes=None, top_claims=None):
    sn = special_nodes or []
    tc = top_claims or []
    mc = len(sn) + len(tc)
    mids = [n["id"] for n in sn + tc]
    mm = [
        {"id": n["id"], "type": n.get("type"), "recall_keywords": n.get("recall_keywords")}
        for n in sn + tc
    ]
    return {
        "match_count": mc,
        "special_nodes": sn,
        "top_claims": tc,
        "type_counts": {},
        "age": {"not_recalled_recently": []},
        "issues": {},
        "matched_ids": mids,
        "matched_meta": mm,
    }


class SurfaceRenderTestBase(unittest.TestCase):
    def setUp(self):
        self.server = fresh_server()
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = TEST_DIR
        self.hook.KNOWLEDGE_DB_PATH = str(_db_path())


# ---------------------------------------------------------------------------
# 1. Chain-bearing node gets a "Triggered principles" section
# ---------------------------------------------------------------------------

class TestTriggeredPrinciplesSectionRenders(SurfaceRenderTestBase):

    def test_special_node_with_chain_gets_triggered_principles_section(self):
        cs_head = self.server_cs = _seed_cornerstone(
            self.server, "SurfaceHeadCS", recall_summary="surface head summary",
        )
        ls1 = _seed_lesson(
            self.server, recall_summary="surface lesson summary", generalized_by=cs_head,
        )
        result = _make_result(special_nodes=[_special_entry(node_id=ls1, recall_summary="surface lesson summary")])

        nudge = self.hook.format_nudge(result, session_id="s1", stats={})

        self.assertIn("  Specials:", nudge)
        self.assertIn(ls1, nudge)
        self.assertIn("  Triggered principles:", nudge)
        self.assertIn(f"↑ principle: {cs_head} — surface head summary", nudge)

    def test_no_ancestor_node_gets_no_section(self):
        ls1 = _seed_lesson(self.server, recall_summary="lonely lesson summary")
        result = _make_result(special_nodes=[_special_entry(node_id=ls1, recall_summary="lonely lesson summary")])

        nudge = self.hook.format_nudge(result, session_id="s1", stats={})

        self.assertNotIn("Triggered principles", nudge)


# ---------------------------------------------------------------------------
# 2. Byte-identical Specials/Top-claims regression guard
# ---------------------------------------------------------------------------

class TestSpecialsBlockByteIdentical(SurfaceRenderTestBase):

    def test_specials_block_unchanged_with_and_without_chain(self):
        ls_with_chain = _seed_lesson(self.server, recall_summary="chain lesson summary")
        cs_head = _seed_cornerstone(self.server, "ByteIdCS", recall_summary="byte id head summary")
        ls_with_chain_2 = _seed_lesson(
            self.server, recall_summary="chain lesson summary", generalized_by=cs_head,
        )

        result_no_chain = _make_result(
            special_nodes=[_special_entry(node_id=ls_with_chain, recall_summary="chain lesson summary")],
        )
        result_with_chain = _make_result(
            special_nodes=[_special_entry(node_id=ls_with_chain_2, recall_summary="chain lesson summary")],
        )

        nudge_no_chain = self.hook.format_nudge(result_no_chain, session_id="s1", stats={})
        nudge_with_chain = self.hook.format_nudge(result_with_chain, session_id="s2", stats={})

        # Both renders share the identical Specials block SHAPE (same
        # entry content modulo node id) -- extract and compare the
        # "  Specials:" line-group, which must be byte-identical in
        # structure regardless of the additive section below it.
        def _specials_block(nudge, node_id):
            lines = nudge.splitlines()
            start = lines.index("  Specials:")
            # Specials block is exactly the header + one entry line (+ any
            # continuation) up to the next top-level section or EOF.
            block = [lines[start]]
            for line in lines[start + 1:]:
                if line.startswith("  ") and not line.startswith("    "):
                    break
                block.append(line)
            return "\n".join(block).replace(node_id, "<NID>")

        specials_no_chain = _specials_block(nudge_no_chain, ls_with_chain)
        specials_with_chain = _specials_block(nudge_with_chain, ls_with_chain_2)
        self.assertEqual(specials_no_chain, specials_with_chain)

        # And the additive section only appears in the WITH-chain render.
        self.assertNotIn("Triggered principles", nudge_no_chain)
        self.assertIn("Triggered principles", nudge_with_chain)


# ---------------------------------------------------------------------------
# 3. Render-budget cap -- exactly 2 full chain blocks, the rest bare IDs
# ---------------------------------------------------------------------------

class TestRenderBudgetCap(SurfaceRenderTestBase):

    def test_five_distinct_chains_cap_at_two_full_blocks(self):
        heads = []
        specials = []
        for i in range(5):
            cs_head = _seed_cornerstone(
                self.server, f"CapCS{i}", recall_summary=f"cap head summary {i}",
            )
            ls = _seed_lesson(
                self.server, recall_summary=f"cap lesson summary {i}", generalized_by=cs_head,
            )
            heads.append(cs_head)
            specials.append(_special_entry(node_id=ls, recall_summary=f"cap lesson summary {i}"))
        # Only the first 3 fit the Specials-loop's own [:3] cap; use the
        # remaining 2 as top_claims so all 5 candidates actually render in
        # Specials/Top claims this prompt (the section's own precondition).
        result = _make_result(special_nodes=specials[:3], top_claims=specials[3:])

        nudge = self.hook.format_nudge(result, session_id="s1", stats={})

        full_lines = [l for l in nudge.splitlines() if "↑ principle:" in l and " — " in l]
        bare_lines = [
            l for l in nudge.splitlines()
            if "↑ principle:" in l and " — " not in l
        ]
        self.assertEqual(len(full_lines), 2, nudge)
        self.assertEqual(len(bare_lines), 3, nudge)
        # Every head appears exactly once (deduped, no double-render).
        for head_id in heads:
            self.assertEqual(
                sum(1 for l in nudge.splitlines() if head_id in l and "↑ principle:" in l),
                1,
                nudge,
            )


# ---------------------------------------------------------------------------
# 4. Cross-node head-dedupe within one prompt
# ---------------------------------------------------------------------------

class TestCrossNodeHeadDedupe(SurfaceRenderTestBase):

    def test_two_recalled_nodes_sharing_a_head_merge_into_one_line(self):
        cs_head = _seed_cornerstone(self.server, "SharedSurfaceCS", recall_summary="shared surface summary")
        ls1 = _seed_lesson(self.server, recall_summary="lesson one", generalized_by=cs_head)
        ls2 = _seed_lesson(self.server, recall_summary="lesson two", generalized_by=cs_head)

        result = _make_result(
            special_nodes=[_special_entry(node_id=ls1, recall_summary="lesson one")],
            top_claims=[_special_entry(node_id=ls2, recall_summary="lesson two", ntype="observation_factual")],
        )
        nudge = self.hook.format_nudge(result, session_id="s1", stats={})

        principle_lines = [l for l in nudge.splitlines() if cs_head in l and "↑ principle:" in l]
        self.assertEqual(len(principle_lines), 1, nudge)


# ---------------------------------------------------------------------------
# 6. Failure isolation -- a raising generalization_chain never breaks render
# ---------------------------------------------------------------------------

class TestFailureIsolation(SurfaceRenderTestBase):

    def test_generalization_chain_raising_does_not_break_specials_render(self):
        cs_head = _seed_cornerstone(self.server, "CrashSurfaceCS")
        ls1 = _seed_lesson(self.server, recall_summary="crash lesson summary", generalized_by=cs_head)
        result = _make_result(special_nodes=[_special_entry(node_id=ls1, recall_summary="crash lesson summary")])

        def _boom(*_a, **_kw):
            raise RuntimeError("simulated generalization_chain failure")

        with unittest.mock.patch.object(self.hook, "_import_generalization_chain", return_value=_boom):
            nudge = self.hook.format_nudge(result, session_id="s1", stats={})

        self.assertIn("  Specials:", nudge)
        self.assertIn(ls1, nudge)
        self.assertNotIn("Triggered principles", nudge)


# ---------------------------------------------------------------------------
# 7. cycle_detected (WS2 round 2, #2753 -- WS1 5e14f83b's new field)
# ---------------------------------------------------------------------------

class TestCycleDetectedRendersMalformedNotice(SurfaceRenderTestBase):
    """Sibling of the tripwire hook's identically-named test class -- same
    rationale (see that module's docstring for the full write-up: WS1's
    cycle-guard fix, 5e14f83b, added `cycle_detected` to
    generalization_chain()'s return dict, and a candidate whose chain
    reports it must render a visible malformed-data notice rather than the
    normal head/rung structure or silent omission). Applied here to the
    surface hook's per-prompt "Triggered principles" section instead of the
    tripwire hook's per-fire "[triggered principles]" one.

    Stubs `_import_generalization_chain`, same rationale as the tripwire
    hook's sibling class: WS1's write-time guard should make a real cycle
    unreachable through the guarded engram_add_* tools this module's own
    fixtures use.
    """

    def _stub_chain(self, cycle_detected_ids, normal_chains=None):
        """See the tripwire hook's identically-named helper -- same shape,
        same semantics, duplicated rather than imported because the two
        hook modules are loaded via separate importlib spec_from_file_location
        calls with no shared test-support module between them."""
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

    def test_cycle_detected_candidate_renders_warning_not_normal_line(self):
        _seed_lesson(self.server, recall_summary="unused")  # forces the DB file to exist
        with unittest.mock.patch.object(
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_cyclic"}),
        ):
            block = self.hook._render_triggered_principles_section(["ls_cyclic"], db_path=_db_path())

        self.assertIn("  Triggered principles:", block)
        self.assertIn("ls_cyclic", block)
        self.assertIn("malformed", block.lower())
        # Never the normal "↑ principle: <id>[ — <summary>]" form for this id.
        self.assertNotIn("↑ principle: ls_cyclic", block)

    def test_cycle_detected_candidate_is_not_silently_omitted(self):
        """The section must be emitted at all (never "") when the ONLY
        candidate is cycle-detected -- omission would misreport "no chain
        exists"."""
        _seed_lesson(self.server, recall_summary="unused")
        with unittest.mock.patch.object(
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_cyclic"}),
        ):
            block = self.hook._render_triggered_principles_section(["ls_cyclic"], db_path=_db_path())
        self.assertNotEqual(block, "")

    def test_cycle_detected_candidate_alongside_a_healthy_head_renders_both(self):
        """A cycle-detected candidate does not crowd out, merge into, or
        get crowded out by a normal chain-bearing candidate this prompt."""
        _seed_lesson(self.server, recall_summary="unused")
        healthy_chain = {
            "start_id": "ls_healthy",
            "start_resolved_id": "ls_healthy",
            "roots": ["ls_healthy"],
            "nodes": {
                "cs_head": {
                    "original_id": "cs_head", "parents": [], "depth": 1,
                    "recall_summary": "head summary",
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
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_cyclic"}, normal_chains={"ls_healthy": healthy_chain}),
        ):
            block = self.hook._render_triggered_principles_section(
                ["ls_cyclic", "ls_healthy"], db_path=_db_path(),
            )

        self.assertIn("malformed", block.lower())
        self.assertIn("ls_cyclic", block)
        self.assertIn("↑ principle: cs_head — head summary", block)

    def test_cycle_detected_never_breaks_specials_render(self):
        """T2 'never blocks' posture, end-to-end through format_nudge: a
        cycle-detected chain must never suppress Specials/Top claims."""
        ls1 = _seed_lesson(self.server, recall_summary="crash lesson summary")
        result = _make_result(
            special_nodes=[_special_entry(node_id=ls1, recall_summary="crash lesson summary")],
        )
        with unittest.mock.patch.object(
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({ls1}),
        ):
            nudge = self.hook.format_nudge(result, session_id="s1", stats={})

        self.assertIn("  Specials:", nudge)
        self.assertIn(ls1, nudge)
        self.assertIn("  Triggered principles:", nudge)
        self.assertIn("malformed", nudge.lower())


# ---------------------------------------------------------------------------
# 8. truncated (WS2 round 3, #2753 -- present since WS1's original commit,
#    read by _render_triggered_principles_section for the first time this
#    round)
# ---------------------------------------------------------------------------

class TestTruncatedRendersPartialNotice(SurfaceRenderTestBase):
    """Sibling of the tripwire hook's identically-named test class -- same
    rationale (see that module's docstring for the full write-up: a
    candidate whose chain reports `heads: []`, `cycle_detected: False`, and
    `truncated: True` has real partial ancestry cut off by max_depth, not a
    malformed graph, and must render a visibly distinct "chain truncated"
    notice rather than the normal head/rung structure, the cycle-detected
    malformed wording, or silent omission).
    """

    def _stub_chain(self, truncated_ids, normal_chains=None):
        """See the tripwire hook's identically-named helper -- same shape,
        same semantics, duplicated rather than imported for the same
        reason as the cycle_detected sibling above."""
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

    def test_truncated_candidate_renders_notice_not_normal_line_not_malformed(self):
        _seed_lesson(self.server, recall_summary="unused")  # forces the DB file to exist
        with unittest.mock.patch.object(
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}),
        ):
            block = self.hook._render_triggered_principles_section(["ls_truncated"], db_path=_db_path())

        self.assertIn("  Triggered principles:", block)
        self.assertIn("ls_truncated", block)
        self.assertIn("truncated", block.lower())
        # Never the normal "↑ principle: <id>[ — <summary>]" form for this id.
        self.assertNotIn("↑ principle: ls_truncated", block)
        # Never the cycle-detected wording -- these are distinct states.
        self.assertNotIn("malformed", block.lower())
        self.assertNotIn("⚠", block)

    def test_truncated_candidate_is_not_silently_omitted(self):
        """The section must be emitted at all (never "") when the ONLY
        candidate is truncated-with-no-head -- omission would misreport
        "no chain exists" for a candidate with real (if unreached)
        ancestry."""
        _seed_lesson(self.server, recall_summary="unused")
        with unittest.mock.patch.object(
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}),
        ):
            block = self.hook._render_triggered_principles_section(["ls_truncated"], db_path=_db_path())
        self.assertNotEqual(block, "")

    def test_truncated_candidate_alongside_a_healthy_head_renders_both(self):
        """A truncated-with-no-head candidate does not crowd out, merge
        into, or get crowded out by a normal chain-bearing candidate this
        prompt."""
        _seed_lesson(self.server, recall_summary="unused")
        healthy_chain = {
            "start_id": "ls_healthy",
            "start_resolved_id": "ls_healthy",
            "roots": ["ls_healthy"],
            "nodes": {
                "cs_head": {
                    "original_id": "cs_head", "parents": [], "depth": 1,
                    "recall_summary": "head summary",
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
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({"ls_truncated"}, normal_chains={"ls_healthy": healthy_chain}),
        ):
            block = self.hook._render_triggered_principles_section(
                ["ls_truncated", "ls_healthy"], db_path=_db_path(),
            )

        self.assertIn("truncated", block.lower())
        self.assertIn("ls_truncated", block)
        self.assertIn("↑ principle: cs_head — head summary", block)

    def test_truncated_never_breaks_specials_render(self):
        """T2 'never blocks' posture, end-to-end through format_nudge: a
        truncated-with-no-head chain must never suppress Specials/Top
        claims."""
        ls1 = _seed_lesson(self.server, recall_summary="crash lesson summary")
        result = _make_result(
            special_nodes=[_special_entry(node_id=ls1, recall_summary="crash lesson summary")],
        )
        with unittest.mock.patch.object(
            self.hook, "_import_generalization_chain",
            return_value=self._stub_chain({ls1}),
        ):
            nudge = self.hook.format_nudge(result, session_id="s1", stats={})

        self.assertIn("  Specials:", nudge)
        self.assertIn(ls1, nudge)
        self.assertIn("  Triggered principles:", nudge)
        self.assertIn("truncated", nudge.lower())


if __name__ == "__main__":
    unittest.main()
