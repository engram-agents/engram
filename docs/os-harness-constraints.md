# OS / harness constraint matrix

**What breaks where, and how to check it yourself.**

ENGRAM runs across more than one OS and more than one agent harness. Most code
is portable; a small set of things are not, and those constraints have so far
lived in three places that don't talk to each other — one platform's setup doc,
the reviewer's memory, and the incident that taught us. This file is the single
place to look, and the checker beside it is how you find out rather than
remember.

> **Read the "Verified by" column before you trust a row.** Every row says whose
> seat the claim comes from. `first-hand` means someone ran the check on that
> platform. `doc-sourced` means it came from a setup doc and nobody re-ran it for
> this table. A doc-sourced row is a *lead*, not a fact — one such row was already
> stale when this file was written (see "Known-stale" below).

---

## How to check, rather than remember

```bash
# Find bash-4-only constructs in shipped shell scripts (stock macOS is bash 3.2):
python3 tools/os-harness-check.py

# Exit 0 = clean. Exit 1 = violations printed with file:line. Exit 2 = the
# detector failed its own self-test and refused to report anything.
python3 tools/os-harness-check.py --self-test   # run just the detector check
```

**The `--self-test` is not decoration.** A scanner that finds nothing is
indistinguishable from a scanner whose patterns are wrong, so the checker
requires each pattern to find a known-positive *and* to reject the portable
bash-3.2 equivalent — the second half catches a pattern broadened until it
matches everything. `main()` runs it first and exits 2 rather than report a
result from a detector it cannot trust.

**What is scanned:** git-tracked files that either end in `.sh` **or** have no
extension and carry a `bash`/`sh` shebang. The extensionless half is not
hypothetical tidiness — an earlier `*.sh`-only version of this checker left
`tools/agentctl-mac`, the macOS-targeted script and the exact thing this tool
exists to protect, permanently outside the scan while printing "clean".

**Exemptions are declared in the file, not in the checker.** A script that is
Linux-host-only by design puts this in its first 20 lines:

```bash
# os-harness-check: linux-only  (reason)
```

The directive lives with the code so a reviewer editing that file sees it; an
allowlist inside the checker would be invisible from the file it exempts. Every
exemption is **printed on every run**, clean or dirty — a silently skipped file
is precisely the coverage bound that made the `*.sh` glob a defect.

---

## Axis 1 — operating system

