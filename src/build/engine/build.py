"""tools.engine.build — build transforms for the ENGRAM plugin tree.

Port of tools/build-plugin.sh semantics into testable Python functions.
Pure Python 3 stdlib; ZERO imports from ENGRAM runtime modules.

Each section mirrors the corresponding numbered section in build-plugin.sh.
Comments referencing bash line numbers are included where the mapping is
non-obvious.

REPLICATION NOTES (bash oddities / edge cases faithfully ported):
  - The tools/ copy loop in build-plugin.sh iterates SHIPPED_PATHS that match
    '^tools/', and for each entry, if the source is a directory it does
    ``cp -r src/. dest/`` (copy contents, not the dir itself).  Ported exactly
    using shutil.copytree(dirs_exist_ok=True).
  - templates/ uses ``cp -a`` (archive mode, preserving permissions/timestamps)
    while tools/ uses ``cp -r``.  Python's shutil.copytree does not replicate
    mtime/owner, so both are ported as copytree with the same semantics — any
    timestamp difference between legacy and engine is a nondeterminism that the
    golden test normalizes (stat skipping; only content + names are compared).
  - hooks/claude enumeration uses ``find hooks/claude -maxdepth 1 \\( -name
    '*.py' -o -name '*.sh' \\) | sort``.  Ported with os.scandir + sort.
  - agents/claude enumeration uses ``find agents/claude -maxdepth 1 -name '*.md'
    ! -name 'README.md' | sort``.  Ported identically.
  - skills/claude enumeration iterates ``skills/claude/*/`` (glob pattern).
    Ported with sorted(os.scandir()).
  - output-styles/claude iterates ``output-styles/claude/*`` files only.
  - The {{PYTHON}} substitution in start-engram-daemon.sh is replicated exactly:
    sed 's|{{PYTHON}}|$HOME/.engram/venv/bin/python3|g'
    Note: the literal '$HOME' is preserved in the file (not expanded at build
    time), matching the legacy script comment and behavior.
  - hooks.json is filtered BEFORE hook scripts are copied (same ordering as
    build-plugin.sh step 3).
  - The hooks.json filter uses PLUGIN_ROOT variable to write the filtered result
    — same output path and indent/newline-trailing behavior as the embedded
    Python in build-plugin.sh.
  - The consistency gate (step 6b) is all-or-nothing: every reference in the
    built hooks.json must exist on disk.
  - plugin.json is copied from repo_root/plugin.json.
  - .mcp.json is written from src/build/packaging/mcp.json (NOT repo-root .mcp.json) and
    renamed to .mcp.json at the plugin root.
  - Build manifest paths list is ordered by the copy sequence (not alphabetical),
    matching COPIED_PATHS accumulation order in the bash script.

FLAG: The bash script's COPIED_PATHS list records "packaging/mcp.json" as the
path for the .mcp.json source — this is an artifact of how the bash script
tracks source paths, not destination paths.  We replicate this in
shipped_paths of the build manifest.

Phase 3 additions
-----------------
  - Platform profiles (src/build/packaging/platforms/): loaded at build time and baked
    into the bundle root as ``platform.json`` (read-only runtime fact).
  - --target claude-code|codex (default: claude-code = today's behavior).
    For codex: emits .codex-plugin/plugin.json, hooks.json references
    the profile's plugin_root_env (CLAUDE_PLUGIN_ROOT — Codex injects this
    variable natively), and the MCP config is emitted as a plain JSON object.
  - --identity self|foreign (default: self = today's behavior).
    foreign drops identity_coupled entries from tiers.json regardless of tier,
    then runs a leak-scan VALIDATE step over the emitted tree.
  - --engram-home <path>: parameterizes ENGRAM_HOME pinning in hook commands
    for foreign bundles (substituted during hooks.json generation).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from .manifest import select_shippables, load_manifest, resolve_tier


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

HOOK_REF_RE = re.compile(r'\bhooks/([A-Za-z0-9_\-]+\.(?:py|sh))\b')

PYTHON_SUBSTITUTION_TARGET = "{{PYTHON}}"
PYTHON_SUBSTITUTION_VALUE = "$HOME/.engram/venv/bin/python3"

# Patterns that must not appear in a foreign bundle.
# These catch PERSONAL IDENTITY strings — the builder's specific home path and
# agent identifier.  They do NOT include 'agents-shared' because that string
# appears in hook source files as a configurable environment-variable default
# (os.environ.get("INTER_AGENT_DIR", "/home/agents-shared/...")) which is a
# legitimate deployment configuration default, not a personal identity leak.
#
# IMPORTANT: these patterns are built via concatenation rather than written as
# plain string literals.  If the literals appeared here verbatim, this source
# file would match its own patterns when it is shipped at the convenience tier
# (tools/engine is a shipped tools/ entry), causing the foreign leak-scan to
# flag three self-inflicted hits.  Concatenation produces identical regex
# semantics while keeping this file clean.
_AB = "agent-" + "borges"  # personal agent identifier — do NOT expand inline
FOREIGN_LEAK_PATTERNS: list[re.Pattern[str]] = [
    re.compile("/home/" + _AB),
    re.compile(r"\b" + _AB + r"\b"),
]

SCAN_EXTENSIONS = {".py", ".md", ".sh", ".json", ".template", ".service"}
SKIP_DIRS = {".git", "__pycache__", "node_modules"}
# scan-leaks.py self-matches on its own pattern literals; skip it in the scanner
SKIP_FILES = {"scan-leaks.py"}


# ---------------------------------------------------------------------------
# build-time version stamping
# ---------------------------------------------------------------------------


def compute_build_version(repo_root: str) -> str:
    """Compute a unique per-rebuild plugin version string.

    Format: ``0.1.0-dev.<YYYYMMDDHHMMss>.<sha7>``

    The prerelease form (``-dev.``) is used rather than semver build metadata
    (``+``) because the Claude Code plugin manager's comparison semantics for
    build metadata are not exposed in this repo.  The prerelease form orders
    correctly under semver and is universally accepted by JSON schema validators.

    The timestamp leads the prerelease identifier chain (rather than the sha)
    because semver prerelease identifiers compare left-to-right.  With the sha
    first, a rebuild at a lexically-lower sha could compare as a downgrade under
    precedence-based comparators even though the timestamp is strictly later.
    Timestamp-first is strictly monotonic under precedence comparators AND still
    unique under difference comparators — robust under unknown plugin-manager
    semantics.

    Within a single build invocation this function should be called ONCE and the
    result threaded through to every place that emits a version string — callers
    must not call it multiple times, as the timestamp component moves between calls.

    The sha7 reflects committed HEAD, not working-tree state; uncommitted changes
    do not alter the sha component — the timestamp provides per-rebuild uniqueness.

    Parameters
    ----------
    repo_root:
        Absolute path to the engram-alpha repo root.  Used for ``git rev-parse``.

    Returns
    -------
    str
        Version string, e.g. ``"0.1.0-dev.20260606153042.1a4c9b4"``.
        Falls back to ``"0.1.0-dev.<stamp>.unknown"`` if git is unavailable.
    """
    # Short SHA of HEAD at the source tree
    try:
        sha7 = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        sha7 = "unknown"

    # Semver §9: numeric identifiers MUST NOT include leading zeroes.
    # A pure-numeric sha7 (e.g. "0149257") would violate this; prefix with "g"
    # (git-describe convention) to make it alphanumeric, which has no such
    # restriction.
    if sha7.isdigit():
        sha7 = "g" + sha7

    # UTC timestamp — second-level granularity so consecutive dirty-tree
    # rebuilds at the same SHA produce distinct version strings
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")

    # Timestamp leads the prerelease chain: semver identifiers compare
    # left-to-right, so timestamp-first is strictly monotonic under
    # precedence-based comparators regardless of sha lexical ordering.
    # The sha remains as the second identifier for per-commit uniqueness.
    # (The 14-digit timestamp begins with '2', so no leading-zero issue there;
    # the isdigit guard above still applies to sha7, now the second identifier.)
    return f"0.1.0-dev.{stamp}.{sha7}"


# ---------------------------------------------------------------------------
# platform profile helpers
# ---------------------------------------------------------------------------


def load_platform_profile(repo_root: str, target: str) -> dict[str, Any]:
    """Load and return a platform profile from packaging/platforms/<target>.json.

    Parameters
    ----------
    repo_root:
        Absolute path to the engram-alpha repo root.
    target:
        Platform target name (e.g. "claude-code", "codex").

    Returns
    -------
    dict
        The parsed platform profile.

    Raises
    ------
    FileNotFoundError
        If the profile file does not exist.
    ValueError
        If the profile is missing a required "platform" field.
    """
    profile_path = os.path.join(repo_root, "src", "build", "packaging", "platforms", f"{target}.json")
    if not os.path.isfile(profile_path):
        raise FileNotFoundError(
            f"Platform profile not found: {profile_path} "
            f"(target={target!r})"
        )
    with open(profile_path, encoding="utf-8") as f:
        profile = json.load(f)
    if "platform" not in profile:
        raise ValueError(
            f"Platform profile {profile_path} is missing required 'platform' field"
        )
    return profile


def bake_platform_json(plugin_root: str, profile: dict[str, Any]) -> None:
    """Write the platform profile as platform.json at the bundle root.

    Parameters
    ----------
    plugin_root:
        Absolute path to the output plugin directory.
    profile:
        The platform profile dict (from load_platform_profile).
    """
    dest = os.path.join(plugin_root, "platform.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# identity-axis helpers
# ---------------------------------------------------------------------------


def select_shippables_with_identity(
    manifest: dict[str, Any],
    tier: str,
    multi_agent: bool,
    identity: str,
) -> list[dict[str, Any]]:
    """Return shippable entries, filtering identity_coupled when identity=foreign.

    Extends select_shippables with the identity axis:
      - identity=self (default): no additional filtering (today's behavior).
      - identity=foreign: drops any entry with identity_coupled=true, regardless
        of tier or multi_agent.

    Parameters
    ----------
    manifest:
        Parsed tiers.json dict.
    tier, multi_agent:
        Passed through to select_shippables.
    identity:
        "self" or "foreign".

    Returns
    -------
    list[dict]
        Filtered shippable entries.
    """
    entries = select_shippables(manifest, tier, multi_agent)
    if identity == "foreign":
        entries = [e for e in entries if not e.get("identity_coupled")]
    return entries


# ---------------------------------------------------------------------------
# codex-specific emission helpers
# ---------------------------------------------------------------------------


def emit_codex_plugin_json(
    plugin_root: str,
    repo_root: str,
    profile: dict[str, Any],
    build_version: str | None = None,
    plugin_data: dict | None = None,
) -> None:
    """Emit .codex-plugin/plugin.json at the bundle root.

    Mirrors the shape from the verified test bundle:
      { name, version, description }
    Derives name/description from the repo's plugin.json; uses build_version
    for the version field (stamped at build time) when provided.

    Parameters
    ----------
    plugin_root:
        Absolute path to the output plugin directory.
    repo_root:
        Absolute path to the repo root (source of plugin.json).
    profile:
        The loaded codex platform profile (unused here beyond validation).
    build_version:
        Pre-computed build version string (from compute_build_version).
        When provided, this overrides the version in the source plugin.json.
        When None, falls back to the source plugin.json version.
    plugin_data:
        Pre-loaded plugin.json dict (already resolved via source_root in the
        caller). When provided, the file read is skipped — avoids a redundant
        open and ensures source_root is honoured correctly.
    """
    if plugin_data is not None:
        src_data = plugin_data
    else:
        src_plugin = os.path.join(repo_root, "plugin.json")
        with open(src_plugin, encoding="utf-8") as f:
            src_data = json.load(f)

    codex_plugin_dir = os.path.join(plugin_root, ".codex-plugin")
    os.makedirs(codex_plugin_dir, exist_ok=True)

    version = build_version if build_version is not None else src_data.get("version", "0.1.0")

    codex_manifest = {
        "name": src_data.get("name", "engram"),
        "version": version,
        "description": src_data.get("description", ""),
        "paths": {
            "skills": "skills/",
            "mcp_servers": ".mcp.json",
            "hooks": "hooks/hooks.json",
        },
    }
    dest = os.path.join(codex_plugin_dir, "plugin.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(codex_manifest, f, indent=2)
        f.write("\n")


def emit_codex_mcp_json(
    plugin_root: str,
    repo_root: str,
    profile: dict[str, Any],
    engram_home: str | None,
) -> None:
    """Emit .mcp.json for codex.

    Codex natively injects CLAUDE_PLUGIN_ROOT and CLAUDE_PLUGIN_DATA into hook
    subprocess environments — it keeps the Claude-compatible variable names for
    plugin-ecosystem compatibility (same hooks.json schema, same stdin-JSON
    protocol, same event names as Claude Code).  Hook commands therefore use
    ${CLAUDE_PLUGIN_ROOT} and resolve correctly without any renaming.

    NOTE: whether Codex also expands ${CLAUDE_PLUGIN_ROOT} inside .mcp.json
    *command* strings at load time is empirically UNVERIFIED — the working E2E
    test bundle hardcoded an absolute MCP path.  MCP absolute-path handling is a
    deliberate follow-up, not fixed here.

    The ENGRAM_HOME env is set in the mcp server env dict if engram_home is
    provided.

    Parameters
    ----------
    plugin_root:
        Absolute path to the output plugin directory.
    repo_root:
        Absolute path to the repo root.
    profile:
        The loaded codex platform profile.
    engram_home:
        Optional ENGRAM_HOME path to pin in the MCP config env.
    """
    root_env = profile.get("plugin_root_env", "CLAUDE_PLUGIN_ROOT")
    server_entry: dict[str, Any] = {
        "command": f"${{{root_env}}}/launch-engram-server.sh",
        "args": [],
    }
    if engram_home:
        server_entry["env"] = {"ENGRAM_HOME": engram_home}

    mcp_config = {"mcpServers": {"engram": server_entry}}

    dest = os.path.join(plugin_root, ".mcp.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(mcp_config, f, indent=2)
        f.write("\n")


def rewrite_hooks_for_codex(
    hooks_cfg: dict[str, Any],
    profile: dict[str, Any],
    engram_home: str | None,
) -> dict[str, Any]:
    """Rewrite hook commands to use the codex profile's plugin_root_env.

    Replaces ${CLAUDE_PLUGIN_ROOT} with ${<plugin_root_env>} in all hook
    commands, and prepends ENGRAM_HOME=<path> if engram_home is given.

    With codex.json correctly setting plugin_root_env = "CLAUDE_PLUGIN_ROOT",
    this function becomes a no-op for the variable substitution (replacing
    ${CLAUDE_PLUGIN_ROOT} with ${CLAUDE_PLUGIN_ROOT}), which is the correct
    behavior: Codex injects CLAUDE_PLUGIN_ROOT into hook subprocess envs.

    Parameters
    ----------
    hooks_cfg:
        Filtered hooks.json dict (already tier/identity filtered).
    profile:
        The loaded codex platform profile.
    engram_home:
        Optional ENGRAM_HOME path to prepend to hook commands.

    Returns
    -------
    dict
        Rewritten hooks config.
    """
    root_env = profile.get("plugin_root_env", "CLAUDE_PLUGIN_ROOT")
    old_root = "${CLAUDE_PLUGIN_ROOT}"
    new_root = f"${{{root_env}}}"

    def _rewrite_cmd(cmd: str) -> str:
        # Replace plugin root env reference
        cmd = cmd.replace(old_root, new_root)
        # Prepend ENGRAM_HOME if requested and not already present
        if engram_home and "ENGRAM_HOME=" not in cmd:
            cmd = f"ENGRAM_HOME={engram_home} {cmd}"
        return cmd

    import copy
    result = copy.deepcopy(hooks_cfg)
    for event_name, groups in result.get("hooks", {}).items():
        for group in groups:
            for entry in group.get("hooks", []):
                if "command" in entry:
                    entry["command"] = _rewrite_cmd(entry["command"])
    return result




# ---------------------------------------------------------------------------
# Codex custom-agent transform helpers
# ---------------------------------------------------------------------------


def _parse_agent_frontmatter(src_path: str) -> tuple[dict[str, Any], str]:
    """Parse the small YAML-frontmatter subset used by agents/claude/*.md.

    The build engine intentionally stays stdlib-only, so this is not a general
    YAML parser. It handles the scalar and one-line list shapes present in the
    shipped fairy specs: ``key: value``, ``tools: *``, and
    ``tools: [Read, Write]``.
    """
    with open(src_path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---\n"):
        raise ValueError(f"Agent spec missing YAML frontmatter: {src_path}")

    try:
        _blank, frontmatter, body = text.split("---\n", 2)
    except ValueError as exc:
        raise ValueError(f"Agent spec has unterminated frontmatter: {src_path}") from exc

    data: dict[str, Any] = {}
    for raw_line in frontmatter.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Unsupported frontmatter line in {src_path}: {raw_line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [item.strip() for item in inner.split(",") if item.strip()]
        elif value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        else:
            data[key] = value
    return data, body.lstrip("\n")


def _agent_tools_to_list(raw_tools: Any) -> list[str] | str:
    if raw_tools == "*":
        return "*"
    if isinstance(raw_tools, list):
        return [str(t).strip() for t in raw_tools if str(t).strip()]
    if isinstance(raw_tools, str):
        return [t.strip() for t in raw_tools.split(",") if t.strip()]
    return []


def _codex_model_config(profile: dict[str, Any], claude_tier: str | None) -> dict[str, str]:
    """Map Claude-style model tiers (opus/sonnet/haiku) through codex.json."""
    tiers = profile.get("agent_model_tiers", {})
    default_tier = profile.get("default_agent_model_tier", "sonnet")
    tier = claude_tier or default_tier
    cfg = tiers.get(tier) or tiers.get(default_tier) or {}
    return {k: str(v) for k, v in cfg.items() if v is not None}


def _toml_scalar(value: Any) -> str:
    """Return a TOML-safe scalar using JSON string escaping for strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    """Return a TOML-safe string array."""
    return "[" + ", ".join(_toml_scalar(value) for value in values) + "]"


def _engram_mcp_tool_names(tools: list[str] | str) -> list[str]:
    """Extract raw ENGRAM MCP tool names from Claude-style tool identifiers.

    Claude agent specs name MCP tools as ``mcp__engram__engram_query``.
    Codex MCP policy allowlists use the server-local tool name
    (``engram_query``) under the installed plugin MCP policy table.
    """
    if tools == "*":
        return []
    prefix = "mcp__engram__"
    return [
        tool[len(prefix) :]
        for tool in tools
        if isinstance(tool, str) and tool.startswith(prefix)
    ]


def _emit_codex_agent_toml(
    src_path: str,
    dest_path: str,
    manifest_key: str,
    profile: dict[str, Any],
) -> None:
    """Transform a Claude Markdown fairy spec into Codex custom-agent TOML."""
    meta, body = _parse_agent_frontmatter(src_path)

    name = meta.get("name")
    description = meta.get("description")
    if not name or not description:
        raise ValueError(f"Agent spec {src_path} missing required name/description")

    tools = _agent_tools_to_list(meta.get("tools", []))
    model_cfg = _codex_model_config(profile, meta.get("model"))

    notes: list[str] = [
        "# Codex transform metadata",
        f"- Source Claude spec: `{manifest_key}`.",
    ]
    if "isolation" in meta:
        notes.append(
            f"- Claude isolation: `{meta['isolation']}`. Preserve this operationally; "
            "keep the task scoped and isolated from unrelated work."
        )
    if meta.get("default_background") is True:
        notes.append(
            "- Claude default_background: true. You may work without chatty progress "
            "updates; return a concise structured handoff when done."
        )
    if tools == "*":
        notes.append("- Claude tools whitelist: `*` (full tool access in the source spec).")
    elif tools:
        notes.append("- Claude tools whitelist: " + ", ".join(f"`{tool}`" for tool in tools) + ".")
    notes.append(
        "- Codex custom-agent TOML cannot express every Claude frontmatter field; "
        "the non-equivalent fields above are preserved here as instructions."
    )

    developer_instructions = "\n".join(notes) + "\n\n" + body

    lines: list[str] = [
        f"name = {_toml_scalar(name)}",
        f"description = {_toml_scalar(description)}",
        f"developer_instructions = {_toml_scalar(developer_instructions)}",
    ]
    for key in ("model", "model_reasoning_effort"):
        if key in model_cfg:
            lines.append(f"{key} = {_toml_scalar(model_cfg[key])}")

    # Codex custom-agent config inherits optional config fields when omitted.
    # For fairies whose Claude whitelist has no ENGRAM MCP entry, explicitly
    # clear MCP servers so a parent session's ENGRAM write tools do not leak in.
    # For fairies with explicit ENGRAM MCP entries, preserve the whitelist as
    # a Codex MCP allowlist instead of inheriting the whole server.  This keeps
    # read-only dream/summary fairies read-only while still allowing the
    # dream-master's ``tools: *`` spec to inherit full read/write access.
    engram_tools = _engram_mcp_tool_names(tools)
    if tools != "*" and not engram_tools:
        lines.append("mcp_servers = {}")
    elif engram_tools:
        # ENGRAM is provided by the installed plugin, not by a top-level
        # user-defined MCP server.  In Codex config, plugin-bundled MCP tool
        # policy lives under [plugins."<plugin>@<marketplace>".mcp_servers.*].
        # Emitting a partial [mcp_servers.engram] table with only enabled_tools
        # is malformed because top-level MCP server tables require a transport
        # (command or url).
        plugin_policy_id = profile.get("plugin_mcp_policy_id", "engram@engram-local")
        lines.extend(
            [
                "",
                f"[plugins.{_toml_scalar(plugin_policy_id)}.mcp_servers.engram]",
                f"enabled_tools = {_toml_array(engram_tools)}",
            ]
        )

    with open(dest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")


# ---------------------------------------------------------------------------
# leak scanner (inline, for foreign bundle VALIDATE step)
# ---------------------------------------------------------------------------


def scan_for_leaks(
    bundle_root: str,
    patterns: list[re.Pattern[str]] | None = None,
) -> list[tuple[str, int, str]]:
    """Scan a bundle tree for identity leak patterns.

    Parameters
    ----------
    bundle_root:
        Absolute path to the bundle root to scan.
    patterns:
        List of compiled regex patterns to search for.
        Defaults to FOREIGN_LEAK_PATTERNS.

    Returns
    -------
    list of (rel_path, lineno, snippet) tuples
        Empty list means clean.
    """
    if patterns is None:
        patterns = FOREIGN_LEAK_PATTERNS

    hits: list[tuple[str, int, str]] = []
    for dirpath, dirs, files in os.walk(bundle_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if fname in SKIP_FILES:
                continue
            ext = os.path.splitext(fname)[1]
            if ext not in SCAN_EXTENSIONS:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            rel = os.path.relpath(fpath, bundle_root)
            for i, line in enumerate(content.splitlines(), 1):
                for pat in patterns:
                    if pat.search(line):
                        hits.append((rel, i, line.strip()[:120]))
                        break  # one hit per line is enough
    return hits


# ---------------------------------------------------------------------------
# log helpers (print-based, matching build-plugin.sh log() format)
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Print a [build-plugin] prefixed message, matching build-plugin.sh log()."""
    print(f"[build-plugin] {msg}")


def _die(msg: str) -> None:
    """Print an error and raise RuntimeError, matching build-plugin.sh die()."""
    import sys
    print(f"[build-plugin] ERROR: {msg}", file=sys.stderr)
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# shipping-set helpers
# ---------------------------------------------------------------------------


def _build_ship_set(shipped_entries: list[dict[str, Any]]) -> set[str]:
    """Return a set of paths for O(1) membership testing."""
    return {e["path"] for e in shipped_entries}


def _ships(ship_set: set[str], path: str) -> bool:
    return path in ship_set


# ---------------------------------------------------------------------------
# filter_hooks_json — step 3a (pure transform, testable independently)
# ---------------------------------------------------------------------------


def filter_hooks_json(
    hooks_cfg: dict[str, Any],
    shipped_paths: set[str],
) -> dict[str, Any]:
    """Filter a hooks.json config dict to include only entries whose hook file ships.

    Mirrors the embedded Python in build-plugin.sh step 3.

    Algorithm:
      1. For each hook entry, extract hooks/<file> from the command string.
      2. Drop the entry if <file> is NOT in the shipped hook basenames.
         Entries with no hooks/<file> reference are always kept.
      3. Prune event-arrays / group-arrays that became empty.

    Parameters
    ----------
    hooks_cfg:
        Parsed hooks/hooks.json dict.
    shipped_paths:
        Set of manifest paths that ship (e.g. "hooks/claude/engram-surface-hook.py").
        Used to derive the shipped hook basenames.

    Returns
    -------
    dict
        Filtered hooks config with the same structure as hooks_cfg, minus
        dropped entries and empty groups/events.
    """
    # Build set of shipped hook basenames from shipped paths.
    shipped_hook_names: set[str] = set()
    for p in shipped_paths:
        if p.startswith("hooks/claude/"):
            shipped_hook_names.add(os.path.basename(p))

    def hook_ships(entry: dict[str, Any]) -> bool:
        """True iff this hook entry should be kept at the current build config."""
        cmd = entry.get("command", "")
        m = HOOK_REF_RE.search(cmd)
        if m is None:
            return True  # no hook-file reference → keep unconditionally
        return m.group(1) in shipped_hook_names

    filtered_hooks: dict[str, Any] = {}
    for event_name, groups in hooks_cfg.get("hooks", {}).items():
        filtered_groups = []
        for group in groups:
            raw_entries = group.get("hooks", [])
            kept_entries = [e for e in raw_entries if hook_ships(e)]
            if not kept_entries:
                # All entries in this group were dropped → prune the group.
                continue
            filtered_group = {k: v for k, v in group.items() if k != "hooks"}
            filtered_group["hooks"] = kept_entries
            filtered_groups.append(filtered_group)
        if not filtered_groups:
            # All groups for this event were pruned → omit the event key.
            continue
        filtered_hooks[event_name] = filtered_groups

    return {"hooks": filtered_hooks}


def _count_filter_stats(
    original: dict[str, Any],
    filtered: dict[str, Any],
    shipped_paths: set[str],
) -> tuple[int, int]:
    """Return (kept, dropped) entry counts for logging."""
    total = sum(
        len(group.get("hooks", []))
        for groups in original.get("hooks", {}).values()
        for group in groups
    )
    kept = sum(
        len(group.get("hooks", []))
        for groups in filtered.get("hooks", {}).values()
        for group in groups
    )
    dropped = total - kept
    return kept, dropped


# ---------------------------------------------------------------------------
# post-build consistency check — step 6b
# ---------------------------------------------------------------------------


def check_hooks_consistency(plugin_root: str) -> list[str]:
    """Check hooks.json ↔ shipped-files consistency (all-or-nothing).

    Every file referenced in the BUILT hooks/hooks.json must exist on disk in
    plugin_root/hooks/.

    Parameters
    ----------
    plugin_root:
        Path to the assembled plugin root directory.

    Returns
    -------
    list[str]
        Sorted list of hook filenames that are referenced but missing.
        Empty list means the check passed.
    """
    hooks_json_path = os.path.join(plugin_root, "hooks", "hooks.json")
    with open(hooks_json_path, encoding="utf-8") as f:
        data = json.load(f)

    referenced: set[str] = set()
    for event_name, groups in data.get("hooks", {}).items():
        for group in groups:
            for entry in group.get("hooks", []):
                cmd = entry.get("command", "")
                for m in HOOK_REF_RE.finditer(cmd):
                    referenced.add(m.group(1))

    missing: list[str] = []
    for fname in sorted(referenced):
        fpath = os.path.join(plugin_root, "hooks", fname)
        if not os.path.exists(fpath):
            missing.append(fname)

    return missing


# ---------------------------------------------------------------------------
# file-copy plan + execution — the main build orchestrator
# ---------------------------------------------------------------------------


def build_plugin(
    repo_root: str,
    plugin_root: str,
    tier: str,
    multi_agent: bool,
    manifest: dict[str, Any] | None = None,
    target: str = "claude-code",
    identity: str = "self",
    engram_home: str | None = None,
) -> list[str]:
    """Assemble the ENGRAM plugin tree.

    Mirrors build-plugin.sh sections 1–7 exactly for the claude-code target.
    Phase 3 additions:
      - target: "claude-code" (default, today's behavior) or "codex"
      - identity: "self" (default) or "foreign" (drops identity_coupled entries)
      - engram_home: optional ENGRAM_HOME path for foreign/codex bundles

    Parameters
    ----------
    repo_root:
        Absolute path to the engram-alpha repo root.
    plugin_root:
        Absolute path to the output plugin directory.
    tier:
        Depth tier (e.g. "convenience").
    multi_agent:
        Whether to include multi-agent-gated mechanisms.
    manifest:
        Pre-loaded manifest dict (optional; loaded from repo_root if None).
    target:
        Platform target: "claude-code" or "codex".
    identity:
        Identity scope: "self" (today) or "foreign" (drops identity_coupled).
    engram_home:
        Optional ENGRAM_HOME path to embed in hook commands and MCP config.

    Returns
    -------
    list[str]
        The COPIED_PATHS list in accumulation order, matching the bash script's
        COPIED_PATHS array.
    """
    # Load manifest if not supplied
    if manifest is None:
        manifest_path = os.path.join(repo_root, "src", "build", "packaging", "tiers.json")
        manifest = load_manifest(manifest_path)

    # source_root — relative to repo_root; default "." preserves current behavior.
    # Individual mechanism entries may carry a "source" field (repo-root-relative path)
    # to override source_root for that entry — used when a mechanism's source lives
    # outside the source_root tree (e.g. src/build/engine ships as bundle tools/engine).
    source_root = manifest.get("source_root", ".")

    # Compute build version once — threaded through to all version-bearing outputs
    # so all emitted files carry the same stamp within a single build invocation.
    build_version = compute_build_version(repo_root)

    # Load platform profile
    profile = load_platform_profile(repo_root, target)

    chosen_rank_str = tier
    tier_rank_val = {"essential": 0, "convenience": 1, "dev": 2}.get(tier, -1)
    ma_str = "yes" if multi_agent else "no"

    _log(
        f"Tier: {tier} (rank={tier_rank_val}), "
        f"multi-agent: {ma_str}, "
        f"target: {target}, "
        f"identity: {identity}"
    )

    # Build shipping set (with identity axis filtering)
    shipped_entries = select_shippables_with_identity(manifest, tier, multi_agent, identity)
    ship_set = _build_ship_set(shipped_entries)

    # ── Clean and create plugin root ─────────────────────────────────────────

    _log(f"Cleaning {plugin_root}")
    if os.path.exists(plugin_root):
        shutil.rmtree(plugin_root)
    os.makedirs(plugin_root, exist_ok=True)

    copied_paths: list[str] = []

    # ── 1. Root-level files — manifest-derived ───────────────────────────────
    # Derive from shipped_entries (path contains no '/') so new root files
    # added to tiers.json ship automatically — no hardcoded list to maintain.
    # Previously this section hardcoded ["server.py", "bootstrap.py", "SKILL.md",
    # "launch-engram-server.sh"] + an engram_*.py glob; a convenience-tier root
    # file (viz_server.py) was silently omitted.  Fixes #666.

    _log("Copying root-level files → plugin root (manifest-derived)")
    root_entries = [e for e in shipped_entries if "/" not in e["path"]]
    if not any(e["path"] == "server.py" for e in root_entries):
        _die("server.py not in shipped root entries — manifest may be misconfigured")
    for entry in root_entries:
        p = entry["path"]
        src = os.path.join(repo_root, source_root, p)
        if not os.path.exists(src):
            _die(f"Manifest root entry {p!r} does not exist at {src}")
        dst = os.path.join(plugin_root, p)
        shutil.copy2(src, dst)
        copied_paths.append(p)
        _log(f"  {p}")

    # Ensure launch-engram-server.sh is executable after copy
    launch_sh = os.path.join(plugin_root, "launch-engram-server.sh")
    if os.path.isfile(launch_sh):
        os.chmod(launch_sh, os.stat(launch_sh).st_mode | 0o111)

    # ── 1b. tools/ — manifest-driven filtering ───────────────────────────────

    _log("Copying tools/ → plugin tools/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "tools"), exist_ok=True)

    # Iterate SHIPPED_PATHS that match '^tools/'
    for entry in [e for e in shipped_entries if e["path"].startswith("tools/")]:
        p = entry["path"]
        # Per-mechanism "source" field: repo-root-relative override for entries
        # whose source lives outside source_root (e.g. src/build/engine → bundle tools/engine).
        if "source" in entry:
            src = os.path.join(repo_root, entry["source"])
        else:
            src = os.path.join(repo_root, source_root, p)
        if not os.path.exists(src):
            _die(f"Manifest entry {p!r} does not exist at {src}")
        dst = os.path.join(plugin_root, p)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            # cp -r src/. dest/ — copy contents into dest
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
        copied_paths.append(p)
        _log(f"  {p}")

    # ── 1c. templates/ — manifest-filtered ───────────────────────────────────

    _log("Copying templates → plugin templates/")
    os.makedirs(os.path.join(plugin_root, "templates"), exist_ok=True)

    for p in [e["path"] for e in shipped_entries if e["path"].startswith("templates/")]:
        src = os.path.join(repo_root, source_root, p)
        if not os.path.exists(src):
            _die(f"Manifest entry {p} does not exist at {src}")
        dst = os.path.join(plugin_root, p)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            # cp -a src/. dest/ — copy contents (archive mode in bash; copytree here)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
        copied_paths.append(p)
        _log(f"  {p}")

    # ── 2. Manifests ─────────────────────────────────────────────────────────

    _log(f"Writing plugin.json (version={build_version!r})")
    src_plugin_path = os.path.join(repo_root, "plugin.json")
    with open(src_plugin_path, encoding="utf-8") as f:
        plugin_data = json.load(f)
    plugin_data["version"] = build_version
    with open(os.path.join(plugin_root, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump(plugin_data, f, indent=2)
        f.write("\n")
    copied_paths.append("plugin.json")

    if target == "codex":
        # Codex: emit .codex-plugin/plugin.json instead of a raw .mcp.json copy
        _log("  (codex) emitting .codex-plugin/plugin.json")
        emit_codex_plugin_json(plugin_root, repo_root, profile, build_version=build_version, plugin_data=plugin_data)
        _log("  (codex) emitting .mcp.json (no ${CLAUDE_PLUGIN_ROOT})")
        emit_codex_mcp_json(plugin_root, repo_root, profile, engram_home)
        copied_paths.append(".codex-plugin/plugin.json")
        copied_paths.append(".mcp.json")
    else:
        # claude-code: Source: src/build/packaging/mcp.json → destination: .mcp.json at plugin root
        shutil.copy2(
            os.path.join(repo_root, "src", "build", "packaging", "mcp.json"),
            os.path.join(plugin_root, ".mcp.json"),
        )
        copied_paths.append("packaging/mcp.json")

    # Bake platform.json into bundle root
    _log(f"  Baking platform.json (platform={target!r})")
    bake_platform_json(plugin_root, profile)
    copied_paths.append("platform.json")

    # ── 3. Hooks — manifest-filtered ─────────────────────────────────────────

    _log("Copying hooks → plugin hooks/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "hooks"), exist_ok=True)

    _log(
        f"Filtering hooks.json for tier={tier} "
        f"multi-agent={'yes' if multi_agent else 'no'}"
    )

    # Read source hooks.json
    src_hooks_path = os.path.join(repo_root, source_root, "hooks", "hooks.json")
    with open(src_hooks_path, encoding="utf-8") as f:
        hooks_data = json.load(f)

    # Filter (tier + identity axis)
    filtered_hooks = filter_hooks_json(hooks_data, ship_set)

    # For codex target: rewrite commands to use profile's plugin_root_env
    if target == "codex":
        _log(
            f"  (codex) rewriting hook commands: "
            f"${{CLAUDE_PLUGIN_ROOT}} → ${{{profile.get('plugin_root_env', 'CLAUDE_PLUGIN_ROOT')}}}"
        )
        filtered_hooks = rewrite_hooks_for_codex(filtered_hooks, profile, engram_home)

    # Count stats for logging (mirrors bash script output)
    kept, dropped = _count_filter_stats(hooks_data, filtered_hooks, ship_set)

    # Write filtered hooks.json
    dest_hooks_json = os.path.join(plugin_root, "hooks", "hooks.json")
    with open(dest_hooks_json, "w", encoding="utf-8") as f:
        json.dump(filtered_hooks, f, indent=2)
        f.write("\n")

    print(
        f"[build-plugin] hooks.json filtered: {kept} entries kept, "
        f"{dropped} dropped (tier/MA excluded)"
    )

    copied_paths.append("hooks/hooks.json")

    # Copy hook scripts filtered through the manifest predicate
    hooks_dir = os.path.join(repo_root, source_root, "hooks", "claude")
    # Enumerate: find hooks/claude -maxdepth 1 \( -name '*.py' -o -name '*.sh' \) | sort
    hook_sources = sorted(
        os.path.join(hooks_dir, fname)
        for fname in os.listdir(hooks_dir)
        if (fname.endswith(".py") or fname.endswith(".sh"))
        and os.path.isfile(os.path.join(hooks_dir, fname))
    )
    for src in hook_sources:
        fname = os.path.basename(src)
        manifest_key = f"hooks/claude/{fname}"
        if _ships(ship_set, manifest_key):
            shutil.copy2(src, os.path.join(plugin_root, "hooks", fname))
            copied_paths.append(manifest_key)
            _log(f"  {manifest_key}")
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # Substitute {{PYTHON}} → $HOME/.engram/venv/bin/python3 in start-engram-daemon.sh
    daemon_sh = os.path.join(plugin_root, "hooks", "start-engram-daemon.sh")
    if os.path.isfile(daemon_sh):
        with open(daemon_sh, encoding="utf-8") as f:
            content = f.read()
        new_content = content.replace(PYTHON_SUBSTITUTION_TARGET, PYTHON_SUBSTITUTION_VALUE)
        with open(daemon_sh, "w", encoding="utf-8") as f:
            f.write(new_content)
        _log(
            f"  Substituted {{{{PYTHON}}}} → "
            f"{PYTHON_SUBSTITUTION_VALUE} in start-engram-daemon.sh"
        )

    # ── 4. Skills — manifest-filtered ────────────────────────────────────────

    _log("Copying skills → plugin skills/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "skills"), exist_ok=True)

    skills_dir = os.path.join(repo_root, source_root, "skills", "claude")
    if not os.path.isdir(skills_dir):
        _die(f"skills/claude/ not found at {skills_dir}")

    # Enumerate: for skill_dir in skills/claude/*/
    skill_dirs = sorted(
        entry.name
        for entry in os.scandir(skills_dir)
        if entry.is_dir()
    )
    for skill_name in skill_dirs:
        manifest_key = f"skills/claude/{skill_name}"
        if _ships(ship_set, manifest_key):
            src = os.path.join(skills_dir, skill_name)
            dst = os.path.join(plugin_root, "skills", skill_name)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            copied_paths.append(manifest_key)
            _log(f"  {manifest_key}")
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # ── 5. Agents — manifest-filtered ────────────────────────────────────────

    _log("Copying agents → plugin agents/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "agents"), exist_ok=True)

    agents_dir = os.path.join(repo_root, source_root, "agents", "claude")
    if not os.path.isdir(agents_dir):
        _die(f"agents/claude/ not found at {agents_dir}")

    # Enumerate: find agents/claude -maxdepth 1 -name '*.md' ! -name 'README.md' | sort
    agent_sources = sorted(
        os.path.join(agents_dir, fname)
        for fname in os.listdir(agents_dir)
        if fname.endswith(".md") and fname != "README.md"
        and os.path.isfile(os.path.join(agents_dir, fname))
    )
    for src in agent_sources:
        fname = os.path.basename(src)
        manifest_key = f"agents/claude/{fname}"
        if _ships(ship_set, manifest_key):
            if target == "codex":
                dest_name = os.path.splitext(fname)[0] + ".toml"
                _emit_codex_agent_toml(
                    src,
                    os.path.join(plugin_root, "agents", dest_name),
                    manifest_key,
                    profile,
                )
                _log(f"  {manifest_key} -> agents/{dest_name}")
            else:
                shutil.copy2(src, os.path.join(plugin_root, "agents", fname))
                _log(f"  {manifest_key}")
            copied_paths.append(manifest_key)
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # ── 6. Output styles — manifest-filtered ─────────────────────────────────

    _log("Copying output-styles → plugin output-styles/ (manifest-filtered)")
    os.makedirs(os.path.join(plugin_root, "output-styles"), exist_ok=True)

    styles_dir = os.path.join(repo_root, source_root, "output-styles", "claude")
    if not os.path.isdir(styles_dir):
        _die(f"output-styles/claude/ not found at {styles_dir}")

    # Enumerate: for src in output-styles/claude/* (files only)
    for fname in sorted(os.listdir(styles_dir)):
        src = os.path.join(styles_dir, fname)
        if not os.path.isfile(src):
            continue
        manifest_key = f"output-styles/claude/{fname}"
        if _ships(ship_set, manifest_key):
            shutil.copy2(src, os.path.join(plugin_root, "output-styles", fname))
            copied_paths.append(manifest_key)
            _log(f"  {manifest_key}")
        else:
            _log(f"  (tier-filtered) {manifest_key}")

    # ── 6b. hooks.json ↔ shipped-files consistency gate ──────────────────────

    _log("Checking hooks.json ↔ shipped-files consistency (all-or-nothing)")
    missing = check_hooks_consistency(plugin_root)
    if missing:
        import sys
        print(
            "[build-plugin] ERROR: built hooks.json references hook file(s) that are "
            "MISSING from the plugin:",
            file=sys.stderr,
        )
        for f in sorted(missing):
            print(f"[build-plugin]   MISSING: hooks/{f}", file=sys.stderr)
        print(
            "[build-plugin] The hooks.json filter step should have dropped these entries.",
            file=sys.stderr,
        )
        print(
            "[build-plugin] Check the filtering logic in build-plugin.sh step 3.",
            file=sys.stderr,
        )
        raise RuntimeError(
            f"hooks.json consistency check failed: missing {missing}"
        )

    n_referenced = len(
        set(
            m.group(1)
            for groups in json.loads(
                open(os.path.join(plugin_root, "hooks", "hooks.json")).read()
            ).get("hooks", {}).values()
            for group in groups
            for entry in group.get("hooks", [])
            for m in HOOK_REF_RE.finditer(entry.get("command", ""))
        )
    )
    _log(
        f"hooks.json ↔ shipped-files: OK ({n_referenced} referenced, all present)"
    )

    # ── 7. Build manifest ─────────────────────────────────────────────────────

    build_manifest_path = os.path.join(plugin_root, ".engram-build-manifest.json")
    _log(f"Writing build manifest → {build_manifest_path}")

    build_manifest: dict[str, Any] = {
        "tier": tier,
        "multi_agent": multi_agent,
        "target": target,
        "identity": identity,
        "shipped_paths": copied_paths,
    }
    with open(build_manifest_path, "w", encoding="utf-8") as f:
        json.dump(build_manifest, f, indent=2)
        f.write("\n")

    # ── 8. Leak-scan VALIDATE step (foreign bundles only) ────────────────────

    if identity == "foreign":
        _log("Running leak-scan VALIDATE over emitted bundle (foreign identity)...")
        leaks = scan_for_leaks(plugin_root)
        if leaks:
            import sys
            print(
                "[build-plugin] ERROR: Leak-scan FAILED — identity strings found in "
                "foreign bundle:",
                file=sys.stderr,
            )
            for rel, lineno, snippet in leaks:
                print(f"[build-plugin]   {rel}:{lineno}  {snippet}", file=sys.stderr)
            raise RuntimeError(
                f"Leak-scan failed: {len(leaks)} hit(s) in foreign bundle"
            )
        _log("  Leak-scan: CLEAN")

    # ── Done ──────────────────────────────────────────────────────────────────

    _log("")
    _log(f"Plugin tree assembled at: {plugin_root}")
    _log(f"  tier={tier}  multi_agent={'true' if multi_agent else 'false'}")
    _log("")
    _log("Contents:")
    # Enumerate tree, skipping __pycache__ and .pyc — mirrors build-plugin.sh
    all_paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(plugin_root):
        # Filter __pycache__ from traversal
        dirnames[:] = [d for d in sorted(dirnames) if d != "__pycache__"]
        for fname in sorted(filenames):
            if fname.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, plugin_root)
            all_paths.append(rel)
    for rel in sorted(all_paths):
        _log(f"  {rel}")

    _log("")
    _log("To verify manifest JSON:")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/plugin.json'))\" && echo OK")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/.mcp.json'))\" && echo OK")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/hooks/hooks.json'))\" && echo OK")
    _log(f"  python3 -c \"import json; json.load(open('{plugin_root}/.engram-build-manifest.json'))\" && echo OK")

    return copied_paths


def write_build_manifest(
    plugin_root: str,
    tier: str,
    multi_agent: bool,
    shipped_paths: list[str],
    target: str = "claude-code",
    identity: str = "self",
) -> None:
    """Write .engram-build-manifest.json to plugin root."""
    manifest: dict[str, Any] = {
        "tier": tier,
        "multi_agent": multi_agent,
        "target": target,
        "identity": identity,
        "shipped_paths": shipped_paths,
    }
    path = os.path.join(plugin_root, ".engram-build-manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
