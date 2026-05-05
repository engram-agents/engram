# viz_server — multi-agent graph switcher

**Status:** active (design settled, implementation queued)
**Started:** 2026-05-04 (Lei + Borges, evening)
**ENGRAM pointer:** (TBD — pinned obs after this doc commits)
**Branch:** feature/viz-server-multi-agent

## Goal

Replace the current per-agent viz_server (separate processes per DB, both currently stale on pre-migration spt-side data) with a **single multi-agent visualization** that reads multiple agents' graphs and lets the viewer switch between them via a UI dropdown. Living agent data lives at `/home/<agent-uid>/.engram[*]/knowledge.db` post-migration; the unified viz reads each one read-only.

Lei's framing: peer-readable graphs are not just acceptable, they're useful — younger spawned agents can learn from Borges and Mneme's accumulated style and substance.

## Permission model: Option A — group `agents` peer-read

Decision (Lei, 2026-05-04 evening): open agent home dirs and DBs for read access to anyone in group `agents`. Read-only is not dangerous. Mutual peer-readability is fine for a small collaborative agent set (Borges + Mneme + 1-2 future spawns).

**Migration commands** (one-shot, run by Lei as root):

```bash
# 1. Make parent home dirs traversable to group=agents (needed so members
#    of `agents` can `cd` into the .engram subdir). Without this, even with
#    .engram itself opened, the parent /home/<uid>/ blocks the path.
sudo chgrp agents /home/agent-borges /home/agent-mneme
sudo chmod g+x   /home/agent-borges /home/agent-mneme

# 2. Open Borges's .engram dir + DB (incl. WAL friends -shm, -wal)
sudo chgrp agents /home/agent-borges/.engram
sudo chmod g+rx   /home/agent-borges/.engram
sudo chgrp agents /home/agent-borges/.engram/knowledge.db*
sudo chmod g+r    /home/agent-borges/.engram/knowledge.db*

# 3. Open Mneme's .engram-gemini dir + DB (incl. WAL friends)
sudo chgrp agents /home/agent-mneme/.engram-gemini
sudo chmod g+rx   /home/agent-mneme/.engram-gemini
sudo chgrp agents /home/agent-mneme/.engram-gemini/knowledge.db*
sudo chmod g+r    /home/agent-mneme/.engram-gemini/knowledge.db*

# 4a. Quick read-permission check (no SQL exercised)
sudo -u agent-borges test -r /home/agent-mneme/.engram-gemini/knowledge.db && echo "borges→mneme readable"
sudo -u agent-mneme  test -r /home/agent-borges/.engram/knowledge.db      && echo "mneme→borges readable"

# 4b. Thorough check (uses python's bundled sqlite3 module — sqlite3 CLI
#     is not installed on this host)
sudo -u agent-borges python3 -c "import sqlite3; print(sqlite3.connect('/home/agent-mneme/.engram-gemini/knowledge.db').execute('SELECT COUNT(*) FROM nodes').fetchone()[0])" && echo "borges→mneme query OK"
sudo -u agent-mneme  python3 -c "import sqlite3; print(sqlite3.connect('/home/agent-borges/.engram/knowledge.db').execute('SELECT COUNT(*) FROM nodes').fetchone()[0])"      && echo "mneme→borges query OK"
```

The parent-home-dir step (1) is easy to miss but mandatory — the home
dir is mode 750 by default which blocks group-mode-x for members of
the `agents` (since group ≠ agent-X by default). After step 1, the
home dir keeps its content private (agents still can't `ls` it because
no g+r), but they CAN traverse THROUGH it to the explicitly-opened
.engram subdir. That's the right granularity: the subdir contents are
peer-readable, but the home dir's other contents (diary/, secrets/,
logs/, etc.) stay invisible to peers.

**Persistence note:** the per-agent home directory `~/.engram` is created at install time. The installer (install.sh / install-gemini.sh) should set the group + g+rx mode at install time so this opening is automatic on fresh installs. **Action item:** add the chgrp + chmod to install.sh + install-gemini.sh as part of Phase 1.

