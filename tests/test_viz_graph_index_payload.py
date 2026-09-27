"""Contract-first tests for engram-alpha#2866 fix 4 -- scoping the
/api/graph payload into index nodes + on-demand GET /api/node.

Written BEFORE the implementation, per this repo's CLAUDE.md
"Contract-first tests" convention, against the design-of-record comment
on issue #2866 (2026-09-26). These tests MUST fail on
origin/dev (get_graph_index/get_node don't exist yet; /api/graph still
serves get_graph's full payload; /api/node doesn't exist) and pass once
graph.py + server.py are implemented against this contract.

INDEX WHITELIST (exactly these keys per index node):
    id, type, status, is_current, confidence, importance_score,
    created_at, tainted_by, stale_by, source_domain, source_title, label
and, ONLY for nodes of type `definition`, `term`.

Import-pattern contract (#948 wave B -- R1): this file imports engram_core,
engram_observation, engram_derive, engram_epistemic, and engram_revision
directly and contains no `import server`.

House style for the HTTP-layer tests (4/5/6) follows
tests/test_viz_server_gzip_threading.py and
tests/test_viz_server_cascade_markers.py: a real HTTPServer + urlopen
against it (no mocking of the HTTP layer), and a real cascade_markers
table built through the ordinary public API (retract/supersede), not a
metadata fabrication.
"""
from __future__ import annotations

import atexit
import gzip
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 -- must follow sys.path setup

import engram_core  # noqa: E402
import engram_derive  # noqa: E402
import engram_epistemic  # noqa: E402
import engram_observation  # noqa: E402
import engram_revision  # noqa: E402
from viz import agents as viz_agents  # noqa: E402
from viz import server as viz_server  # noqa: E402
from viz.data import graph as viz_graph_data  # noqa: E402


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_viz_graph_index_payload_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

# The 11 plain (non-derived) whitelist keys -- "label" and "term" are
# derived fields handled separately below, not raw columns copied 1:1.
PLAIN_WHITELIST_KEYS = {
    "id", "type", "status", "is_current", "confidence", "importance_score",
    "created_at", "tainted_by", "stale_by", "source_domain", "source_title",
}
FULL_WHITELIST_KEYS = PLAIN_WHITELIST_KEYS | {"label"}  # + "term" for definitions only


