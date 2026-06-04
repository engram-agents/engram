"""tools.engine.cli — command-line entry point for the ENGRAM build engine.

Usage:
    python3 -m tools.engine.cli build [--tier T] [--multi-agent] [--output DIR]

Flag-compatible with tools/build-plugin.sh:
    --tier <essential|convenience|dev>   depth tier (default: manifest's default_tier)
    --multi-agent                         include multi-agent-gated mechanisms
    --output <DIR>                        output directory (default: build/plugin)
    --help / -h                           show help and exit

Must be run from the repo root (the directory containing server.py).
"""

from __future__ import annotations

import argparse
import os
import sys


def _usage() -> None:
    print(
        "Usage: python3 -m tools.engine.cli build "
        "[--tier <essential|convenience|dev>] [--multi-agent] [--output DIR]\n"
        "\n"
        "Assembles the ENGRAM Claude Code plugin tree.\n"
        "\n"
        "Options:\n"
        "  --tier <tier>    Depth tier to build: essential, convenience, or dev.\n"
        "                   Default: the manifest's default_tier (convenience).\n"
        "                   Cumulative: dev includes convenience includes essential.\n"
        "  --multi-agent    Include multi-agent-gated mechanisms (baton, letter, forum\n"
        "                   coordination hooks, inter-agent tools). Off by default.\n"
        "  --output <DIR>   Output directory. Default: build/plugin\n"
        "  --help           Show this help message and exit.\n"
        "\n"
        "Tier filtering uses packaging/tiers.json as the single source of truth.\n"
        "The build manifest is written to <output>/.engram-build-manifest.json.\n"
        "\n"
        "The command must be run from the repo root (the directory containing server.py).\n"
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
        _usage()
        return 0

    # Repo root guard: must be run from a directory containing server.py
    repo_root = os.getcwd()
    if not os.path.isfile(os.path.join(repo_root, "server.py")):
        print(
            f"[build-plugin] ERROR: Must be run from the repo root "
            f"(expected server.py at {os.path.join(repo_root, 'server.py')})",
            file=sys.stderr,
        )
        return 1

    manifest_path = os.path.join(repo_root, "packaging", "tiers.json")
    if not os.path.isfile(manifest_path):
        print(
            f"[build-plugin] ERROR: packaging/tiers.json not found at {manifest_path} "
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
        )
    except RuntimeError as e:
        # build_plugin already printed the error message
        return 1
    except Exception as e:
        print(f"[build-plugin] ERROR: {e}", file=sys.stderr)
        return 1

    return 0


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

    print(
        f"[build-plugin] ERROR: Unknown subcommand: {argv[0]!r}. "
        "Supported subcommands: build",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
