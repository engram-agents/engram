---
name: engram-health-exam
description: 'Post-upgrade systematic self-health exam. Run this checklist after any ENGRAM upgrade (or whenever runtime health is in doubt) to confirm the operational layer is alive before resuming work. Covers: MCP server round-trip, hook delivery, surface daemon + semantic recall, viz server, DB schema/migration, config sanity, and tier integrity. Complements selftest.py (code-correctness) — this is the runtime/operational layer, not the test suite.'
---

# ENGRAM Health Exam — Post-Upgrade Checklist

`selftest.py` tells you the *code* is correct. This skill tells you the *running install* is alive.
The implemented-≠-shipped-≠-alive gap is the #824/#840 class of silent failures: a hook that fires but
doesn't deliver, a daemon that restarts with a bad socket, a viz server that crashed overnight.
**Run this after every upgrade, and any time "something feels off."**

**How to use this skill:**

- Work through steps in order.
- Each step has a PASS criterion and a FAIL/REMEDIATION note.
- Stop at the first FAIL and remediate before continuing — a downstream check that passes on a broken
  foundation is misleading.
- The exam is **advisory** — it does not block work if one check fails, but loud failures should be
  investigated before trusting the corresponding mechanism.

**Primary trigger:** after each `engram-upgrade` run (Step 7 in that skill points here).

---

## Step 1 — MCP server round-trip

Call `engram_stats`. The MCP server must answer and the graph DB must be readable.

```
engram_stats({})
```

**PASS:** the response includes `total_nodes` (≥ 0) and `health_score` (0.0–1.0) without error.

**FAIL:** the tool call errors, times out, or returns an empty response.

Remediation:
```bash
# Check if the server process is running
pgrep -f "launch-engram-server.sh|engram/server.py" || echo "server process not found"

# Attempt manual restart (plugin install):
~/.engram/marketplace/plugins/engram/launch-engram-server.sh &
# Then reconnect MCP: /mcp in Claude Code
```

---

## Step 2 — Session-start hook delivery

The SessionStart hook writes a per-session marker. Verify it fired AND delivered context to this session.

```bash
# Check that the current session marker exists (hook wrote it):
ls -t ~/.engram/sessions/ | head -5

# Confirm the hook injected content into the session banner by checking
# that the warm-briefing was loaded (proxy: the file's mtime is recent):
stat ~/.engram/warm-briefing.md 2>/dev/null | grep -i modify
```

**PASS:** a `.json` session marker file exists for the current session (most-recent by mtime); the session
banner in your current context included ENGRAM identity content (the model-gate line, graph stats, or the
warm-briefing pointer).

**FAIL:** no session marker exists for the current session, or the session banner was absent / empty.

Remediation — this is the #824 muted-stdout class:
```bash
# Check the hook's recent output in the session log:
tail -20 ~/.engram/sessions/$(ls -t ~/.engram/sessions/ | head -1)

# Verify the hook is registered in the plugin hooks config:
cat ~/.engram/marketplace/plugins/engram/hooks/hooks.json | python3 -m json.tool | grep -A3 "session"
```

---

## Step 3 — UserPromptSubmit hook delivery (forum-surfaced cursor)

The forum-prompt hook (`engram-forum-prompt-hook.py`) fires on every prompt and updates `~/.engram/forum-surfaced-cursor.txt`.

```bash
# Check the surfaced cursor was updated recently (within the last few minutes):
stat ~/.engram/forum-surfaced-cursor.txt 2>/dev/null | grep -i modify

# And confirm the surface hook is registered:
cat ~/.engram/marketplace/plugins/engram/hooks/hooks.json | python3 -m json.tool | grep -A3 "UserPromptSubmit"
```

**PASS:** `forum-surfaced-cursor.txt` exists and its mtime is from this session (within the last
~5 minutes of real time).

**FAIL:** the file is absent, or its mtime is from a prior day/session — the hook is registered but
not delivering.

Remediation — same muted-stdout class:
```bash
# Check if the daemon is running (surface hook depends on it):
pgrep -f "engram-surface-daemon" || echo "daemon not running"
# Restart the daemon if absent (it auto-restarts on next hook fire if the start-daemon script is registered):
bash ~/.engram/marketplace/plugins/engram/hooks/claude/start-engram-daemon.sh
```

---

## Step 4 — Surface daemon alive + semantic recall

The surface daemon powers semantic recall. Test it directly.

```bash
# Check the daemon process:
pgrep -af "engram-surface-daemon" || echo "daemon not running"
```

Then call `engram_query` with a term you know is in the graph (any recent topic):

```
engram_query({"payload_json": "{\"query\": \"<a term from your recent work>\", \"limit\": 3}"})
```

**PASS:** daemon process exists; `engram_query` returns ≥ 1 result (semantic recall working).

**FAIL — daemon not running:** semantic recall falls back to FTS-only (weaker recall, no embedding-based
matches). Not a hard block, but degrades surfacing quality.

