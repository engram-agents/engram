"""Mechanical guard: no test may read the retired cascade metadata store.

WHY THIS EXISTS. Slice 1 (#2013/#2009) moved cascade state off
``metadata.tainted_by`` / ``metadata.stale_by`` / ``metadata.stale_replacement``
into the ``cascade_markers`` table. Nothing writes those metadata keys any more,
so a NEGATIVE assertion over them is permanently true and cannot fail — it reads
as a passing guard while guarding nothing. Five such sites were found by hand
during this slice, the last one inside the migration commit's own file with four
of its five siblings correctly converted.

WHY IT IS AN AST WALK AND NOT A REGEX. The first version of this file WAS a
regex pair, and a reviewer falsified its central claim: it only matched when the
receiver happened to be named ``meta``/``metadata`` on a single line, so a
renamed receiver, an inline ``.get()``, a dict subscript, and any multi-line
assert all slipped through. That is the same mistake this guard exists to
correct, committed one level up — the earlier fixes enumerated SITES, and the
regex then enumerated SHAPES. An AST walk matches on the retired key being read
out of a mapping at all, regardless of spelling, receiver name, or line breaks.

SCOPE NOTES.
  * ``support_lost`` is NOT part of the migration and legitimately still lives
    in metadata, so it is not a retired key and is never flagged.
  * ``warnings.tainted_by`` / ``warnings.stale_by`` on an inspect response are
    RENDER keys, not the store — the response still carries them by design.
    Reads whose base expression mentions a response container are exempt.
"""

import ast
import pathlib
import re

TESTS_DIR = pathlib.Path(__file__).resolve().parent

RETIRED_KEYS = frozenset({"tainted_by", "stale_by", "stale_replacement"})

# The retired STORE is a node's ``metadata`` blob. A retired key read out of
# anything else — a viz/inspect response payload, a retract return value — is a
# RENDER key, still populated by design, and must not be flagged. So the
# discriminator is provenance: does this read come from metadata?
#
# KNOWN BOUND, stated rather than papered over: provenance is tracked BY NAME —
# any expression whose source mentions "meta", plus variables assigned from one.
# A metadata read laundered through a name carrying NO trace of metadata
# (``json.loads(raw).get("stale_by")``) is NOT caught.
#
# The substring is deliberately loose rather than word-bounded. A word-bounded
# version missed ``metadata_blob`` / ``node_metadata`` / ``raw_metadata_dict``,
# which plainly DO carry a metadata trace — so the documented bound was narrower
# than the real one, which is the same overclaim this file already had to correct
# once. For a SAFETY guard the bias belongs on over-flagging: a false positive is
# loud and self-correcting (rename the variable), while a false negative is
# exactly the silent class this file exists to prevent. The cost is that a name
# like ``metaclass`` would be flagged near a retired key; that is loud, and it
# has no instances.
_METADATA_HINT = re.compile(r"meta", re.I)


class _RetiredKeyReadVisitor(ast.NodeVisitor):
    """Flags every read of a retired key out of a mapping, in any spelling."""

    def __init__(self, source: str):
        self.source = source
        self.hits: list[tuple[int, str]] = []
        self.metadata_names = self._metadata_bound_names(source)

    @staticmethod
    def _metadata_bound_names(source: str) -> set:
        """Names assigned from a metadata-bearing expression, so a renamed
        receiver (``node_meta = json.loads(row["metadata"])``) is still tracked.
        """
        names = set()
        for n in ast.walk(ast.parse(source)):
            if isinstance(n, ast.Assign):
                rhs = ast.get_source_segment(source, n.value) or ""
                if _METADATA_HINT.search(rhs):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
        return names

    def _from_metadata(self, base_node) -> bool:
        seg = ast.get_source_segment(self.source, base_node) or ""
        if _METADATA_HINT.search(seg):
            return True
        # a bare name assigned earlier from a metadata-bearing expression
        if isinstance(base_node, ast.Name):
            return base_node.id in self.metadata_names
        return False

    def _flag(self, node, base_node):
        if not self._from_metadata(base_node):
            return  # render key on a response payload, not the retired store
        seg = ast.get_source_segment(self.source, node) or ""
        self.hits.append((node.lineno, seg.replace("\n", " ")[:120]))

    def visit_Compare(self, node):
        # "tainted_by" in meta   /   "tainted_by" not in meta
        if (isinstance(node.left, ast.Constant)
                and node.left.value in RETIRED_KEYS
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)):
            self._flag(node, node.comparators[0])
        self.generic_visit(node)

    def visit_Subscript(self, node):
        # meta["tainted_by"]  /  anything["stale_by"]
        sl = node.slice
        if isinstance(sl, ast.Constant) and sl.value in RETIRED_KEYS:
            self._flag(node, node.value)
        self.generic_visit(node)

    def visit_Call(self, node):
        # <anything>.get("tainted_by", ...)  — receiver name irrelevant
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in RETIRED_KEYS):
            self._flag(node, func.value)
        self.generic_visit(node)