def _make_fresh_mods():
    """Mirrors tests/test_viz_server_cascade_markers.py's _make_fresh_mods,
    extended with engram_epistemic for the definition-node fixture."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    epistemic = importlib.import_module("engram_epistemic")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, epistemic, core


def _seed_ob(obs, claim="Test claim.", url="https://example.com/src",
             interpretation=None):
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Source",
        quoted_text=claim,
        interpretation=interpretation or f"Interpretation: {claim}",
        claim=claim,
        quote_type="hard_data",
    ))
    assert "error" not in r, f"Failed to create observation: {r}"
    return r["observation_id"]


# Varied (non-repeating) heavy text per node, so the fixture's heavy fields
# (interpretation, logical_chain) resemble a real graph closely enough for
# the gzip size-ratio test (#7) to be discriminating. An IDENTICAL long
# string reused across every node was tried first and produced a
# non-discriminating fixture (measured ratio ~34%, ABOVE the 25% bound) --
# gzip's dictionary crushes cross-node repetition disproportionately in the
# FULL payload (which carries that repeated text many times over) while the
# index payload (distinct id/timestamp per node) doesn't benefit the same
# way, so identical filler biases the ratio artificially high relative to a
# real graph's genuinely-varied per-node text. Per the spec: "make the
# fixture large/realistic enough that the ratio is meaningful ... if you
# cannot make it discriminating, STOP and report rather than weakening the
# threshold" -- this is that fix, not a threshold weakening.
_VOCAB = ["engram", "cascade", "provenance", "derivation", "claim", "confidence",
          "observation", "quote", "interpretation", "schema", "graph", "tenant",
          "payload", "index", "node", "edge", "retract", "supersede", "taint",
          "stale", "memory", "tier", "importance", "recall", "keyword", "summary",
          "epistemic", "axiom", "premise", "evidence", "citation"]


def _varied_text(seed, n_words=250):
    import random
    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCAB) for _ in range(n_words))


def _build_fixture_graph():
    """A realistic fixture: >=1 observation, >=1 derivation, >=1 definition,
    and (per test 3's explicit requirement) a node with BOTH tainted_by and
    stale_by set, built via real retract/supersede cascades -- mirrors
    test_viz_server_cascade_markers.py's established pattern (real
    cascade_markers rows, not a metadata fabrication). Heavy text fields
    are seeded with DISTINCT, non-repeating content per node (see
    _varied_text above) so the size-ratio test is discriminating.

    Returns (db_path, ids) where ids is a dict of the fixture's node ids
    keyed by role, for the tests below to reference.
    """
    rev, obs, derive, epistemic, core = _make_fresh_mods()

    dv_tainted_logical_chain = _varied_text("dv_tainted")
    ob_retract_root = _seed_ob(obs, "Will be retracted.",
                                url="https://example.com/src-0",
                                interpretation=_varied_text("ob_retract_root"))
    dv_tainted = json.loads(derive._derive_impl(
        claim="Derived from the soon-to-be-retracted node.",
        supporting_ids=ob_retract_root,
        logical_chain=dv_tainted_logical_chain,
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in dv_tainted, dv_tainted
    dv_tainted_id = dv_tainted["derivation_id"]

    ob_supersede_root = _seed_ob(obs, "Will be superseded.",
                                  url="https://example.com/src-1",
                                  interpretation=_varied_text("ob_supersede_root"))
    dv_stale = json.loads(derive._derive_impl(
        claim="Derived from the soon-to-be-superseded node.",
        supporting_ids=ob_supersede_root,
        logical_chain=_varied_text("dv_stale"),
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in dv_stale, dv_stale
    dv_stale_id = dv_stale["derivation_id"]

    ret = json.loads(rev._retract_impl(
        node_id=ob_retract_root,
        error_type="fabricated_quote",
        reason="Test retract for viz-index-payload coverage.",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test",
    ))
    assert ret.get("status") == "retracted", ret

    ob_new = _seed_ob(obs, "Replacement for the superseded node.",
                       url="https://example.com/src-2",
                       interpretation=_varied_text("ob_new"))
    sup = json.loads(rev._supersede_impl(
        old_node_id=ob_supersede_root,
        new_node_id=ob_new,
    ))
    assert sup.get("status") == "superseded", sup

    def_result = json.loads(epistemic._add_definition_impl(
        term="recession",
        definition=("Two consecutive quarters of negative GDP growth. "
                    + _varied_text("definition")),
    ))
    assert def_result.get("status") == "created", def_result
    def_id = def_result["definition_id"]

    # Enough extra, untouched nodes -- each with DISTINCT heavy text -- that
    # the fixture is more than a minimal toy graph and the size ratio
    # reflects a realistic mixture (a real graph has thousands of nodes with
    # non-repeating content; this fixture needs enough of the same SHAPE to
    # be discriminating, not to match the real scale).
    extra_ids = [
        _seed_ob(obs, f"Untouched observation {i}.",
                 url=f"https://example.com/src-extra-{i}",
                 interpretation=_varied_text(f"extra-{i}"))
        for i in range(30)
    ]

    db_path = str(core.DB_PATH)
    ids = {
        "ob_retract_root": ob_retract_root,
        "dv_tainted": dv_tainted_id,
        "dv_tainted_logical_chain": dv_tainted_logical_chain,
        "ob_supersede_root": ob_supersede_root,
        "dv_stale": dv_stale_id,
        "ob_new": ob_new,
        "definition": def_id,
        "extra": extra_ids,
    }
    return db_path, ids


# ---------------------------------------------------------------------------
# 1 & 2. get_graph_index -- whitelist key set (both directions) + label/term
# ---------------------------------------------------------------------------

class TestGetGraphIndexWhitelist(unittest.TestCase):
    def setUp(self):
        self.db_path, self.ids = _build_fixture_graph()

    def test_every_index_node_keys_are_subset_of_whitelist(self):
        index = viz_graph_data.get_graph_index(self.db_path)
        self.assertNotIn("error", index, index)
        for n in index["nodes"]:
            extra = set(n.keys()) - FULL_WHITELIST_KEYS - {"term"}
            self.assertEqual(
                extra, set(),
                f"index node {n.get('id')!r} carries non-whitelisted "
                f"key(s) {extra!r} -- the payload has grown back: {n!r}",
            )

    def test_every_index_node_has_every_required_whitelist_key(self):
        """The other half of the whitelist test: every PLAIN key that the
        full node actually carries (get_graph's own output) must survive
        into the index node under the same name -- and 'label' is always
        derived and present (empty string if no claim), on every node
        regardless of type."""
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        index = viz_graph_data.get_graph_index(self.db_path)
        for n in index["nodes"]:
            self.assertIn("label", n, n)
            full_node = full_by_id[n["id"]]
            for key in PLAIN_WHITELIST_KEYS:
                if key in full_node:
                    self.assertIn(
                        key, n,
                        f"index node {n['id']!r} is missing whitelist key "
                        f"{key!r} that the full node carries",
                    )

    def test_no_index_node_carries_a_key_the_full_node_lacks_with_an_invented_default(self):
        """'A whitelisted key that the full node lacks: carry it through
        with the same value/absence get_graph gives (do not invent
        defaults).' tainted_by/stale_by are the two whitelist keys that are
        genuinely absent on an untouched node (per
        test_viz_server_cascade_markers.py's own omission convention) --
        confirm the index node omits them too rather than inventing []/None."""
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        index = viz_graph_data.get_graph_index(self.db_path)
        index_by_id = {n["id"]: n for n in index["nodes"]}

        clean_id = self.ids["extra"][0]
        self.assertNotIn("tainted_by", full_by_id[clean_id])
        self.assertNotIn("stale_by", full_by_id[clean_id])
        self.assertNotIn(
            "tainted_by", index_by_id[clean_id],
            "index node invented a tainted_by default the full node doesn't carry",
        )
        self.assertNotIn(
            "stale_by", index_by_id[clean_id],
            "index node invented a stale_by default the full node doesn't carry",
        )

    def test_term_present_iff_type_is_definition(self):
        index = viz_graph_data.get_graph_index(self.db_path)
        by_id = {n["id"]: n for n in index["nodes"]}

        def_node = by_id[self.ids["definition"]]
        self.assertEqual(def_node["type"], "definition")
        self.assertIn("term", def_node)
        self.assertEqual(def_node["term"], "recession")

        non_def_node = by_id[self.ids["ob_new"]]
        self.assertNotEqual(non_def_node["type"], "definition")
        self.assertNotIn(
            "term", non_def_node,
            "'term' must be omitted (not None) for non-definition nodes",
        )

    def test_definition_term_absent_from_metadata_produces_term_none(self):
        """A definition full_node whose metadata dict has NO 'term' key at
        all (e.g. a hand-edited or pre-migration row) -- not reachable via
        engram_add_definition, which always writes
        metadata={"term": ..., "definition": ...} (see
        engram_epistemic._add_definition_impl), so a hand-built full_node
        dict -- shaped like get_graph()'s own per-row output (get_graph's
        base_cols), not a DB-seeded fixture -- is the only way to exercise
        this branch.

        Contract (engram-alpha#2866 fix 4 round 2 item 3): the index node
        must still be produced (no raise), must contain only whitelisted
        keys, and 'term' is PRESENT (not absent) with value None -- the
        implementation's `idx["term"] = term` assignment at the end of the
        definition branch runs unconditionally, regardless of whether
        `term` was actually found inside metadata.
        """
        full_node = {
            "id": "df_test_no_term_key",
            "type": "definition",
            "claim": "test: definition claim with no term in metadata",
            "created_at": "2026-09-27T00:00:00Z",
            "status": "active",
            "is_current": 1,
            "confidence": None,
            "metadata": json.dumps({"definition": "a definition with no term key"}),
        }
        idx = viz_graph_data._index_node(full_node)
        extra = set(idx.keys()) - FULL_WHITELIST_KEYS - {"term"}
        self.assertEqual(extra, set(), f"non-whitelisted key(s): {extra!r}")
        self.assertIn(
            "term", idx,
            "'term' must be PRESENT (not absent) even when metadata lacks the key",
        )
        self.assertIsNone(
            idx["term"],
            "'term' must be None (not raise) when metadata has no 'term' key",
        )

    def test_definition_metadata_none_produces_term_none(self):
        """The sibling case: metadata itself is None (the shape get_graph
        would hand back for a NULL metadata column -- also unreachable via
        engram_add_definition, which always writes a non-null JSON blob).
        This is a DIFFERENT code branch from the missing-key case above --
        `if meta:` is falsy here, so the json.loads/`.get('term')` path
        never runs at all, vs. running-but-returning-None above -- so both
        are tested even though the OUTPUT is identical (term: None) in
        both cases: the spec asks for this second case only if the code
        path distinguishes it from the first, and it does, at the control-
        flow level, even though the two converge on the same result.
        'Absent' (no "metadata" key in the dict at all) hits the identical
        `if meta:` falsy branch as `metadata: None` -- get_graph's own
        SELECT always includes the metadata column, so a real full_node
        dict is never missing the key outright; metadata=None is the
        realistic representative of that branch and covers it.
        """
        full_node = {
            "id": "df_test_metadata_none",
            "type": "definition",
            "claim": "test: definition claim with metadata=None",
            "created_at": "2026-09-27T00:00:00Z",
            "status": "active",
            "is_current": 1,
            "confidence": None,
            "metadata": None,
        }
        idx = viz_graph_data._index_node(full_node)
        extra = set(idx.keys()) - FULL_WHITELIST_KEYS - {"term"}
        self.assertEqual(extra, set(), f"non-whitelisted key(s): {extra!r}")
        self.assertIn("term", idx, "'term' must be PRESENT even when metadata is None")
        self.assertIsNone(idx["term"], "'term' must be None (not raise) when metadata is None")

    def test_label_is_first_80_chars_of_claim(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        index = viz_graph_data.get_graph_index(self.db_path)
        for n in index["nodes"]:
            full_claim = full_by_id[n["id"]].get("claim") or ""
            self.assertEqual(
                n["label"], full_claim[:80],
                f"node {n['id']!r}: label must be the first 80 chars of claim",
            )

    def test_label_is_empty_string_when_no_claim(self):
        """Every observation's underlying evidence node (created as a side
        effect of _add_observation_impl) carries claim=None in the real
        schema -- this exercises get_graph_index's actual no-claim path
        against a real fixture row, not a fabricated dict."""
        full = viz_graph_data.get_graph(self.db_path)
        evidence_nodes = [n for n in full["nodes"] if n["type"] == "evidence"]
        self.assertGreater(len(evidence_nodes), 0,
                            "fixture must contain at least one evidence node")
        for n in evidence_nodes:
            self.assertIsNone(n.get("claim"))

        index = viz_graph_data.get_graph_index(self.db_path)
        index_by_id = {n["id"]: n for n in index["nodes"]}
        for n in evidence_nodes:
            self.assertEqual(index_by_id[n["id"]]["label"], "")


# ---------------------------------------------------------------------------
# 3. get_node(id) equals get_graph's node for that id
# ---------------------------------------------------------------------------

class TestGetNode(unittest.TestCase):
    def setUp(self):
        self.db_path, self.ids = _build_fixture_graph()

    def test_get_node_matches_get_graph_for_observation(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        node = viz_graph_data.get_node(self.db_path, self.ids["ob_new"])
        self.assertEqual(node, full_by_id[self.ids["ob_new"]])

    def test_get_node_matches_get_graph_for_derivation(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        node = viz_graph_data.get_node(self.db_path, self.ids["dv_tainted"])
        self.assertEqual(node, full_by_id[self.ids["dv_tainted"]])

    def test_get_node_matches_get_graph_for_definition(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        node = viz_graph_data.get_node(self.db_path, self.ids["definition"])
        self.assertEqual(node, full_by_id[self.ids["definition"]])

    def test_get_node_matches_get_graph_for_tainted_and_stale_nodes(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}

        tainted = viz_graph_data.get_node(self.db_path, self.ids["dv_tainted"])
        self.assertEqual(tainted, full_by_id[self.ids["dv_tainted"]])
        self.assertIn("tainted_by", tainted)

        stale = viz_graph_data.get_node(self.db_path, self.ids["dv_stale"])
        self.assertEqual(stale, full_by_id[self.ids["dv_stale"]])
        self.assertIn("stale_by", stale)
        self.assertIn("stale_replacement", stale)

    def test_get_node_unknown_id_returns_none(self):
        node = viz_graph_data.get_node(self.db_path, "x_9999999")
        self.assertIsNone(node)


# ---------------------------------------------------------------------------
# HTTP-layer tests (4, 5, 6, 7) -- real HTTPServer + urlopen, per
# tests/test_viz_server_gzip_threading.py's house style.
# ---------------------------------------------------------------------------

class _HttpLayerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.ids = _build_fixture_graph()
        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test",
                           "db": cls.db_path},
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

        cls.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default

    def _get_json(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urlopen(url, timeout=5) as resp:
            return resp, json.loads(resp.read())


class TestApiGraphServesIndexOnly(_HttpLayerTestBase):
    """Test 4: /api/graph serves the index -- no heavy keys on any node."""

    _HEAVY_KEYS = {"interpretation", "quoted_text", "logical_chain", "claim",
                   "metadata"}

    def test_api_graph_has_no_heavy_keys_on_any_node(self):
        _, data = self._get_json("/api/graph?agent=test_agent")
        self.assertNotIn("error", data, data)
        self.assertGreater(len(data["nodes"]), 0)
        for n in data["nodes"]:
            present_heavy = self._HEAVY_KEYS & set(n.keys())
            self.assertEqual(
                present_heavy, set(),
                f"/api/graph node {n.get('id')!r} carries heavy key(s) "
                f"{present_heavy!r} -- the index route is leaking the full payload",
            )

    def test_api_graph_nodes_match_get_graph_index_exactly(self):
        expected = viz_graph_data.get_graph_index(self.db_path)
        _, data = self._get_json("/api/graph?agent=test_agent")
        expected_by_id = {n["id"]: n for n in expected["nodes"]}
        got_by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(set(got_by_id), set(expected_by_id))
        for nid, exp_node in expected_by_id.items():
            self.assertEqual(got_by_id[nid], exp_node, nid)

    def test_api_graph_edges_unchanged(self):
        """Scope bound: get_graph's edge payload must not change."""
        full = viz_graph_data.get_graph(self.db_path)
        _, data = self._get_json("/api/graph?agent=test_agent")
        self.assertEqual(data["edges"], full["edges"])

    def test_api_graph_response_keys_are_subset_of_whitelist_both_directions(self):
        """Test 1 (whitelist, both directions), run against the LIVE route
        response rather than the isolated get_graph_index() function --
        deliberately, so this test (unlike a function-level-only version)
        is sensitive to a regression in server.py's OWN wiring of the
        route to the index function, not just to a regression inside
        graph.py's derivation logic. This is the discriminating-control
        distinction: TestGetGraphIndexWhitelist above tests the function in
        isolation and stays green even if server.py's /api/graph route were
        wired back to get_graph -- this one does not."""
        _, data = self._get_json("/api/graph?agent=test_agent")
        for n in data["nodes"]:
            extra = set(n.keys()) - FULL_WHITELIST_KEYS - {"term"}
            self.assertEqual(
                extra, set(),
                f"/api/graph node {n.get('id')!r} carries non-whitelisted "
                f"key(s) {extra!r}: {n!r}",
            )
            self.assertIn("label", n, n)


class TestApiGraphSizeRatio(_HttpLayerTestBase):
    """Test 7 (size ratio), run against the LIVE route's actual gzip'd
    response bytes -- deliberately, for the same reason as the whitelist
    test above: this must be sensitive to server.py's route wiring, not
    just to graph.py's get_graph_index() derivation in isolation."""

    def test_api_graph_gzip_response_is_at_most_25_percent_of_full_gzip(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_gzip_len = len(gzip.compress(json.dumps(full, default=str).encode()))

        req = Request(f"http://127.0.0.1:{self.port}/api/graph?agent=test_agent",
                       headers={"Accept-Encoding": "gzip"})
        with urlopen(req, timeout=5) as resp:
            raw = resp.read()
            self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
            index_gzip_len = int(resp.headers.get("Content-Length"))
            self.assertEqual(index_gzip_len, len(raw))

        ratio = index_gzip_len / full_gzip_len
        self.assertLessEqual(
            ratio, 0.25,
            f"/api/graph's own gzip'd response ({index_gzip_len}B) is "
            f"{ratio:.1%} of the full payload's gzip ({full_gzip_len}B) -- "
            f"expected <=25%.",
        )


class TestApiNodeRoute(_HttpLayerTestBase):
    """Test 5: GET /api/node?id=<id>&agent=<a> returns the full node JSON
    (same as get_node/get_graph), with the same tenant resolution as
    /api/graph."""

    def test_api_node_returns_full_node_matching_get_node(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        target_id = self.ids["dv_tainted"]

        _, data = self._get_json(f"/api/node?id={target_id}&agent=test_agent")
        expected = dict(full_by_id[target_id])
        expected["agent"] = "test_agent"
        self.assertEqual(data, expected)

    def test_api_node_carries_heavy_fields_unlike_the_index_route(self):
        target_id = self.ids["dv_tainted"]
        _, data = self._get_json(f"/api/node?id={target_id}&agent=test_agent")
        self.assertIn("logical_chain", data)
        self.assertEqual(data["logical_chain"], self.ids["dv_tainted_logical_chain"])

    def test_api_node_definition_matches_get_node(self):
        full = viz_graph_data.get_graph(self.db_path)
        full_by_id = {n["id"]: n for n in full["nodes"]}
        target_id = self.ids["definition"]

        _, data = self._get_json(f"/api/node?id={target_id}&agent=test_agent")
        expected = dict(full_by_id[target_id])
        expected["agent"] = "test_agent"
        self.assertEqual(data, expected)

    def test_api_node_same_tenant_resolution_as_api_graph_unknown_agent(self):
        """Same mechanism as /api/graph's own db_path resolution: an unknown
        agent name resolves db_path to falsy via the identical
        viz_agents._resolve_agent() call, so both routes must behave the
        SAME way (mirrors /api/graph's own convention: 200 + an explicit
        error, since /api/graph itself does not 404 on an unknown agent --
        see server.py's `/api/graph` branch)."""
        graph_url = f"http://127.0.0.1:{self.port}/api/graph?agent=totally-unknown-agent"
        with urlopen(graph_url, timeout=5) as resp:
            graph_status = resp.status
            graph_data = json.loads(resp.read())
        self.assertEqual(graph_status, 200)
        self.assertIn("error", graph_data)

        node_url = (f"http://127.0.0.1:{self.port}/api/node"
                    f"?id={self.ids['dv_tainted']}&agent=totally-unknown-agent")
        with urlopen(node_url, timeout=5) as resp:
            node_status = resp.status
            node_data = json.loads(resp.read())
        self.assertEqual(node_status, 200)
        self.assertIn("error", node_data)


class TestApiNodeErrorContract(_HttpLayerTestBase):
    """Test 6: unknown id -> 404 with Content-Length: 0 (keep-alive clients
    hang on a body-less response without a length -- #2926's lesson).
    Missing id param -> 400 with an explicit Content-Length."""

    def test_unknown_id_returns_404_with_content_length_zero(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/node?id=x_9999999&agent=test_agent")
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 404)
        self.assertEqual(body, b"")
        self.assertEqual(resp.getheader("Content-Length"), "0")

    def test_missing_id_param_returns_400_with_explicit_content_length(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/node?agent=test_agent")
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 400)
        content_length = resp.getheader("Content-Length")
        self.assertIsNotNone(content_length)
        self.assertEqual(int(content_length), len(body))
        self.assertGreater(len(body), 0)
        data = json.loads(body)
        self.assertIn("error", data)

    def test_empty_id_param_returns_400(self):
        """id= with an empty value is functionally 'missing' -- same
        contract as no id param at all."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/node?id=&agent=test_agent")
            resp = conn.getresponse()
            resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 400)


# ---------------------------------------------------------------------------
# 7. Size test -- gzip(index JSON) <= 25% of gzip(full JSON) on the fixture.
# ---------------------------------------------------------------------------

class TestIndexPayloadSizeRatio(unittest.TestCase):
    def setUp(self):
        self.db_path, self.ids = _build_fixture_graph()

    def test_gzip_index_is_at_most_25_percent_of_gzip_full(self):
        full = viz_graph_data.get_graph(self.db_path)
        index = viz_graph_data.get_graph_index(self.db_path)

        full_bytes = json.dumps(full, default=str).encode()
        index_bytes = json.dumps(index, default=str).encode()

        full_gzip_len = len(gzip.compress(full_bytes))
        index_gzip_len = len(gzip.compress(index_bytes))

        ratio = index_gzip_len / full_gzip_len
        self.assertLessEqual(
            ratio, 0.25,
            f"index gzip ({index_gzip_len}B) is {ratio:.1%} of full gzip "
            f"({full_gzip_len}B) -- expected <=25%. If this fixture isn't "
            f"discriminating enough to exercise the ratio meaningfully, "
            f"the fixture needs more/heavier nodes, per the spec -- do not "
            f"weaken this threshold.",
        )
        # Sanity floor: the fixture must actually be non-trivial in size,
        # or a 25% pass here would be meaningless (e.g. both near-zero).
        self.assertGreater(full_gzip_len, 2000,
                            "fixture's full gzip payload is too small to be "
                            "a meaningful size-ratio test")


if __name__ == "__main__":
    unittest.main()
