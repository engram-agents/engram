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

## Step 0 — Plugin tree and compatibility links

The live plugin tree must exist. Legacy helper paths may be symlinks, but a
symlink can survive while its old install target disappears, so `test -L`
alone is not enough to call it healthy.

```bash
test -d ~/.engram/marketplace/plugins/engram
# Loop variable is `p`, never `path`: in zsh `path` is a tied array bound to
# $PATH, so `for path in ...` wipes PATH for the rest of the shell (#3068).
for p in tools hooks; do
  target=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$HOME/.engram/$p" 2>/dev/null) || target=""
  if [ -e "$HOME/.engram/$p" ] && [ -n "$target" ] && [ -e "$target" ]; then
      echo "PASS: ~/.engram/$p -> $target"
  else
      echo "FAIL: ~/.engram/$p is absent, dangling, or unresolvable"
  fi
  # `test -e` and `test -L` are builtins, so this still answers even if the resolver above died.
  # Gated on -e as well as -L: an ABSENT path is not "a real directory", and the
  # pinned-scatter remediation is the wrong fix for a path that simply is not there.
  if [ -e "$HOME/.engram/$p" ] && [ ! -L "$HOME/.engram/$p" ]; then
      echo "NOTE: ~/.engram/$p is a real directory, not a symlink — see engram-upgrade's pinned-scatter remediation"
  fi
done
```

**PASS:** the plugin tree exists and both compatibility paths resolve to
existing targets. **FAIL:** either path is absent, dangling, or unresolvable
(a `NOTE:` line means the path is a real directory rather than a symlink — it
resolves, but it is pinned to whatever it last held). Repoint a dangling
symlink to `~/.engram/marketplace/plugins/engram/<path>`; preserve a real
directory and follow `engram-upgrade`'s pinned-scatter remediation instead of
replacing it blindly.

---

## Step 1 — MCP server round-trip

Call `engram_stats`. The MCP server must answer and the graph DB must be readable.

```
engram_stats({})
```

