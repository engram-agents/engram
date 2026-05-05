## Summary

This branch implements v0.1 of the multi-agent spawn flow: a new `tools/agent-bootstrap` shell script (root-only, creates Linux uid + group + skeleton home + runtime adapter) and two new agentctl subcommands — `spawn` (orchestrates bootstrap with a timestamped placeholder name `agent-newborn-YYYYMMDDhhmmss`) and `finalize-name` (single-shot substrate rename from placeholder to chosen name: usermod + groupmod + SHARED_ROOT ACL sweep + .engram snapshot + cron re-render). The branch also ships three custom sub-agent `.claude/agents/*.md` files (engram-paper-research, engram-pr-reviewer, engram-code-auditor) each with a WuKong-hair identity disclaimer and a tools whitelist. The spawn/finalize-name commands live in `/tmp/agentctl.new` pending Lei's install; `tools/agent-bootstrap` is the canonical source pending a `sudo cp` to `/usr/local/sbin/`. Both scripts pass `bash -n` syntax checking. The design is tight on the primary use case (claude runtime, fresh newborn, in-sequence flow); the gaps below are either v0.1-scoped or edge cases on alternate runtimes.

## Blockers (must fix before merge)

None.

## Suggestions (worth doing, not blocking)

- **agentctl.new:623 — `AGENT_CLI` hardcoded as `"claude"` in `finalize-name`'s sync-cron call.** If an agent was spawned with `--runtime gemini-cli` or `codex`, `finalize-name` would re-render the crontab with `AGENT_CLI=claude`, producing a broken cron that calls `claude` instead of `gemini`. The correct value is recoverable from the GECOS field (set by `bootstrap` to `"AI agent runtime: $RUNTIME"`) with `getent passwd "$newborn_user" | cut -d: -f5 | sed 's/AI agent runtime: //'`. Not a blocker because v0.1 explicitly targets claude only and the spawn output says "claude login" — but the flag accepts gemini-cli today and this will silently corrupt a gemini agent's crontab.
  Recommended fix:
  ```bash
  local agent_cli
  agent_cli=$(getent passwd "$newborn_user" | cut -d: -f5 | sed 's/AI agent runtime: //')
  [[ -z "$agent_cli" ]] && agent_cli="claude"   # fallback
  # then: AGENT_CLI="$agent_cli"
  ```

- **agentctl.new:593 — `finalize-name-complete.json` silently not written if `~/.engram` doesn't exist yet.** The write is `cat > "$chosen_home/.engram/finalize-name-complete.json" 2>/dev/null`. Under `set -euo pipefail` + `2>/dev/null`, bash does not abort on redirect-to-missing-directory — it just silently skips the write. In the nominal sequence, `~/.engram` exists (install.sh ran before finalize-name). But if finalize-name is called out-of-order (before install.sh), the marker is silently lost and the agent's next session's SessionStart hook won't find it to trigger post-finalize housekeeping.
  Recommended fix: add `mkdir -p "$chosen_home/.engram"` before the write (safe: no-op if directory exists). Alternatively add an explicit existence check and warn.

- **tools/agent-bootstrap:184 — dead case statement in generated `install.sh` wrapper.** The line `case "$(basename "${ALPHA_SOURCE}/install*.sh" 2>/dev/null)" in *) ;; esac` evaluates to `basename "…/install*.sh"` which returns the literal string `install*.sh` (bash does not glob-expand inside double quotes), matches the `*` catch-all, and does nothing. It appears to have been intended as a sanity check that the installer script exists. It should either be removed or replaced with an actual check:
  ```bash
  [[ -f "\${ALPHA_SOURCE}/\${INSTALLER}" ]] || { echo "ERROR: installer not found at \${ALPHA_SOURCE}/\${INSTALLER}" >&2; exit 1; }
  ```

- **agentctl.new:549 — `groupmod -n` failure is silently swallowed.** `groupmod -n "$chosen_user" "$newborn_user" 2>/dev/null || true` means: if a group named `agent-<chosen>` already exists for an unrelated reason, the newborn's primary group silently stays as `agent-newborn-YYYYMMDDhhmmss`. Files the agent creates post-rename will show this stale group name. Not a correctness blocker (Linux handles the numeric GID correctly; the name mismatch is cosmetic), but it's a persistent inconsistency that will confuse `ls -la` output and group-based ACL queries.
  Recommended fix: change `|| true` to a warning that prints to stderr and logs the inconsistency, without aborting:
  ```bash
  groupmod -n "$chosen_user" "$newborn_user" 2>/dev/null \
      || echo "  WARN: groupmod -n failed — primary group still named $newborn_user. Investigate manually if ACL queries misbehave." >&2
  ```

