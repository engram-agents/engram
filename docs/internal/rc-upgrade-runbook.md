# rc fleet upgrade — per-seat runbook (v0.5.0 rc cycle; committed cross-platform per #2411)

Owner: Kepler (acting commander). Executed per seat by Lei (reload trio) + the seat itself (pre-flight).
Authority: Lei's 2026-08-30 morning plan — one upgrade, at the current target tag (rc1 when the plan was set; see the Target-tag line below); marker migration at upgrade time; sweep waits for the dream master.

**Target tag: `v0.5.0-rc3` (rc2 is superseded — no seat deploys it; upgrade window opens on the maintainer's go). Reload trio = the seat's operator per host. This file is the canonical, host-neutral copy — the per-host `/home/agents-shared/` copy was NOT reachable off-host (measured on the macOS seats, #2411), which is why it lives in the repo now.**

## Step 0 — BEFORE upgrading: record this seat's expected value — DURABLY, FIRST, UNRECOVERABLE AFTER

**Ordering is load-bearing (Ariadne, 13:5xZ): pre-count → write it somewhere that SURVIVES
(a comment on #2411, or this seat's history file — never terminal scrollback) → THEN upgrade →
THEN migrate → THEN compare.** If the migration clears the metadata fields as it writes rows,
a seat that upgrades before counting has permanently lost the number its own validation needs —
silently. And the baseline cannot be reconstructed from the migration's own output (that count
is the thing under test; a migration that skips a class reports a self-consistent number and
passes its own check). The instrument must be one the migration does not touch.
**A seat that has ALREADY upgraded before counting must say so — its night is a different test
and gets labelled, not pooled.**

Count CURRENT nodes with a non-empty metadata `stale_by` — **structured read, key-presence,
never truthiness (under-counts: eats empty lists) and never raw substring (over-counts: matches
prose)** — both naive routes measured wrong, in opposite directions, on two seats (Borges 13:2xZ,
Ariadne 13:4xZ). The canonical instrument is `tools/rc-upgrade-verify.py` in this repo (run: `$HOME/.engram/venv/bin/python3 tools/rc-upgrade-verify.py` from your checkout; `ENGRAM_REPO`/`ENGRAM_HOME`/`ENGRAM_PY` override nonstandard layouts — probes `~/engram-alpha`, `~/src/engram-alpha`, `~/GitHub/engram-alpha`, fails LOUD naming every candidate). Write the number down: **N**.
Known values at rc1: borges N=135 · ariadne N=32 (live tainted_by = 0 both) · kepler/mira/aleph N=0 ·
zephyr N=1 (all supersede) · **luria N=111, the one seat with a live CURRENT `tainted_by` entry —
a known false positive on that seat's own graph, but real coverage of the tainted_by→retract branch
every other seat's data leaves untested**. (Zephyr's N was recorded here as 0 in an earlier draft
of this doc — corrected against his own #2411 comment, which reports N=1. Specific per-seat node
IDs live on #2411 and each seat's own history, not in this shipped doc — see issue #51's
no-shipped-node-IDs policy.)

N is the positive control's EXPECTED VALUE. A control with no expected value cannot fail,
which makes it not a control (Ariadne's rule). Note: a seat can be stale in THREE layers —
agent cache, marketplace source, AND the DB schema itself (Ariadne's seat has no
cascade_markers table at all); the migration + `/mcp` reconnect handles the third.

## Per-seat sequence (FOUR steps — step 4 is the one that historically gets dropped)

1. **Rebuild the source tree at the target tag (see the Target-tag line above)**
   `bash tools/install-local-marketplace.sh --target <claude-code|codex>` from the
   tagged (detached-HEAD) checkout — no flags needed for a normal deploy tag.
   Hooks / skills / tools are live immediately (directory-source topology; hooks re-read per prompt).
   The installer refuses to build from an untagged HEAD (#2586); since this
   runbook always upgrades from a tagged checkout that guard is a no-op here —
   `--allow-untagged` is the override, not part of this flow. **A detached HEAD
   at a deployable tag also no longer needs `--allow-branch`** (#2711 — before
   this fix, the #794 branch-name guard refused every detached-HEAD tag
   checkout, since a detached HEAD's "branch name" is the literal string
   `HEAD`; five seats hit this on the 2026-09-06/07 upgrade and improvised
   `--allow-branch` as a workaround, forum #400). The bare command above is
   now the correct form — no `--allow-branch` needed — as long as the target
   tag is merged into `origin/dev` or `origin/main` (true for every normal
   deploy tag; still needing `--allow-branch` on a fresh deploy tag would mean
   something went wrong with how the tag was cut, not that the flag is
   optional).

2. **Refresh the agent cache** — `/plugin marketplace update engram-local` then `/reload-plugins`.
   AGENTS (dream-master, all fairies) load from the installPath CACHE snapshot, not the source tree.
   A rebuild alone is INERT for them (#1639; re-measured by Ariadne 2026-08-30: both layers 11 days
   stale on her seat, at identical line count with different content — line/size/version checks lie).

3. **Reconnect the MCP server** — `/mcp` reconnect (or full restart).
   The server process is from session start; every new verb needs the new process — and
   **the marker migration (rung 5, the #2324 backfill) fires ITSELF on this first server start**
   (no separate step; measured idempotent — Borges, on a byte COPY of his graph: 135 nodes →
   241 markers, re-run no-op, timestamps recovered). Optional pre-verification: run rung 5
   against a byte copy of your OWN graph first (one command, no copy-paste: `~/.engram/venv/bin/python3 tools/rc-upgrade-verify.py` from your checkout — nobody runs it on another seat's graph). The control gains a second signal: the migration's printed node
   count must equal step 0's independent pre-count N.

4. **POSITIVE CONTROL — run AFTER step 2's cache refresh, before this seat sleeps tonight:**
   Tooling (example-host seats): `/home/agents-shared/ariadne/rc1-postupgrade-check.sh` (read-only; positive-controlled —
   it correctly FAILED both checks pre-upgrade on her seat). **No hand-substitution needed any
   more**: `tools/rc-upgrade-verify.py` BANKS your seat's own baseline to `~/.engram/rc1-baseline.json`
   on a clean run (and refuses to overwrite it), and Ariadne's check reads that file when present
   (falling back to her constants only if it's absent — so run the #2411 script FIRST; a fallback
   read against her numbers on your seat is the wrong comparison). The baseline file also carries
   `expect_migration_line_tonight` — false means a missing migration line on YOUR seat is expected,
   true means its absence is the alarm. Check 1 (agent cache) is seat-independent as-is.
   - `grep -c engram_cascade_frontier <installPath-cache>/agents/engram-dream-master.md` → **non-zero**
     (dev's spec has 4 hits / 627 lines; the stale copy has 0 / 484). Zero after refresh = a real
     failure, not an ambiguous one.
   - **#2411 migration read-back against step 0's N**: post-migration marker rows must account for N.
     On a seat with N>0, **"frontier clear" is a FAILED MIGRATION wearing a passing result** — the
     legacy markers are still in metadata fields where the frontier does not look. Zero rows + N>0 =
     the migration didn't run (or the schema never migrated — check cascade_markers table exists).
   - Tonight's dream record MUST contain the mandatory **"Cascade sweep (mode B)"** section.
     **A missing section is a deployment failure wearing a clean night's clothes — the live test is
     VOID, not passed** (Ariadne's rule, #2229). And a PRESENT section reading "clear" is only a
     real result on a seat whose pre-count N is accounted for.

## Cliff notice (release-note line, ruled on #2411)
Nodes carrying historical stale/taint field-state become plain-revise-REFUSED at upgrade (the #2343/#2396
guards see the migrated markers). The refusal self-explains with next_call/then routing to the frontier.
~135 such nodes on the oldest graph; per-seat counts from the #2411 query.

## Codex seats (Aleph / Clio / Mira-codex)
Same four steps; the reload trio's Claude-specific verbs map per seat owner's runbook. Aleph + Clio are
normal fleet for ENGRAM work (Lin's rule); their upgrades go through their own host's operator.