def _scan(source: str) -> list:
    v = _RetiredKeyReadVisitor(source)
    v.visit(ast.parse(source))
    return v.hits


def _offending():
    out = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue  # this file names the keys it bans
        src = path.read_text()
        for lineno, seg in _scan(src):
            out.append(f"{path.name}:{lineno}: {seg}")
    return out


def test_no_test_reads_the_retired_cascade_metadata_store():
    hits = _offending()
    assert not hits, (
        "Test(s) read cascade metadata keys retired by #2013/#2009 slice 1. "
        "Nothing writes these keys, so an assertion over them cannot fail and "
        "guards nothing. Query the cascade_markers table instead (see "
        "_tainted_by/_stale_by in tests/test_contradiction_cascade.py).\n  "
        + "\n  ".join(hits)
    )


def test_the_guard_fires_on_every_shape_that_defeated_its_regex_predecessor():
    """Positive control. A guard's health comes from its definition, never its
    silence, so it is pinned against (a) the real historical incident text and
    (b) each shape that slipped past the regex version — the four a reviewer
    constructed and empirically confirmed were vacuous-but-unflagged.
    """
    must_flag = {
        # real historical text, verbatim from the three fixed sites
        "historical/register_exemplar": 'return by_id in meta.get("tainted_by", [])',
        "historical/contradiction":     'assert "tainted_by" not in meta, f"{nid} bad"',
        "historical/cascade_semantics": 'assert cs_x not in meta.get("tainted_by", [])',
        # the four regex escapes
        "escape/renamed receiver":  'assert "tainted_by" not in node_meta',
        "escape/inline get on metadata": 'assert json.loads(row["metadata"]).get("stale_by") is None',
        "escape/dict subscript":    'assert metadata["tainted_by"] == []',
        "escape/multi-line":        'assert (\n    "stale_by"\n    not in meta\n)',
        "escape/stale_replacement": 'x = meta.get("stale_replacement")',
        # compound metadata names — missed by the word-bounded hint a reviewer
        # falsified; pinned so the loose substring cannot be tightened silently
        "compound/metadata_blob":   'def c(metadata_blob):\n    assert "tainted_by" not in metadata_blob',
        "compound/node_metadata":   'assert "stale_by" not in node_metadata',
        "compound/raw dict":        'assert not raw_metadata_dict.get("stale_by")',
        "escape/renamed via assign": 'node_m = json.loads(row["metadata"])\nassert "stale_by" not in node_m',
    }
    must_not_flag = {
        "correct replacement":  'conn.execute("SELECT 1 FROM cascade_markers WHERE node_id = ?")',
        "helper call":          'assert not _tainted_by(core, nid)',
        "support_lost stays":   'assert not meta.get("support_lost")',
        "support_lost in-test": 'assert "support_lost" not in meta',
        "render key, warnings": 'assert node["warnings"]["tainted_by"] == []',
        "render key, get":      'assert not warnings.get("stale_by")',
        "return value read":    'ids = {t["id"] for t in ret["tainted_downstream"]}',
        "viz render payload":   'by_id = {n["id"]: n for n in graph["nodes"]}\nassert "stale_by" not in by_id[dv_id]',
    }
    for name, src in must_flag.items():
        assert _scan(src), f"must flag but did not: {name} :: {src!r}"
    for name, src in must_not_flag.items():
        assert not _scan(src), f"must NOT flag but did: {name} :: {src!r}"


def test_the_guards_known_bound_is_real_and_documented():
    """Pin the limitation the docstring claims, so the claim cannot quietly
    drift from the behaviour. A metadata read laundered through a name with no
    metadata trace is NOT caught — asserted here deliberately, so that if the
    guard is ever widened to catch it, this test fails and the docstring's
    stated bound must be corrected in the same commit.
    """
    laundered = 'assert "stale_by" not in blob'
    assert not _scan(laundered), (
        "guard now catches the laundered shape — good, but update the KNOWN "
        "BOUND paragraph in this file's docstring, which still says it does not"
    )
