# What ENGRAM's install creates, and what uninstall removes today (#4446 step 1)

This is the contract for #4446 step 2: "uninstall removes everything install created". Each row is one artifact that an install script creates or modifies. The columns say which script does it, on which platform, and whether `uninstall-local-marketplace.sh` removes it today. Step 2's tests take one cell per row from this table.

- **Read at:** `dev` 20df8edc (2026-11-07). The line numbers below are at that commit.
- **No code change** is made by this document.
- **Context:** external security audit epic #4439, Medium finding 11. It reports a LaunchAgent with `RunAtLoad`+`KeepAlive` (`setup-surface-daemon-service.sh`) that uninstall leaves behind, together with most other install artifacts.

## What was read, and how (so the table's limits are visible)

**Read whole:**
- `src/engram/tools/install.sh` (666 lines)
- `src/engram/tools/uninstall-local-marketplace.sh` (208 lines)
- `src/engram/bootstrap.py` lines 186-383 and 444-730 (its `main()`, `substitute_template`, `merge_codex_config_toml`, `_render_codex_target` and `_set_cleanup_period_days`)
- `src/engram/tools/install-local-marketplace.sh` lines 400-1016 (the assemble, marker, config, venv, pre-warm and registration sections)

**Read by write sites:**
- `install-local-marketplace.sh` lines 1-399, `setup-surface-daemon-service.sh` (918 lines) and `operator-setup-viz.sh` (438 lines).
- For these I searched for every line matching `mkdir|ln -s|cp|mv|cat >|> "$|>>|tee|install -d/-m|rsync|systemctl|launchctl|chmod|chown|touch|rm -|git clone|-m venv|pip install|claude plugin|codex plugin|crontab|plutil|defaults write|settings.json|write_text|json.dump|symlink|shutil.|.mkdir(|open(...w/a)`, then read each hit in context, plus the unit and plist naming and the legacy-unit transition.
- A write done some other way, for example by a Python helper these scripts call, is outside that search. `tools/check_stale_processes.py`, `install_selfcheck.py` and `set_forum_url.py` were not read in the first version. The reviewer-fairy read their docstrings and every write-shaped line: `set_forum_url.py` writes only `config.json` (C7), `check_stale_processes.py` writes nothing, and `install_selfcheck.py` starts the MCP server, which is how C14 entered the table.

**Templates read for paths, restart policy and log targets:**
- `engram-surface-daemon.{service,plist}.template`
- `engramd-owner.{service,plist}.template`
- `engram-viz-user.service.template`
- `engram-workbench-user.service.template`
- `engram-viz-server.plist.template`

**Host-CLI side effects** (what `claude plugin marketplace add / install / enable` writes) are not ENGRAM code. The rows below give them as seen on one installed Linux seat (n=1, one Claude Code version). Whether `claude plugin marketplace remove` undoes each of them is **not measured**.

**Not read:**
- `start-engram-daemon.sh` and `start-engramd-owner.sh` were read only for the runtime file paths they name (section D).
- See "Out of scope" at the end.

## The uninstall today

`uninstall-local-marketplace.sh` does exactly three things (lines 122-182):

1. Runs `claude plugin marketplace remove engram-local`, or `codex plugin remove engram@engram-local` with `--target codex`.
2. Removes `$ENGRAM_HOME/marketplace/` (`rm -rf`).
3. Removes `$ENGRAM_HOME/.deployed-version`.

It states that it preserves `$ENGRAM_HOME` data, the venv and `~/.claude/CLAUDE.md` (lines 12-19). It stops no service, unloads no LaunchAgent and removes no symlink.

## A. Services (these keep running after uninstall)