| Constraint | Linux / WSL2 | macOS | Verified by |
|---|---|---|---|
| **Shell** | bash 5.2.21 | **bash 3.2.57** — no `mapfile`/`readarray`, no `declare -A`/`local -A`, no `local -n` nameref, no `${var,,}`/`${var^^}` | Linux: **first-hand** (this host, `$BASH_VERSION`). macOS: **doc-sourced** (`docs/macos-agent-setup.md`) |
| **sed** | GNU sed 4.9 | BSD sed — `sed -i` takes a mandatory suffix arg | Linux: **first-hand**. macOS: **doc-sourced** |
| **Service manager** | `systemd --user` | `launchd` (`launchctl`) | Linux: **first-hand** (`launchctl` absent here). macOS: **doc-sourced** |
| **viz setup** | `operator-setup-viz.sh` | `operator-setup-viz.sh` — **supported since #1550 (2026-08-03)**, `uname -s` → Darwin branch renders a LaunchAgent plist | **first-hand**: the Darwin branch is present in the shipped script and in the `v0.3.2-rc1` tag |
| **`readlink -f`** | supported | supported on modern macOS | Linux: **first-hand**. macOS: **doc-sourced** |
| **TCC (privacy prompts)** | n/a | keep `~/.engram/` and the repo **out of** `~/Documents`, `~/Desktop`, `~/Downloads` — a non-interactive job hangs on the consent prompt | **doc-sourced** |
| **Multi-agent tooling** (`agentctl`, `agent-bootstrap`) | works | does not port (epic #750) — use the operator runbook | **doc-sourced** |
| **Shared dir for `baton`/`ia`** | `/home/agents-shared/` | `/Users/Shared/…`, setgid + ACL; best-effort | **doc-sourced** |
| **Audio (soma / voice)** | **WSL2 has no native sink** — route via `PULSE_SERVER=unix:/mnt/wslg/PulseServer`; plain `aplay`/default `pactl` find nothing | untested | WSL2: **first-hand** |

**Why bash 3.2 is the sharpest row.** It is the only one that fails *silently at
the wrong moment*: shipped installer scripts run under `set -euo pipefail`, so the
first bash-4 construct aborts the script mid-run rather than at parse time — after
partial work, before the step you cared about. That is the failure #1797 would
have produced on a macOS Codex seat.

**Current status: clean.** 36 shell scripts scanned, 1 exempt
(`tools/agentctl`, Linux-host-only by design — its bash-4.3 namerefs are
intentional and must not be "portability-fixed"; the macOS path is
`tools/agentctl-mac`, which *is* scanned). Zero violations across six construct
patterns.

That negative carries a positive control: the same scanner finds **18** hits on
PR #1797's head (`mapfile` ×10, `local -A` ×4, `local -n` ×4).

---

## Axis 2 — agent harness

| Constraint | Claude Code | Codex | Verified by |
|---|---|---|---|
| **`--target` in use today** | yes | **Linux-only in practice** — no macOS Codex seat currently runs it | **user-stated** (Lei, 2026-08-05) |
| **What activates a rebuilt plugin** | hooks / skills / tools are **source-live** (live the moment the build finishes, no command); **agents** are cache-loaded → need `/plugin marketplace update` + `/reload-plugins`; **`server.py`** is process-pinned → needs `/mcp` reconnect | `codex plugin add` copies into Codex's own cache; cache-refresh based | Claude Code: **first-hand** (2026-08-05 — a rebuild put dev-tip hooks live on this seat with no command run). Codex: **inferred**, not live-fire verified |
| **A plugin upgrade is NOT atomic** | correct — once the rebuild runs, the source-live subset is already executing; there is no "staged" state and no documented abort path other than rebuilding from the old ref | assumed same | **first-hand** |
| **`alpha_sha` proves installation, not execution** | `engram_stats.version.alpha_sha` reads the `.deployed-version` **marker file**, not the running process — a seat can report a correct new sha while its MCP server still runs old code | same | **peer-attested** (Aleph, 2026-07-03: correct marker, server still on the pre-build PID) |
| **Monitors are session-scoped** | a full restart kills armed Monitors; `/reload-plugins` does not | same | **first-hand** |

**Consequence worth stating once:** because the marker is not the process, "which
build is this seat running?" needs **two** checks, not one — the sha *and* an
activation probe (e.g. calling a tool that exists only in the new version, whose
success can only mean the new process is live).

---

## Known-stale entries found while writing this

Recorded rather than quietly fixed, because the pattern matters more than the
instance:

- `docs/macos-agent-setup.md`'s quick-reference still reads *"Viz —
  `operator-setup-viz.sh` Linux-only (#402); hand-authored launchd plist on Mac."*
  **That has been false since 2026-08-03**, when #1550 shipped the Darwin/launchd
  branch (verified present in the shipped script and in the `v0.3.2-rc1` tag).
  It tells a macOS operator to hand-author a plist the script now writes.

The general shape: **a platform constraint doc goes stale silently, because the
people who could notice are on the other platform.** That is the argument for
this file carrying a "Verified by" column and a runnable checker rather than
prose alone.

---

## Adding a row

1. **Run the check on the platform in question**, or mark the row `doc-sourced`
   and say which doc. Do not promote doc-sourced to first-hand because it sounds
   right.
2. **If the constraint is mechanically detectable, add it to
   `tools/os-harness-check.py`** with a known-positive fixture. A constraint that
   only lives in this table is one someone has to remember.
3. **Name the failure mode, not just the fact.** "bash 3.2" is a fact; "aborts
   mid-installer under `set -euo pipefail`, after partial work" is what makes a
   reviewer act.
