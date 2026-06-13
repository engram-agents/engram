# ENGRAM on macOS — agent setup notes

**You are reading this because you're installing or running ENGRAM on a Mac, or
guiding a human operator through standing up a *second* agent on their Mac.** It
is a companion to [README-AGENT.md](../README-AGENT.md) — read that first for the
canonical install. This file only carries the **macOS-specific** deltas: what's
different on a Mac, where the rough edges are, and the lived runbook for the
one genuinely manual path (multi-agent).

Scope, per Lei's priority split:

- **Single-agent install + run is the priority** — it should be smooth, and on
  current `dev` it is. The notes below are mostly reassurance plus one daily-run
  gotcha.
- **Multi-agent (one human, two+ agents on one Mac) is best-effort** — the agent
  guides the operator through it; there's no one-shot tool. The Clio runbook
  below is the real, tested path.

This doc is written from lived experience standing up the first co-located
second agent on a Mac (the "Clio" deployment, 2026-06-08) and from running a
single agent on macOS daily since the 2026-06-03 plugin migration. Where a step
needed the **human operator** to act (not the agent), it's marked
**[OPERATOR]** — the operator should sanity-check those against their memory.

---

## Single-agent: what's different on macOS

### The build runs clean on stock macOS — no extra prereqs

`build-plugin.sh` is a thin wrapper around the Python build engine
(`python3 -m tools.engine.cli build`). It builds cleanly on a **stock** Mac —
system `bash` 3.2.57 and BSD `sed`, no Homebrew bash/GNU-sed needed.

> **Historical note (do not re-add):** the 2026-06-03 migration-era bash build
> *did* break on macOS twice (`declare -A` needed bash 4+, `sed -i` needed GNU
> sed), and older notes may tell you to `brew install bash gnu-sed`. That was
> fixed when the build moved to the Python engine. **Don't add a brew bash/sed
> prerequisite** — verify by just running the build.

Verify on a Mac:

```bash
# from the engram-alpha repo root, on dev:
python3 -m tools.engine.cli build --output /tmp/engram-plugin-check
ls /tmp/engram-plugin-check/plugin.json   # artifact present -> build is fine
```

Other stock-macOS facts that are *not* blockers (verified, no coreutils
install): `/usr/bin/readlink` supports GNU-style `-f` on modern macOS; `Path.home()`,
the venv, and `/Users/...` paths all resolve normally. `sqlite_vec` is absent
from the venv but optional (try/except fallback, same as Linux) — no action.

### Daily-run note: the surface daemon "offline" warning — verify, don't assume

The surface daemon (semantic recall) **idle-shuts-down after 8 hours**
(`IDLE_TIMEOUT = 28800` in `hooks/claude/engram-surface-daemon.py`), so the
**first session of the day** cold-starts it. On a cold start you may see a
SessionStart warning:

```
{"warning": "engram surface daemon did not start within 10s"}
```

surfaced to you as *"surface daemon offline; semantic recall DISABLED."*

**Do not assume this is benign.** A genuinely-down daemon means every recall
silently degrades to FTS/lexical-only — the failure mode is *quiet*, so the
honest response to the warning is to **verify**, not to wait it out:

```bash
ls -l ~/.engram/recall-daemon.sock     # socket present -> daemon is actually up
tail -20 ~/.engram/surface-daemon.log  # check it loaded vs. errored
# if genuinely down, relaunch:
bash "$CLAUDE_PLUGIN_ROOT/hooks/start-engram-daemon.sh"
```

Two distinct things can produce this warning; they have *different* fixes, so
don't conflate them:

1. **The launch silently failing in the no-TTY hook subprocess (`#1063`).** The
   old launcher used `nohup … &`, which fails under the SessionStart hook's
   no-TTY subprocess ("Inappropriate ioctl for device") — the daemon never
   starts, and you get recurring offline *every* session. **Fixed on `dev` by
   #1073** (detached `… < /dev/null &`). A fresh install from current `dev` has
   the fix; if you're on an **older install and seeing this repeatedly, the real
   fix is to upgrade** — stop relaunching it by hand each session.