**PASS:** the response includes a non-empty `node_counts_by_type` mapping and a
numeric `health_score` without error. (`health_score` is currently reported on
a 0–100 scale; see #2940 for the old-vs-v2 formula-surface decision.)

**FAIL:** the tool call errors, times out, or returns an empty response.

Remediation:
```bash
# Check if the server process is running
# -u: own seat only; never -a — on Darwin -a re-admits ancestors, so the else branch never fires (#2132/#2133).
if pids=$(pgrep -u "$(id -u)" -f "launch-engram-server.sh|engram/server.py"); then
    echo "$pids"
else
    rc=$?
    [ "$rc" -eq 1 ] && echo "server process not found" || echo "could not check: pgrep exited $rc"
fi

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

# Inspect the newest marker. The hook writes a fresh marker; it does not touch
# warm-briefing.md, so that file's mtime is not a delivery signal.
marker=$(ls -t ~/.engram/sessions/*.json 2>/dev/null | head -1)
python3 -m json.tool "$marker"
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
# Check the surfaced cursor timestamp (portable across GNU/Linux and macOS):
python3 -c "import os,time; p=os.path.expanduser('~/.engram/forum-surfaced-cursor.txt'); print(time.ctime(os.path.getmtime(p)), p)"

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
# -u: own seat only; never -a — on Darwin -a re-admits ancestors, so the else branch never fires (#2132/#2133).
if pids=$(pgrep -u "$(id -u)" -f "engram-surface-daemon"); then
    echo "$pids"
else
    rc=$?
    [ "$rc" -eq 1 ] && echo "daemon not running" || echo "could not check: pgrep exited $rc"
fi
# Restart the daemon if absent (it auto-restarts on next hook fire if the start-daemon script is registered):
bash ~/.engram/marketplace/plugins/engram/hooks/start-engram-daemon.sh
```

---

## Step 4 — Surface daemon alive + semantic recall

The surface daemon powers semantic recall. Test it directly.

```bash
# Check the daemon process:
# -u: own seat only; never -a — on Darwin -a re-admits ancestors (else branch never fires), so take argv from ps (#2132/#2133).
if pids=$(pgrep -u "$(id -u)" -f "engram-surface-daemon"); then
    ps -o pid=,args= -p "$(echo "$pids" | tr '\n' ',' | sed 's/,$//')"
else
    rc=$?
    [ "$rc" -eq 1 ] && echo "daemon not running" || echo "could not check: pgrep exited $rc"
fi
```

Then call `engram_query` with a term you know is in the graph (any recent topic):

```
engram_query({"payload_json": "{\"query\": \"<a term from your recent work>\", \"top_k\": 3}"})
```

**PASS:** daemon process exists; `engram_query` returns ≥ 1 result (semantic recall working).

**FAIL — daemon not running:** semantic recall falls back to FTS-only (weaker recall, no embedding-based
matches). Not a hard block, but degrades surfacing quality.

Remediation:
```bash
bash ~/.engram/marketplace/plugins/engram/hooks/start-engram-daemon.sh
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

**PASS:** `engram_diagnose` returns structured `structure`, `epistemic`, and
`memory` sections without a tool error. Its `health_score` is numeric. Do not
require it to equal the viz health-v2 score while #2940 is unresolved: they are
currently different live formula surfaces.

**FAIL:** the tool errors, returns an empty response, or omits those core
sections. Proceed to the manual schema check.

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

## Step 5.5 — FTS ranking-path integrity

`nodes_fts` is an external-content FTS5 table: the index stores tokens/positions only, and *ranking* a
match (`ORDER BY rank`, which `engram_query` and `_similar_existing_matches`'s dedup check both use)
reads auxiliary data to score it. If that data has desynced for one term's postings, ranking raises
SQLite's generic `database disk image is malformed` *for that term* — while `PRAGMA quick_check` still
says "ok", plain `MATCH` (no ranking) still works, and every other term ranks fine. So this is invisible
to every check this exam already runs, and stays silent until a real query happens to rank the one bad
term (engram-alpha#2678, forum #398).

```bash
python3 -c "
import sys, sqlite3, os
sys.path.insert(0, os.path.expanduser('~/.engram/marketplace/plugins/engram'))
import engram_backup
p = os.path.expanduser('~/.engram/knowledge.db')
conn = sqlite3.connect(f'file:{p}?mode=ro', uri=True)  # read-only -- this step reads a possibly-corrupt graph
print(engram_backup.check_fts_integrity(conn))
"
```

**PASS:** `{'status': 'ok', 'probed_terms': N}` — every vocabulary term both plain-matched and ranked
clean, with nothing left unclassified.

**FAIL:** `{'status': 'mismatch', 'problems': [{'term': ..., 'error': ..., 'stage': 'ranked_match'}, ...],
'unprobed': [...]}` — CONFIRMED ranking-path corruption: at least one term's plain match succeeded but
ranking it raised. `unprobed` may also be present alongside (see PARTIAL below) but never changes this
verdict once `problems` is non-empty. A real, actionable finding. Consider `engram_backup.rebuild_fts_index`
(own-substrate authority — snapshot `knowledge.db` first, then rebuild, then re-run this check to confirm)
— though note the repair's effectiveness against this failure class is not yet verified; confirm rather
than assume. This is a footnote fix for one seat, not a substitute for reporting it — the cause (how the
corruption happened) is worth a forum post regardless.

**PARTIAL (an unknown, not a confirmed problem — investigate, don't treat as FAIL):**
`{'status': 'partial', 'unprobed': [{'term': ..., 'error': ..., 'stage': 'plain_match'}, ...], 'probed_terms': N}`
— every term that WAS probed came back clean, but at least one term's PLAIN (unranked) query itself
raised, so it was never reached by the ranked check at all. Not classified as syntax-vs-corruption (the
error text is the same generic message either way); on the default tokenizer no legitimate vocab term
produces this, so an entry here is worth investigating — it may be damage worse than a `problems` entry
(bad enough to break even the simple path), or it may be a real syntax restriction on a tokenizer this
design hasn't been tested against. Kept distinct from FAIL deliberately: reporting this as confirmed
damage would risk a false alarm on an untested tokenizer, the exact failure shape an earlier version of
this design was rejected for.

**ADVISORY (not a FAIL on a real install):** `{'status': 'unavailable', 'reason': '...'}` — the probe
itself couldn't run AT ALL (no `nodes_fts` or `nodes_fts_vocab` table, e.g. a fresh/minimal DB) — distinct
from PARTIAL, where the probe ran and most terms were checked. Shouldn't happen against a normal live
`knowledge.db`; if it does, note the reason and move on rather than treating it as a schema failure.

**Design note, so a future edit doesn't quietly reintroduce noise or blindness:** three designs were
tried, in order, each falsified by the next real measurement against the one graph in the fleet that
actually had this bug — see `check_fts_integrity`'s own docstring for the full history. Neither of FTS5's
`'integrity-check'` special-command variants works: the *plain* form tolerates ENGRAM's by-design
retracted-row exclusion (#781) but reported CLEAN against a byte-copy of the affected graph — a false
negative on the exact case this step exists to catch. The `rank=1` ("more thorough") form correctly
caught that same real corruption, but false-positives on *any* graph with even one retracted node, and
that false positive survives healing the retracted rows out of a scratch copy after the fact — not just
a tuning problem, a structural one. This probes real `MATCH` vs `MATCH ... ORDER BY rank` for terms from
`nodes_fts_vocab` instead — retraction-immune by construction (retracted rows never enter a `MATCH`
result set), and it targets the layer that's actually damaged. **A first version of this probe sampled
only the top-k highest-document-frequency terms (k=30) and was validated against the affected graph — it
caught the one poisoned term then known.** A *full-vocabulary* sweep of that same graph then found 24
poisoned terms, not 1 — the one caught by the k=30 sample was the ONLY one in the top-30 by frequency; the
other 23 spanned mid/low frequency and bare numerals, uncorrelated with rank. No k a person would pick
would have caught more than one of twenty-four. So this sweeps the full vocabulary by default (`probe_k`
is `None`) rather than sampling — confirmed cheap on both the largest known affected seat (1.5s over
26,745 terms) and a second, healthy seat (0.53s over 13,805 terms, 33 retracted nodes, zero false
positives). `probe_k` survives only as an explicit opt-in sampling escape hatch for a future graph where
full-vocab cost turns out not to be negligible — never lower the default without a fresh cost measurement
on a graph at least as large as the ones above.

---

## Step 6 — Viz server health (if configured)

Skip this step if you do not use the viz dashboard (T2 feature, may not be installed).

```bash
agent=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('~/.engram/config.json')))['agent_name'])")
curl -s "http://localhost:5001/api/health?agent=$agent" | python3 -m json.tool
```

**PASS:** JSON response for the expected `agent` with a numeric `health_score`.
A populated value means the viz server is up and can read that seat's graph DB.

**FAIL:** connection refused, non-200 response, or no `health_score` in the JSON.

```bash
# Check if it's supposed to be running (systemd service or manual start).
# tools/operator-setup-viz.sh is the canonical path (idempotent; its last
# step is this same restart) -- use the bare restart below only for a
# code-only refresh where setup is already known-good.
systemctl --user status engram-viz-user.service 2>/dev/null || echo "no systemd service"
# Manual start (viz/server.py uses relative imports -- must run as a
# module with the plugin engram dir as the working directory, not as a
# direct script path):
cd ~/.engram/marketplace/plugins/engram && python3 -m viz.server &
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
python3 tools/selftest.py --smoke 2>&1 | tail -20
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
