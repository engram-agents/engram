#!/usr/bin/env bash
# Stub `claude` CLI for the Docker install test harness.
#
# install.sh requires `claude` on PATH and calls `claude mcp add engram
# -s user -- <python> <server.py>` at step 4. We can't pip-install Claude
# Code in Docker (it's a native binary), so this stub returns success for
# the subcommands install.sh actually invokes, letting the rest of the
# install flow proceed.
#
# Real MCP registration isn't part of what this harness verifies — the
# test scope is "install.sh produces the expected files + ENGRAM seed
# graph + first-run-pending marker." The MCP layer requires real Claude
# Code and is out of scope for clean-target Docker testing.
set -euo pipefail

case "${1:-}" in
  mcp)
    case "${2:-}" in
      add|remove|list)
        # Echo what install.sh would have done so the test log shows the
        # call surface that's being faked.
        echo "[claude-stub] $* (faked OK in Docker test)"
        exit 0
        ;;
      *)
        echo "[claude-stub] mcp $2: not implemented in stub" >&2
        exit 0
        ;;
    esac
    ;;
  --version|-v)
    echo "claude-stub 0.0.0 (Docker test harness)"
    exit 0
    ;;
  --help|-h)
    echo 'claude-stub: docker test harness — only `mcp add/remove/list` and `--version` are implemented'
    exit 0
    ;;
  *)
    # Unknown subcommand — succeed silently so install.sh continues.
    # Surfaced via stderr so the test log shows what was faked.
    echo "[claude-stub] unrecognized invocation: $* (returning 0 for test harness)" >&2
    exit 0
    ;;
esac