**Subdirectories with sensitive content** (e.g., `~/.engram/secrets/`, `~/.engram/diary/`) stay private — only the top-level dir + `knowledge.db` itself become group-readable. Diary and secrets keep their default mode 700.

## Architecture

**Single process, single port, config-driven.**

- One viz_server process (replaces both current ones).
- One port (5001 by default; 5002 retired).
- New `--config` flag pointing at JSON describing all known agents:

  ```json
  {
    "agents": [
      {"name": "borges", "db": "/home/agent-borges/.engram/knowledge.db",      "label": "Borges (Claude)"},
      {"name": "mneme",  "db": "/home/agent-mneme/.engram-gemini/knowledge.db", "label": "Mneme (Gemini)"}
    ],
    "default": "borges"
  }
  ```

  Config lives at `~/.config/engram-viz/agents.json` (or `/etc/engram-viz/agents.json` for systemd-managed installs).

- `--db` flag is **kept** for backward compat — `--db PATH` registers a single anonymous agent named "default". Existing systemd units don't break during cutover.
- Backend opens a per-agent sqlite connection on first request, caches it (read-only, immutable=1 mode for safety).
- API endpoints accept `?agent=<name>` query parameter. Default to config.default if omitted.
- UI gets a top-bar dropdown listing config-loaded agents; selection updates a `?agent=…` URL fragment and re-renders the graph.

## Migration steps (in order)

### Phase 1: Permission opening
- Run the chgrp + chmod commands above (Lei, sudo).
- Verify cross-uid read works.
- Update install.sh + install-gemini.sh to set group+mode at install time.

### Phase 2: viz_server.py refactor
- Add `--config` flag and JSON loader.
- Refactor DB connection logic: replace single-connection `_db = sqlite3.connect(DB_PATH)` with a `_get_db(agent_name)` helper + cache.
- Add `?agent=` parameter handling on every API endpoint.
- Update HTML/JS to render an agent dropdown from `/api/agents` (new endpoint that returns `[{name, label, current_node_count}, ...]`).
- Sandbox-test against fixture DBs.

### Phase 3: systemd unit consolidation
- Replace `engram-viz.service` (current — runs as spt, points at one DB) with `engram-viz.service` (new — runs as agent-borges with `agents` secondary group, --config flag).
- Optional: keep an `engram-viz-borges.service` separate from `engram-viz-mneme.service` for clarity, OR a single `engram-viz.service` serving all agents. **Recommended: single unit, simpler operationally.**

  ```ini
  [Unit]
  Description=ENGRAM Visualization Server (multi-agent)
  After=network.target

  [Service]
  Type=simple
  User=agent-borges
  Group=agent-borges
  SupplementaryGroups=agents
  WorkingDirectory=/home/agent-borges/engram-alpha
  ExecStart=/home/linuxbrew/.linuxbrew/bin/python3 /home/agent-borges/.engram/viz_server.py --bind 127.0.0.1 --port 5001 --config /etc/engram-viz/agents.json
  Restart=always
  ```

- Ship `/etc/engram-viz/agents.json` as part of the install (or as a one-time admin step).

### Phase 4: Cutover + validation
- Stop the two stale instances (PID 212 systemd `engram-viz.service`, PID 463 manual launch).
- Start the new unified instance.
- Visit `http://localhost:5001/` — confirm dropdown lists Borges + Mneme.
- Switch to each, verify the live graph data renders (post-migration node counts, recent commits, etc.).
- Health page `http://localhost:5001/health` shows DB status per agent.

## Edge cases / open questions

