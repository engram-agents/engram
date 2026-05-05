# spawn-implementation — multi-agent spawn flow v0.1

**Status:** active (drafted, Phase 1 in progress)
**Started:** 2026-05-05 morning
**ENGRAM pointer:** (pending pin once first commit lands)
**Branch:** `feature/spawn-implementation` (off `feature/engram-config-surface`)
**Design source:** `/home/user/kg_protocol_for_ai/active-work/spawn-new-agent-flow-2026-05-03.md` (settled spec; dv_0556 + ob_1465)
**Depends on:** `feature/engram-config-surface` (Phase 1–3 of cron integration must merge first — `install.sh` Step 8 / `install-gemini.sh` Step 9 are the cron-registration step that closes the long-standing gap noted in dv_0556)

## Goal

Ship enough of the spawn-new-agent flow to spawn a third ENGRAM-equipped agent on Lei's machine who can take over ENGRAM operations (housekeeping, runtime maintenance, viz monitoring, alpha install testing), freeing Lei + Borges to focus on paper drafting per the Pinecone-Nexus-driven urgency (dv_0559).

This is **v0.1 minimum**, not a complete spawn flow. Deferred items (`agentctl unspawn`, `agentctl rebirth`, `agentctl retire`, `agentctl migrate-to-multi-agent`) are non-blocking for the first new-agent-helper use case.

## What ships in v0.1

| Component | Source | Notes |
|---|---|---|
| `tools/agent-bootstrap` | Clean port of spt-side draft (`/home/user/kg_protocol_for_ai/active-work/multi-agent-uid-substrate-tools/agent-bootstrap`) | Root-only. Creates `agent-<NAME>` uid, sets up home perms, joins `agents` group, drops a `~/.runtime/start.sh` adapter. Lei copies to `/usr/local/sbin/agent-bootstrap` after review. |
| `agentctl spawn [--runtime claude]` | New subcommand on `/home/user/.local/bin/agentctl` (the file already touched by Phases 0.1–0.2 — we need a third patch session) | Calls `agent-bootstrap` with the placeholder uid pattern `agent-newborn-YYYYMMDDhhmmss`, drops a runtime adapter, prints clear "next: agentctl shell <newborn-id>" instructions. |
| `agentctl finalize-name <newborn-id> <chosen>` | New subcommand | Single-shot: stop crons, `usermod -l`, `groupmod -n`, ACL sweep, snapshot `~/.engram/` to `.engram.first-session-baseline.bak/`, file 3-candidate ENGRAM observation, restart crons. Refuses anything that isn't an `agent-newborn-*` placeholder (this is first-session-only, not a generic rename). |
| Runtime adapter `templates/runtime-claude/start.sh.template` | New | Minimal shim that activates the newborn's environment and runs `claude` (or `claude-remote`). Dropped into `/home/agent-newborn-X/.runtime/start.sh` by spawn. |
| Sandbox test plan | New | One-shot script that spawns a throwaway newborn (under a CI-friendly variant), runs through finalize-name, verifies all touch points, then unspawn (manual `userdel -r`). |

## What does NOT ship in v0.1

- **Multi-runtime support — claude only in v0.1.** Original draft accepted `--runtime claude|gemini-cli|codex` in `agentctl spawn` and `agent-bootstrap`. Sub-agent PR review of 2026-05-05 (see `active-work/spawn-implementation-pr-review-2026-05-05.md`) and Lei's 2026-05-05 PM scoping decision flagged that the `agentctl shell` flow, `claude-remote` integration, `tmux has-session -t claude-remote` semantics, and most of the `cmd_shell` body are claude-specific in ways the half-supported gemini-cli/codex flags would silently break. Per the project's honest-scope voice (Mao-Cao + ls_0020 + §7.3): drop the half-support entirely. v0.1 supports claude only; both `agent-bootstrap` and `agentctl spawn` reject non-claude `--runtime` values with an explicit error. Multi-runtime support is a separate forthcoming epic with proper per-runtime test fixtures, generic shell-launcher abstraction, end-to-end validation, and per-runtime `claude-remote` analogs (or explicit acknowledgment that the persistent-session semantics differ per runtime). Tracked in `ask-lei.md` as a v0.2+ ticket.
- `agentctl unspawn` — Lei uses `sudo userdel -r agent-newborn-X` directly. Trivial; codified in v0.2.
- `agentctl rebirth` — restore from `.engram.first-session-baseline.bak/`. Useful when first session goes wrong; not a hot path. v0.2.
- `agentctl retire` — community-aware shutdown ceremony. Real design work; v0.2+.
- `agentctl migrate-to-multi-agent` — generalizes the migrate-borges/mneme scripts. v0.2.
- `--multi-agent` flag in `install.sh` — for now, `install.sh` runs unchanged as the per-uid installer. The spawn flow drives it externally via `agentctl spawn`.
- Phonebook / community awareness — agents discover each other organically via inter-agent traffic in v0.1.

