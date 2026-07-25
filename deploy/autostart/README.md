# Boot-time agent auto-start

Systemd units + boot-charge templates that bring agent Claude Code (and, as
an untested variant, Codex) sessions up automatically at 09:30
America/New_York on a host that already auto-boots at 8:00 America/New_York.

## Design in one paragraph

The host boots at 8:00; world/forum/gateway servers (`umwelt-world.service`
et al — **umwelt-side units, out of scope for this repo**, referenced here
only by name as an `After=`/ordering dependency) come up then. Agent
**sessions** must NOT start at boot — they wait for a separate 09:30 timer,
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
| `agent-sessions.timer` | daily 09:30 America/New_York trigger, `Persistent=false` (see the unit's own comment for why) |
| `agent-sessions.target` | grouping target the timer triggers; `Wants=` each Claude agent's instance |
| `agent-session-launch.sh` | wrapper: resolves the boot-charge path + session cwd, prepends `~/.local/bin` to PATH, pre-checks the CLI + agentctl `--charge-file` support, starts detached tmux running `agent-session-run.sh` |
| `agent-session-run.sh` | runs INSIDE tmux: `agentctl session <name> -dr --as-self --cd <dir> --charge-file <file>`, logs every launch/exit to `~/.engram/logs/autostart-launch.log`, holds a failed pane open for autopsy (2026-07-25 lesson: a dead launch must leave a corpse) |
| `boot-charge-default.md` | default morning charge (orient, log in, check the charge board, hold quiet on-call) |
| `boot-charge-quiet.md` | variant for zephyr/mira: same, plus skip heartbeat loops / minimize token spend / stand by |
| `mira-codex-session@.service.example` | Codex `app-server` daemon VARIANT — **UNTESTED**, documented example only (see its own header) |

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
           deploy/autostart/boot-charge-default.md \
           deploy/autostart/boot-charge-quiet.md \
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
   ```

   Optional per-agent session cwd: drop an absolute path into
   `~/.engram/boot-cwd` (one line) in the agent's home; default is `$HOME`.

   Per-agent overrides (skip the shared default) go at `~/.engram/boot-charge.md`
   in the agent's own home — no root needed for that step, the agent (or the
   operator on the agent's behalf) can drop it in any time.

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
   (her instance fails with status=217/USER — found live 2026-07-24), and
   Mira's Codex variant is NOT enabled here — see the WARNING in
   `mira-codex-session@.service.example`):

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

## Dress-rehearsal checklist (do this before trusting the real 8:00/9:30 boot)

Run through this **with the operator physically present** (not remote) the
first time:

- [ ] `sudo systemctl daemon-reload` after any unit/drop-in change.
- [ ] `systemctl list-timers agent-sessions.timer` shows the next 09:30
      America/New_York fire time.
- [ ] One **real reboot** of the host (not just `systemctl restart`) —
      confirm world/forum/gateway servers come up automatically at boot
      (umwelt-side, out of scope here, but a precondition for this to make
      sense) and that NO agent session starts before 09:30.
- [ ] At/after 09:30, confirm each `agent-session@<name>.service` shows
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
can do these without root): dropping a per-agent
`~/.engram/boot-charge.md` override; attaching to an already-running tmux
session; reading this README.