- **What if an agent's DB is missing or unreadable?** Show in the dropdown as "unavailable" with the underlying error in a tooltip. Don't crash the server.
- **What if a future agent is added?** Update `agents.json`, no code change. systemd unit reloads on its own (or one-time SIGHUP).
- **Mneme's DB already exists at the spt-side path** (`/home/user/.engram-gemini/`)? Yes — that's pre-migration data. The new config points at the post-migration `/home/agent-mneme/.engram-gemini/`. Spt-side dir can stay as backup until soak completes.
- **Per-agent connection caching**: open lazily (first request), cache in a module-level dict keyed by agent_name. Connection is read-only `sqlite3.connect(path, uri=True)` with `?mode=ro&immutable=1` URI flags (immutable signals SQLite to skip lock files — safe for read-only access of a DB the OWNER is actively writing).
- **WAL-mode considerations**: the agent's live process is writing in WAL mode. A read-only opener with `immutable=1` may miss recent writes (the WAL hasn't checkpointed). **Trade-off acceptable** for a viz tool — viewer can refresh; near-real-time is good enough. If we want bleeding-edge, drop `immutable=1` and the reader joins the WAL view (slightly slower, but sees committed writes).
- **Auth model**: the viz currently binds to 127.0.0.1, no auth. With multiple agent DBs visible, this stays the same — anyone with localhost access (i.e., shell on this host) can view. Acceptable for current threat model. Revisit if/when remote access is added.
- **viz directory structure**: `viz_server.py` lives at the alpha root and is also deployed to `~/.engram/viz_server.py`. Lei's choice between running the live deployed copy vs the alpha source for the systemd unit. Recommend running the deployed copy (matches the dev/runtime split: source in alpha, executed from runtime).

## Implementation phases (post-design)

| Phase | Work | Estimated effort | Blocking? |
|-------|------|------------------|-----------|
| 1 | Permission migration + install.sh updates | 30 min | Lei sudo for the migration |
| 2 | viz_server.py code refactor + sandbox tests | 1.5–2 hrs | None |
| 3 | systemd unit + agents.json | 30 min | Lei sudo to install/replace |
| 4 | Cutover + validation | 30 min | Lei to stop old + start new |

Total: 3–3.5 hours focused work. Can absorb a compaction in the middle without losing state since this doc captures the spec.

## Testing plan

**Sandbox (in-process, no live impact):**
- Create temp DB fixtures with known node counts for "agent_a" and "agent_b"
- Spin up viz_server with `--config` pointing at fixtures
- HTTP-test each `/api/...?agent=...` endpoint returns the right data per agent
- Verify default agent loads on `?agent=` omitted
- Verify error handling when an agent's DB doesn't exist

**Live (against actual /home/agent-*/.engram* DBs):**
- After permission migration: verify cross-uid reads work
- After Phase 2 ship: launch viz_server manually under agent-borges + agents group, point at config
- Visit UI in browser, switch between agents, confirm graphs render
- Check health page

## Files touched (for the eventual PR)

- `viz_server.py` — major refactor (~300 lines added/changed)
- `install.sh` — chgrp + chmod for `~/.engram/` and `knowledge.db`
- `install-gemini.sh` — same for `~/.engram-gemini/`
- `tools/deploy.sh` — no manifest change (viz_server.py already deployed)
- New: `templates/engram-viz.service` — systemd unit template
- New: `templates/agents.json.example` — config example
- `DEVELOPMENT.md` — section on the multi-agent viz setup
- `README.md` — user-facing note about the new switcher

## Open: cross-platform note (per dv_0557 collab protocol)

`viz_server.py` is engine-layer (agent-agnostic — reads sqlite, no model-specific logic). Lives at the root, no sidecar. **Mneme: please mirror once dev catches up; the change should land on your tree without modification.**

## Decisions log

- **2026-05-04 evening:** Lei greenlit Option A (peer-read group=agents) over Option B (dedicated viz uid) and Option C (agentctl share). Reason: small agent set, peer-readable is useful, less ceremony.
- **2026-05-04 evening:** Lei picked single-port consolidation (5001) over keeping 5001+5002 split. Reason: single UI is the user-facing goal.
- **2026-05-04 evening:** Lei greenlit single systemd unit over per-agent units. Reason: simpler operationally; the in-process agent selector achieves what per-agent units would have.
- **2026-05-04 evening:** Implementation deferred to fresh context (next session). Compaction between this design + implementation is acceptable; this doc captures the spec.

## Sequence after this doc commits

1. ENGRAM observation pointing at this doc, focused as the active-build epic pointer.
2. Nap-checkpoint (preserves the design under high context pressure).
3. Next session (post-compaction or fresh tomorrow): pick up Phase 1 by asking Lei to run the migration commands, then Phase 2 refactor begins.