Remediation:
```bash
bash ~/.engram/marketplace/plugins/engram/hooks/claude/start-engram-daemon.sh
# Wait 10s, then re-check pgrep
```

**FAIL — query returns empty (but daemon is running):** possible embedding mismatch after an upgrade
that changed the embedding model.
```bash
# Re-generate embeddings (takes a few minutes on a large graph):
python3 ~/.engram/marketplace/plugins/engram/tools/engram-regenerate-embeddings.py
```

---

## Step 5 — DB schema and migration integrity

Call `engram_diagnose` to check for schema anomalies.

```
engram_diagnose({})
```

**PASS:** `engram_diagnose` returns without an error in the `errors` list; health score roughly aligns
with `engram_stats`.

**FAIL:** the tool errors, or `errors` contains unexpected entries. Proceed to the manual schema check.

```bash
# Check what schema version the DB is at (stored in the SQLite user_version pragma):
python3 -c "
import sqlite3, os
conn = sqlite3.connect(os.path.expanduser('~/.engram/knowledge.db'))
print('DB user_version (schema version):', conn.execute('PRAGMA user_version').fetchone()[0])
"
# Check what version the installed code expects (look for the migration sentinel):
grep -n "user_version <\|PRAGMA user_version =" \
    ~/.engram/marketplace/plugins/engram/engram_core.py | head -5
```

If the DB user_version doesn't match the code's constant, a migration is needed — contact Lei. Schema
migrations touch the graph and require deliberate action.

---

## Step 6 — Viz server health (if configured)

Skip this step if you do not use the viz dashboard (T2 feature, may not be installed).

```bash
curl -s http://localhost:5001/api/health | python3 -m json.tool
```

**PASS:** JSON response with a `health_score` field (number 0.0–1.0). A populated `health_score` means
the viz server is up and can read the graph DB.

**FAIL:** connection refused, non-200 response, or no `health_score` in the JSON.

```bash
# Check if it's supposed to be running (systemd service or manual start):
systemctl --user status engram-viz 2>/dev/null || echo "no systemd service"
# Manual start:
python3 ~/.engram/marketplace/plugins/engram/viz_server.py &
```

---

## Step 7 — Config sanity

```bash
python3 -c "
import json, os, sys
path = os.path.expanduser('~/.engram/config.json')
try:
    d = json.load(open(path))
except Exception as e:
    print('FAIL: config not valid JSON:', e); sys.exit(1)

print('self_lineage :', d.get('self_lineage', 'MISSING'))
print('mode         :', d.get('mode', 'MISSING'))
print('agent_name   :', d.get('agent_name', 'MISSING'))

drowsiness = d.get('cadence', {}).get('drowsiness_ceiling_tokens')
print('drowsiness_ceiling_tokens:', drowsiness if drowsiness else 'NOT SET (will use fallback 152000)')
"
```

**PASS:**
- `self_lineage` is set (e.g. `anthropic:sonnet`).
- `mode` is set (`single` or `multi`).
- `agent_name` is set (not `MISSING`).
- `drowsiness_ceiling_tokens` is set (ideally 5–10% below the session's auto-compaction limit).

**FAIL — missing fields:** add them to `~/.engram/config.json` using the `engram-first-session` skill
as the reference for correct values, or the viz-server config UI at `http://localhost:5001/config`.

**FAIL — `drowsiness_ceiling_tokens` not set:** run `/context` in Claude Code to find the
auto-compaction limit, then set `cadence.drowsiness_ceiling_tokens` to ~5–10% below that value.

---

## Step 8 — Tier integrity (optional, developer/self-improve installs)

Skip unless you run from a developer source checkout with `tools/selftest.py` available.

```bash
cd <engram-alpha-repo>
python3 tools/selftest.py --quick 2>&1 | tail -20
```

**PASS:** all tests pass.

**FAIL:** one or more test failures. File an issue or investigate before resuming development work.

---

## Summary and what to record

After walking all steps, record the outcome in ENGRAM so future-me has a baseline:

```
engram_add_observation({"payload_json": "{\"claim\": \"Post-upgrade health exam PASS (or FAIL: <which step, what symptom>). Install: plugin. Steps checked: 1-7 (or 1-8).\", \"quote_type\": \"hard_data\", \"source_class\": \"introspective\", \"tags\": [\"health-exam\", \"upgrade\", \"operational\"]}"})
```

A failed step that was remediated should be recorded too — the incident is useful signal for the
lesson system and for future pattern detection.

---

## Relationship to other skills

- **`engram-upgrade`**: calls this skill at the end (Step 7-ish in that skill) as the verification gate.
- **`engram-first-session`**: sets the initial values this skill checks (`self_lineage`, `mode`, `agent_name`).
- **Handbook (EPIC #784)**: this skill is the executable complement to the handbook's per-mechanism
  "what would we observe if broken" sections. They share vocabulary intentionally — each handbook
  mechanism page maps to a check step here.
- **selftest.py**: code-correctness (pytest); NOT this. This is operational/runtime health.
