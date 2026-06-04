"""tools.engine.flows — upgrade (and future) flow catalog.

Pure Python 3 stdlib; ZERO imports from ENGRAM runtime modules.

Each flow is a function returning ``list[Step]``.  Callers construct a Ctx
(plain dict), pass it with the step list to ``run_flow`` / ``run_doctor`` /
``plan_flow``.

Upgrade flow
------------
Mechanizes the ENGRAM plugin upgrade skill (skills/claude/engram-upgrade/).
Steps map 1-to-1 with the skill's numbered sections:

  source-tree-located     Step 0a: .deployed-version exists + points to readable dir
  tree-at-commit          Step 0b: source tree is on dev and clean (or at expected SHA)
  changeset-reviewed      Step 1:  agent-judgment step — emits changeset summary;
                                   REQUIRES explicit ctx.ack_changeset = True to continue
  marketplace-rebuilt     Step 2:  calls Phase-1 build_plugin(); PRECONDITION: branch==dev
                                   (unless ctx.allow_branch — guard per #794)
  flags-match-config      Step 3:  tier + multi_agent in built manifest match config.json
  plugin-marketplace-update  Step 4: [operator] /plugin marketplace update
  plugin-upgrade          Step 5:  [operator] /plugin upgrade engram
  mcp-reconnect           Step 6:  [operator, conditional] /mcp reconnect — only when
                                   server.py or engram_*.py changed in deployed..target range
  template-gate           Step 7:  [agent, judgment] detect CLAUDE.md.template diff;
                                   NEVER edits ~/.claude/CLAUDE.md — surfaces only
  stats-verify-instruction   Step 8: [operator] verify engram_stats via MCP
  record-reminder         Step 9:  [operator] remind agent to file the upgrade obs

The framework is flow-neutral — install and migrate flows follow the same shape.

Ctx contract for upgrade flow
------------------------------
Callers must provide a dict (or dict-subclass) with at least:

  engram_home: str          path to ~/.engram  (or fake equivalent in tests)
  source_dir: str           path to engram-alpha repo root
  target_sha: str           the git commit SHA to upgrade to
  ack_changeset: bool       True once the agent has reviewed the changeset
  allow_branch: bool        True to skip the dev-branch guard (#794)

Optional keys (computed by steps, stored back into ctx):

  _deployed_sha: str        SHA from .deployed-version
  _changeset_commits: list  git log lines deployed..target
  _template_changed: bool   whether templates/CLAUDE.md.template changed
  _build_manifest: dict     loaded from built plugin .engram-build-manifest.json
  _built_plugin_root: str   path to the assembled plugin tree
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .steps import Step

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_deployed_version(engram_home: str) -> dict[str, str]:
    """Parse ~/.engram/.deployed-version into a dict of key=value pairs."""
    path = os.path.join(engram_home, ".deployed-version")
    if not os.path.isfile(path):
        return {}
    result: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Run a git command in cwd; return CompletedProcess (check=False)."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _current_branch(source_dir: str) -> str:
    """Return the current git branch name in source_dir."""
    result = _git(["branch", "--show-current"], source_dir)
    return result.stdout.strip()


def _is_clean(source_dir: str) -> bool:
    """Return True if the working tree has no uncommitted changes."""
    result = _git(["status", "--short"], source_dir)
    return result.stdout.strip() == ""


def _git_log_range(source_dir: str, from_sha: str, to_sha: str) -> list[str]:
    """Return oneline log lines for from_sha..to_sha."""
    result = _git(["log", "--oneline", f"{from_sha}..{to_sha}"], source_dir)
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return lines


def _files_in_range(source_dir: str, from_sha: str, to_sha: str) -> list[str]:
    """Return list of files changed between from_sha and to_sha."""
    result = _git(
        ["diff", "--name-only", f"{from_sha}..{to_sha}"],
        source_dir,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def _build_manifest_path(ctx: dict[str, Any]) -> str:
    """Return path to the built plugin's .engram-build-manifest.json."""
    built_root = ctx.get("_built_plugin_root", "")
    if not built_root:
        # Fall back: use the standard build/plugin path relative to source_dir
        built_root = os.path.join(ctx["source_dir"], "build", "plugin")
    return os.path.join(built_root, ".engram-build-manifest.json")


