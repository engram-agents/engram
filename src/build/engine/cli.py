"""tools.engine.cli — command-line entry point for the ENGRAM build engine.

Usage:
    python3 -m tools.engine.cli build [--tier T] [--multi-agent] [--output DIR]
                                      [--target <claude-code|codex>]
                                      [--identity <self|foreign>]
                                      [--engram-home PATH]
    python3 -m tools.engine.cli plan <flow>
    python3 -m tools.engine.cli run <flow> [--ack-changeset] [--allow-branch]
    python3 -m tools.engine.cli doctor

Subcommands:
    build   — assemble the ENGRAM plugin tree (Phase 1–3)
    plan    — print the step DAG + current check() state for a flow
    run     — execute a flow until DONE / PAUSED / FAILED
    doctor  — run verify() on every step and print a health table

Exit codes for `run`:
    0  DONE    — all steps satisfied
    3  PAUSED  — waiting for operator action at a step
    1  FAILED  — a step's verify() returned False

Flag-compatible with tools/build-plugin.sh (build subcommand):
    --tier <essential|convenience|dev>   depth tier (default: manifest's default_tier)
    --multi-agent                         include multi-agent-gated mechanisms
    --output <DIR>                        output directory (default: build/plugin)
    --target <claude-code|codex>          platform target (default: claude-code)
    --identity <self|foreign>             identity scope (default: self)
    --engram-home <PATH>                  ENGRAM_HOME path for foreign/codex bundles
    --help / -h                           show help and exit

Must be run from the repo root (the directory containing server.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys


# ---------------------------------------------------------------------------
# build subcommand (Phase 1 — unchanged)
# ---------------------------------------------------------------------------


def _usage() -> None:
    print(
        "Usage: python3 -m tools.engine.cli <subcommand> [options]\n"
        "\n"
        "Subcommands:\n"
        "  build   Assemble the ENGRAM Claude Code plugin tree.\n"
        "  plan    Print the step DAG + current check() state for a flow.\n"
        "  run     Execute a flow until DONE, PAUSED, or FAILED.\n"
        "  doctor  Run verify() on every step; print health table.\n"
        "\n"
        "Run `python3 -m tools.engine.cli <subcommand> --help` for per-subcommand help.\n"
        "\n"
        "Exit codes for `run`: 0=DONE  3=PAUSED  1=FAILED\n"
        "\n"
        "The command must be run from the repo root (the directory containing src/engram/server.py).\n"
        "Output is written to build/plugin/ by default — this directory is gitignored.\n"
        "Re-running is safe: the output tree is cleaned and rebuilt each time."
    )


def build_command(args: list[str]) -> int:
    """Execute the 'build' subcommand.  Returns exit code."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.engine.cli build",
        add_help=False,
    )
    parser.add_argument("--tier", default=None, metavar="TIER")
    parser.add_argument("--multi-agent", dest="multi_agent", action="store_true")
    parser.add_argument(
        "--output",
        default=None,
        metavar="DIR",
        help="Output directory (default: build/plugin)",
    )
    parser.add_argument(
        "--target",
        default="claude-code",
        metavar="TARGET",
        help="Platform target: claude-code (default) or codex",
    )
    parser.add_argument(
        "--identity",
        default="self",
        metavar="IDENTITY",
        help="Identity scope: self (default) or foreign",
    )
    parser.add_argument(
        "--engram-home",
        dest="engram_home",
        default=None,
        metavar="PATH",
        help="ENGRAM_HOME path to embed in hook commands and MCP config",
    )
    parser.add_argument(
        "--allow-branch",
        dest="allow_branch",
        action="store_true",
        help="Allow building from non-dev branch (overrides #794's guard)",
    )
    parser.add_argument("--help", "-h", action="store_true")

    parsed, unknown = parser.parse_known_args(args)

    if unknown:
        print(
            f"[build-plugin] ERROR: Unknown argument(s): {' '.join(unknown)}. "
            "Run with --help for usage.",
            file=sys.stderr,
        )
        return 1

    if parsed.help:
        print(
            "Usage: python3 -m tools.engine.cli build "
            "[--tier <essential|convenience|dev>] [--multi-agent] [--output DIR]\n"
            "       [--target <claude-code|codex>] [--identity <self|foreign>]\n"
            "       [--engram-home PATH]\n"
            "\n"
            "Assembles the ENGRAM plugin tree.\n"
            "\n"
            "Options:\n"
            "  --tier <tier>       Depth tier to build: essential, convenience, or dev.\n"
            "                      Default: the manifest's default_tier (convenience).\n"
            "                      Cumulative: dev includes convenience includes essential.\n"
            "  --multi-agent       Include multi-agent-gated mechanisms (baton, letter, forum\n"
            "                      coordination hooks, inter-agent tools). Off by default.\n"
            "  --output <DIR>      Output directory. Default: build/plugin\n"
            "  --target <TARGET>   Platform target: claude-code (default) or codex.\n"
            "  --identity <scope>  Identity scope: self (default) or foreign.\n"
            "                      foreign drops identity_coupled entries and runs a\n"
            "                      leak-scan over the emitted bundle.\n"
            "  --engram-home PATH  ENGRAM_HOME path to embed in hook commands and MCP config.\n"
            "  --allow-branch      Allow building from a non-dev branch (overrides #794's guard).\n"
            "  --help              Show this help message and exit.\n"
            "\n"
            "Tier filtering uses src/build/packaging/tiers.json as the single source of truth.\n"
            "The build manifest is written to <output>/.engram-build-manifest.json.\n"
            "\n"
            "The command must be run from the repo root (the directory containing src/engram/server.py).\n"
            "Output is written to build/plugin/ by default — this directory is gitignored.\n"
            "Re-running is safe: the output tree is cleaned and rebuilt each time."
        )
        return 0

    # Validate --target
    valid_targets = {"claude-code", "codex"}
    if parsed.target not in valid_targets:
        print(
            f"[build-plugin] ERROR: Unknown --target {parsed.target!r}. "
            f"Valid targets: {', '.join(sorted(valid_targets))}",
            file=sys.stderr,
        )
        return 1

    # Validate --identity
    valid_identities = {"self", "foreign"}
    if parsed.identity not in valid_identities:
        print(
            f"[build-plugin] ERROR: Unknown --identity {parsed.identity!r}. "
            f"Valid values: {', '.join(sorted(valid_identities))}",
            file=sys.stderr,
        )
        return 1

    # Repo root guard: must be run from a directory containing src/engram/server.py
    # (server.py moved from repo root to src/engram/ in Phase 1 of #1093;
    # root-level compat symlink removed in Phase 4).
    repo_root = os.getcwd()
    if not os.path.isfile(os.path.join(repo_root, "src", "engram", "server.py")):
        print(
            f"[build-plugin] ERROR: Must be run from the repo root "
            f"(expected src/engram/server.py at {os.path.join(repo_root, 'src', 'engram', 'server.py')})",
            file=sys.stderr,
        )
        return 1

    # #794 branch guard: refuse non-dev builds unless --allow-branch is set.
    # git branch --show-current returns "" in detached HEAD — treat as a block (fail safe).
    if not parsed.allow_branch:
        import subprocess as _sp
        _br = _sp.run(
            ["git", "branch", "--show-current"],
            cwd=repo_root, capture_output=True, text=True,
        ).stdout.strip()
        if not _br or _br != "dev":
            _br_desc = "detached HEAD (not on a named branch)" if not _br else f"branch {_br!r}"
            print(
                f"[build-plugin] ERROR: Source repo is at {_br_desc}, not 'dev'. "
                "Building from a non-dev state can deploy unmerged code. "
                "Pass --allow-branch to override (e.g. for metric-eval checkouts). "
                "Guard per #794.",
                file=sys.stderr,
            )
            return 1

    manifest_path = os.path.join(repo_root, "src", "build", "packaging", "tiers.json")
    if not os.path.isfile(manifest_path):
        print(
            f"[build-plugin] ERROR: src/build/packaging/tiers.json not found at {manifest_path} "
            "— is PR-1 merged?",
            file=sys.stderr,
        )
        return 1

    # Import engine modules (deferred to avoid import-time failures when --help is used)
    from .manifest import load_manifest, resolve_tier
    from .build import build_plugin

    try:
        manifest = load_manifest(manifest_path)
        tier = resolve_tier(manifest, parsed.tier)
    except (FileNotFoundError, ValueError) as e:
        print(f"[build-plugin] ERROR: {e}", file=sys.stderr)
        return 1

    plugin_root = parsed.output if parsed.output else os.path.join(repo_root, "build", "plugin")

    multi_agent: bool = parsed.multi_agent

    try:
        build_plugin(
            repo_root=repo_root,
            plugin_root=plugin_root,
            tier=tier,
            multi_agent=multi_agent,
            manifest=manifest,
            target=parsed.target,
            identity=parsed.identity,
            engram_home=parsed.engram_home,
        )
    except RuntimeError as e:
        # build_plugin already printed the error message
        return 1
    except Exception as e:
        print(f"[build-plugin] ERROR: {e}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Shared: resolve flow by name
# ---------------------------------------------------------------------------


_FLOW_NAMES = ("upgrade",)


def _resolve_flow(flow_name: str) -> list:
    """Return the step list for a named flow, or raise SystemExit on unknown name."""
    from .flows import upgrade_flow

    if flow_name == "upgrade":
        return upgrade_flow()

    print(
        f"[engine] ERROR: Unknown flow {flow_name!r}. "
        f"Available flows: {', '.join(_FLOW_NAMES)}",
        file=sys.stderr,
    )
    sys.exit(1)


def _build_ctx_from_env() -> dict:
    """Build a minimal context dict from environment / .deployed-version.

    Used by the CLI when no --ctx-file is provided.  Callers may augment.
    """
    engram_home = os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram"))
    # source_dir: try .deployed-version first, fallback to cwd
    source_dir = os.getcwd()
    dv_path = os.path.join(engram_home, ".deployed-version")
    if os.path.isfile(dv_path):
        with open(dv_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("deployed_from="):
                    candidate = line.strip().partition("=")[2]
                    if os.path.isdir(candidate):
                        source_dir = candidate
    return {
        "engram_home": engram_home,
        "source_dir": source_dir,
        "target_sha": "HEAD",
        "ack_changeset": False,
        "allow_branch": False,
    }


# ---------------------------------------------------------------------------
# plan subcommand
# ---------------------------------------------------------------------------


def plan_command(args: list[str]) -> int:
    """Execute the 'plan' subcommand.  Returns exit code."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.engine.cli plan",
        add_help=False,
    )
    parser.add_argument("flow", nargs="?", default=None)
    parser.add_argument("--help", "-h", action="store_true")

    parsed, unknown = parser.parse_known_args(args)

    if parsed.help or parsed.flow is None:
        print(
            "Usage: python3 -m tools.engine.cli plan <flow>\n"
            "\n"
            "Print the step DAG and current check() state for a flow.\n"
            "\n"
            f"Available flows: {', '.join(_FLOW_NAMES)}\n"
        )
        return 0

    if unknown:
        print(
            f"[engine] ERROR: Unknown argument(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
        return 1

    steps = _resolve_flow(parsed.flow)
    ctx = _build_ctx_from_env()

    from .flows import plan_flow

    plan = plan_flow(steps, ctx)

    print(f"Flow: {parsed.flow}  ({len(plan)} steps)\n")
    print(f"  {'STEP':<35}  {'KIND':<10}  {'SATISFIED':<10}  REQUIRES")
    print(f"  {'-'*35}  {'-'*10}  {'-'*10}  --------")
    for entry in plan:
        satisfied_str = "yes" if entry["satisfied"] else "no"
        if entry["error"]:
            satisfied_str = f"ERROR: {entry['error']}"
        requires_str = ", ".join(entry["requires"]) if entry["requires"] else "(none)"
        print(
            f"  {entry['id']:<35}  {entry['kind']:<10}  {satisfied_str:<10}  {requires_str}"
        )
    print()

    return 0


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------


def run_command(args: list[str]) -> int:
    """Execute the 'run' subcommand.  Returns exit code (0/1/3)."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.engine.cli run",
        add_help=False,
    )
    parser.add_argument("flow", nargs="?", default=None)
    parser.add_argument(
        "--ack-changeset",
        dest="ack_changeset",
        action="store_true",
        help="Acknowledge the changeset review (satisfies changeset-reviewed step)",
    )
    parser.add_argument(
        "--allow-branch",
        dest="allow_branch",
        action="store_true",
        help="Allow building from non-dev branch (overrides #794's guard)",
    )
    parser.add_argument("--help", "-h", action="store_true")

    parsed, unknown = parser.parse_known_args(args)

    if parsed.help or parsed.flow is None:
        print(
            "Usage: python3 -m tools.engine.cli run <flow> [--ack-changeset] [--allow-branch]\n"
            "\n"
            "Execute a flow until DONE, PAUSED, or FAILED.\n"
            "\n"
            f"Available flows: {', '.join(_FLOW_NAMES)}\n"
            "\n"
            "Options:\n"
            "  --ack-changeset   Acknowledge the changeset review step.\n"
            "  --allow-branch    Allow building from a non-dev branch.\n"
            "\n"
            "Exit codes: 0=DONE  3=PAUSED  1=FAILED\n"
        )
        return 0

    if unknown:
        print(
            f"[engine] ERROR: Unknown argument(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
        return 1

    steps = _resolve_flow(parsed.flow)
    ctx = _build_ctx_from_env()
    ctx["ack_changeset"] = bool(parsed.ack_changeset)
    ctx["allow_branch"] = bool(parsed.allow_branch)

    from .steps import run_flow, Done, Paused, Failed

    result = run_flow(steps, ctx)

    if isinstance(result, Done):
        print(f"[engine] DONE — flow '{parsed.flow}' completed successfully.")
        return 0

    if isinstance(result, Paused):
        print(f"[engine] PAUSED at step: {result.step_id!r}")
        print()
        print(result.instruction)
        print()
        print("Re-run after the operator action to continue.")
        return 3

    if isinstance(result, Failed):
        print(
            f"[engine] FAILED at step: {result.step_id!r}",
            file=sys.stderr,
        )
        print(f"  Reason: {result.reason}", file=sys.stderr)
        return 1

    # Should not reach here
    print(f"[engine] ERROR: unexpected result type {type(result)}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# doctor subcommand
# ---------------------------------------------------------------------------


def doctor_command(args: list[str]) -> int:
    """Execute the 'doctor' subcommand.  Returns 0 if all steps verified, 1 otherwise."""
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.engine.cli doctor",
        add_help=False,
    )
    parser.add_argument(
        "flow",
        nargs="?",
        default="upgrade",
        help="Flow to check (default: upgrade)",
    )
    parser.add_argument("--help", "-h", action="store_true")

    parsed, unknown = parser.parse_known_args(args)

    if parsed.help:
        print(
            "Usage: python3 -m tools.engine.cli doctor [flow]\n"
            "\n"
            "Run verify() on every step of the named flow and print a health table.\n"
            "Doctor mode is read-only: it never calls apply() or emits operator instructions.\n"
            "\n"
            f"Available flows: {', '.join(_FLOW_NAMES)}  (default: upgrade)\n"
        )
        return 0

    if unknown:
        print(
            f"[engine] ERROR: Unknown argument(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
        return 1

    steps = _resolve_flow(parsed.flow)
    ctx = _build_ctx_from_env()

    from .steps import run_doctor

    results = run_doctor(steps, ctx)

    all_ok = all(r.satisfied for r in results)

    print(f"Doctor report — flow: {parsed.flow}\n")
    print(f"  {'STEP':<35}  {'STATUS':<10}  DETAIL")
    print(f"  {'-'*35}  {'-'*10}  ------")
    for r in results:
        status = "OK" if r.satisfied else "FAIL"
        detail = r.error or ""
        print(f"  {r.step_id:<35}  {status:<10}  {detail}")
    print()

    if all_ok:
        print("[engine] All steps verified OK.")
        return 0
    else:
        broken = [r.step_id for r in results if not r.satisfied]
        print(
            f"[engine] {len(broken)} step(s) not satisfied: {', '.join(broken)}",
            file=sys.stderr,
        )
        return 1


# ---------------------------------------------------------------------------
# main dispatcher
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for python3 -m tools.engine.cli."""
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        _usage()
        return 0

    if argv[0] in ("--help", "-h"):
        _usage()
        return 0

    if argv[0] == "build":
        return build_command(argv[1:])

    if argv[0] == "plan":
        return plan_command(argv[1:])

    if argv[0] == "run":
        return run_command(argv[1:])

    if argv[0] == "doctor":
        return doctor_command(argv[1:])

    print(
        f"[engine] ERROR: Unknown subcommand: {argv[0]!r}. "
        "Supported subcommands: build, plan, run, doctor",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
