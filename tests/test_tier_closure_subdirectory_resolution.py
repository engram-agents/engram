"""Falsifier tests for engram-alpha#2549: the tier-closure checker
(``tests/test_tiers_manifest.py``'s ``test_import_dep_tier_consistency``)
must actually resolve imports that cross INTO a subpackage from a sibling
file, not just same-directory imports.

Before this fix, three plausible entry-point import spellings —
``from health import engine``, ``import health.engine``, and
``from health.engine import X`` — all silently failed to resolve to
``health/engine.py`` under ANY spelling (see engram-alpha#2548's docstring
note, which documents the pre-fix gap and is now historical). The checker's
whole purpose (the #1087/#1088 class) is catching exactly this shape: an
essential-tier file importing a module that ships only at a more
restrictive tier. A checker that cannot even SEE the import cannot catch
the violation, so the two things this file exists to prove are:

  1. NEGATIVE CONTROL — before the fix, a deliberately mis-tiered
     cross-subdirectory import produces ZERO violations (the checker is
     blind to it). This is the failing state the fix corrects.
  2. POSITIVE CONTROL — after the fix, the SAME scenario produces a real
     violation naming the mistiering. This is the falsifier Kepler asked
     for on forum #387/engram-alpha#2537: "a falsifier that shows the
     checker failing on a deliberately mis-tiered health/-style importee
     before it passes on the real tree."

Both controls run against real temp files on disk (not just AST snippets)
and the actual ``_find_tier_violations``/``_resolve_import_candidates``
functions from ``test_tiers_manifest.py`` — not a reimplementation that
could silently drift from what the real gate does.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_tiers_manifest import _find_tier_violations, _resolve_import_candidates  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Unit-level: _resolve_import_candidates resolves real cross-boundary
#    import spellings to the correct nested path (no filesystem needed).
# ---------------------------------------------------------------------------


class TestResolveImportCandidatesCrossBoundary:
    """The three entry-point spellings from engram-alpha#2548/#2549's own
    write-up, each independently confirmed to resolve to the real nested
    candidate path — this is the core fix, isolated from the file-scanning
    machinery around it.

    ``from X import Y`` always yields TWO candidates (the module ``X``
    itself, and ``X/Y`` in case ``Y`` is a submodule, not a plain
    attribute — see ``_resolve_import_candidates``'s own docstring for
    why both are needed), so these tests assert membership (``in``), not
    exact-list equality, except for the plain ``ast.Import`` cases (no
    such ambiguity exists there — every ``alias.name`` IS the module)."""

    def test_from_health_import_engine(self):
        """The exact case the first version of this fix MISSED, caught by
        this same test suite: without the submodule-candidate extension,
        ``from health import engine`` only resolved to the (nonexistent)
        ``health.py``, never to ``health/engine.py`` — silently identical
        to the pre-fix gap for this one spelling. Regression guard."""
        node = ast.parse("from health import engine").body[0]
        assert _resolve_import_candidates(node, "server.py") == ["health.py", "health/engine.py"]

    def test_import_health_dot_engine(self):
        node = ast.parse("import health.engine").body[0]
        assert _resolve_import_candidates(node, "server.py") == ["health/engine.py"]

    def test_from_health_dot_engine_import_x(self):
        node = ast.parse("from health.engine import X").body[0]
        assert "health/engine.py" in _resolve_import_candidates(node, "server.py")

    def test_explicit_relative_import_within_subdirectory_unchanged(self):
        """The pre-existing (accidental) same-directory relative-import
        case must keep resolving to the real target — this is the
        regression guard for the case the OLD code already got right, by
        accident, per #2548's finding."""
        node = ast.parse("from .curves import CurveSpec").body[0]
        assert "health/curves.py" in _resolve_import_candidates(node, "health/engine.py")

    def test_dotted_absolute_import_within_subdirectory_now_resolves(self):
        """The case #2548 documented as silently skipped — now resolves."""
        node = ast.parse("from health.curves import CurveSpec").body[0]
        assert "health/curves.py" in _resolve_import_candidates(node, "health/engine.py")

    def test_bare_sibling_import_within_subdirectory_resolves_both_root_and_sibling(self):
        """A bare, undotted absolute import (``import curves``) tries BOTH
        the manifest source ROOT (correct per Python's own semantics — a
        plain ``import`` is never implicitly relative) AND the importer's
        own directory (this repo's own extensively-used sys.path-insert +
        sibling-import convention, e.g. tools/baton.py's ``from forum_api
        import (...)`` — see the function's own docstring for the reviewer-
        fairy-caught regression this preserves). Whichever candidate
        doesn't exist as a real mechanism is silently ignored."""
        node = ast.parse("import curves").body[0]
        candidates = _resolve_import_candidates(node, "health/engine.py")
        assert "curves.py" in candidates
        assert "health/curves.py" in candidates

    def test_real_repo_case_tools_dotted_absolute_import(self):
        """A REAL, currently-shipped case (not synthetic): tools/cohort_dispatch.py
        imports tools.recall_summary_validator via a dotted absolute import.
        Under the OLD code this was silently skipped (module contains a
        dot). Confirmed resolving correctly now — this is the same case
        cited in the PR body as evidence the fix isn't purely hypothetical."""
        node = ast.parse("from tools.recall_summary_validator import validate").body[0]
        assert "tools/recall_summary_validator.py" in _resolve_import_candidates(
            node, "tools/cohort_dispatch.py"
        )

    def test_stdlib_dotted_import_resolves_to_a_path_but_wont_match_a_mechanism(self):
        """``_resolve_import_candidates`` doesn't know about stdlib — it
        resolves urllib.error to a candidate path like anything else. The
        exclusion happens one layer up, in ``_find_tier_violations``, via
        the ``path_to_entry`` membership check. Documenting the boundary
        here so a future reader doesn't mistake this for a stdlib-aware
        resolver."""
        node = ast.parse("import urllib.error").body[0]
        assert "urllib/error.py" in _resolve_import_candidates(node, "tools/forum.py")

    def test_relative_import_two_levels_up(self):
        """level=2 strips one additional trailing directory component
        beyond level=1's "this package" — verifying the general level-walk
        loop, not just the level=1 case every other test here exercises."""
        node = ast.parse("from ..shared import util").body[0]
        assert node.level == 2
        assert "health/shared/util.py" in _resolve_import_candidates(node, "health/sub/engine.py")

    def test_from_dot_import_x_resolves_via_submodule_candidate_only(self):
        """``from . import x`` (``node.module is None``) has no module-level
        candidate at all — it resolves ENTIRELY through the submodule-per-
        name path. An earlier draft of this fix explicitly called this case
        out of scope; the redesign that fixed test_from_health_import_engine
        covers it for free, so it's asserted here rather than left
        undocumented."""
        node = ast.parse("from . import curves").body[0]
        assert node.module is None
        assert _resolve_import_candidates(node, "health/engine.py") == ["health/curves.py"]