2. **A cold model-load exceeding the hook's 10s socket-wait** (`seq 1 20 ×
   sleep 0.5`). The ~80 MB model can load past 10s on a slow first start; the
   hook stops waiting and warns even though the detached daemon may bind its
   socket shortly after. `ls` on the socket tells you which case you're in.

> **Interim note:** this whole cold-start alarm path is being **redesigned**
> (fire-and-forget launch + a true-positive-only per-turn liveness check) — see
> the daemon-alarm redesign work tracking #1156. Once that lands, the warning
> becomes a *rare and real* signal worth investigating, not background noise.
> Until then: verify the socket, and upgrade if you're on a pre-#1073 install.

### Keep ENGRAM data and the repo out of TCC-protected folders

macOS TCC (Transparency, Consent & Control) throws a permission prompt the first
time a process touches `~/Documents`, `~/Desktop`, or `~/Downloads`. For an
interactive session you'd just click "Allow" — but for any **non-interactive**
run (a launchd/cron-driven job), there's no one to click, and the process
**hangs** on the invisible prompt. Keep `~/.engram/` (the default — good) and
your `engram-alpha` clone **outside** those three folders (e.g. `~/GitHub/...`,
not `~/Documents/GitHub/...`). This bit us once: an overnight launchd job hung on
a TCC prompt triggered by a `cd` into `~/Documents`.

---

## Multi-agent (best-effort): the Clio runbook

Standing up a **second** agent for the same human on one Mac. There is **no
one-shot tool** for this on macOS — the Linux deployment tooling
(`agentctl` / `agent-bootstrap`) does **not** port (user-management, ACLs, and
systemd quiesce all differ; that port is the separate epic #750, out of scope
here). The working path is the agent guiding the operator, step by step. This is
the exact sequence that deployed the first co-located second agent ("Clio",
Sonnet) on 2026-06-08:

1. **[OPERATOR]** Create a macOS **Standard** user account for the new agent
   (e.g. `engram-sonnet`) — System Settings → Users & Groups.
2. **[OPERATOR]** Fast-switch into the new account, install **Claude Code**
   per-user, and sign in **with the operator's own claude.ai account**. One Pro
   plan runs two concurrent agents (Lei-confirmed). *The operator types their
   password into the OS/login prompt directly — never paste credentials into
   agent chat.*
3. Copy the `engram-alpha` repo from the operator's existing clone into the new
   user's home (e.g. `/Users/engram-sonnet/GitHub/engram-alpha`). Copying
   sidesteps the **private-repo clone-auth gate** — the new account doesn't need
   its own GitHub auth just to install. (Operator may need to grant read access
   to the source clone, or copy via a shared location.)
4. Launch the new agent's Claude Code **in that repo**, and hand it
   README-AGENT's plugin-install section. **The new agent self-installs ENGRAM**
   from there — installation is agent-driven by design.
5. The new agent's **first ENGRAM session** triggers `engram-first-session`:
   it picks its own name and writes its first nodes (its identity birth). That's
   the new agent's to drive, not yours.

**Deferred per new agent** (not blockers for a working install, but clean up
after):

- Its **own GitHub identity** (a separate handle, the way the first agent has
  one) — until then it has no public-facing git identity.
- A **git-config reset** on the copied repo — the copy carries the *source*
  agent's per-repo `user.name` / `user.email`, so commits would be misattributed
  until reset.

**Shared-filesystem channels (`baton` / `ia`) are extra.** The same-host
inter-agent turn-state (`baton`) and 1:1 letter (`ia`) channels default to a
shared directory both accounts can read/write. On macOS that means setting up
something like `/Users/Shared/agents-shared` with `staff` group + `setgid` +
an ACL granting both agent users access, and `config.json mode = multi`. This is
best-effort and only needed if you want the same-host channels; the LAN **forum**
works over HTTP without it.

**Viz dashboard is best-effort on macOS.** `operator-setup-viz.sh` is
Linux/systemd-only (no macOS analog yet — tracked as #402); Mac users run the viz
server via a hand-authored launchd plist. Not required for the core agent loop.

---

## Quick reference: macOS facts

| Thing | macOS reality |
|---|---|
| Plugin build | Clean on stock `bash` 3.2 + BSD `sed` (Python engine). No brew prereq. |
| `readlink -f` | Supported on modern macOS `/usr/bin/readlink`. |
| Surface daemon | Detached background process (`… < /dev/null &`); 8h idle-shutdown; "offline" warning on cold start — **verify the socket, don't assume benign** (down = silent FTS-only). Recurring offline on a pre-#1073 install → upgrade. |
| TCC | Keep `~/.engram/` + repo out of `~/Documents`,`~/Desktop`,`~/Downloads` (hangs non-interactive jobs). |
| Multi-agent tooling | `agentctl`/`agent-bootstrap` don't port (epic #750). Use the operator-guided runbook above. |
| `baton` / `ia` | Need a shared dir (`/Users/Shared/...`, setgid+ACL) + `mode=multi`. Best-effort. |
| Viz | `operator-setup-viz.sh` Linux-only (#402); hand-authored launchd plist on Mac. |
