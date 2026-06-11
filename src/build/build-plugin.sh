#!/usr/bin/env bash
# build-plugin.sh — thin wrapper around the tools.engine Python build engine.
#
# Delegates to: python3 -m tools.engine.cli build <args>
#
# OUTPUT: build/plugin/ (default) or the path specified by --output.
# Flag-compatible with the previous bash implementation:
#   --tier <essential|convenience|dev>   depth tier (default: manifest default)
#   --multi-agent                         include multi-agent-gated mechanisms
#   --output <DIR>                        override output directory
#   --help / -h                           show help and exit
#
# The engine is now the sole implementation; the legacy bash script and
# golden equivalence tests were retired when the engine graduated.
#
# Run from the repo root (engram-alpha/).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"

cd "$REPO_ROOT"
exec python3 -m tools.engine.cli build "$@"