# ---------------------------------------------------------------------------
# 2. End-to-end falsifier: a real mis-tiered cross-subdirectory import,
#    scanned by the actual _find_tier_violations gate over real temp files.
# ---------------------------------------------------------------------------


class TestCrossBoundaryMistieringFalsifier:
    """Kepler's requested falsifier (forum #387, engram-alpha#2537): prove
    the checker actually catches a health/-style entry-point mistiering,
    not just that it doesn't crash on the real tree."""

    @pytest.fixture
    def health_style_fixture(self, tmp_path):
        """A minimal two-file fixture mimicking the real shape: a root
        ``server.py``-style importer (essential tier) that imports a
        subpackage module (``health/engine.py``) mistiered to ``dev``.
        Neither file does anything beyond the one import — the fixture
        exists to be scanned, not executed."""
        root_dir = tmp_path
        health_dir = tmp_path / "health"
        health_dir.mkdir()

        (root_dir / "server.py").write_text("import health.engine\n")
        (health_dir / "engine.py").write_text("VALUE = 1\n")

        mechanisms = [
            {"path": "server.py", "tier": "essential"},
            {"path": "health/engine.py", "tier": "dev"},  # deliberately mistiered
        ]
        return str(root_dir), mechanisms

    def test_negative_control_old_resolution_misses_the_mistiering(self, health_style_fixture):
        """Reproduces the PRE-FIX behavior directly (same-directory,
        single-segment resolution only) to prove the failing state existed
        — this is the "before" half of the before/after falsifier, run
        against the actual OLD algorithm's logic rather than asserted from
        memory."""
        src_root, mechanisms = health_style_fixture
        path_to_entry = {e["path"]: e for e in mechanisms}

        # The OLD resolution rule, transcribed verbatim from the pre-#2549
        # code (see git history / engram-alpha#2548's docstring note) —
        # same-directory, single-segment only, no dot handling.
        importer_path = "server.py"
        full_path = os.path.join(src_root, importer_path)
        tree = ast.parse(open(full_path, encoding="utf-8").read())
        node = next(n for n in ast.iter_child_nodes(tree) if isinstance(n, (ast.Import, ast.ImportFrom)))

        old_candidates = []
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "." not in module and module:
                dir_part = os.path.dirname(importer_path)
                old_candidates.append(f"{dir_part}/{module}.py" if dir_part else f"{module}.py")
        else:
            for alias in node.names:
                name = alias.name.split(".")[0]
                dir_part = os.path.dirname(importer_path)
                old_candidates.append(f"{dir_part}/{name}.py" if dir_part else f"{name}.py")

        assert old_candidates == ["health.py"], (
            "test setup drifted from the documented pre-fix resolution shape"
        )
        assert "health.py" not in path_to_entry, (
            "the OLD candidate never matches a real mechanism entry — "
            "this is the silent miss the fix corrects"
        )

    def test_positive_control_new_resolution_catches_the_mistiering(self, health_style_fixture):
        """The actual gate, with the #2549 fix, run against the same
        fixture — must produce a real TIER violation naming both paths."""
        src_root, mechanisms = health_style_fixture
        violations = _find_tier_violations(mechanisms, src_root)

        assert violations, (
            "expected the mis-tiered health/engine.py import to be caught; "
            "got zero violations — the fix did not close the gap"
        )
        assert any("server.py" in v and "health/engine.py" in v and "TIER" in v for v in violations), (
            f"violation list didn't name the expected TIER mismatch: {violations!r}"
        )

    def test_positive_control_same_tier_produces_no_violation(self, tmp_path):
        """Sibling positive control: the SAME import shape, but same-tier,
        must NOT be flagged — proves the fix doesn't over-fire on every
        cross-boundary import, only genuinely mistiered ones."""
        root_dir = tmp_path
        health_dir = tmp_path / "health"
        health_dir.mkdir()

        (root_dir / "server.py").write_text("import health.engine\n")
        (health_dir / "engine.py").write_text("VALUE = 1\n")

        mechanisms = [
            {"path": "server.py", "tier": "essential"},
            {"path": "health/engine.py", "tier": "essential"},  # same tier — no violation expected
        ]

        violations = _find_tier_violations(mechanisms, str(root_dir))
        assert not violations, f"expected no violations for a same-tier import, got: {violations!r}"

    def test_full_real_tree_still_passes(self):
        """The real, unmodified manifest + repo tree, run through the fixed
        gate end-to-end — the same invocation ``test_import_dep_tier_consistency``
        itself makes. Belt-and-suspenders alongside that test: confirms the
        fix doesn't introduce any NEW false positive across the entire real
        mechanism set, including the real cross-directory dotted imports
        found in tools/ (cohort_dispatch.py → recall_summary_validator.py,
        recall_summary_payload.py) that were previously invisible to the
        checker and are now genuinely resolved and checked."""
        import sys as _sys

        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_tiers_manifest import REPO_ROOT, _load_manifest

        manifest = _load_manifest()
        mechanisms = manifest["mechanisms"]
        src_root = os.path.join(REPO_ROOT, manifest.get("source_root", "."))

        violations = _find_tier_violations(mechanisms, src_root)
        assert not violations, (
            f"fix introduced new false-positive violations on the real tree: {violations!r}"
        )


