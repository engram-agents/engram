# Spawn-flow test harness

Validates V1 §3.3 (Phase 5 spawn-flow real-world validation): runs
`agentctl spawn` end-to-end inside a clean Ubuntu 24.04 container, then
verifies the newborn agent is correctly created, install.sh runs cleanly
under the newborn uid, and `agentctl finalize-name` completes the rename.

The container provides root → no host-sudo dependency. The host machine
doesn't need to spin up an actual third agent uid for this validation.

## Usage

```bash
# From the alpha repo root.
docker build -t engram-spawn-test:latest -f tests/spawn/Dockerfile .
docker run --rm -v "$(pwd):/alpha:ro" engram-spawn-test:latest
```

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | All three phases passed |
| 1 | `agentctl spawn` or its bootstrap step failed |
| 2 | Newborn substrate verification failed |
| 3 | Newborn's install.sh exited non-zero |
| 4 | Install completed but post-install assertions failed |
| 5 | `agentctl finalize-name` failed or post-rename state is wrong |

## Phases

1. **`agentctl spawn --runtime claude`** — creates `agent-newborn-<UTC-timestamp>` user with home dir, `.bashrc`, `~/.runtime/start.sh`, `~/.runtime/install.sh`. Asserted via Linux user/group lookup + file-presence checks.
2. **install.sh as the newborn** — the newborn runs `bash ~/.runtime/install.sh`, which chains to alpha's `install.sh`. Asserted via the same post-install file checks as `tests/install/` plus seed-graph node count + placeholder retention. Cron is opt-in (default-off as of 2026-05-15) so the harness invocation skips it naturally.
3. **`agentctl finalize-name <newborn-tag> testbot`** — renames the user/group/home; verifies the old name is gone and the new name has the `finalize-name-complete.json` marker.

## What this harness does NOT cover

- Real Claude session as the newborn (claude CLI is stubbed; first-session naming dialogue runs only in real Claude Code)
- Multi-runtime spawn (gemini-cli, codex) — v0.1 of agentctl is claude-only
- External-cron setup (the legacy heartbeat/sleep crontab was removed in issue #785)
- Inter-agent comms (telegram dispatcher, channel dispatcher) — those need a second agent in the topology

## Vendor note (issue #50)

`agentctl` is vendored at `tools/agentctl` (issue #50). The Dockerfile copies
it directly from there; the old `tests/spawn/agentctl-snapshot` has been
removed.

## Pairs with

- `tests/install/` (PR #49, open at time of writing) — the install-side harness this one chains into via Phase 2. Once PR #49 lands, this cross-reference becomes a live tree reference; until then the path exists only on the #49 branch.