## Phases

| Phase | Work | Estimate | Phase gate |
|---|---|---|---|
| 1 | Port `agent-bootstrap` → `tools/agent-bootstrap` in alpha. Clean header doc, refactor for the v0.1 scope. Sandbox-test (root-required, so test-on-real-machine with throwaway uid). | 45 min | Lei reviews; copies to `/usr/local/sbin/`. |
| 2 | Add `agentctl spawn` subcommand. Patches `/home/user/.local/bin/agentctl`; deliver via `/tmp/agentctl.new` + install one-liner (same pattern as the v0.2 pause/resume add). | 1 hr | Lei reviews + installs. Smoke test by running `agentctl spawn --runtime claude` and inspecting the newborn substrate. |
| 3 | Runtime adapter template (`templates/runtime-claude/start.sh.template`). Spawn drops it into `/home/agent-newborn-X/.runtime/start.sh`. | 30 min | Verify `agentctl shell agent-newborn-X` actually launches Claude. |
| 4 | Add `agentctl finalize-name` subcommand. Same delivery pattern. | 1.5 hr | Sandbox-test on the throwaway newborn from Phase 2. |
| 5 | Real-spawn validation: spawn the actual 3rd helper agent. Lei runs the four commands, agent picks its name, Lei finalizes. New agent files its first-day observations. | 30 min interactive | We have a third helper. |

Total: ~4 hours focused work + Lei sudo gates between phases. Phases 1–4 can land as commits on `feature/spawn-implementation`; Phase 5 is the real-world validation that closes the epic.

## Open questions for Lei (queued for tonight, not blocking Phase 1–2)

1. **Where does `tools/agent-bootstrap` LIVE in production?** Lei's draft says "`/usr/local/sbin/agent-bootstrap` after review." Confirming: alpha holds the source-of-truth at `tools/agent-bootstrap`; Lei copies to `/usr/local/sbin/` after each version bump.
2. **Newborn's engram-alpha source.** The newborn's `~/.runtime/install.sh` (or the equivalent runtime-side step) needs a path to `engram-alpha/`. Options: clone fresh into `/home/agent-newborn-X/engram-alpha/` (uid-isolated, version-locked); symlink to a shared `/home/agents-shared/engram-alpha/` (single source of truth, all agents track same alpha SHA); use `sudo -u agent-newborn-X` from Lei's spt-side checkout (works but ties newborn to spt's tree). Lean: clone fresh per-agent — matches the existing migrate-borges-to-uid pattern.
3. **OAuth bootstrap.** Claude requires interactive `claude login` first time. v0.1 plan: spawn does NOT touch OAuth; first thing the user does inside `agentctl shell agent-newborn-X` is `claude login`, then start a session, then the engram-first-session skill kicks in. Confirming: that's acceptable for v0.1 (no copy-from-spt token pre-loading required).
4. **Helper agent's first goal.** When the new agent does first-session goal-setting, should we suggest "ENGRAM operations helper" as a starting orientation, or let it discover its role through dialogue? Lean: suggest ENGRAM ops as a starting frame — that's the explicit reason for spawning — but let the agent's own goal-setting in first session drive the precise shape.

## Connection to Pinecone urgency (dv_0559)