# ---------------------------------------------------------------------------
# 3. Regression falsifier: this repo's own sys.path-insert + sibling-import
#    convention (tools/, hooks/claude/) must STAY checked by the fix that
#    added source-root resolution for absolute imports. Caught by reviewer-
#    fairy round 1 on PR #2550: treating every absolute import as
#    resolved-from-source-root-only silently dropped same-directory
#    resolution for the ~20 real imports in this repo using that
#    convention (e.g. tools/baton.py's "from forum_api import (...)",
#    where forum_api.py lives in the SAME tools/ directory as baton.py,
#    resolvable only because that convention puts the importer's own
#    directory on sys.path at runtime).
# ---------------------------------------------------------------------------


class TestSameDirectoryConventionPreservedForAbsoluteImports:
    """Mirrors TestCrossBoundaryMistieringFalsifier's shape, but for the
    OPPOSITE direction: a same-directory sibling import (this repo's own
    convention), not a cross-boundary one. Proves the fix that added
    source-root resolution didn't silently remove the resolution that was
    already correct before this PR."""

    @pytest.fixture
    def sibling_convention_fixture(self, tmp_path):
        """A minimal two-file fixture mimicking tools/baton.py's own real
        shape: a subdirectory file importing a SIBLING in the same
        subdirectory (not the source root) via a bare absolute import,
        mistiered relative to its importer."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        (tools_dir / "consumer.py").write_text("import helper\n")
        (tools_dir / "helper.py").write_text("VALUE = 1\n")

        mechanisms = [
            {"path": "tools/consumer.py", "tier": "essential"},
            {"path": "tools/helper.py", "tier": "dev"},  # deliberately mistiered
        ]
        return str(tmp_path), mechanisms

    def test_same_directory_sibling_mistiering_is_still_caught(self, sibling_convention_fixture):
        """The regression test the reviewer-fairy asked for: a same-
        directory sibling import, deliberately mistiered, must still
        produce a violation after this fix — proving the additive
        source-root candidate didn't silently replace the pre-existing
        same-directory candidate."""
        src_root, mechanisms = sibling_convention_fixture
        violations = _find_tier_violations(mechanisms, src_root)

        assert violations, (
            "expected the mis-tiered tools/helper.py sibling import to be "
            "caught; got zero violations — the source-root-only resolution "
            "regressed same-directory coverage"
        )
        assert any(
            "tools/consumer.py" in v and "tools/helper.py" in v and "TIER" in v for v in violations
        ), f"violation list didn't name the expected TIER mismatch: {violations!r}"

    def test_same_directory_sibling_same_tier_produces_no_violation(self, tmp_path):
        """Sibling positive control: the identical shape, same tier, must
        NOT be flagged."""
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()

        (tools_dir / "consumer.py").write_text("import helper\n")
        (tools_dir / "helper.py").write_text("VALUE = 1\n")

        mechanisms = [
            {"path": "tools/consumer.py", "tier": "essential"},
            {"path": "tools/helper.py", "tier": "essential"},
        ]

        violations = _find_tier_violations(mechanisms, str(tmp_path))
        assert not violations, f"expected no violations for a same-tier sibling import, got: {violations!r}"

    def test_real_repo_case_tools_baton_imports_forum_api_from_same_directory(self):
        """The REAL case that motivated this whole class (not synthetic,
        verified by reading tools/baton.py's actual imports directly
        before writing this test): ``from forum_api import ...`` — a
        same-directory sibling import (both files live in tools/), one of
        the reviewer-fairy's cited 20 real instances on PR #2550. Must
        resolve to tools/forum_api.py, not (only) a nonexistent
        forum_api.py at the source root."""
        node = ast.parse("from forum_api import ForumClient").body[0]
        candidates = _resolve_import_candidates(node, "tools/baton.py")
        assert "tools/forum_api.py" in candidates
