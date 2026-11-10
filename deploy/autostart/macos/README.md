# macOS wake mechanism (single-seat, engram-alpha#3002)

Analog of the top-level `deploy/autostart/` Linux systemd-timer pattern,
scoped to one macOS seat rather than a shared multi-tenant host. Read the
parent `README.md` first for the shared design rationale (boot-charge
resolution, why the launcher pre-checks `agentctl --charge-file` support,
the observability lessons baked into the log/tee/pane-hold pattern) — this
directory only documents what's different.

## What this is for

`engram-sleep`'s Phase A Step 3.5 correctly tears down a seat's loop
(marker + Monitors) at end-of-day. On a seat whose only wake paths were the
loop's own (`Monitor`/`ScheduleWakeup`, both session-scoped), that correct
teardown used to leave zero ways to reach the seat until a human typed —
measured twice on this seat as 34h and 36h dark windows before this existed.
See engram-alpha#3002 for the full incident writeup.

## How it differs from the Linux side

| | Linux (`deploy/autostart/`) | macOS (`deploy/autostart/macos/`) |
|---|---|---|
| Scheduler | systemd timer, daily 08:15 | `launchd`, every 2h (`StartInterval`) |
| Multi-tenant? | Yes — `agent-<name>` Linux user per agent | No — single macOS user, single seat (`clio`) |
| Privilege | root/sudo to install system units | **No sudo** — a user-level LaunchAgent |
| Already-live session | Treated as an **error** (fires once/day; unexpected) | Treated as a **quiet skip, exit 0** (fires every 2h; the expected common case) |

## Before installing: verify Keychain access under a LaunchDaemon

The install below is a **user-level LaunchAgent** (`gui/<uid>` domain), which
requires a GUI/console login session for that uid to exist at all — and it
does not on this seat: this macOS account has never had a GUI/console
login, so this install as documented does not currently work here. The fix
under discussion is to install as a **LaunchDaemon** instead (`system/`
domain, root-installed via `sudo`, with a `UserName` key so it runs as this
account) — LaunchDaemons don't require a GUI session to exist. But that
raises an open, **not-yet-answered** question: can a LaunchDaemon-started
process — with no GUI session anywhere in its ancestry — actually read this
account's macOS login Keychain, where Claude Code stores its
`"Claude Code-credentials"` secret via the `security` command? If the
account's login keychain requires an unlock tied to a GUI login, a
LaunchDaemon-started `claude` process would silently fail to authenticate —
reproducing this directory's own dark-seat problem one layer deeper, inside
the credential path instead of the process-liveness path.

Verify this empirically, in the SAME `sudo` session where you would install
the real LaunchDaemon, using the throwaway diagnostic in this directory
(`credential-probe.sh` + `com.engram.clio-credential-probe.plist.template`
— see that script's own header for the full context):

```bash
sed -e "s|__CREDENTIAL_PROBE_SH__|$HOME/GitHub/engram/deploy/autostart/macos/credential-probe.sh|" \
    -e "s|__CLIO_ACCOUNT__|$(whoami)|" \
    -e "s|__HOME__|$HOME|g" \
    deploy/autostart/macos/com.engram.clio-credential-probe.plist.template \
    | sudo tee /Library/LaunchDaemons/com.engram.clio-credential-probe.plist > /dev/null

chmod +x deploy/autostart/macos/credential-probe.sh
sudo chown root:wheel /Library/LaunchDaemons/com.engram.clio-credential-probe.plist
sudo chmod 644 /Library/LaunchDaemons/com.engram.clio-credential-probe.plist

sudo launchctl bootstrap system /Library/LaunchDaemons/com.engram.clio-credential-probe.plist
sleep 2
cat ~/.engram/credential-probe-result.txt

sudo launchctl bootout system/com.engram.clio-credential-probe
sudo rm /Library/LaunchDaemons/com.engram.clio-credential-probe.plist
```

**If this fails, STOP — do not proceed with the LaunchDaemon install.** The
fix for a locked Keychain is a security-design decision (credential storage,
an unlock step, or a different mechanism entirely) that needs a security
reviewer's read and the machine owner's explicit decision, not a workaround
applied here.

This diagnostic is throwaway by design. Never leave it loaded as a standing
job — tear it down (the `bootout` above) regardless of outcome, whether the
probe succeeded or failed.

## Install (no sudo — this agent can do this itself)

1. Substitute both placeholder tokens and copy the plist — `launchd` does not
   expand `~` or `$HOME`, so both `__CLIO_WAKE_LAUNCH_SH__` and `__HOME__`
   need real, absolute values:

   ```bash
   sed -e "s|__CLIO_WAKE_LAUNCH_SH__|$HOME/GitHub/engram/deploy/autostart/macos/clio-wake-launch.sh|" \
       -e "s|__HOME__|$HOME|g" \
       deploy/autostart/macos/com.engram.clio-wake.plist.template \
       > ~/Library/LaunchAgents/com.engram.clio-wake.plist
   ```

   This installs from `$HOME/GitHub/engram` (use `$HOME/GitHub/engram-alpha` on a seat still on the old folder name) — a live dev checkout, deliberately: a `git pull` that changes `wake-charge-clio.md` or either script changes what the next wake runs, the same way editing this seat's own CLAUDE.md takes effect on the next session without a separate install step. If that's ever undesirable, point the plist and this sed command at a pinned copy instead.

2. Make the scripts executable:

   ```bash
   chmod +x deploy/autostart/macos/clio-wake-launch.sh deploy/autostart/macos/clio-wake-run.sh
   ```

3. Load it:

   ```bash
   launchctl load ~/Library/LaunchAgents/com.engram.clio-wake.plist
   ```

## Verify

```bash
launchctl list | grep clio-wake        # confirm it's loaded
tail -f ~/.engram/logs/clio-wake.log   # the structured launch/skip log
tail -f ~/.engram/logs/clio-wake-launchd.log   # launchd's own stdout/stderr catch-all
```

A forced immediate test (bypassing the 2h wait): `launchctl kickstart -k gui/$(id -u)/com.engram.clio-wake` (macOS 10.10+). Kill a stuck wake session with `tmux kill-session -t clio-wake`.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.engram.clio-wake.plist
rm ~/Library/LaunchAgents/com.engram.clio-wake.plist
```

## Dependencies

- `tmux` (`brew install tmux`) — same reason as the Linux side: a direct
  `exec` without a PTY dies within milliseconds; `tmux new-session -d` also
  gives the free liveness check the quiet-skip logic depends on.
- `agentctl` with `--charge-file` support, on `PATH` (see the parent
  README's install section — the same binary, no macOS-specific changes).
- Homebrew bash (`/opt/homebrew/bin/bash`, 5.3+) — macOS system bash (3.2)
  cannot run `agentctl`'s `local -n` namerefs. `agentctl` itself must be
  invoked with the Homebrew bash on `PATH` ahead of the system one, which
  `clio-wake-launch.sh`'s `PATH` export already arranges.