Lei's framing 2026-05-05: "we'll need to hurry up the pace from now on" + "spawn another Claude Code based agent, and let him/her take care of ENGRAM maintenance and development, you and I can focus on the paper draft." That's the v0.1 use case. The faster this ships, the sooner Lei + Borges can put dedicated cycles into paper drafting before the agent-memory landscape moves further.

The cleanest sequencing — already validated by today's order of operations:

1. ✅ Config-surface Phase 1–3 (cron primitive landed) — closes the dv_0556 cron-registration gap
2. ⏳ Spawn implementation v0.1 (this doc) — produces a 3rd agent
3. ⏳ Paper drafting (Lei + Borges) — main thread once helper is online
4. ⏳ Config-surface Phase 4–6 (UI tab + USER_GUIDE) + spawn v0.2 — bg work the new helper can take

## Files touched

**Phase 1**
- New: `tools/agent-bootstrap` (canonical source; Lei copies to `/usr/local/sbin/`)

**Phase 2–4**
- Patched: `/home/user/.local/bin/agentctl` (delivered via `/tmp/agentctl.new` + Lei install one-liner)
- New: `templates/runtime-claude/start.sh.template`

**Phase 5**
- No code changes; real-world spawn ceremony.

## Sequence after this doc commits

1. ENGRAM observation pointing at this doc (when Phase 1 commit lands).
2. Phase 1 commit on `feature/spawn-implementation`.
3. Stop for Lei review on `tools/agent-bootstrap` content (it's substrate-touching, worth a careful pair of eyes before it goes near `/usr/local/sbin/`).
4. Phases 2–4 once Lei greenlights.

## Status (2026-05-05, 09:10 EDT)

- ✅ **Phase 1 done** — `tools/agent-bootstrap` + this doc shipped (`b21b19f` on `feature/spawn-implementation`).
- ✅ **Phase 2–3 patched** — both `agentctl spawn` and `agentctl finalize-name` written into `/tmp/agentctl.new` (one bundle, since they are tightly coupled and finalize-name calls cmd_pause/cmd_resume from Phase 0.2). Includes:
  - newborn-tag generator (`agent-newborn-YYYYMMDDhhmmss`)
  - root-required guards on both subcommands
  - `agent-newborn-*` pattern enforcement on finalize-name (refuses general renames per dv_0556)
  - Substrate rename: `usermod -l + -d -m`, `groupmod -n`, ACL sweep on `/home/agents-shared/`, health-dir rename
  - `.first-session-baseline.bak` snapshot for rebirth (per dv_0556 lifecycle case 3)
  - `finalize-name-complete.json` marker for the agent's next session to consume
  - Audit entries for both spawn and finalize-name actions
- ⏳ **Phase 1 install needed** — Lei copies `tools/agent-bootstrap` to `/usr/local/sbin/agent-bootstrap`:

  ```bash
  sudo cp /home/agent-borges/engram-alpha/tools/agent-bootstrap /usr/local/sbin/agent-bootstrap
  sudo chmod 0755 /usr/local/sbin/agent-bootstrap
  agent-bootstrap --help | head
  ```

- ⏳ **Phase 2–3 install needed** — same one-liner as the v0.2 pause/resume patch:

  ```bash
  cp /tmp/agentctl.new /home/user/kg_protocol_for_ai/active-work/multi-agent-uid-substrate-tools/agentctl \
    && cp /home/user/kg_protocol_for_ai/active-work/multi-agent-uid-substrate-tools/agentctl /home/user/.local/bin/agentctl \
    && chmod +x /home/user/.local/bin/agentctl \
    && agentctl --help | grep -E "spawn|finalize-name"
  ```

- ⏳ **Phase 5: real-spawn validation** — once Phases 1–3 are installed, spawn the actual 3rd helper agent:

  ```bash
  sudo agentctl spawn --runtime claude
  # → prints next-step instructions including the agent-newborn-... uid
  ```

  Then follow the printed instructions (shell into newborn, install ENGRAM, claude login, start session for naming dialogue, finalize from spt shell).