| # | Artifact (path) | Created by | Platform | Restart policy | Removed by uninstall today |
|---|---|---|---|---|---|
| A1 | `~/.config/systemd/user/engram-surface-daemon-<home_id>.service`, enabled and started (`systemctl --user enable --now`) | `setup-surface-daemon-service.sh` (lines 665, 677, 713/718, 724-725). Called by `install.sh` step 8 unless `--no-recall-service` (default on); step 8 skips it with only a WARN when `systemctl` is missing or there is no user session bus (`install.sh` lines 596-610). Also called by the first-session skill when the user picks the service | Linux | `Restart=always`, `RestartSec=5s`; `ExecCondition` tests only that `$ENGRAM_HOME` exists | **NOT** |
| A2 | `~/.config/systemd/user/default.target.wants/engram-surface-daemon-<home_id>.service` (the enable symlink) | `systemctl --user enable` in A1 | Linux | n/a | **NOT** |
| A3 | `~/Library/LaunchAgents/com.engram.surface-daemon.<home_id>.plist`, loaded (`launchctl bootstrap gui/<uid>`) | `setup-surface-daemon-service.sh` (lines 752, 764-766, 774/780, 879). Same callers as A1 (install.sh skips it with a WARN when `launchctl` is missing). **This is the audit's finding 11** | macOS | `RunAtLoad` + `KeepAlive {PathState: $ENGRAM_HOME exists}` (#4305; the template's own comment calls it best-effort). Uninstall keeps `$ENGRAM_HOME`, so the condition stays true | **NOT** |
| A4 | `~/.config/systemd/user/engramd-owner-<home_id>.service`, enabled and started | `setup-surface-daemon-service.sh --owner` (lines 669, 677, 724-725). Run by the engram-upgrade skill (b1) or by hand. **Not** run by `install.sh` | Linux | `Restart=on-failure`, `RestartSec=10s` with `RestartSteps=5` / `RestartMaxDelaySec=300s` backoff, `StartLimitIntervalSec=0` (never stops retrying), `RestartPreventExitStatus=4`; no home condition | **NOT** |
| A5 | `~/.config/systemd/user/default.target.wants/engramd-owner-<home_id>.service` | `systemctl --user enable` in A4 | Linux | n/a | **NOT** |
| A6 | `~/Library/LaunchAgents/com.engram.engramd-owner.<home_id>.plist`, loaded | `setup-surface-daemon-service.sh --owner` (lines 756, 774/780, 879) | macOS | `RunAtLoad`; `KeepAlive {SuccessfulExit: false}`, `ThrottleInterval 60`; no home condition | **NOT** |
| A7 | `~/.config/systemd/user/engram-viz-user.service`, enabled and restarted | `operator-setup-viz.sh` (lines 260-285). Called by `install.sh --viz-service` (Linux only) and by the first-session skill | Linux | `Restart=on-failure` | **NOT** |
| A8 | `~/.config/systemd/user/engram-workbench-user.service`, enabled and restarted, only when `viz.workbench` imports | `operator-setup-viz.sh` (lines 201-202, 296-314) | Linux | `Restart=on-failure` | **NOT** |
| A9 | `default.target.wants/` symlinks for A7 and A8 | `systemctl --user enable` | Linux | n/a | **NOT** |
| A10 | `~/Library/LaunchAgents/com.engram.viz-server.plist`, loaded | `operator-setup-viz.sh` (lines 366-406). `install.sh --viz-service` does **nothing** on macOS (line 630), but the script itself installs this when run directly or from the first-session skill | macOS | `RunAtLoad` + `KeepAlive` (unconditional) | **NOT** |
| A11 | `~/Library/LaunchAgents/com.engram.viz-server.plist.bak-pre-setup-<UTC>` | `operator-setup-viz.sh` (lines 384-388), when a plist already existed | macOS | n/a | **NOT** |

**Where the services point after uninstall (from the unit text; not run):**
- **When the setup script runs from the installed plugin tree**, the rendered units and plists launch files **inside `$ENGRAM_HOME/marketplace/`**. That is what `install.sh` (lines 597, 614, 626, from `PLUGIN_ROOT` at line 208), the engram-upgrade skill and the first-session skill do. That is the tree uninstall removes. For example, this seat's A1 `ExecStart` is `~/.engram/venv/bin/python3 ~/.engram/marketplace/plugins/engram/hooks/engram-surface-daemon.py`.
- Each setup script takes its paths from its own real location (`setup-surface-daemon-service.sh` lines 233-263; `operator-setup-viz.sh` lines 97-102). So a unit is **not** inside the marketplace when:
  - the script was run from a source checkout (the unit points at the checkout, which uninstall does not delete, and the service keeps running healthily);
  - `ENGRAM_HOOKS_DIR` was set (line 258);
  - `--allow-path` let through a temp or worktree path (lines 383-390);
  - the first-session skill's fallback path under `~/.claude/plugins/cache/` was used.
  Step 2 needs a separate cell for the checkout case.
