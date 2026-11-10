# Boot-time agent auto-start

Systemd units + boot-charge templates that bring agent Claude Code (and, after
a supervised trial, Codex) sessions up automatically at 08:15
America/New_York on a host that already auto-boots at 8:00 America/New_York.

## Design in one paragraph

The host boots at 8:00; world/forum/gateway servers (`umwelt-world.service`
et al — **umwelt-side units, out of scope for this repo**, referenced here
only by name as an `After=`/ordering dependency) come up then. Agent
**sessions** must NOT start at boot — they wait for a separate 08:15 timer,
staggered ~30s apart across agents. Each session runs as its own Linux user
(`agent-<name>`) inside a detached `tmux` session so the operator can `ssh`
in (including over tailscale) and `tmux attach` later. Because the systemd
units run with `User=agent-%i`, **no sudo is available or needed inside
them** — that's the reason `tools/agentctl session` grew an `--as-self` mode
(see that file's own header + PR).

## Files

| File | Role |
|------|------|
| `agent-session@.service` | per-agent template unit (`User=agent-%i`); runs `agent-session-launch.sh %i` |
| `agent-sessions.timer` | daily 08:15 America/New_York trigger, `Persistent=false` (see the unit's own comment for why) |
| `agent-sessions.target` | grouping target the timer triggers; `Wants=` each Claude agent's instance |
| `agent-session-launch.sh` | wrapper: sources the optional host-common `boot-env.common`, then optional per-agent `~/.engram/boot-env`, first, passes the installed charge directory + session cwd to the runner, prepends `~/.local/bin` to PATH, pre-checks the CLI + agentctl `--charge-file` support, starts detached tmux running `agent-session-run.sh` |
| `agent-session-run.sh` | runs INSIDE tmux: detects the platform as the target agent, selects and renders a harness-valid charge, then invokes `agentctl session <name> -d [-r for Claude] --as-self --cd <dir> --charge-file <file>`; logs every launch/exit and holds a failed pane open for autopsy |
| `boot-charge-default.md` / `boot-charge-quiet.md` | common default / quiet charge framing, each with a harness-instruction placeholder |
| `boot-charge-default-claude.md` / `boot-charge-quiet-claude.md` | Claude-specific heartbeat components for the matching common profile |
| `boot-charge-codex.md` | single-sourced Codex heartbeat component, shared by both profiles |
| `mira-codex-session@.service.example` | **RETIRED safety note**: explains why a direct app-server unit must not be installed; Codex uses the ordinary tmux launcher + daemon-managed remote TUI. |

## Install (operator/sudo actions — HOST-OWNER-ONLY)

Everything in this section requires root or an account with `sudo`. Run
these from an `engram-alpha` checkout on the host that will run the agent
sessions.

1. **Install the wrapper + boot-charge templates to a fixed, checkout-independent
   path** (mirrors `tools/agentctl`'s own `/usr/local/sbin/agent-bootstrap`
   convention — the systemd units reference this fixed path, not wherever the
   repo happens to be checked out):

   ```bash
   sudo mkdir -p /usr/local/lib/agentctl/autostart
   sudo cp deploy/autostart/agent-session-launch.sh \
           deploy/autostart/agent-session-run.sh \
           deploy/autostart/boot-charge-*.md \
           /usr/local/lib/agentctl/autostart/
   sudo chmod 0755 /usr/local/lib/agentctl/autostart/agent-session-launch.sh \
                   /usr/local/lib/agentctl/autostart/agent-session-run.sh
   sudo chmod 0644 /usr/local/lib/agentctl/autostart/boot-charge-*.md
   ```

   The launch chain requires an `agentctl` with `--charge-file` support
   (2026-07-25) on the system path — keep `/usr/local/bin/agentctl` in sync
   with `tools/agentctl` when installing:

   ```bash
   sudo install -m 0755 tools/agentctl /usr/local/bin/agentctl
   sudo install -m 0755 tools/agent-bootstrap /usr/local/sbin/agent-bootstrap
   ```

   `agentctl` keeps its audit log and share state in the root-only
   `/var/lib/agentctl` (engram-alpha#4405), never under the agent-writable
   `/home/agents-shared`. The first `sudo agentctl spawn|finalize-name|share|unshare`
   creates it (`mkdir -m 0755`, then verifies owner, mode and that it is not a
   link); it refuses, naming the repair command, if it exists in any other shape.
   To create it by hand: `sudo install -d -m 0755 -o root -g root /var/lib/agentctl /var/lib/agentctl/share-state`.
   `finalize-name` also needs `/home/agents-shared/health` to be root-owned with
   the sticky bit; read its mode first (`getfacl -p /home/agents-shared/health`)
   and add only `+t`, keeping the setgid bit.

   Session cwd, resolved at launch time from `$HOME` (nothing is written at
   rename time): (1) the first line of `~/.engram/boot-cwd` if it names an
   existing directory wins, even if untrusted; (2) else the first existing
   directory among `$HOME/engram-alpha`, `$HOME/umwelt`, `$HOME` that Claude
   Code has trusted (same read-only check as the preflight below); (3) else,
   if none is trusted, the first existing one in that order, plus the
   `ERROR:`. When trust cannot be verified (`CLAUDE_CONFIG_DIR` set, or no
   `python3`) every candidate counts as untrusted, so rule 3 applies. The
   boot-cwd line is normalised first (trailing CR, surrounding whitespace and
   one trailing `/` are stripped; relative paths are not resolved). Every
   launch prints one stderr line `boot cwd: <dir> (from <arm>)`, where
   `<arm>` says why: `boot-cwd`, `$HOME/umwelt, first trusted`, or
   `$HOME/engram-alpha, none trusted`.

   Trust preflight: Claude Code stalls on its "trust this folder?" dialog in a
   directory it has not seen (observed once, ~3 h, #4170). The launcher reads
   `~/.claude.json` (read-only; it never writes or creates it) and, if the
   chosen cwd lacks `projects.<dir>.hasTrustDialogAccepted: true`, prints an
   `ERROR:` line to stderr naming the directory. A missing, unreadable or
   unparseable file, or a missing `python3`, counts as untrusted. If
   `CLAUDE_CONFIG_DIR` is set (after `boot-env`), trust cannot be verified from
   `~/.claude.json`, so the launcher prints an `ERROR:` naming it. It is a
   warning, not a refusal: the session still launches. One-time fix:
   `tmux attach -t agent-<name>` and accept the dialog.
   Measured once (n=1, 10-26): the trust flag for `$HOME` stayed false
   after a clean exit and restart; prefer a `boot-cwd` naming an
   already-trusted project.
   A seat that sets `CLAUDE_CONFIG_DIR` on purpose (in `boot-env`, the host-common
   boot-env file, or its inherited environment) gets that `ERROR:` on every launch, even when the
   directory is trusted. The launcher never reads `$CLAUDE_CONFIG_DIR`'s own
   config, because its layout is unverified. So the line is expected there,
   not a fault.

   Per-agent overrides are harness-specific: use
   `~/.engram/boot-charge-claude.md` or `~/.engram/boot-charge-codex.md`.
   They are complete user-owned charges and are never rewritten by the
   launcher. `~/.engram/boot-charge.md` remains a **Claude-only legacy
   compatibility override**; a Codex launch ignores it, logs a visible warning,
   and falls back to the installed Codex charge. This prevents an old generic
   file from giving a Codex session Claude-only actions. To select the shipped
   quiet common framing without a full override, put exactly `quiet` in
   `~/.engram/boot-charge-profile` (default is `default`).

   Optional per-agent environment: `~/.engram/boot-env` (plain `VAR=value` /
   `export VAR=value` lines) is sourced by the launcher before any pre-check,
   auto-exported, and inherited by the whole tmux → agentctl → CLI chain. Use
   it for agents whose platform CLI needs profile-set variables the clean
   systemd environment lacks (e.g. zephyr's non-Anthropic backend needs his
   `ANTHROPIC_*`/Ollama variables — normally set in `.bash_profile`, which
   nothing in the boot chain reads). Keep it owned by the agent (`0640`).

   Linux user-session variables (#4328): after both boot-env files are
   sourced, the launcher fills `XDG_RUNTIME_DIR` (to `/run/user/<uid>`, when
   that is a directory owned by the agent's uid) and then
   `DBUS_SESSION_BUS_ADDRESS` (to `unix:path=$XDG_RUNTIME_DIR/bus`, when that
   is a socket), each only when unset or empty, so `systemctl --user` works in
   the session. A value already set and non-empty (boot-env, host-common file,
   inherited) is never overridden. If something is missing, the variable stays
   unset, ONE stderr line starting `WARNING: autostart session env:` names it,
   and the launch continues. `AGENT_RUN_USER_BASE` (default `/run/user`) is a
   test seam; production never sets it.

   Optional host-common environment: a second file, sourced immediately
   BEFORE the per-agent `boot-env`, carries settings common to every seat on
   the host (first planned use: a `CLAUDE_CODE_PLUGIN_DIRS=...` line).
   - **Path:** `/home/agents-shared/autostart/boot-env.common` on Linux,
     `/Users/Shared/autostart/boot-env.common` on macOS. Set
     `AGENT_BOOT_ENV_COMMON` (full path) in the launcher's environment to
     override (tests, operators). Nothing ships at this path; create it by hand.
   - **Precedence:** common first, per-agent second, so a variable set in both
     ends up with the **per-agent** value.
   - **Ownership:** operator-owned, group-readable by the agent seats, not
     agent-writable (it affects every seat on the host). The **directory**
     must be searchable (`x`) by the seats, e.g. `chmod 755`. If it is not,
     the file reads as ABSENT, silently, with no warning and no values,
     because every site tests `-e` first.
   - **Failure policy:** absent is normal and silent. Present but unreadable
     gives one WARNING line (stderr; the macOS wrapper writes it to its log)
     and the launch continues, still sourcing the per-agent file, so one
     permission slip cannot stop every seat from booting. A sourcing error in
     the file aborts the launch scripts exactly like a broken per-agent file;
     `agentctl session` ignores such errors, as it does for the
     per-agent file.
   - **Sourced by all three launch paths:** `agent-session-launch.sh` (Linux
     autostart), `macos/clio-wake-launch.sh` (macOS wake wrapper), and the
     shared `agentctl session` prologue (manual launches).

2. **Install the systemd units:**

   ```bash
   sudo cp deploy/autostart/agent-session@.service \
           deploy/autostart/agent-sessions.timer \
           deploy/autostart/agent-sessions.target \
           /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

3. **Enable the timer + the per-agent instances** (five Claude agents — only
   agents with an `agent-<name>` Linux user on THIS host; Luria is off-host
   (her instance fails with status=217/USER — found live 2026-07-24). Mira's
   Codex instance remains excluded pending a supervised trial; when enabled,
   add the ordinary `agent-session@mira.service` instance (not a direct
   app-server unit):

   ```bash
   sudo systemctl enable agent-sessions.timer
   for name in kepler borges ariadne sol zephyr; do
       sudo systemctl enable "agent-session@${name}.service"
   done
   ```

   **`enable-linger` is NOT needed here** — unlike the forum's
   `systemctl --user` service (which needs `loginctl enable-linger` to
   survive logout/reboot without an active login session), these are
   **system** units (`/etc/systemd/system/`, `User=agent-%i`), which systemd
   starts on its own schedule regardless of whether the target user is
   logged in. Linger is a `--user`-unit concern only.


### Codex topology (Mira)

Codex must not run as a standalone `codex app-server` systemd service. The
operator's TUI and `codex-umwelt-wake` must talk to the **same managed daemon**
and the same persisted thread. The ordinary `agent-session@mira.service` chain
already supplies the required named `agent-mira` tmux pane. Inside that pane,
`agentctl session mira --as-self ...` detects Codex, runs the idempotent
`codex app-server daemon start`, verifies `daemon version` and its Unix socket,
and then executes `codex --remote unix://<socket> ...` in that pane.

For an interactive terminal, use `agentctl attach mira` (or `-r` to observe).
A stale/unreachable daemon socket is a loud launch failure: do not work around
it by opening a local Codex TUI, because that splits the wake watcher from the
human-facing thread.

## Stagger the five agents ~30s apart

`agent-sessions.target`'s `Wants=` fires all five instances at once by
default. Stagger them with a per-instance drop-in (`ExecStartPre=/bin/sleep N`)
so five simultaneous Claude Code cold-starts don't hit the world/API all at
the same second:

```bash
sudo mkdir -p /etc/systemd/system/agent-session@kepler.service.d
cat <<'EOF' | sudo tee /etc/systemd/system/agent-session@kepler.service.d/stagger.conf
[Service]
ExecStartPre=/bin/sleep 0
EOF

sudo mkdir -p /etc/systemd/system/agent-session@borges.service.d
cat <<'EOF' | sudo tee /etc/systemd/system/agent-session@borges.service.d/stagger.conf
[Service]
ExecStartPre=/bin/sleep 30
EOF

# ... ariadne=60, sol=90, zephyr=120, following the same pattern.

sudo systemctl daemon-reload
```

(An `ExecStartPre=` drop-in is additive per-instance, not shared across the
template, so this is the right seam — the base `agent-session@.service`
stays identical for all instances.)

## Stopping a session: know what `systemctl stop` does NOT do

`agent-session@<name>` is `Type=oneshot` + `RemainAfterExit=yes`: the unit's own
process exits once tmux has forked the detached session, so **`systemctl stop
agent-session@<name>` does NOT terminate the live tmux session** — it only clears
the unit's "active" latch. To actually end a session: `sudo -u agent-<name> tmux
kill-session -t agent-<name>` (or attach and exit normally). This is deliberate:
the unit is a launcher, not a supervisor, and a session's lifecycle belongs to
the agent + operator, not to systemd's restart machinery.

## Dress-rehearsal checklist (do this before trusting the real 8:00/8:15 boot)

Run through this **with the operator physically present** (not remote) the
first time:

- [ ] `sudo systemctl daemon-reload` after any unit/drop-in change.
- [ ] `systemctl list-timers agent-sessions.timer` shows the next 08:15
      America/New_York fire time.
- [ ] One **real reboot** of the host (not just `systemctl restart`) —
      confirm world/forum/gateway servers come up automatically at boot
      (umwelt-side, out of scope here, but a precondition for this to make
      sense) and that NO agent session starts before 08:15.
- [ ] At/after 08:15, confirm each `agent-session@<name>.service` shows
      `active (exited)` (expected for `Type=oneshot` +
      `RemainAfterExit=yes` — the launcher exits quickly once tmux forks
      off; the session itself lives on independently).
- [ ] `sudo -u agent-<name> tmux attach -t agent-<name>` (or `ssh` in as
      that user, including over tailscale, then `tmux attach`) — the
      session should already be processing its boot charge.
- [ ] **Verify avatars appear in the world** for each agent that logged in
      per its boot charge — this is the actual "did the whole chain work"
      signal, not just "did the service start".
- [ ] Deliberately fail one instance (e.g. temporarily `chmod -x` its
      `claude` binary) and confirm it shows `failed` in `systemctl status`
      and does NOT retry on its own (`Restart=no` — see the unit's own
      comment on why this is deliberate).

## Which steps are host-owner-only

**Host-owner-only** (require root/sudo on the physical or admin-tailscale
host): everything in "Install" and "Stagger" above, plus the dress-rehearsal
reboot itself.

**Not host-owner-only** (any agent, or the operator on an agent's behalf,
can do these without root): dropping a harness-specific per-agent
`~/.engram/boot-charge-claude.md` or `~/.engram/boot-charge-codex.md`
override (or selecting `~/.engram/boot-charge-profile`); attaching to an
already-running tmux session; reading this README.
