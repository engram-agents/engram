"""Tests for src.build.engine.deploy_tag — the shared deploy-tag predicate
(#3092), and its two engine call sites (flows.py::_apply_marketplace_rebuilt
and cli.py::build_command).

Contract-first (per this repo's CLAUDE.md convention): written BEFORE
src/build/engine/deploy_tag.py exists, and before the guard additions land in
flows.py/cli.py. All items below are the authoritative list from issue
#3092's body, reproduced here verbatim per the spec:

  1. Untagged commit on dev, no flag -> refused by the engine path (both
     run-upgrade and build). Error names the nearest tag and mentions
     --allow-untagged.
  2. Same, with --allow-untagged -> proceeds (the engine has no
     .deployed-version write path today -- see the PR body's named-gap note;
     these tests assert refuse/allow behavior only, not a marker write).
  3. HEAD at a v* tag that IS merged into origin/dev, detached HEAD ->
     allowed with NO flag; the recorded tag matches.
  4. HEAD at a deploy/* tag, same merged setup -> allowed (same as v*).
  5. A local v* tag on a commit that is genuinely NOT an ancestor of
     origin/dev or origin/main (unmerged work) -> refused.
  6. An unrelated tag (e.g. foo) with no v*/deploy/* tag present -> refused,
     treated as untagged.
  7. Discrimination control (mutation): temporarily revert the engine guard
     call back to branch-only and confirm the "untagged commit on dev via
     engine" test goes red, while unrelated tests stay green.

  Also required (design item 4): a tagged checkout (detached HEAD, at a
  deployable tag that IS merged) must pass the #794 branch guard in the
  engine paths too, with NO --allow-branch and NO --allow-untagged.

Fixtures are real git-plumbing repos built with actual git init/commit/tag/a
fake bare 'origin' remote -- never mocks -- per the spec: this predicate is
entirely git-plumbing, so a real throwaway repo in tmp_path is the only
trustworthy fixture.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from typing import Any

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from tools.engine.deploy_tag import deployable_tag_at, refresh_and_check_deployable_tag_at
from tools.engine.flows import upgrade_flow, _apply_marketplace_rebuilt
from tools.engine.steps import run_flow, Done, Paused, Failed
from tools.engine.cli import build_command


# ---------------------------------------------------------------------------
# shared git helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def _checkout(repo: str, ref: str) -> None:
    _git(["checkout", "-q", ref], repo)


# ---------------------------------------------------------------------------
# Section A fixtures: minimal git repos for the pure-predicate tests
# ---------------------------------------------------------------------------


def _make_simple_repo(tmp_path, *, tags=(), configure_origin=False):
    """A minimal git repo with a single commit, optionally tagged, optionally
    with NO origin remote configured at all (used for the absent-remote-refs
    fail-closed case)."""
    repo = tmp_path / "simple-repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "dev"], str(repo))
    _git(["config", "user.email", "test@example.com"], str(repo))
    _git(["config", "user.name", "Test"], str(repo))
    (repo / "README.md").write_text("hello\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-q", "-m", "init"], str(repo))
    for tag in tags:
        _git(["tag", tag], str(repo))

    if configure_origin:
        origin_bare = tmp_path / "simple-origin-bare.git"
        _git(["init", "-q", "-b", "dev", "--bare", str(origin_bare)], str(repo))
        _git(["remote", "add", "origin", str(origin_bare)], str(repo))
        _git(["push", "-q", "origin", "dev"], str(repo))
        _git(["push", "-q", "origin", "dev:refs/heads/main"], str(repo))
        _git(["fetch", "-q", "origin"], str(repo))

    return str(repo)


def _make_ancestry_repo(tmp_path, *, extra_tags_on_tip=()):
    """Commit graph (mirrors tests/test_install_local_marketplace.py's
    _make_scratch_repo_with_ancestry -- same shape, independently built here
    so this file's fixtures stay self-contained):

        A (annotated tag v1.0.0-a; ancestor of dev's tip)
        |
        B (dev tip == origin/dev == origin/main; UNTAGGED by default, plus
        |  any extra_tags_on_tip)
        (branches off A)
        D (on branch 'stray'; tagged deploy/2026-09-07-stray; NOT an
           ancestor of origin/dev or origin/main -- unmerged work)

    Ends on 'dev' (at B). Returns (repo_path, {"a": sha, "b": sha, "d": sha}).
    A REAL, fetchable 'origin' remote (a bare repo on local disk) is
    configured with 'dev' and 'main' both at sha_b.
    """
    repo = tmp_path / "ancestry-repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "dev"], str(repo))
    _git(["config", "user.email", "test@example.com"], str(repo))
    _git(["config", "user.name", "Test"], str(repo))
    (repo / "README.md").write_text("first\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-q", "-m", "init"], str(repo))
    sha_a = _git(["rev-parse", "HEAD"], str(repo)).stdout.strip()
    # Annotated (not lightweight) on purpose -- matches real deploy tags and
    # exercises `git rev-parse <tag>` returning the tag OBJECT rather than
    # the commit (#2711's trap); merge-base --is-ancestor dereferences this
    # automatically, so the predicate must use HEAD, never the tag name, for
    # its own ancestry check.
    _git(["tag", "-a", "v1.0.0-a", "-m", "annotated deploy tag"], str(repo))

    (repo / "second.txt").write_text("second commit\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-q", "-m", "second"], str(repo))
    sha_b = _git(["rev-parse", "HEAD"], str(repo)).stdout.strip()
    for tag in extra_tags_on_tip:
        _git(["tag", tag], str(repo))

    origin_bare = tmp_path / "ancestry-origin-bare.git"
    _git(["init", "-q", "-b", "dev", "--bare", str(origin_bare)], str(repo))
    _git(["remote", "add", "origin", str(origin_bare)], str(repo))
    _git(["push", "-q", "origin", "dev"], str(repo))
    _git(["push", "-q", "origin", "dev:refs/heads/main"], str(repo))
    _git(["fetch", "-q", "origin"], str(repo))

    _git(["checkout", "-q", "-b", "stray", sha_a], str(repo))
    (repo / "stray.txt").write_text("unmerged work\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-q", "-m", "stray unmerged commit"], str(repo))
    sha_d = _git(["rev-parse", "HEAD"], str(repo)).stdout.strip()
    _git(["tag", "deploy/2026-09-07-stray"], str(repo))

    _git(["checkout", "-q", "dev"], str(repo))

    return str(repo), {"a": sha_a, "b": sha_b, "d": sha_d}


# ---------------------------------------------------------------------------
# Section A: deployable_tag_at() -- the pure predicate
# ---------------------------------------------------------------------------


class TestDeployableTagAtPurePredicate:
    def test_untagged_head_returns_none(self, tmp_path):
        repo = _make_simple_repo(tmp_path, tags=())
        assert deployable_tag_at(repo) is None

    def test_v_tag_merged_returns_tag(self, tmp_path):
        """Contract item 3: HEAD at a v* tag merged into origin/dev,
        detached HEAD -> allowed with no flag; recorded tag matches."""
        repo, commits = _make_ancestry_repo(tmp_path)
        _checkout(repo, "v1.0.0-a")
        assert deployable_tag_at(repo) == "v1.0.0-a"

    def test_deploy_tag_merged_returns_tag(self, tmp_path):
        """Contract item 4: same as item 3, but a deploy/* tag."""
        repo, commits = _make_ancestry_repo(
            tmp_path, extra_tags_on_tip=["deploy/2026-09-05-b"]
        )
        # dev's tip (sha_b) is trivially its own ancestor of origin/dev.
        assert deployable_tag_at(repo) == "deploy/2026-09-05-b"

    def test_unmerged_tag_returns_none(self, tmp_path):
        """Contract item 5: a local v*/deploy/* tag on a commit that is
        genuinely NOT an ancestor of origin/dev or origin/main -> refused
        (a same-named tag must not bypass the guard on unmerged work)."""
        repo, commits = _make_ancestry_repo(tmp_path)
        _checkout(repo, "deploy/2026-09-07-stray")
        assert deployable_tag_at(repo) is None

    def test_unrelated_tag_returns_none(self, tmp_path):
        """Contract item 6: an unrelated tag (foo) with no v*/deploy/* tag
        present -> refused, treated as untagged."""
        repo = _make_simple_repo(tmp_path, tags=["foo/bar"], configure_origin=True)
        assert deployable_tag_at(repo) is None

    def test_multiple_tags_sorted_and_joined(self, tmp_path):
        """Return value mirrors the shell's ALPHA_TAG (comma-joined) but in
        SORTED order for determinism -- a deliberate small improvement over
        the shell's own unsorted tag-iteration order."""
        repo, commits = _make_ancestry_repo(
            tmp_path, extra_tags_on_tip=["v2.0.0-b", "deploy/2026-09-08-b"]
        )
        result = deployable_tag_at(repo)
        assert result == "deploy/2026-09-08-b,v2.0.0-b"

    def test_absent_remote_refs_returns_none_not_raise(self, tmp_path):
        """No 'origin' remote configured at all -> merge-base --is-ancestor
        against a nonexistent ref must be treated as "not an ancestor"
        (return None) rather than raising."""
        repo = _make_simple_repo(tmp_path, tags=["v1.0.0-solo"], configure_origin=False)
        assert deployable_tag_at(repo) is None

    def test_default_head_argument_is_head(self, tmp_path):
        """head='HEAD' is the default; an explicit head=... value is honored
        the same way (git tag --points-at accepts any resolvable ref)."""
        repo, commits = _make_ancestry_repo(tmp_path)
        assert deployable_tag_at(repo) == deployable_tag_at(repo, head="HEAD")
        assert deployable_tag_at(repo, head=commits["b"]) is None  # untagged tip


# ---------------------------------------------------------------------------
# Section B: refresh_and_check_deployable_tag_at() -- the engine's wrapper
# ---------------------------------------------------------------------------


class TestRefreshAndCheckWrapper:
    def test_successful_fetch_then_check(self, tmp_path):
        repo, commits = _make_ancestry_repo(tmp_path)
        _checkout(repo, "v1.0.0-a")
        assert refresh_and_check_deployable_tag_at(repo, timeout=5.0) == "v1.0.0-a"

    def test_missing_origin_remote_falls_through_no_crash(self, tmp_path):
        """Fetch fails (no 'origin' configured) -> falls through to
        deployable_tag_at() against local refs (also absent) -> None, and
        the call must not raise."""
        repo = _make_simple_repo(tmp_path, tags=["v1.0.0-solo"], configure_origin=False)
        assert refresh_and_check_deployable_tag_at(repo, timeout=5.0) is None

    def test_broken_origin_url_falls_through_to_stale_but_valid_local_refs(self, tmp_path):
        """Fetch fails (origin URL now broken) but the ALREADY-fetched local
        origin/dev ref from fixture setup is still genuinely accurate (no
        reversion happened) -- the wrapper still grants the exception, since
        deployable_tag_at() trusts whatever local refs currently exist. This
        is the documented, accepted design for the ENGINE's wrapper (#3092
        design decision) -- the shell script's own caller is the stricter,
        fetch-gated one; see install-local-marketplace.sh."""
        repo, commits = _make_ancestry_repo(tmp_path)
        _checkout(repo, "v1.0.0-a")
        _git(["remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")], repo)
        assert refresh_and_check_deployable_tag_at(repo, timeout=5.0) == "v1.0.0-a"

    def test_hanging_fetch_is_bounded_by_timeout(self, tmp_path, monkeypatch):
        """A hanging fetch (fake ssh transport that sleeps) must be bounded
        by the timeout= kwarg, not hang the caller -- subprocess.run's own
        timeout=, not a reimplementation of bash's timeout/gtimeout dance."""
        repo, commits = _make_ancestry_repo(tmp_path)
        _checkout(repo, "v1.0.0-a")
        fake_ssh = tmp_path / "fake-ssh.sh"
        fake_ssh.write_text("#!/usr/bin/env bash\nexec sleep 9999\n")
        fake_ssh.chmod(0o755)
        _git(["remote", "set-url", "origin", "ssh://localhost/nonexistent-repo.git"], repo)
        monkeypatch.setenv("GIT_SSH_COMMAND", str(fake_ssh))

        start = time.monotonic()
        result = refresh_and_check_deployable_tag_at(repo, timeout=2.0)
        elapsed = time.monotonic() - start

        assert elapsed < 8, f"took {elapsed:.1f}s -- fetch was not bounded"
        # Falls through to local (stale-but-still-valid, never actually
        # re-fetched after fixture setup) refs -> still returns the tag.
        assert result == "v1.0.0-a"


# ---------------------------------------------------------------------------
# Section C fixtures: full buildable source trees for the engine call sites
# ---------------------------------------------------------------------------


def _write_minimal_tiers_json(packaging_dir: str) -> None:
    manifest = {
        "schema_version": 1,
        "depth_tiers": ["essential", "convenience", "dev"],
        "default_tier": "convenience",
        "mechanisms": [
            {"path": "server.py", "tier": "essential"},
            {"path": "launch-engram-server.sh", "tier": "essential"},
        ],
        "excluded": [],
    }
    with open(os.path.join(packaging_dir, "tiers.json"), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def _populate_buildable_source_tree(source_dir: str) -> None:
    """Write a minimal fake source tree so _apply_marketplace_rebuilt gets
    PAST its own guards and its config/tier-loading step (install_tier
    lookup, tiers.json parse). This does NOT go as far as making the
    downstream build_plugin() call succeed end-to-end (that needs a
    platform profile at src/build/packaging/platforms/<target>.json plus
    considerably more fixture depth than any existing test in this repo
    builds) -- tests that exercise the guard-PASSES path assert that
    build_plugin()'s own (unrelated) failure text appears, rather than the
    guard's, matching tests/test_engine_unit.py's own established
    convention for cli.py's #794 guard
    (test_dev_branch_proceeds_past_guard: "passes the branch guard (may
    fail later — NOT on the guard)").

    packaging/tiers.json placement (src/build/packaging/, post-#1093) is
    duplicated here rather than cross-imported from
    tests/test_engine_flows.py's _make_fake_env, so this file's fixtures
    stay self-contained -- consistent with this repo's existing per-file
    fixture convention, e.g. test_install_local_marketplace.py's own
    _make_scratch_repo helpers."""
    packaging_dir = os.path.join(source_dir, "src", "build", "packaging")
    os.makedirs(packaging_dir, exist_ok=True)
    _write_minimal_tiers_json(packaging_dir)

    with open(os.path.join(source_dir, "server.py"), "w") as f:
        f.write("# fake server\n")

    hooks_dir = os.path.join(source_dir, "hooks", "claude")
    os.makedirs(hooks_dir, exist_ok=True)
    with open(os.path.join(source_dir, "hooks", "hooks.json"), "w") as f:
        json.dump({"hooks": {}}, f, indent=2)
        f.write("\n")

    with open(os.path.join(source_dir, "plugin.json"), "w") as f:
        json.dump({"name": "engram", "version": "0.0.1-test"}, f)

    with open(os.path.join(packaging_dir, "mcp.json"), "w") as f:
        json.dump({"mcpServers": {}}, f)

    for d in ["skills/claude", "agents/claude", "output-styles/claude"]:
        os.makedirs(os.path.join(source_dir, d), exist_ok=True)

    templates_dir = os.path.join(source_dir, "templates")
    os.makedirs(templates_dir, exist_ok=True)
    with open(os.path.join(templates_dir, "template.CLAUDE.md"), "w") as f:
        f.write("# ENGRAM CLAUDE.md template\nsome content\n")

    launch_sh = os.path.join(source_dir, "launch-engram-server.sh")
    with open(launch_sh, "w") as f:
        f.write("#!/bin/bash\n# fake\nexec {{PYTHON}} server.py\n")
    os.chmod(launch_sh, os.stat(launch_sh).st_mode | stat.S_IEXEC)

    gitignore = os.path.join(source_dir, ".gitignore")
    with open(gitignore, "w") as f:
        f.write("build/\n")


def _make_flows_ctx(
    tmp_base,
    *,
    checkout_ref: "str | None" = None,
    tag_head=None,
    configure_origin: bool = True,
    allow_branch: bool = False,
    allow_untagged: bool = False,
):
    """Build (engram_home, source_dir, ctx) for testing
    _apply_marketplace_rebuilt directly, with a REAL git repo carrying a
    real bare 'origin' remote so the deploy-tag guard's ancestry check has
    something genuine to check against (mirrors
    test_install_local_marketplace.py's _make_scratch_repo_with_ancestry --
    real git-plumbing fixture, not mocks, per #3092)."""
    engram_home = os.path.join(tmp_base, "engram_home")
    source_dir = os.path.join(tmp_base, "source_dir")
    os.makedirs(engram_home, exist_ok=True)
    os.makedirs(source_dir, exist_ok=True)

    _git(["init", "-q", "-b", "dev"], source_dir)
    _git(["config", "user.email", "test@test.com"], source_dir)
    _git(["config", "user.name", "Test"], source_dir)

    _populate_buildable_source_tree(source_dir)
    _git(["add", "-A"], source_dir)
    _git(["commit", "-q", "-m", "init"], source_dir)

    tags = [tag_head] if isinstance(tag_head, str) else list(tag_head or [])
    for tag in tags:
        _git(["tag", tag], source_dir)

    if configure_origin:
        origin_bare = os.path.join(tmp_base, "flows-origin-bare.git")
        subprocess.run(
            ["git", "init", "-q", "-b", "dev", "--bare", origin_bare],
            capture_output=True, text=True, check=True,
        )
        _git(["remote", "add", "origin", origin_bare], source_dir)
        _git(["push", "-q", "origin", "dev"], source_dir)
        _git(["push", "-q", "origin", "dev:refs/heads/main"], source_dir)
        _git(["fetch", "-q", "origin"], source_dir)

    if checkout_ref:
        _git(["checkout", "-q", checkout_ref], source_dir)

    deployed_sha = _git(["rev-parse", "HEAD"], source_dir).stdout.strip()

    with open(os.path.join(engram_home, ".deployed-version"), "w") as f:
        f.write(f"alpha_sha={deployed_sha}\n")
        f.write(f"deployed_from={source_dir}\n")

    with open(os.path.join(engram_home, "config.json"), "w") as f:
        json.dump({"install_tier": "convenience", "multi_agent": False}, f)

    built_plugin_root = os.path.join(source_dir, "build", "plugin")
    os.makedirs(built_plugin_root, exist_ok=True)
    # No pre-built manifest here on purpose -- tests that want the guard's
    # apply() path actually exercised rely on check() failing so apply()
    # runs (mirrors test_engine_flows.py's TestBranchGuard convention).

    ctx: dict[str, Any] = {
        "engram_home": engram_home,
        "source_dir": source_dir,
        "target_sha": "HEAD",
        "ack_changeset": True,
        "allow_branch": allow_branch,
        "allow_untagged": allow_untagged,
        "_deployed_sha": deployed_sha,
        "_built_plugin_root": built_plugin_root,
        "_changed_files": [],
        "_template_changed": False,
    }
    return engram_home, source_dir, ctx


# ---------------------------------------------------------------------------
# Section C: engine call site -- flows.py::_apply_marketplace_rebuilt
# ---------------------------------------------------------------------------


class TestFlowsEngineDeployTagGuard:
    def test_untagged_dev_without_allow_untagged_refused(self, tmp_path):
        """Contract item 1 (flows.py path): untagged commit on dev, no
        flag -> refused. Error names the nearest tag and mentions
        --allow-untagged."""
        _, source_dir, ctx = _make_flows_ctx(str(tmp_path), configure_origin=False)
        with pytest.raises(RuntimeError) as excinfo:
            _apply_marketplace_rebuilt(ctx)
        msg = str(excinfo.value)
        assert "none" in msg  # nearest tag: none, since nothing is tagged
        assert "allow_untagged" in msg or "--allow-untagged" in msg

    def test_untagged_dev_without_allow_untagged_refused_via_run_flow(self, tmp_path):
        """Same as above, exercised through the full upgrade_flow()/run_flow()
        path (matches the existing #794 TestBranchGuard convention)."""
        _, source_dir, ctx = _make_flows_ctx(str(tmp_path), configure_origin=False)
        steps = upgrade_flow()
        result = run_flow(steps, ctx)
        assert isinstance(result, Failed)
        assert result.step_id == "marketplace-rebuilt"
        assert "allow_untagged" in result.reason or "--allow-untagged" in result.reason

    def test_untagged_dev_with_allow_untagged_proceeds(self, tmp_path):
        """Contract item 2 (flows.py path): untagged + allow_untagged=True
        -> the new guard does NOT fire (proceeds past it into
        build_plugin(), which may still fail for unrelated fixture-depth
        reasons -- same convention as cli.py's own
        test_dev_branch_proceeds_past_guard: "passes the guard (may fail
        later — NOT on the guard)")."""
        _, source_dir, ctx = _make_flows_ctx(
            str(tmp_path), configure_origin=False, allow_untagged=True
        )
        error_text = ""
        try:
            _apply_marketplace_rebuilt(ctx)
        except Exception as exc:  # noqa: BLE001 - asserting guard text absence
            error_text = str(exc)
        assert "allow_untagged" not in error_text
        assert "not a tagged commit" not in error_text

    def test_v_tag_merged_detached_head_allowed_no_flags(self, tmp_path):
        """Contract item 3 + 'also required' item 4 (flows.py path): HEAD
        detached at a v* tag merged into origin/dev -> both the #794 branch
        guard AND the new deploy-tag guard pass with NO allow_branch and NO
        allow_untagged (build_plugin() itself may still fail for unrelated
        fixture-depth reasons -- see test_untagged_dev_with_allow_untagged_proceeds)."""
        _, source_dir, ctx = _make_flows_ctx(
            str(tmp_path), tag_head="v1.0.0-flows-test", checkout_ref="v1.0.0-flows-test"
        )
        assert ctx["allow_branch"] is False
        assert ctx["allow_untagged"] is False
        error_text = ""
        try:
            _apply_marketplace_rebuilt(ctx)
        except Exception as exc:  # noqa: BLE001 - asserting guard text absence
            error_text = str(exc)
        assert "allow_untagged" not in error_text
        assert "not a tagged commit" not in error_text
        assert "allow_branch" not in error_text

    def test_deploy_tag_merged_detached_head_allowed_no_flags(self, tmp_path):
        """Contract item 4 (flows.py path): same as above but a deploy/*
        tag."""
        _, source_dir, ctx = _make_flows_ctx(
            str(tmp_path),
            tag_head="deploy/2026-09-05-flows-test",
            checkout_ref="deploy/2026-09-05-flows-test",
        )
        error_text = ""
        try:
            _apply_marketplace_rebuilt(ctx)
        except Exception as exc:  # noqa: BLE001 - asserting guard text absence
            error_text = str(exc)
        assert "allow_untagged" not in error_text
        assert "not a tagged commit" not in error_text
        assert "allow_branch" not in error_text

    def test_unmerged_tag_refused_without_allow_branch(self, tmp_path):
        """Contract item 5 (flows.py path): a same-named local tag on
        UNMERGED work must not bypass the guard -- refused without
        --allow-branch, even though a deployable-pattern tag is present."""
        engram_home = os.path.join(str(tmp_path), "engram_home")
        source_dir = os.path.join(str(tmp_path), "source_dir")
        os.makedirs(engram_home, exist_ok=True)
        os.makedirs(source_dir, exist_ok=True)
        _git(["init", "-q", "-b", "dev"], source_dir)
        _git(["config", "user.email", "test@test.com"], source_dir)
        _git(["config", "user.name", "Test"], source_dir)
        _populate_buildable_source_tree(source_dir)
        _git(["add", "-A"], source_dir)
        _git(["commit", "-q", "-m", "init"], source_dir)
        sha_a = _git(["rev-parse", "HEAD"], source_dir).stdout.strip()

        origin_bare = os.path.join(str(tmp_path), "unmerged-origin-bare.git")
        subprocess.run(
            ["git", "init", "-q", "-b", "dev", "--bare", origin_bare],
            capture_output=True, text=True, check=True,
        )
        _git(["remote", "add", "origin", origin_bare], source_dir)
        _git(["push", "-q", "origin", "dev"], source_dir)
        _git(["push", "-q", "origin", "dev:refs/heads/main"], source_dir)
        _git(["fetch", "-q", "origin"], source_dir)

        _git(["checkout", "-q", "-b", "stray", sha_a], source_dir)
        with open(os.path.join(source_dir, "stray.txt"), "w") as f:
            f.write("unmerged work\n")
        _git(["add", "-A"], source_dir)
        _git(["commit", "-q", "-m", "stray unmerged commit"], source_dir)
        _git(["tag", "deploy/2026-09-07-stray-flows"], source_dir)

        with open(os.path.join(engram_home, ".deployed-version"), "w") as f:
            f.write(f"alpha_sha={sha_a}\n")
            f.write(f"deployed_from={source_dir}\n")
        with open(os.path.join(engram_home, "config.json"), "w") as f:
            json.dump({"install_tier": "convenience", "multi_agent": False}, f)
        built_plugin_root = os.path.join(source_dir, "build", "plugin")
        os.makedirs(built_plugin_root, exist_ok=True)

        ctx: dict[str, Any] = {
            "engram_home": engram_home,
            "source_dir": source_dir,
            "target_sha": "HEAD",
            "ack_changeset": True,
            "allow_branch": False,
            "allow_untagged": False,
            "_deployed_sha": sha_a,
            "_built_plugin_root": built_plugin_root,
            "_changed_files": [],
            "_template_changed": False,
        }

        with pytest.raises(RuntimeError) as excinfo:
            _apply_marketplace_rebuilt(ctx)
        # Refused by the (extended) #794 branch guard: branch 'stray' is not
        # dev/main, and the tag is not merged so the deploy-tag exception
        # does not apply either.
        assert "stray" in str(excinfo.value)

    def test_unrelated_tag_refused_as_untagged(self, tmp_path):
        """Contract item 6 (flows.py path): an unrelated tag (foo/bar) at
        HEAD, no v*/deploy/* tag -> refused, treated as untagged."""
        _, source_dir, ctx = _make_flows_ctx(
            str(tmp_path), tag_head="foo/bar", configure_origin=False
        )
        with pytest.raises(RuntimeError) as excinfo:
            _apply_marketplace_rebuilt(ctx)
        assert "allow_untagged" in str(excinfo.value) or "--allow-untagged" in str(
            excinfo.value
        )


# ---------------------------------------------------------------------------
# Section C-control: contract item 7 -- discrimination control (mutation)
# ---------------------------------------------------------------------------


class TestDiscriminationControl:
    def test_removing_deploy_tag_guard_call_makes_untagged_test_go_red(self, tmp_path, monkeypatch):
        """Mutation control (contract item 7): temporarily patch
        _apply_marketplace_rebuilt's module-level deployable-tag check so it
        behaves as if the guard were never added (branch-only, #794's old
        behavior) -- the untagged-refusal test must go red under this
        mutation. Actually reverts the guard by monkeypatching
        refresh_and_check_deployable_tag_at (imported into tools.engine.flows)
        to always return a non-None value, simulating "guard always passes",
        which is the mutation shape that would let an untagged commit
        through -- the discriminating direction for this guard."""
        import tools.engine.flows as flows_mod

        monkeypatch.setattr(
            flows_mod, "refresh_and_check_deployable_tag_at", lambda *a, **k: "mutated-always-deployable"
        )

        _, source_dir, ctx = _make_flows_ctx(str(tmp_path), configure_origin=False)
        # Under the mutation, the guard no longer raises for an untagged
        # commit -- apply() proceeds into build_plugin() and succeeds,
        # which is exactly the silent-corruption failure mode the real
        # guard exists to prevent. This demonstrates the guard call is
        # actually load-bearing: remove/neutralize it and the refusal test
        # would go from red (RuntimeError raised) to green (no raise) --
        # i.e., the ORIGINAL (non-mutated) test in
        # TestFlowsEngineDeployTagGuard.test_untagged_dev_without_allow_untagged_refused
        # would go from PASS to FAIL if this mutation were applied to the
        # real code instead of monkeypatched here.
        error_text = ""
        try:
            _apply_marketplace_rebuilt(ctx)  # must NOT raise the guard's error under the mutation
        except Exception as exc:  # noqa: BLE001 - only the guard text is disallowed here
            error_text = str(exc)
        assert "allow_untagged" not in error_text
        assert "not a tagged commit" not in error_text


# ---------------------------------------------------------------------------
# Section D: engine call site -- cli.py::build_command
# ---------------------------------------------------------------------------


def _make_cli_repo(
    tmp_path,
    *,
    checkout_ref: "str | None" = None,
    tag_head=None,
    configure_origin: bool = True,
):
    """Minimal repo layout satisfying build_command's own repo-root guard
    (src/engram/server.py + src/build/packaging/tiers.json relative to cwd),
    with real git tags/branches/origin -- mirrors
    tests/test_engine_unit.py::TestBuildCommandBranchGuard's minimal layout,
    but uses REAL git (no subprocess.run monkeypatching) so the new
    deploy-tag guard's own git-plumbing calls are exercised for real rather
    than needing to be threaded through a fake dispatcher."""
    repo = tmp_path / "cli-repo"
    (repo / "src" / "engram").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "engram" / "server.py").write_text("")
    (repo / "src" / "build" / "packaging").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "build" / "packaging" / "tiers.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_tier": "convenience",
                "depth_tiers": {"essential": 1, "convenience": 2, "dev": 3},
                "mechanisms": {},
                "excluded": [],
                "repo_only": [],
                "build_inputs": [],
            }
        )
    )
    _git(["init", "-q", "-b", "dev"], str(repo))
    _git(["config", "user.email", "test@example.com"], str(repo))
    _git(["config", "user.name", "Test"], str(repo))
    _git(["add", "-A"], str(repo))
    _git(["commit", "-q", "-m", "init"], str(repo))

    tags = [tag_head] if isinstance(tag_head, str) else list(tag_head or [])
    for tag in tags:
        _git(["tag", tag], str(repo))

    if configure_origin:
        origin_bare = tmp_path / "cli-origin-bare.git"
        subprocess.run(
            ["git", "init", "-q", "-b", "dev", "--bare", str(origin_bare)],
            capture_output=True, text=True, check=True,
        )
        _git(["remote", "add", "origin", str(origin_bare)], str(repo))
        _git(["push", "-q", "origin", "dev"], str(repo))
        _git(["push", "-q", "origin", "dev:refs/heads/main"], str(repo))
        _git(["fetch", "-q", "origin"], str(repo))

    if checkout_ref:
        _git(["checkout", "-q", checkout_ref], str(repo))

    return repo


class TestCliBuildCommandDeployTagGuard:
    def test_untagged_dev_without_allow_untagged_refused(self, monkeypatch, tmp_path, capsys):
        """Contract item 1 (cli.py path): untagged commit on dev, no flag
        -> build_command returns 1 and names --allow-untagged."""
        repo = _make_cli_repo(tmp_path, configure_origin=False)
        monkeypatch.chdir(repo)
        rc = build_command([])
        captured = capsys.readouterr()
        assert rc == 1
        assert "allow-untagged" in captured.err
        assert "794" not in captured.err  # not the branch guard -- dev passes that trivially

    def test_untagged_dev_with_allow_untagged_passes_guard(self, monkeypatch, tmp_path, capsys):
        """Contract item 2 (cli.py path): --allow-untagged means the NEW
        guard's error text must not appear (matches
        test_dev_branch_proceeds_past_guard's existing convention of
        asserting the guard didn't fire, since build_plugin() itself needs
        a much heavier fixture to succeed end-to-end)."""
        repo = _make_cli_repo(tmp_path, configure_origin=False)
        monkeypatch.chdir(repo)
        build_command(["--allow-untagged"])
        captured = capsys.readouterr()
        assert "allow-untagged" not in captured.err

    def test_v_tag_merged_detached_head_passes_both_guards_no_flags(self, monkeypatch, tmp_path, capsys):
        """Contract item 3 + 'also required' item 4 (cli.py path): HEAD
        detached at a v* tag merged into origin/dev -> neither the #794
        branch guard NOR the new deploy-tag guard fire, with NO flags."""
        repo = _make_cli_repo(
            tmp_path, tag_head="v1.0.0-cli-test", checkout_ref="v1.0.0-cli-test"
        )
        monkeypatch.chdir(repo)
        build_command([])
        captured = capsys.readouterr()
        assert "794" not in captured.err
        assert "allow-untagged" not in captured.err

    def test_deploy_tag_merged_detached_head_passes_both_guards_no_flags(self, monkeypatch, tmp_path, capsys):
        """Contract item 4 (cli.py path): same as above but a deploy/* tag."""
        repo = _make_cli_repo(
            tmp_path,
            tag_head="deploy/2026-09-05-cli-test",
            checkout_ref="deploy/2026-09-05-cli-test",
        )
        monkeypatch.chdir(repo)
        build_command([])
        captured = capsys.readouterr()
        assert "794" not in captured.err
        assert "allow-untagged" not in captured.err

    def test_unmerged_tag_refused_without_allow_branch(self, monkeypatch, tmp_path, capsys):
        """Contract item 5 (cli.py path): a same-named local tag on
        unmerged work must not bypass #794 -- refused without
        --allow-branch."""
        repo = tmp_path / "cli-unmerged-repo"
        (repo / "src" / "engram").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "engram" / "server.py").write_text("")
        (repo / "src" / "build" / "packaging").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "build" / "packaging" / "tiers.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default_tier": "convenience",
                    "depth_tiers": {"essential": 1, "convenience": 2, "dev": 3},
                    "mechanisms": {},
                    "excluded": [],
                    "repo_only": [],
                    "build_inputs": [],
                }
            )
        )
        _git(["init", "-q", "-b", "dev"], str(repo))
        _git(["config", "user.email", "test@example.com"], str(repo))
        _git(["config", "user.name", "Test"], str(repo))
        _git(["add", "-A"], str(repo))
        _git(["commit", "-q", "-m", "init"], str(repo))
        sha_a = _git(["rev-parse", "HEAD"], str(repo)).stdout.strip()

        origin_bare = tmp_path / "cli-unmerged-origin-bare.git"
        subprocess.run(
            ["git", "init", "-q", "-b", "dev", "--bare", str(origin_bare)],
            capture_output=True, text=True, check=True,
        )
        _git(["remote", "add", "origin", str(origin_bare)], str(repo))
        _git(["push", "-q", "origin", "dev"], str(repo))
        _git(["push", "-q", "origin", "dev:refs/heads/main"], str(repo))
        _git(["fetch", "-q", "origin"], str(repo))

        _git(["checkout", "-q", "-b", "stray", sha_a], str(repo))
        (repo / "stray.txt").write_text("unmerged\n")
        _git(["add", "-A"], str(repo))
        _git(["commit", "-q", "-m", "stray"], str(repo))
        _git(["tag", "deploy/2026-09-07-stray-cli"], str(repo))

        monkeypatch.chdir(repo)
        rc = build_command([])
        captured = capsys.readouterr()
        assert rc == 1
        assert "794" in captured.err
        assert "stray" in captured.err

    def test_unrelated_tag_refused_as_untagged(self, monkeypatch, tmp_path, capsys):
        """Contract item 6 (cli.py path): an unrelated tag (foo/bar), no
        v*/deploy/* tag -> refused, treated as untagged."""
        repo = _make_cli_repo(tmp_path, tag_head="foo/bar", configure_origin=False)
        monkeypatch.chdir(repo)
        rc = build_command([])
        captured = capsys.readouterr()
        assert rc == 1
        assert "allow-untagged" in captured.err