- A1's only start condition (`ExecCondition`) and A3's only keep-alive condition (`PathState`) are that `$ENGRAM_HOME` exists, and uninstall keeps `$ENGRAM_HOME`. So after today's uninstall, A1 (`Restart=always`) and A3 are left supervising a path that no longer exists. That matches the crash-loop the audit's critic describes.
- The owner, viz and workbench units (A4, A6, A7, A8, A10) have **no** home condition at all. A4 never stops retrying by design (`StartLimitIntervalSec=0`); its exit-4 escape is unreachable after uninstall (see A4 below).
- **No Linux unit here stops itself after uninstall (A1, A7, A8 measured as below, A4 by its template). The macOS plists read the same way from their text, not run. Each must be stopped before its tree goes.** That is the property step 2 needs, not only the removal.
  - **A4** retries without limit by design (`StartLimitIntervalSec=0`). Its only no-restart code is 4 (`RestartPreventExitStatus=4`). After uninstall the missing `engramd_owner.py` makes CPython exit 2 (the template's comment on exit 5 names that code), and exit 4 can only come from the deleted script. So the "unless it exits 4" escape is unreachable.
  - **A1, A7 and A8** use `RestartSec=5s`, which stays under systemd's default limit of 5 starts per 10 s. Measured on one Linux host (systemd 255, defaults 10 s / 5) with two throwaway user units running `ExecStart=/bin/false`:
    - with `RestartSec=5s`: 8 restarts in 45 s, still `auto-restart`;
    - the control with `RestartSec=100ms`: `failed` after 5.
  - **On macOS**, A3 keeps a true `PathState`, A6 restarts on any unsuccessful exit (throttled to 60 s), and A10 has unconditional `KeepAlive`. This is from the plist text and was not run.
  - **At login**, the enable symlinks (A2, A5, A9) start the units again, so step 2 must remove them as well as the unit files.

The **legacy fixed-name units** `engram-surface-daemon.service` and `com.engram.surface-daemon` (before #4305) are removed by the setup script itself when it installs the keyed one for the same home (lines 685-700, 811-839). Uninstall does not touch them either. They are a live case in the field: the colleague review of #4448 found the unkeyed `com.engram.surface-daemon` label loaded on a Mac, for another account.

**On a headless Mac** (no GUI session), `launchctl print gui/<uid>` answers "Domain does not support specified action". A3 and A6 can then sit on disk **without ever being loaded**, while the daemons run hook-started (see D). Step 2 needs a "plist present, not loaded" cell for its bootout path. This is from the colleague's seat, read-only, n=1.

## B. Files and links outside `$ENGRAM_HOME`

| # | Artifact | Created or modified by | Platform | Removed by uninstall today |
|---|---|---|---|---|
| B1 | `~/.claude/CLAUDE.md`, **overwritten** with the rendered template | `bootstrap.py` `substitute_template` (lines 190-197, called at 716-721) | both | **NOT** (kept on purpose; restore is #4440's deliverable) |
| B2 | `~/.claude/CLAUDE.md.pre-engram`, a once-only backup | `install.sh` step 6 (lines 518-538). The plugin/bootstrap path makes **none** (#4440) | both | **NOT** |
| B3 | `~/.claude/settings.json`: key `cleanupPeriodDays: 36500`, added only if absent. A missing **or unparsable** `settings.json` is treated as `{}`, so the file is rewritten as just that key, and the whole file is re-serialised with `indent=2` | `bootstrap.py` `_set_cleanup_period_days` (lines 377-400, called at 723) | both | **NOT** |
| B4 | `~/.claude/settings.json`: `extraKnownMarketplaces.engram-local` (directory source → `$ENGRAM_HOME/marketplace`) | `claude plugin marketplace add` (install-local-marketplace.sh line 974). Seen on one seat | both | **unmeasured** |
| B5 | `~/.claude/settings.json`: `enabledPlugins["engram@engram-local"] = true` | `claude plugin enable` (install.sh line 572). Seen on one seat | both | **unmeasured** |
| B6 | `~/.claude/plugins/known_marketplaces.json`: the `engram-local` entry | `claude plugin marketplace add`. Seen on one seat | both | **unmeasured** |
| B7 | `~/.claude/plugins/installed_plugins.json`: the `engram@engram-local` entry | `claude plugin install` (install.sh line 571). Seen on one seat | both | **unmeasured** |
| B8 | `~/.claude/plugins/cache/engram-local/engram/<version>/`, one directory per installed version, never pruned (5 on this seat) | Claude Code on install and update (agents load from here) | both | **unmeasured** |
| B9 | `~/.claude/plugins/data/engram-engram-local/` | Claude Code (per-plugin data directory). Seen on one seat | both | **unmeasured** |
| B10 | `~/.codex/config.toml`: keys `instructions = """…"""` and `experimental_compact_prompt_file`, plus a `# model =` comment line. Prepended; strips any earlier `instructions` block, including a user's own (#4440) | `bootstrap.py` `merge_codex_config_toml` (lines 221-291) | both (Codex target) | **NOT** |
| B11 | `~/.codex/AGENTS.md`, overwritten | `bootstrap.py` (lines 368-373) | both (Codex) | **NOT** |
| B12 | `~/.codex/config.toml` plugin stanza, and `~/.codex/plugins/cache/engram-local/engram/<version>/` | `codex plugin add engram@engram-local` (install-local-marketplace.sh line 856) | both (Codex) | by `codex plugin remove` (uninstall line 129); what that removes is **unmeasured** |
| B13 | Compatibility symlinks `~/.codex/plugins/cache/engram-local/engram/*-dev.*` → the newest cache | install-local-marketplace.sh (lines 891-921) | both (Codex) | inside the B12 cache parent: removed with it only if `codex plugin remove` removes that parent (**unmeasured**) |
| B14 | `<codex cache>/.mcp.json`, patched with an absolute `command` | install-local-marketplace.sh (lines 924-936) | both (Codex) | with the cache, if `codex plugin remove` removes it (**unmeasured**) |
| B15 | `~/.codex/agents/engram-*.toml` (copied, then rewritten in place) | install-local-marketplace.sh (lines 941-961) | both (Codex) | **NOT** |
| B16 | `~/.local/bin/engram-viz-setup`, a symlink to the setup script inside the marketplace tree (dangles after uninstall) | `operator-setup-viz.sh` (lines 329-330, 414-415) | both | **NOT** |
| B17 | The embedding model under the Hugging Face cache (`$HF_HUB_CACHE`, else `$HF_HOME/hub`, else `~/.cache/huggingface/hub`), e.g. `models--sentence-transformers--all-MiniLM-L6-v2` | install-local-marketplace.sh pre-warm (lines 574-651, called at 711) | both | **NOT**. Shared cache: other software may use the same model, so removing it is a decision, not a default |
| B18 | Claude Code itself (`~/.local/bin/claude` and its data), only with `--install-claude-code` | install.sh step 2 (Anthropic's installer, lines 416-430) | both | **NOT**. Third-party software; out of ENGRAM's uninstall by nature |

## C. Inside `$ENGRAM_HOME` (default `~/.engram`) and the venv

| # | Artifact | Created or modified by | Kind | Removed by uninstall today |
|---|---|---|---|---|
| C1 | `$ENGRAM_HOME/marketplace/` (assembled plugin tree and `.claude-plugin/marketplace.json`) | install-local-marketplace.sh (lines 425-460) | install | **YES** (line 165) |
| C2 | `$ENGRAM_HOME/.deployed-version` | install-local-marketplace.sh (lines 485-495) | install | **YES** (line 177) |
| C3 | `$ENGRAM_HOME/tools` → `<plugin root>/tools` (symlink; dangles after uninstall) | `bootstrap.py` (lines 565-593), plugin mode | install | **NOT** |
| C4 | `$ENGRAM_HOME/hooks` → `<plugin root>/hooks` (symlink; dangles after uninstall) | `bootstrap.py` (lines 607-637), plugin mode | install | **NOT** |
| C5 | `$ENGRAM_HOME/tools.scatter-backup/` and `hooks.scatter-backup/`, only when real directories were there | `bootstrap.py` (lines 577-578, 615-616) | install (migration) | **NOT** |
| C6 | `~/.engram/venv/` (always under `$HOME/.engram`, independent of `ENGRAM_HOME`) | install.sh step 4 (lines 451-470); kept in sync by install-local-marketplace.sh (lines 700-751) | install | **NOT** (kept on purpose, uninstall line 18) |
| C7 | `$ENGRAM_HOME/config.json`: created, or `install_tier` written; `forum.url` with `--forum-url` | install.sh step 3 (lines 218-268, 435-445); install-local-marketplace.sh (lines 519-538); `set_forum_url.py` (install.sh lines 635-641) | user data | **NOT** (kept on purpose) |
| C8 | `$ENGRAM_HOME/knowledge.db` (and its `-wal`/`-shm`) | `bootstrap.py` seed graph | user data | **NOT** (kept on purpose) |
| C9 | `$ENGRAM_HOME/warm-briefing.md` | `bootstrap.py` (lines 693-698) | user data | **NOT** (kept on purpose) |
| C10 | `$ENGRAM_HOME/seed-manifest.json` | `bootstrap.py` (lines 682-690) | install record | **NOT** |
| C11 | `$ENGRAM_HOME/first-run-pending` (until the first session clears it) | `bootstrap.py` (lines 703-708) | install record | **NOT** |
| C12 | `$ENGRAM_HOME/codex-compact-prompt.md` | `bootstrap.py` (lines 321-323), Codex target | install | **NOT** |
| C13 | `$ENGRAM_HOME/install-logs/<UTC>/`, only on a failed `install.sh` run | install.sh `_on_exit` (lines 170-179, 398) | install record | **NOT** |
| C14 | `$ENGRAM_HOME/.gitignore`; `$ENGRAM_HOME/.git/` (`git init`, local `user.email`/`user.name`); `.git/hooks/reference-transaction` (the rewrite guard) | The first MCP server start: `engram_core._init_git` and `_install_rewrite_guard` (`engram_core.py` lines 3069-3096, 3118-3142). `install.sh` step 9 triggers it through `install_selfcheck.py`'s `mcp-handshake` (`install_selfcheck.py` lines 29-31), unless a live server holds the ready marker (lines 443-446). Not with `ENGRAM_DISABLE_GIT`. The same start also creates `$ENGRAM_HOME/db-backup/.pending/` (`server.py` lines 8958-8970 → `engram_snapshot_worker.start_worker` → `_pending_dir`, lines 99-102, 593-595) and `$ENGRAM_HOME/logs/sessions/` (`server.py` line 8997 → `engram_log_emitter.Emitter`, lines 118-119); whether they hold files after a handshake is not checked | data history | **NOT** |
| C15 | `$ENGRAM_HOME/knowledge.db.setup.lockdir/` (a 0700 directory, not removed when the lock is released; the holder pid is written to `holder` inside it) | `engram_core._acquire_db_setup_lock` (`SETUP_LOCK_SUFFIX`, `engram_core.py` lines 578 and 884-901). It is called from `_get_db` (line 6831) on a process's first store setup. On a fresh install the first caller is `bootstrap.py`'s seed graph (install.sh step 6, the same path as C8); otherwise the first MCP server or hook | data-dir lock | **NOT** |

**Fresh-DB condition.** `install.sh` runs `bootstrap.py` only when `knowledge.db` is absent or has 0 nodes (lines 501-515). So the bootstrap rows are written on a fresh install only, and are not refreshed by a re-install: B1, B3, B10, B11, C3, C4, C5, C8, C9, C10, C11, C12.

**Not rows, for step 2's fixtures:**
- `install.sh` step 1 creates the `$CLAUDE_HOME` and `$ENGRAM_HOME` directories (lines 373, 379).
- pip writes its own cache (`~/.cache/pip` by default).
- The self-check's `mcp-handshake` restores or removes `mcp-tools-ready.json` after itself (`install_selfcheck.py` lines 411-434), so it leaves no row.

## D. Runtime files that the services and hooks write into `$ENGRAM_HOME` (not install, listed for step 2's stop-then-remove)

These paths are named in the unit templates and the two launchers. The running code writes them; no install script writes them. They live in `$ENGRAM_HOME`, which uninstall keeps by design. They are listed because a service stopped in step 2 leaves them stale:

- `surface-daemon.log`
- `recall-daemon.sock`
- `recall-daemon.pid`
- `daemon-launch-attempt`
- `engramd-owner.log`
- `engramd.pid`
- `run/engramd.sock`
- `knowledge.db.maint.lockdir/`
- `viz-server.log` and `viz-server.err` (macOS viz)

The pid and socket files above can name **live processes with no unit at all**. The recall daemon and the owner are hook-started when no supervisor is registered (`start-engram-daemon.sh`, `start-engramd-owner.sh`). So stopping only the units in A leaves them running out of `$ENGRAM_HOME/marketplace`. Step 2 must also stop hook-started daemons, found through these files.

## Out of scope here (not user-install artifacts; named so their absence is a choice)

- `src/forum/deploy/install-forum-service.sh`, which installs the operator's forum service.
- `deploy/autostart/` (agent-session units and the macOS seat plists).
- `tools/agent-bootstrap` and `tools/agentctl`, which are root-run seat provisioning.
- `/etc/engram-viz/agents.json`, which the Linux viz unit reads but no script creates.
- Systemd drop-ins an operator writes by hand next to a unit (for example `<unit>.service.d/*.conf`).
- `build/plugin/` inside the source checkout (written by `build-plugin.sh`).

## Counts (over the rows above)

- 44 rows in A, B and C: A 11, B 18, C 15.
- Uninstall removes 2 of them for certain (C1, C2).
- 9 rows depend on host-CLI behaviour that is unmeasured: B4-B9 for Claude Code, B12-B14 for Codex.
- 5 rows are kept on purpose by uninstall's own header or by design: B1, C6, C7, C8, C9.
- The other 28 are not removed today, including every service in A.
- These counts come from `grep` over the table's own rows, not from adding them up by hand.

## Revision history

- 6ab58de5: first version, 42 rows.
- Second version, after the reviewer-fairy round:
  - **Added C14**, `$ENGRAM_HOME/.git`, created by the first MCP server start, which `install.sh` step 9 triggers.
  - **Qualified** the claim that units launch files inside the marketplace: it holds only when the setup script runs from the installed tree.
  - **Scoped** the `$ENGRAM_HOME`-only condition to A1 and A3, and gave A4's full restart policy.
  - **Moved B13 to unmeasured.**
  - **Added** the fresh-DB condition and the fixture notes.
  - **Dropped** an unmeasured start-limit guess, and replaced it with the measured no-self-stop paragraph (requested in the security read on the first version).
- Third version, after the reviewer-fairy's second round:
  - Added the two other directories the same server start creates, inside C14.
  - Corrected C14's self-check line range.
  - Reconciled A4's exit-4 clause.
  - Bounded the no-self-stop headline.
- Fourth version, after the merge of #4448 (b0f21c14), folding the colleague review's three riders (#4446 comment 6715576159):
  - Added C15, the setup lockdir.
  - Noted that the hook-started daemons have no unit.
  - Added the headless-Mac "not loaded" case.
  - Recorded that the legacy label is live in the field.
