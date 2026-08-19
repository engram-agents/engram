"""
Binds src/engram/skills/claude/engram-standpoint/SKILL.md's "Reading the
STANDPOINT warning" worked example to the actual STANDPOINT: line
engram_core._validate_reasoning_structure emits — issue #2022.

The doc has drifted from the code before (silently, until a coder-fairy
re-captured the string for an unrelated reason during #2020) — the only
reason it was correct was coincidence. This test removes the coincidence:
it renders the real STANDPOINT: line via the fixture below and asserts the
doc's fenced example contains it VERBATIM (exact containment, not a
structural/sampled check — a doc quoting what code emits has exactly one
actual output, so exact containment is both correct and practical; see
issue #2022 comment 3, Ariadne's refinement of the initial "test 2" shape
borrowed from umwelt's test_plugin_sync.py).

Fixture shape (per #2022 comment 1 — reproduces the doc line
character-for-character, proven by a coder-fairy during #2020 round 4):
  1. File an unrelated openai:gpt-lineage observation first, so the graph
     carries >= 2 distinct lineages overall — otherwise the lineage-uniform
     verdict is suppressed by the actionability gate
     (engram_core._graph_lineage_count(conn) >= 2) and there is nothing to
     assert against.
  2. File two hard_data observations with distinct standpoint_author_id and
     identical standpoint_lineage="anthropic:opus".
  3. engram_derive over those two premises, reasoning_type="inductive_causal".
  4. Pull the STANDPOINT: line out of the returned structure_warnings.

Import-pattern contract (tests/IMPORT_RUBRIC.md R1/R4):
    This file imports engram_core, engram_observation, and engram_derive
    directly and contains no ``import server``. Module objects are passed
    to fresh_module() (R4) for per-test reload isolation — mirrors
    test_cascade_semantics.py's helper shape (import-pattern contract
    comment at the top of that file).
"""

import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tests/ dir so conftest.fresh_module / conftest.REPO_ROOT are importable
# both under pytest and when the file is run as a standalone script.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module, REPO_ROOT  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_derive
import engram_observation


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_standpoint_skill_doc_sync_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

# Located from REPO_ROOT (conftest.py's own pattern) — no absolute /home/...
# path, so this works in any clone or worktree.
SKILL_MD_PATH = os.path.join(
    REPO_ROOT, "src", "engram", "skills", "claude", "engram-standpoint", "SKILL.md"
)


def _make_fresh_mods():
    """Reload the observation/derive family, configure a clean test DB.

    Returns (obs, derive, core) — freshly reloaded module objects with
    engram_core pointing to TEST_DIR. ENGRAM_NO_DB_GUARDS is set explicitly
    (conftest.py sets it too for the pytest-collection path, but this
    mirrors the standalone-fixture requirement from #2022 comment 1 so the
    fixture setup doesn't depend on being run under pytest).
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    obs = fresh_module(engram_observation)
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return obs, derive, core


def _seed_ob(obs, n, quote_type="hard_data", lineage="", author="", collection=""):
    """File one observation with the given standpoint fields; return its id."""
    kwargs = dict(
        url=f"https://example.com/source-{n}",
        title=f"Source {n}",
        quoted_text=f"Measured value number {n} is {n * 7}.",
        interpretation=f"Measurement {n}.",
        claim=f"Value number {n} is {n * 7}",
        quote_type=quote_type,
    )
    if lineage:
        kwargs["standpoint_lineage"] = lineage
    if author:
        kwargs["standpoint_author_id"] = author
    if collection:
        kwargs["standpoint_collection_id"] = collection
    r = json.loads(obs._add_observation_impl(**kwargs))
    assert "error" not in r, f"observation {n} failed: {r}"
    return r["observation_id"]


def test_standpoint_skill_doc_example_matches_actual_rendering():
    """SKILL.md's worked STANDPOINT: example must literally contain the line
    the code actually renders for the documented scenario (author-diverse,
    lineage-uniform, both premises hard_data / reality-verified)."""
    obs, derive, core = _make_fresh_mods()

    # Unrelated cross-lineage observation — bumps _graph_lineage_count to
    # >= 2 so the lineage-uniform verdict clears the actionability gate and
    # actually renders a standalone line (engram_core.py's
    # "lineage: 1 cluster / N leaves (⚠ uniform ...)" branch).
    _seed_ob(obs, 9, lineage="openai:gpt")

    # No standpoint_collection_id set — matches the doc's worked example,
    # which shows author + lineage only (collection is untracked here, so
    # its axis is omitted per the all-or-omit rule and folds into "others
    # unchecked", not rendered as its own diverse/uniform segment).
    o1 = _seed_ob(obs, 1, lineage="anthropic:opus", author="test:author_one")
    o2 = _seed_ob(obs, 2, lineage="anthropic:opus", author="test:author_two")

    r = json.loads(derive._derive_impl(
        claim="Derived claim from premises.",
        supporting_ids=f"{o1},{o2}",
        logical_chain="Both premises jointly support the derived claim.",
        reasoning_type="inductive_causal",
    ))
    assert "error" not in r, f"derive failed: {r}"
    warnings = r.get("structure_warnings", [])
    standpoint_lines = [w for w in warnings if w.startswith("STANDPOINT:")]
    assert len(standpoint_lines) == 1, (
        f"expected exactly one STANDPOINT: line, got: {warnings}"
    )
    rendered = standpoint_lines[0]

    skill_text = Path(SKILL_MD_PATH).read_text()
    assert rendered in skill_text, (
        "SKILL.md's 'Reading the STANDPOINT warning' worked example has "
        "drifted from the STANDPOINT: line engram_core."
        "_validate_reasoning_structure actually emits — regenerate the "
        "fenced example in "
        "src/engram/skills/claude/engram-standpoint/SKILL.md (the "
        "'## Reading the STANDPOINT warning' section) from this fixture's "
        "actual rendered output; do not hand-edit it.\n"
        f"Actual rendered line:\n  {rendered!r}"
    )
    print(f"  PASS: SKILL.md example matches actual rendering: {rendered[:120]!r}")


if __name__ == "__main__":
    test_standpoint_skill_doc_example_matches_actual_rendering()
    print("All tests passed.")