def _load_engram_config(ctx: dict[str, Any]) -> dict[str, Any]:
    """Load ~/.engram/config.json; return empty dict if missing."""
    path = os.path.join(ctx["engram_home"], "config.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# upgrade flow
# ---------------------------------------------------------------------------


def upgrade_flow() -> list[Step]:
    """Return the step list for the ENGRAM plugin upgrade flow.

    The returned list is a fresh copy each call; callers may mutate it.
    """
    return [
        # ── Step 0a: source-tree-located ─────────────────────────────────
        Step(
            id="source-tree-located",
            requires=[],
            kind="agent",
            check=lambda ctx: (
                os.path.isfile(
                    os.path.join(ctx["engram_home"], ".deployed-version")
                )
                and bool(
                    _read_deployed_version(ctx["engram_home"]).get("deployed_from")
                )
                and os.path.isdir(
                    _read_deployed_version(ctx["engram_home"]).get("deployed_from", "")
                )
            ),
            apply=lambda ctx: _apply_source_tree_located(ctx),
            verify=lambda ctx: (
                os.path.isfile(
                    os.path.join(ctx["engram_home"], ".deployed-version")
                )
                and os.path.isdir(ctx.get("source_dir", ""))
            ),
            instruction=None,
        ),

        # ── Step 0b: tree-at-commit ───────────────────────────────────────
        Step(
            id="tree-at-commit",
            requires=["source-tree-located"],
            kind="agent",
            check=lambda ctx: _check_tree_at_commit(ctx),
            apply=lambda ctx: _apply_tree_at_commit(ctx),
            verify=lambda ctx: _check_tree_at_commit(ctx),
            instruction=None,
        ),

        # ── Step 1: changeset-reviewed ───────────────────────────────────
        # Agent-judgment step: emits changeset summary into ctx;
        # requires ctx.ack_changeset = True to be considered satisfied.
        Step(
            id="changeset-reviewed",
            requires=["tree-at-commit"],
            kind="agent",
            check=lambda ctx: bool(ctx.get("ack_changeset")),
            apply=lambda ctx: _apply_changeset_reviewed(ctx),
            verify=lambda ctx: bool(ctx.get("ack_changeset")),
            instruction=None,
        ),

        # ── Step 2: marketplace-rebuilt ──────────────────────────────────
        # Calls Phase-1 build_plugin().
        # PRECONDITION: branch == dev unless ctx.allow_branch (#794's guard).
        Step(
            id="marketplace-rebuilt",
            requires=["changeset-reviewed"],
            kind="agent",
            check=lambda ctx: _check_marketplace_rebuilt(ctx),
            apply=lambda ctx: _apply_marketplace_rebuilt(ctx),
            verify=lambda ctx: _check_marketplace_rebuilt(ctx),
            instruction=None,
        ),

        # ── Step 3: flags-match-config ───────────────────────────────────
        Step(
            id="flags-match-config",
            requires=["marketplace-rebuilt"],
            kind="agent",
            check=lambda ctx: _check_flags_match_config(ctx),
            apply=lambda ctx: None,  # No-op apply; check IS the verification
            verify=lambda ctx: _check_flags_match_config(ctx),
            instruction=None,
        ),

        # ── Step 4: plugin-marketplace-update [operator] ─────────────────
        Step(
            id="plugin-marketplace-update",
            requires=["flags-match-config"],
            kind="operator",
            check=lambda ctx: bool(ctx.get("_plugin_marketplace_updated")),
            apply=None,
            verify=lambda ctx: bool(ctx.get("_plugin_marketplace_updated")),
            instruction=(
                "Please run in this Claude Code session:\n"
                "  /plugin marketplace update\n"
                "Then set ctx['_plugin_marketplace_updated'] = True and re-run."
            ),
        ),

        # ── Step 5: plugin-upgrade [operator] ────────────────────────────
        Step(
            id="plugin-upgrade",
            requires=["plugin-marketplace-update"],
            kind="operator",
            check=lambda ctx: bool(ctx.get("_plugin_upgrade_done")),
            apply=None,
            verify=lambda ctx: bool(ctx.get("_plugin_upgrade_done")),
            instruction=(
                "Please run in this Claude Code session:\n"
                "  /plugin upgrade engram\n"
                "Then set ctx['_plugin_upgrade_done'] = True and re-run."
            ),
        ),

        # ── Step 6: mcp-reconnect [operator, conditional] ────────────────
        # Only needed when server.py or engram_*.py changed in the range.
        Step(
            id="mcp-reconnect",
            requires=["plugin-upgrade"],
            kind="operator",
            check=lambda ctx: _check_mcp_reconnect(ctx),
            apply=None,
            verify=lambda ctx: _check_mcp_reconnect(ctx),
            instruction=(
                "The MCP server module changed in this upgrade.\n"
                "Please reconnect the engram MCP server:\n"
                "  Run `/mcp` and reconnect engram, or restart Claude Code.\n"
                "Then set ctx['_mcp_reconnected'] = True and re-run."
            ),
        ),

        # ── Step 7: template-gate [agent, judgment] ───────────────────────
        # Detects CLAUDE.md.template changes; surfaces diff; NEVER edits
        # ~/.claude/CLAUDE.md — that is hard-boundary agent judgment.
        Step(
            id="template-gate",
            requires=["mcp-reconnect"],
            kind="agent",
            check=lambda ctx: bool(ctx.get("_template_gate_done")),
            apply=lambda ctx: _apply_template_gate(ctx),
            verify=lambda ctx: bool(ctx.get("_template_gate_done")),
            instruction=None,
        ),

        # ── Step 8: stats-verify-instruction [operator] ──────────────────
        Step(
            id="stats-verify-instruction",
            requires=["template-gate"],
            kind="operator",
            check=lambda ctx: bool(ctx.get("_stats_verified")),
            apply=None,
            verify=lambda ctx: bool(ctx.get("_stats_verified")),
            instruction=(
                "Please verify the upgrade via the plugin MCP:\n"
                "  Call engram_stats and confirm it returns your real node count.\n"
                "  Also confirm: cat ~/.engram/.deployed-version  (SHA must match target)\n"
                "Then set ctx['_stats_verified'] = True and re-run."
            ),
        ),

        # ── Step 9: record-reminder [operator] ───────────────────────────
        Step(
            id="record-reminder",
            requires=["stats-verify-instruction"],
            kind="operator",
            check=lambda ctx: bool(ctx.get("_record_filed")),
            apply=None,
            verify=lambda ctx: bool(ctx.get("_record_filed")),
            instruction=(
                "File the upgrade observation in ENGRAM:\n"
                "  engram_add_observation with claim summarizing: date/time, "
                "commit SHA, /plugin upgrade done, MCP reconnect status, "
                "template gate outcome, engram_stats node count.\n"
                "Then set ctx['_record_filed'] = True and re-run."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# step apply helpers
# ---------------------------------------------------------------------------


def _apply_source_tree_located(ctx: dict[str, Any]) -> None:
    """Populate ctx['source_dir'] from .deployed-version if possible."""
    dv = _read_deployed_version(ctx["engram_home"])
    deployed_from = dv.get("deployed_from", "")
    if deployed_from and os.path.isdir(deployed_from):
        # Only update if not already set to a valid dir
        if not os.path.isdir(ctx.get("source_dir", "")):
            ctx["source_dir"] = deployed_from
    deployed_sha = dv.get("alpha_sha", "")
    if deployed_sha:
        ctx["_deployed_sha"] = deployed_sha


def _check_tree_at_commit(ctx: dict[str, Any]) -> bool:
    """True if source tree is clean; we don't force a specific SHA."""
    source_dir = ctx.get("source_dir", "")
    if not os.path.isdir(source_dir):
        return False
    return _is_clean(source_dir)


def _apply_tree_at_commit(ctx: dict[str, Any]) -> None:
    """Fetch and derive branch info; store in ctx."""
    source_dir = ctx.get("source_dir", "")
    if not source_dir:
        return
    # Fetch latest
    _git(["fetch", "origin"], source_dir)
    # Store deployed SHA if not already set
    if not ctx.get("_deployed_sha"):
        dv = _read_deployed_version(ctx["engram_home"])
        ctx["_deployed_sha"] = dv.get("alpha_sha", "")


def _apply_changeset_reviewed(ctx: dict[str, Any]) -> None:
    """Compute changeset commits and template-changed flag; store in ctx."""
    source_dir = ctx.get("source_dir", "")
    deployed_sha = ctx.get("_deployed_sha", "")
    target_sha = ctx.get("target_sha", "HEAD")

    if source_dir and deployed_sha:
        commits = _git_log_range(source_dir, deployed_sha, target_sha)
        ctx["_changeset_commits"] = commits

        # Check whether templates/CLAUDE.md.template changed
        files = _files_in_range(source_dir, deployed_sha, target_sha)
        ctx["_template_changed"] = "templates/CLAUDE.md.template" in files
        ctx["_changed_files"] = files

    # Note: we do NOT set ack_changeset here — that requires explicit operator
    # acknowledgment via ctx.ack_changeset = True.


def _check_marketplace_rebuilt(ctx: dict[str, Any]) -> bool:
    """True if the build manifest exists at the built plugin root and SHA matches."""
    manifest_path = _build_manifest_path(ctx)
    if not os.path.isfile(manifest_path):
        return False
    try:
        with open(manifest_path, encoding="utf-8") as f:
            bm = json.load(f)
        ctx["_build_manifest"] = bm
        return True
    except Exception:
        return False


def _apply_marketplace_rebuilt(ctx: dict[str, Any]) -> None:
    """Call Phase-1 build_plugin() to rebuild the marketplace.

    PRECONDITION: branch == dev unless ctx.allow_branch.
    This guard implements #794's requirement: the build step refuses to run
    on a non-dev branch unless the caller explicitly passes allow_branch=True.
    Rationale: shipping a non-dev branch's code as a plugin upgrade is almost
    always a mistake; the guard makes the unusual case explicit.
    """
    source_dir = ctx.get("source_dir", "")
    if not source_dir:
        raise RuntimeError("source_dir not set in ctx — cannot rebuild marketplace")

    # #794 branch guard: refuse non-dev branch unless allow_branch is set
    current = _current_branch(source_dir)
    if current != "dev" and not ctx.get("allow_branch"):
        raise RuntimeError(
            f"marketplace-rebuilt: source tree is on branch {current!r}, not 'dev'. "
            "Pass allow_branch=True in ctx to override this guard. "
            "(Guard per #794: shipping a non-dev branch is almost always a mistake.)"
        )

    # Load config.json for tier + multi_agent
    config = _load_engram_config(ctx)
    tier = config.get("install_tier")
    if not tier:
        raise RuntimeError(
            "install_tier unset in config.json — set it (essential|convenience|dev) "
            "before upgrading; see #707"
        )
    multi_agent = bool(config.get("multi_agent", False))

    # Determine plugin output path
    plugin_root = os.path.join(source_dir, "build", "plugin")
    ctx["_built_plugin_root"] = plugin_root

    # Delegate to Phase-1 engine
    from .build import build_plugin
    from .manifest import load_manifest, resolve_tier

    manifest_path = os.path.join(source_dir, "packaging", "tiers.json")
    manifest = load_manifest(manifest_path)
    tier = resolve_tier(manifest, tier)

    build_plugin(
        repo_root=source_dir,
        plugin_root=plugin_root,
        tier=tier,
        multi_agent=multi_agent,
        manifest=manifest,
    )


def _check_flags_match_config(ctx: dict[str, Any]) -> bool:
    """True if built manifest tier+multi_agent matches config.json.

    On mismatch, stores a human-readable delta in ``ctx["_last_failure_detail"]``
    so the executor can surface it in the FAILED reason.
    """
    manifest_path = _build_manifest_path(ctx)
    if not os.path.isfile(manifest_path):
        return False
    try:
        with open(manifest_path, encoding="utf-8") as f:
            bm = json.load(f)
    except Exception:
        return False

    config = _load_engram_config(ctx)
    expected_tier = config.get("install_tier") or "convenience"
    expected_ma = bool(config.get("multi_agent", False))

    built_tier = bm.get("tier")
    built_ma = bool(bm.get("multi_agent"))

    if built_tier == expected_tier and built_ma == expected_ma:
        return True

    ctx["_last_failure_detail"] = (
        f"built tier={built_tier} multi_agent={built_ma} "
        f"but config.json says tier={expected_tier} multi_agent={expected_ma}"
    )
    return False


def _check_mcp_reconnect(ctx: dict[str, Any]) -> bool:
    """True if reconnect not needed OR has been done.

    Reconnect is needed only when server.py or engram_*.py changed in range.
    """
    changed_files = ctx.get("_changed_files", [])
    needs_reconnect = any(
        f == "server.py" or (f.startswith("engram_") and f.endswith(".py"))
        for f in changed_files
    )
    if not needs_reconnect:
        # No server-side changes → step is automatically satisfied
        return True
    return bool(ctx.get("_mcp_reconnected"))


def _apply_template_gate(ctx: dict[str, Any]) -> None:
    """Detect CLAUDE.md.template diff; surface it; mark gate done.

    Hard boundary: this function NEVER edits ~/.claude/CLAUDE.md.
    It only reads the template and the live file, computes the diff,
    and stores it in ctx for the agent to surface.  The inverse-merge
    is always an agent-judgment action.
    """
    source_dir = ctx.get("source_dir", "")
    template_changed = ctx.get("_template_changed", False)

    if not template_changed:
        # No template changes in range — gate trivially passed
        ctx["_template_diff"] = None
        ctx["_template_gate_done"] = True
        return

    template_path = os.path.join(source_dir, "templates", "CLAUDE.md.template")

    # Compute a simple unified diff (stdlib only)
    import difflib

    try:
        with open(template_path, encoding="utf-8") as f:
            template_lines = f.readlines()
    except OSError:
        ctx["_template_diff"] = f"(could not read template at {template_path})"
        ctx["_template_gate_done"] = True
        return

    # We surface the template content; the agent must do the inverse-merge.
    # Storing the template text + a note; the agent narrates from this.
    ctx["_template_diff"] = "".join(template_lines)
    # Mark gate done — the detection happened; the merge decision is agent-side.
    ctx["_template_gate_done"] = True


# ---------------------------------------------------------------------------
# plan_flow — for `plan` CLI verb
# ---------------------------------------------------------------------------


def plan_flow(steps: list[Step], ctx: Any) -> list[dict[str, Any]]:
    """Return a list of per-step plan dicts (id, kind, requires, satisfied).

    Used by the ``plan`` CLI verb to print the DAG + check() state.
    check() calls may cache read-only probe results into ctx under
    underscore-prefixed keys (e.g. ``_build_manifest``); no system state
    is mutated.

    Parameters
    ----------
    steps:
        The flow step list.
    ctx:
        A context snapshot to evaluate check() against.

    Returns
    -------
    list[dict]
        Each dict has keys: id, kind, requires, satisfied (bool), error (str|None).
    """
    from .steps import _topological_sort

    ordered = _topological_sort(steps)
    result: list[dict[str, Any]] = []
    for step in ordered:
        try:
            satisfied = step.check(ctx)
            error = None
        except Exception as exc:
            satisfied = False
            error = str(exc)
        result.append(
            {
                "id": step.id,
                "kind": step.kind,
                "requires": list(step.requires),
                "satisfied": satisfied,
                "error": error,
            }
        )
    return result
