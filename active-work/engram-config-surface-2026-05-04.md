# engram-config-surface — one-stop UI + cron install primitive

**Status:** active (drafted, not started)
**Started:** 2026-05-04 evening (Lei + Borges, after viz-server multi-agent shipped)
**ENGRAM pointer:** (to pin when implementation starts)
**Branch:** (none yet — `feature/engram-config-surface` when work begins)

## Goal

Give an ENGRAM user (and any spawned agent's owner) a single place to **see and tune** runtime knobs, and a single command to **install OS-level scheduling** from those settings. Two output artifacts:

1. A **Settings tab in viz_server** that reads/edits `~/.engram/config.json` (extended schema — see below).
2. A **`tools/sync-cron.sh`** primitive that reads `config.json` and renders/installs a crontab, callable from `install.sh` for first-run wiring and from the Settings UI's "Apply" button for subsequent edits.

## Why this matters now

The alpha is missing both ends of this. Audit (2026-05-04 evening):

- **No UI for runtime knobs.** Memory tier sizes, confidence caps, drowsiness thresholds, importance anchoring weight, and trust pool all live in `~/.engram/config.json` as bare JSON the user edits by hand. Nothing surfaces them.
- **No cron install pathway.** `install.sh:157` explicitly punts: *"Cron registration of heartbeat/sleep is the runtime installer's job (see runtime-install.*.sh in the multi-agent uid-substrate setup) — install.sh only stages the scripts; whoever runs them is responsible for scheduling."* The "runtime installer" referenced is a private artifact of Lei's multi-agent setup, **not** part of the public alpha repo. A fresh `install.sh` user gets `heartbeat.sh` + `sleep.sh` staged to `$ENGRAM_HOME/tools/` but no schedule, no template, no doc.

The cost of this gap compounds as we push toward spawn-flow readiness. A newly spawned agent needs cron up for its first heartbeat to fire — without a primitive, that's a manual crontab edit by Lei (or whoever brought up the agent) every single time. Doesn't scale past two agents.

## Connection to the spawn flow (dv_0556)

The spawn-new-agent design (dv_0556, 2026-05-03) wires:

> hospital-nametag placeholder uid (agent-newborn-YYYYMMDDhhmmss) → first-session naming → uid rename → install → cron registration → first heartbeat fires.

The "cron registration" step in that flow is **exactly the gap this doc closes**. Without `sync-cron.sh`, dv_0556 can't ship as a one-shot script — it needs Lei in the loop to hand-edit cron at the right moment. With it, the whole spawn pipeline becomes Lei-runs-one-script-then-walks-away.

So this work is a precondition for "spawn flow ready for outside use" rather than a side quest. Recommended sequencing: ship `sync-cron.sh` + the config schema extension first (Phase 1–2 below), THEN start the spawn implementation epic with `sync-cron.sh` as a building block.

## Proposed design

### Layer 1 — `~/.engram/config.json` schema extension

Source of truth for everything runtime-tunable. Already exists; add a `cadence` block (and migrate the existing scattered fields underneath their natural namespaces if the cleanup pays off — TBD):

```json
{
  "memory": {
    "tier1_max_nodes": 60,
    "tier2_max_nodes": 600,
    "current_turn": 1247
  },
  "confidence": { "predictive_confidence_cap": 0.60, ... },
  "trust_pool": { ... },
  "cadence": {
    "heartbeat_minutes_per_hour": [0, 30],
    "heartbeat_skip_hours": [20, 21],
    "sleep_local_time": "20:30",
    "sleep_protected_window_minutes": 60,
    "counterpart_offset_minutes": 15,
    "drowsiness_caution_pct": 80,
    "drowsiness_urgent_pct": 90
  }
}
```

Field semantics mirror what's currently hand-rolled into Borges's crontab and the drowsiness defaults shipped in PR #9. Defaults match what the two of us run today; user can tune per-agent.

### Layer 2 — `tools/sync-cron.sh`

Pure primitive. Reads `~/.engram/config.json`, renders a crontab from `templates/crontab.template`, installs it via `crontab -` for the current user. Idempotent: re-running just rewrites the crontab from current config.

Sketch:

```bash
#!/usr/bin/env bash
# tools/sync-cron.sh — render + install crontab from ~/.engram/config.json.
set -euo pipefail
CONFIG="${ENGRAM_HOME:-$HOME/.engram}/config.json"
TEMPLATE="$ALPHA_DIR/templates/crontab.template"

# Use python (shipped with alpha already) to render the template
"$PYTHON_BIN" "$ALPHA_DIR/tools/render_cron.py" \
    --config "$CONFIG" --template "$TEMPLATE" --agent-name "$AGENT_NAME" \
  | crontab -

echo "✓ Crontab installed from $CONFIG"
```

`render_cron.py`: small stdlib script that takes the config + template and emits the final crontab text. No live mutation until the final pipe — supports `--dry-run` to print without installing.

The template uses jinja-style placeholders for the cadence fields:

```
{HEARTBEAT_HOURS_A} {HEARTBEAT_HOURS_B} * * * {ENGRAM_HOME}/tools/heartbeat.sh ...
{SLEEP_MINUTE} {SLEEP_HOUR} * * * {ENGRAM_HOME}/tools/sleep.sh ...
```

Template lives in alpha; the script substitutes from `config.json` + env.

### Layer 3 — Settings tab in viz_server

A fourth top-level UI alongside `/` (graph), `/health` (dashboard), and a new `/settings`. Reads `/api/config`, edits it inline, POSTs to `/api/config` to save, with an "Apply schedule changes" button that shells out to `tools/sync-cron.sh`.

Critical UX rule: **the web UI never silently mutates the OS crontab.** Saving config.json is one button (cheap, no consent needed). Re-rendering cron is a separate button (explicit, shells out, shows the diff between old and new crontab before applying). Saving config WITHOUT applying is fine — the new schedule takes effect on next sync, not on next save.

Read-only fields and ranges should be obvious in the UI: e.g., tier sizes are integers ≥ 1, drowsiness thresholds are 0–100, sleep_local_time is `HH:MM`, sleep_protected_window_minutes ≥ heartbeat skip. Validate on the client AND server (re-validate before write — never trust the client).

Auth: same model as today. viz_server binds 127.0.0.1, no auth, anyone with localhost shell access can edit. Acceptable for current threat model. Document loudly that this surface should not be exposed beyond loopback without rethinking.

## Phases

| Phase | Work | Owner | Estimate |
|-------|------|-------|----------|
| 1 | Config schema extension + `~/.engram/config.json` migration story (existing keys preserved, new `cadence` block added with defaults) | Borges | 30 min |
| 2 | `templates/crontab.template` + `tools/render_cron.py` + `tools/sync-cron.sh` + sandbox tests | Borges | 1.5 hr |
| 3 | `install.sh` integration: call `sync-cron.sh` at end of install with default cadence, gated behind a `--no-cron` opt-out flag for headless setups | Borges | 30 min |
| 4 | viz_server `/api/config` GET/POST endpoints + `/settings` UI tab | Borges | 2 hr |
| 5 | "Apply schedule changes" button: shell out to sync-cron, show diff, require explicit confirm | Borges | 1 hr |
| 6 | Doc: README section on the settings tab, USER_GUIDE on cadence tuning rationale | Borges | 30 min |

Total: ~6 hr focused work. Splittable across sessions; phases 1–3 are valuable shipping units even without the UI.

## Decisions + open questions

1. **Config schema migration → hard-cut, with prod-safety guards.** (Decided 2026-05-04 evening, Lei.) Migration script renames/reshapes existing keys in one pass. Lei's caveat: must not break prod for Borges or Mneme. Concrete safeguards:
   - Dry-run mode prints the planned diff (old → new) without writing.
   - Live mode writes the new `config.json` AND keeps the prior version at `config.json.pre-migration-<timestamp>`. Rollback is a one-liner if anything misbehaves.
   - Migration runs once per agent — guarded by a `schema_version` field in the post-migration file.
   - Sandbox-tested against fixture configs covering the live Borges + Mneme shapes before touching prod.
2. **Drowsiness thresholds vs context-tracker constants.** Drowsiness defaults are currently Python constants in `~/.engram/hooks/context_tracker.py` (per PR #9). Moving them to `config.json` means hooks must read config at fire time. Acceptable cost — config.json is already read by other paths — but worth a note in the migration script.
3. **`sync-cron.sh` behavior on existing crontabs → splice via markers.** (Decided 2026-05-04 evening, Lei: "if a user has existing crontab lines, use splice is natural.") Use `# BEGIN ENGRAM` / `# END ENGRAM` markers around the ENGRAM block. Re-running `sync-cron.sh` only mutates lines between the markers; pre-existing user crontab entries (backups, dotfile sync, anything else) are preserved verbatim. First run on a host without markers inserts them at the end of the existing crontab.
4. **One-stop scope creep.** Lei's framing was "make viz-server a one-stop place for all ENGRAM-related check and config needs." This doc covers cadence + tunables. Future expansion: focus-set management UI, edge-history browser, manual node retraction UI, etc. **Decision for now:** ship the cadence + tunables surface first, treat further consolidation as separate epics.
5. **Spawn flow integration timing.** Should we ship Phases 1–3 (`sync-cron.sh` primitive) and immediately fold into the spawn epic, OR ship 1–6 first and then do spawn? Lean: 1–3 → spawn (unblocks the bigger user-facing win), then come back for 4–6.

## Files touched (for the eventual PRs)

**PR-A (Phases 1–3): cron primitive + install integration**
- `~/.engram/config.json` schema (no source-of-truth file in repo; sample lives at `templates/config.json.example`)
- New: `templates/config.json.example` (extended)
- New: `templates/crontab.template`
- New: `tools/render_cron.py`
- New: `tools/sync-cron.sh`
- `install.sh` — add `sync-cron.sh` call at end, `--no-cron` opt-out flag
- `install-gemini.sh` — same
- `bootstrap.py` — extend default config.json render with `cadence` block

**PR-B (Phases 4–5): UI**
- `viz_server.py` — `/api/config` GET/POST handlers, `/settings` HTML, "Apply" button shell-out
- `tools/render_cron.py` — `--diff` mode

**PR-C (Phase 6): doc**
- `README.md` — settings tab + sync-cron section
- `USER_GUIDE.md` — cadence tuning rationale + protected-window doc

## Sequence after this doc commits

1. ENGRAM observation pointing at this doc (when work begins, not tonight — the active-builds slot is empty post-viz-server-shipped, this would be the next pin).
2. Tomorrow / fresh context: Lei reviews scope; if green, start Phase 1.
3. After Phase 3 ships (cron primitive done): pivot to spawn-flow implementation epic with `sync-cron.sh` as a building block.
4. Loop back to Phases 4–6 (UI surface) after spawn flow is shipping.
