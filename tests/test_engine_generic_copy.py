"""Tests for the generic manifest-driven copy loop + the ``dest`` field
(engram-alpha#2537 amendment 4).

Uses a small, fully synthetic repo tree + manifest (not the real 188-entry
``tiers.json``) so these tests are fast, self-contained, and independent of
what the real manifest happens to contain on any given day. The real-tree
falsifier (build at each tier before/after this change, diff the shipped
file lists, positive control) is reported in the PR body — that is the
required acceptance evidence per Ariadne's build-lead call; these are the
permanent regression tests for the underlying mechanism.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "build"))

from tools.engine.build import _copy_manifest_entry  # noqa: E402


def _make_repo(tmp_path, source_root="src/fake"):
    """A minimal fake repo tree: server.py at root (build_plugin's sanity
    check requires it) plus a handful of files under assorted prefixes."""
    repo_root = tmp_path / "repo"
    src = repo_root / source_root
    src.mkdir(parents=True)
    (src / "server.py").write_text("# fake server\n")
    return repo_root, src


class TestCopyManifestEntry:
    """Unit tests for the shared copy primitive in isolation."""

    def test_plain_entry_no_dest_lands_at_its_own_path(self, tmp_path):
        repo_root, src = _make_repo(tmp_path)
        (src / "root_file.py").write_text("x = 1\n")
        plugin_root = tmp_path / "plugin"

        dest = _copy_manifest_entry(
            {"path": "root_file.py"}, str(repo_root), "src/fake", str(plugin_root)
        )
        assert dest == "root_file.py"
        assert (plugin_root / "root_file.py").is_file()

    def test_new_unclaimed_prefix_ships_via_the_generic_primitive(self, tmp_path):
        """THE regression test for the defect amendment 4 diagnosed and
        confirmed with a real build: a manifest entry under a folder that
        matches none of the six OLD hardcoded sections (root/tools/
        templates/hooks/skills/agents/output-styles) used to be silently
        dropped. The generic primitive has no such prefix list at all —
        any path ships."""
        repo_root, src = _make_repo(tmp_path)
        (src / "health").mkdir()
        (src / "health" / "measurements.py").write_text("MEASUREMENTS = {}\n")
        plugin_root = tmp_path / "plugin"

        dest = _copy_manifest_entry(
            {"path": "health/measurements.py"},
            str(repo_root), "src/fake", str(plugin_root),
        )
        assert dest == "health/measurements.py"
        assert (plugin_root / "health" / "measurements.py").is_file()

    def test_dest_field_overrides_the_physical_location(self, tmp_path):
        """The flattener case: hooks/claude/foo.py -> hooks/foo.py, as data
        (the manifest entry's own "dest"), not as build.py code."""
        repo_root, src = _make_repo(tmp_path)
        (src / "hooks" / "claude").mkdir(parents=True)
        (src / "hooks" / "claude" / "foo.py").write_text("# hook\n")
        plugin_root = tmp_path / "plugin"

        dest = _copy_manifest_entry(
            {"path": "hooks/claude/foo.py", "dest": "hooks/foo.py"},
            str(repo_root), "src/fake", str(plugin_root),
        )
        assert dest == "hooks/foo.py"
        assert (plugin_root / "hooks" / "foo.py").is_file()
        # The un-flattened path must NOT exist — dest is a full override,
        # not an additional copy.
        assert not (plugin_root / "hooks" / "claude" / "foo.py").exists()

    def test_source_and_dest_are_independent_overrides(self, tmp_path):
        """source overrides where a file is READ from; dest overrides where
        it's WRITTEN to. Symmetric, and usable together (e.g. an entry
        whose repo location AND bundle location both differ from its
        manifest path)."""
        repo_root, _src = _make_repo(tmp_path)
        alt_src_dir = repo_root / "somewhere" / "else"
        alt_src_dir.mkdir(parents=True)
        (alt_src_dir / "thing.py").write_text("# elsewhere\n")
        plugin_root = tmp_path / "plugin"

        dest = _copy_manifest_entry(
            {
                "path": "logical/name.py",
                "source": "somewhere/else/thing.py",
                "dest": "physical/place.py",
            },
            str(repo_root), "src/fake", str(plugin_root),
        )
        assert dest == "physical/place.py"
        assert (plugin_root / "physical" / "place.py").is_file()

    def test_directory_entry_copies_contents_not_the_dir_itself(self, tmp_path):
        """cp -r/-a semantics preserved: a directory entry's CONTENTS land
        at dest, matching the pre-existing tools/ and templates/ behavior
        (see build.py's module docstring REPLICATION NOTES)."""
        repo_root, src = _make_repo(tmp_path)
        skill_dir = src / "skills" / "claude" / "a-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# skill\n")
        plugin_root = tmp_path / "plugin"

        dest = _copy_manifest_entry(
            {"path": "skills/claude/a-skill", "dest": "skills/a-skill"},
            str(repo_root), "src/fake", str(plugin_root),
        )
        assert dest == "skills/a-skill"
        assert (plugin_root / "skills" / "a-skill" / "SKILL.md").is_file()

    def test_missing_source_dies_loudly(self, tmp_path):
        repo_root, _src = _make_repo(tmp_path)
        plugin_root = tmp_path / "plugin"
        with pytest.raises(RuntimeError, match="does not exist"):
            _copy_manifest_entry(
                {"path": "nonexistent.py"}, str(repo_root), "src/fake", str(plugin_root)
            )


# NOTE: a full build_plugin() end-to-end test (hooks.json, platform profiles,
# .mcp.json, etc. all present) needs a much larger synthetic fixture than the
# unit tests above — not worth building here when test_engine_phase3.py
# already exercises the real pipeline end-to-end against the REAL repo tree,
# and the required acceptance evidence for THIS change (build at each tier
# before/after, diff the shipped-file lists, positive control) is a real-tree
# falsifier reported in the PR body, not a synthetic one. See that report for
# the final conclusion it reached after three rounds of colleague review: a
# first pass found 10 dev-tier manifest entries appearing after the fix that
# hadn't shipped before, which read as "10 recovered bugs." Investigation
# traced each to its actual consumer and found all 10 were pre-existing
# manifest-tier misclassifications (build-time-only tooling defaulted into
# `mechanisms` by the manifest's friction gradient), unrelated to the
# copy-loop bug this PR fixes — zero were real recoveries. The refactor is
# byte-identical, before vs after, at essential/convenience/dev alike.
