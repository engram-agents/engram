# Install.sh Docker Test Harness

A clean-Ubuntu Docker sandbox for testing `install.sh` end-to-end without polluting your host's `~/.engram` or `~/.claude`. Catches install regressions on a truly-fresh target before they hit a real user's machine.

## What it tests

The harness verifies that `install.sh` on a clean Ubuntu 22.04 + Python 3.11 + git produces:

- `~/.engram/knowledge.db` with the seed graph (≥5 current nodes, ≥3 schema tables)
- `~/.engram/seed-manifest.json`
- `~/.engram/hooks/` + `~/.engram/tools/` populated
- `~/.engram/heartbeat-queue/{to-heartbeat,from-heartbeat}/` with empty append-files
- `~/.engram/first-run-pending` (cold-start marker armed)
- `~/.claude/CLAUDE.md` rendered with `{{AGENT_NAME}}` / `{{USER_NAME}}` placeholders **unresolved** (those are first-session's job, not bootstrap.py's)
- `~/.engram/warm-briefing.md` similarly rendered
- (When PR #44 lands) `~/.engram/HEARTBEAT.md` rendered with placeholders unresolved

## What it does NOT test

- **Real Claude Code MCP registration** — the `claude` CLI is stubbed (Claude Code is a native binary, not pip-installable). MCP behavior must be tested manually on a real install.
- **The first-session cold-start dialogue** — fires on the user's first Claude Code session, not at install time. Out of harness scope.
- **Cron registration** — install.sh's cron step is opt-in (default-off as of 2026-05-15) so the harness invocation `bash install.sh` skips it naturally. Pass `--with-cron` to exercise the cron-install path (note: cron daemon access from inside the harness container is limited).
- **viz_server.py runtime** — install.sh copies the file but doesn't start the daemon.

## Usage

```bash
# Build the image (one-time per change to Dockerfile or Python deps).
# Pre-warms the sentence-transformers MiniLM cache during build, so first
# install run doesn't pay the model-download tax.
cd /home/USER/engram-alpha
docker build -t engram-install-test:latest -f tests/install/Dockerfile .

# Run the test (mounts current alpha tree read-only, exits 0 on pass).
docker run --rm -v "$(pwd):/alpha:ro" engram-install-test:latest

# Inspect a failed run interactively:
docker run --rm -it -v "$(pwd):/alpha:ro" --entrypoint /bin/bash engram-install-test:latest
# inside container:
#   bash run_install_test.sh   # see where the assertion failed
#   ls -la ~/.engram           # inspect partial state
#   bash /alpha/install.sh   # re-run install (cron skipped by default)
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | install.sh ran cleanly AND verification passed |
| 1 | install.sh exited non-zero |
| 2 | install.sh succeeded but verification found missing/wrong artifacts |

## Files

| File | Role |
|---|---|
| `Dockerfile` | Ubuntu 22.04 base + Python deps + sentence-transformers cache pre-warm + claude-stub |
| `claude-stub.sh` | Stub `claude` CLI satisfying `claude mcp add/remove/list` calls |
| `run_install_test.sh` | Container entry-point: copies alpha tree to /tmp, runs install.sh, asserts post-install state |

## When to run

- **Before tagging a V1 release** — every install.sh change should pass the harness on a clean target.
- **After modifying any of**: install.sh, bootstrap.py, the templates/ directory, hooks/claude/, skills/claude/.
- **After a Python-deps change** — fastmcp or sentence-transformers version bumps can break preflight.

## Future extensions

- GitHub Actions workflow that builds + runs the harness on every PR to `dev`.
- Multi-Python-version matrix (3.10, 3.11, 3.12) to catch version-specific regressions.
- Gemini-target variant once `install-gemini.sh` is V1 scope.
- Idempotency check: run install.sh twice in the same container, verify the second run handles `--force` correctly.
