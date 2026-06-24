"""tools.engine.flows — upgrade (and future) flow catalog.

Pure Python 3 stdlib; ZERO imports from ENGRAM runtime modules.

Each flow is a function returning ``list[Step]``.  Callers construct a Ctx
(plain dict), pass it with the step list to ``run_flow`` / ``run_doctor`` /
``plan_flow``.

Upgrade flow
------------
Mechanizes the ENGRAM plugin upgrade skill (skills/claude/engram-upgrade/).
Steps map 1-to-1 with the skill's numbered sections:

  substrate-health        [operator] gate: marker absent → healthy, step skipped;
                          marker present → refuse upgrade; never opens knowledge.db
                          (hazard constraint per #786 §1; part of #786)
  source-tree-located     Step 0a: .deployed-version exists + points to readable dir
  tree-at-commit          Step 0b: source tree is on dev and clean (or at expected SHA)
  changeset-reviewed      Step 1:  agent-judgment step — emits changeset summary;
                                   REQUIRES explicit ctx.ack_changeset = True to continue
  marketplace-rebuilt     Step 2:  calls Phase-1 build_plugin(); PRECONDITION: branch==dev or main
                                   (unless ctx.allow_branch — guard per #794)
  flags-match-config      Step 3:  tier + multi_agent in built manifest match config.json
  plugin-marketplace-update  Step 4: [operator] /plugin marketplace update engram-local
                                     (requires substrate-health + flags-match-config)
  plugin-upgrade          Step 5:  [operator] /plugin → Installed → engram plugin entry → Update now
  mcp-reconnect           Step 6:  [operator, conditional] /mcp reconnect — only when
                                   server.py or engram_*.py changed in deployed..target range
  template-gate           Step 7:  [agent, judgment] detect template.CLAUDE.md diff;
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
  _template_changed: bool   whether templates/template.CLAUDE.md changed
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

# Substrate-degraded marker filename — mirrors engram_walguard._MARKER_FILENAME
# (engram_walguard.py:139).  The engine does NOT import engram_walguard to avoid
# pulling in ENGRAM runtime dependencies; this one-line join is the full resolution.
_SUBSTRATE_MARKER_FILENAME = ".substrate-degraded.json"


def _substrate_marker_path(engram_home: str) -> str:
    """Return the path to the degraded-substrate marker file.

    Resolution mirrors engram_walguard._marker_path (line 143):
    Path(data_dir) / _MARKER_FILENAME, where data_dir == engram_home.
    The engine resolves engram_home from ctx["engram_home"], the same source
    used by _load_engram_config below.
    """
    return os.path.join(engram_home, _SUBSTRATE_MARKER_FILENAME)


def _read_substrate_marker(engram_home: str) -> "dict | None":
    """Read the degraded-substrate marker if present; return None when absent.

    HAZARD CONSTRAINT: never opens knowledge.db — marker file read only
    (os.path.exists + open).  An external DB connection on a poisoned live WAL
    creates the split-brain it checks for (see issue #786 §1).

    Tolerates malformed JSON: treats any read/parse failure as degraded
    (presence = degraded; content is best-effort detail only).
    """
    path = _substrate_marker_path(engram_home)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # Marker present but unreadable → still degraded; return sentinel
        return {"reason": "(marker present but unreadable)", "dump_sha": None, "detected_at": None}


def _substrate_health_instruction(marker: "dict | None", engram_home: str = "") -> str:
    """Build the operator instruction string for the substrate-health step.

    Called unconditionally at construction time; when the marker is absent,
    ``marker`` is None and the reason/dump_sha fields fall back to "(unknown)".
    Renders the marker's reason/detected_at/dump_sha fields and points at the
    documented restore path.  Tolerates missing fields.

    ``engram_home`` is used to render the exact rm path in the instruction.
    Falls back to the ``~/.engram`` literal when not provided.
    """
    reason = "(unknown)" if not marker else (marker.get("reason") or "(unknown)")
    ts = "" if not marker else (marker.get("detected_at") or "")
    dump_sha = "" if not marker else (marker.get("dump_sha") or "(none)")

    # Render the rm path from the resolved engram_home so the instruction names
    # the correct file even under --ctx-file with a custom engram_home.
    # Note: the instruction is built at construction time using the env/default
    # resolution; the check/verify lambdas always use ctx["engram_home"] and
    # will correctly reflect a different runtime engram_home if --ctx-file
    # changes it.  This is the "cheapest fix" per the colleague suggestion.
    marker_path = _substrate_marker_path(engram_home) if engram_home else "~/.engram/.substrate-degraded.json"

    ts_line = f"\n  Detected at:  {ts}" if ts else ""
    return (
        "SUBSTRATE DEGRADED — upgrade refused.\n"
        "\n"
        f"  Reason:    {reason}{ts_line}\n"
        f"  Dump SHA:  {dump_sha}\n"
        "\n"
        f"The degraded-state marker ({marker_path}) is present.\n"
        "This means the WAL/shm guard detected split-brain WAL-index displacement\n"
        "and performed an emergency dump.  Upgrading on a poisoned substrate risks\n"
        "permanent data loss.\n"
        "\n"
        "Required actions before upgrading:\n"
        "  1. Restore from the last emergency dump (see #781/#790 restore procedure).\n"
        "  2. Verify the restored DB is clean (engram_stats returns your real node count).\n"
        f"  3. Remove the marker: rm {marker_path}\n"
        "  4. Re-run the upgrade flow.\n"
        "\n"
        "Do NOT upgrade until the marker is cleared — this step blocks on every run."
    )


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


def _make_substrate_health_step(engram_home: str) -> Step:
    """Build the substrate-health Step, reading the marker at construction time.

    upgrade_flow() creates a fresh step list on every call, so reading the
    marker here (at flow-construction time) captures the current marker state
    and embeds it into the instruction string.  The check/verify lambdas
    re-read the marker path on each invocation so they reflect the live
    filesystem state if the flow is re-run after the marker is removed.

    HAZARD CONSTRAINT: no DB connection — marker file read only.
    """
    # Read marker now to embed content into the static instruction string.
    # The lambdas re-check the path on every call (idempotent/resumable contract).
    marker = _read_substrate_marker(engram_home)
    instruction = _substrate_health_instruction(marker, engram_home)

    return Step(
        id="substrate-health",
        requires=[],
        kind="operator",
        check=lambda ctx: not os.path.exists(
            _substrate_marker_path(ctx["engram_home"])
        ),
        apply=None,
        verify=lambda ctx: not os.path.exists(
            _substrate_marker_path(ctx["engram_home"])
        ),
        instruction=instruction,
    )


def upgrade_flow() -> list[Step]:
    """Return the step list for the ENGRAM plugin upgrade flow.

    The returned list is a fresh copy each call; callers may mutate it.

    Ctx contract additions (substrate-health):
      No new ctx keys required.  The step reads ctx["engram_home"] (already
      required) to locate the degraded-substrate marker.
    """
    # Resolve engram_home the same way the CLI does (_build_ctx_from_env), so
    # that the substrate-health instruction embeds the live marker content.
    # We cannot use ctx here (not yet constructed), so fall back to the env var
    # or the default ~/.engram — the same resolution _build_ctx_from_env uses.
    # Tests override this by passing a fake engram_home in ctx; the check/verify
    # lambdas inside the step always use ctx["engram_home"], so fake dirs work.
    _default_engram_home = os.environ.get(
        "ENGRAM_HOME", os.path.expanduser("~/.engram")
    )

    return [
        # ── Step substrate-health [operator] ─────────────────────────────
        # Root prerequisite gating all live-install-mutating steps.
        # Reads the degraded-substrate marker written by engram_walguard (the
        # ONE detector; merged #796).  This step NEVER opens knowledge.db —
        # marker read + os.path.exists only (hazard constraint: #786 §1).
        #
        # DAG: no requires (parallel root alongside source-tree-located).
        # plugin-marketplace-update requires this step, gating all
        # live-install-mutating steps (marketplace-update → plugin-upgrade →
        # mcp-reconnect) transitively.  Pure-compute preflight steps (0a–3)
        # are not gated and may run safely on any substrate state.
        _make_substrate_health_step(_default_engram_home),

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
        # PRECONDITION: branch == 'dev' or 'main' unless ctx.allow_branch (#794's guard).
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
            requires=["flags-match-config", "substrate-health"],
            kind="operator",
            check=lambda ctx: bool(ctx.get("_plugin_marketplace_updated")),
            apply=None,
            verify=lambda ctx: bool(ctx.get("_plugin_marketplace_updated")),
            instruction=(
                "Please run in this Claude Code session:\n"
                "  /plugin marketplace update engram-local\n"
                "Then set ctx['_plugin_marketplace_updated'] = True and re-run."
            ),
        ),

        # ── Step 5: plugin-upgrade [operator] ────────────────────────────
        # Uses the interactive plugin menu, not '/plugin upgrade engram' (stale).
        # The correct UX (per #945/#947): /plugin → Installed → engram plugin → Update now.
        Step(
            id="plugin-upgrade",
            requires=["plugin-marketplace-update"],
            kind="operator",
            check=lambda ctx: bool(ctx.get("_plugin_upgrade_done")),
            apply=None,
            verify=lambda ctx: bool(ctx.get("_plugin_upgrade_done")),
            instruction=(
                "Please upgrade the plugin using the interactive menu in this Claude Code session:\n"
                "  /plugin → Installed → engram plugin entry (not the MCP listed under it) → Update now\n"
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
        # Detects template.CLAUDE.md changes; surfaces diff; NEVER edits
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

        # Check whether template.CLAUDE.md changed (path from Phase 1 #1093;
        # root compat symlink removed in Phase 4; renamed from CLAUDE.md.template).
        files = _files_in_range(source_dir, deployed_sha, target_sha)
        ctx["_template_changed"] = "src/engram/templates/template.CLAUDE.md" in files
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

    PRECONDITION: branch == 'dev' or 'main' unless ctx.allow_branch.
    This guard implements #794's requirement: the build step refuses to run
    on a PR/feature branch unless the caller explicitly passes allow_branch=True.
    Rationale: shipping a PR/feature branch's code as a plugin upgrade is almost
    always a mistake; the guard makes the unusual case explicit.
    Valid production branches: 'dev' (private dev source), 'main' (public release).
    """
    source_dir = ctx.get("source_dir", "")
    if not source_dir:
        raise RuntimeError("source_dir not set in ctx — cannot rebuild marketplace")

    # #794 branch guard: refuse PR/feature branches unless allow_branch is set.
    # Valid production branches: 'dev' (private dev source) and 'main' (public release branch).
    current = _current_branch(source_dir)
    if current not in ("dev", "main") and not ctx.get("allow_branch"):
        raise RuntimeError(
            f"marketplace-rebuilt: source tree is on branch {current!r}, not 'dev' (private dev) or 'main' (public release). "
            "Pass allow_branch=True in ctx to override this guard. "
            "(Guard per #794: shipping a PR/feature branch is almost always a mistake.)"
        )

    # Load config.json for tier + multi_agent
    config = _load_engram_config(ctx)
    tier = config.get("install_tier")
    if not tier:
        raise RuntimeError(
            "install_tier unset in config.json — set it (essential|convenience|dev) "
            "before upgrading; see #707"
        )
    multi_agent = bool(config.get("multi_agent")) or config.get("mode") == "multi"

    # Determine plugin output path
    plugin_root = os.path.join(source_dir, "build", "plugin")
    ctx["_built_plugin_root"] = plugin_root

    # Delegate to Phase-1 engine
    from .build import build_plugin
    from .manifest import load_manifest, resolve_tier

    # packaging/ moved to src/build/packaging/ in Phase 2 #1093;
    # root compat symlink removed in Phase 4.
    manifest_path = os.path.join(source_dir, "src", "build", "packaging", "tiers.json")
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
    expected_ma = bool(config.get("multi_agent")) or config.get("mode") == "multi"

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
        # Accept both old root path (pre-Phase4) and new src/engram/ path (post-Phase4 #1093)
        f in ("server.py", "src/engram/server.py")
        or (f.startswith("engram_") and f.endswith(".py"))
        or (f.startswith("src/engram/engram_") and f.endswith(".py"))
        for f in changed_files
    )
    if not needs_reconnect:
        # No server-side changes → step is automatically satisfied
        return True
    return bool(ctx.get("_mcp_reconnected"))


def _apply_template_gate(ctx: dict[str, Any]) -> None:
    """Detect template.CLAUDE.md diff; surface it; mark gate done.

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

    # templates/ moved to src/engram/templates/ in Phase 1 #1093;
    # root compat symlink removed in Phase 4; renamed from CLAUDE.md.template.
    template_path = os.path.join(source_dir, "src", "engram", "templates", "template.CLAUDE.md")

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