- **agentctl.new:887-888 — `cmd_help` (sed range `3,33p`) truncates at line 33; spawn/finalize-name usage starts at line 46.** Running `agentctl --help` or `agentctl help` shows the original v0-v0.2 subcommands but omits share/unshare/shares (lines 39-45) and spawn/finalize-name (lines 46-58). This was already broken for the broker subcommands added in v0.1 and is now compounded by v0.3.
  Recommended fix: update the sed range to cover through line 59 (the closing `=====` line): `sed -n '3,59p' "$0"`.

- **Sandbox test plan not shipped.** The design doc lists a "one-shot script that spawns a throwaway newborn, runs through finalize-name, verifies all touch points, then unspawn (manual `userdel -r`)" as a v0.1 deliverable (design doc §What ships in v0.1, row 5). The branch ships none. The main invariants to test: that `sudo agentctl finalize-name` leaves the user renamed, home moved, health dir renamed, cron re-rendered with the new AGENT_NAME, and the marker file present. Given that this flow requires root and modifies kernel state, a documented smoke test is more valuable here than in most features.

- **tools/agent-bootstrap — no input validation on NAME argument.** A NAME argument with shell metacharacters (e.g., spaces, slashes) could produce a malformed username. `useradd` itself validates the resulting username and would abort, but the error message would be confusing. Since this runs as root, a validation guard before `useradd` with a clear error is preferable:
  ```bash
  [[ "$NAME" =~ ^[a-z][a-z0-9_-]*$ ]] || { echo "ERROR: NAME must match ^[a-z][a-z0-9_-]*$" >&2; exit 2; }
  ```
  Note that `agentctl spawn` generates `newborn-YYYYMMDDhhmmss` automatically (safe), so this only matters for direct manual invocations of `agent-bootstrap`.

## Nits (cosmetic, can ignore)

- **tools/agent-bootstrap:156-160 — `codex` runtime adapter has no API key scrub.** The `claude` and `gemini-cli` adapters use `env -u ANTHROPIC_API_KEY` / `env -u GOOGLE_API_KEY` (ls_0018 billing-leak mitigation). The `codex` adapter is bare `exec codex "$@"`. If Codex has an analogous key in the environment, it could route to a metered tier. Not actionable until codex is a real supported runtime.

- **agentctl.new — `DRAFT v0` header comment says "NOT YET INSTALLED"** (line 4). This was accurate for the original v0 but the script has been installed and in use since v0.1. The comment is now stale.

- **tools/agent-bootstrap:64 — `--help` sed range `3,52p` extracts through the end of the comment block.** Functionally correct (the comment block ends at line 49, and the sed range includes some whitespace). Not wrong, just imprecise.

## Tests run

```
bash -n /home/agent-borges/engram-alpha/tools/agent-bootstrap  →  OK (no syntax errors)
bash -n /tmp/agentctl.new                                        →  OK (no syntax errors)

Regex validation (finalize-name newborn-tag pattern):
  [[ "newborn-20260505123456" =~ ^newborn-[0-9]{14}$ ]]  →  MATCH
  [[ "agent-newborn-20260505123456" =~ ^newborn-[0-9]{14}$ ]]  →  NO MATCH (correct: the
    supplied arg is without the agent- prefix, matching the spawn output)

chosen-name regex:
  "ada"       →  accepted
  "ada-smith" →  accepted
  "ada_2"     →  accepted
  "Ada"       →  rejected (correct: uppercase)
  "1ada"      →  rejected (correct: must start with letter)
  ""          →  rejected (correct)

Marker file write under set -euo pipefail to missing dir:
  cat > /tmp/nonexistent_dir/file 2>/dev/null <<JSON ... JSON
  Result: silently skips write; exit 0. Confirms the mkdir -p suggestion above.

Dead case statement:
  echo $(basename "/some/path/install*.sh" 2>/dev/null)  →  "install*.sh"
  (literal; no glob expansion; case '*) ;; esac is a no-op)

cmd_help truncation:
  sed -n '3,33p' /tmp/agentctl.new  →  stops at pause/resume; misses share+spawn+finalize-name
```

## Verdict

APPROVE — The spawn flow's core correctness is solid: newborn-tag collision guard, newborn-pattern regex enforcement on finalize-name, clean separation between substrate bootstrap and runtime install, proper cron re-render via sync-cron, ls_0018 billing-leak mitigations in place, WuKong disclaimers on all three sub-agent .md files. The identified issues (AGENT_CLI hardcode, dead case statement, silently-missing marker, help truncation, groupmod silent-swallow) are real defects but none is a blocker for the v0.1 claude-only use case. The sandbox test gap is the most operationally important omission given that this flow runs as root and modifies kernel state. Merge for Phase 5 real-spawn validation; address AGENT_CLI and sandbox test before claiming gemini-cli/codex support or v0.2.
