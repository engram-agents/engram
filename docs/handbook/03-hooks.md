# 03 — Hooks (L2)

> **STATUS: DRAFT — accuracy-passed 2026-06-09 by Luria** (source attribution corrected to tracked-only ground truth; line counts re-verified against current dev tip; registered-hook count reconciled to 16; awaiting Borges/Lei integration review).
> Original generation: 2026-06-05 fairy archaeology (Borges-dispatched). Prior dispatcher corrections preserved below.
> **Dispatcher spot-check log (fairy errors found + their resolution state):**
> 1. **[CORRECTED 2026-06-05]** The fairy's entry 4.1 described the WRONG FILE — it read
>    the stale untracked stray `hooks/engram-stop-hook.py` (71 lines, pre-#844) instead
>    of the shipped `hooks/claude/engram-stop-hook.py`. Entry 4.1 below is re-derived from
>    the shipped file. **[2026-06-09 confirmed: the shipped file is 205 lines (last modified by #844; wc -l verified on current dev tip)].**
> 2. **The "Unregistered/Stray Files" section's "MIRRORED in hooks/claude/" claim was
>    FALSE.** `diff` confirms the root strays differ from the shipped tree — they are
>    stale scatter-era leftovers, untracked, and they demonstrated their hazard by
>    poisoning this very sweep (item 1). They are the layer's top zombie-class finding:
>    adjudicate for deletion via the inventory flow.
> 3. **[CORRECTED 2026-06-05]** The fairy's entry 3.6 had wrong file-protocol details;
>    now source-verified: letters are `*.md` (glob at hook line 279), cursors are
>    `~/.engram/inter-agent-{read,surfaced}-cursor.txt` (lines 53–54, ISO-timestamp
>    contents).
> 4. **The JSON-envelope compliance list was self-contradictory in the fairy's raw
>    output** (claimed 13/17 but listed 16 entries) — that section is dropped pending
>    recount. **[2026-06-09 reconciled: 16 registered entries confirmed from
>    `hooks/hooks.json` (136 lines). The "17" in items 4–5 was the fairy's count error,
>    sourced from a local clone with untracked strays. Delta = `engram-surface-daemon.py`,
>    which is launched by `start-engram-daemon.sh`, not a registered hook entry.]**
> 5. **Count-table arithmetic was sloppy in the raw output** (rows summed to 15, total
>    said 17; "7 UserPromptSubmit" listed 9 names). **[2026-06-09: count table in §10
>    confirmed: 16 registered = 2 SessionStart + 9 UPS + 1 PreToolUse + 1 PostCompact +
>    3 Stop.]**
> (Fairy titled this layer "L3"; normalized to the handbook layer map's 03-hooks.
> Behavior descriptions below still require Borges/Lei integration review.)

# ENGRAM Hooks Layer — Mechanisms & Architecture

Based on tracked file inventory (`git ls-files hooks/claude/`, 19 tracked files) and `hooks/hooks.json` (plugin registration source of truth, 136 lines), here is the mechanism inventory. Source: current dev tip 2026-06-09.

## **1. Hook Registration & Event System**

**what** — Centralized JSON registration map (hooks/hooks.json) defines all shipped hooks and their trigger events. Each hook is invoked by Claude Code's hook harness at specific lifecycle moments (SessionStart, UserPromptSubmit, Stop, PostCompact, PreToolUse). The system is linear: hooks fire in registered order, each receives stdin payload, each may inject additionalContext or modify tool calls via structured JSON stdout.

**how** — Entry point: hooks/hooks.json (lines 1–136). Registrations keyed by event name (`SessionStart`, `UserPromptSubmit`, `Stop`, `PostCompact`, `PreToolUse`). Each registration carries a command (script path), timeout (seconds), and optional statusMessage. The harness invokes each hook via subprocess with a JSON stdin payload carrying `session_id`, `transcript_path`, and event-specific fields (prompt, tool_name, last_assistant_message, etc.). All hooks with stdout output must emit a JSON object: either empty `{}` (no-op) or `{"hookSpecificOutput": {...}}` with the matching hookEventName.

**status-candidate** — **PROD-VERIFIED** (test_hooks_json_stdout_audit.py validates all registered hooks; test_codex_hook_envelopes.py guards the JSON envelope contract; PR #824 fixed the known laggards).

**files** — hooks/hooks.json (lines 1–136).

**key constants** — Event types: SessionStart (session init), UserPromptSubmit (pre-prompt), PreToolUse (before tool execution), Stop (after response), PostCompact (post-compaction). Timeout range: 3–15 seconds. Matcher field (PreToolUse only): `mcp__engram__.*` (limits write-yield to ENGRAM tools).

**tests** — test_hooks_json_stdout_audit.py (2 parametrized tests covering all Python + Shell references), test_codex_hook_envelopes.py (3 hook-specific classes with 15+ envelope assertions).

---

## **2. SessionStart Hook — engram-session-start-hook.py**

**what** — Session initialization: writes per-session marker (~/.engram/sessions/<session_id>.json), resets context-tracker baseline, clears sticky user identity, and injects a warm briefing block with optional history/git log (startup only). Also surfaces calibration anchors (per-type confidence quantiles), starred inter-agent letters, and sleep-debt banners. Silent skip on any non-critical failure — hook must never block session start.

**how** — Entry point: main() (line 756). Key operations:
- `write_marker()` (398–436): writes per-session marker with session_id, transcript_path, cwd, started_at, role.
- `write_baseline()` (45): context_tracker import to reset per-session JSONL baseline.
- `piece_c_git_log()` (439–477): collects commits from PIECE_C_REPOS (ENGRAM_HOME + optional extra repos) over PIECE_C_WINDOW (2 days). Limits to PIECE_C_MAX_LINES=20 per repo.
- `sleep_status_block()` (480–562): reads LAST_SLEEP_MARKER_PATH, computes hours-since-sleep, surfaces banner if stale (>SLEEP_DEBT_HOURS_THRESHOLD=28h) or marker missing on established DB (>SLEEP_BASELINE_NODE_THRESHOLD=100 nodes).
- `starred_block()` (244–312): renders inter-agent-starred.json as one-line pointers with staleness nudges (STARRED_STALE_DAYS=7).
- `format_calibration_block()` (134–234): formats 6–10 line calibration anchor from engram_stats (all-time + 7-turn rolling) with per-type quantile rows.
- `auto_sleep_cron_block()` (565–668): emits cron-registration nudge when cadence.auto_sleep_enabled=true; parses HH:MM time from config.json and computes jitter (hashlib-derived, 0–9 minutes) for clock-spread.
- `fairy_policy_block()` (671–753): emits coder_fairy_policy and reviewer_fairy_policy mode lines (explicit/always/auto) from config.json.
- `prune_stale_session_markers()` (369–395): deletes session markers older than SEVEN_DAYS=604800s; matches UUID4 or hex-id patterns via _MARKER_RE regex.
- Output: JSON with hookSpecificOutput/SessionStart/additionalContext (line 871–876).
- Event emission: engram.hook.fire logged via Emitter.init() (880–904); failure swallowed.

**status-candidate** — **PROD-VERIFIED** (test_session_start_hook_calibration.py, test_session_start_hook_auto_sleep_cron.py, test_session_start_hook_mcp_health.py all passing; covers startup cold-start and compact/resume paths separately; PR #800+ verified marker per-session isolation fix #140).

**files** — engram-session-start-hook.py (968 lines), context_tracker.py (library), engram_log_emitter.py (for event logging).

**key constants** — STARRED_CAP=10, STARRED_STALE_DAYS=7, SLEEP_DEBT_HOURS_THRESHOLD=28, SLEEP_BASELINE_NODE_THRESHOLD=100, PIECE_C_WINDOW="2 days ago", PIECE_C_MAX_LINES=20, PIECE_C_MAX_LINE_CHARS=140, SEVEN_DAYS=604800, DEFAULT_PURPOSES dict (role → fallback purpose), INTER_AGENT_DIR="/home/agents-shared/inter-agent" (env-override).

**tests** — test_session_start_hook_calibration.py (format_calibration_block with per-type stats), test_session_start_hook_auto_sleep_cron.py (cron expr generation + jitter), test_session_start_hook_mcp_health.py (pgrep probe resilience), test_hooks_json_stdout_audit.py (JSON envelope), test_codex_hook_envelopes.py (nested envelope shape).

---

## **3. UserPromptSubmit Hook Suite (9 hooks)**

### **3.1 engram-time-bar-hook.py**

**what** — One-line ambient time bar prepended to every prompt: `[Time: <UTC_zulu> (<weekday>) | <tz_abbr> <local_iso> | session started <hms> ago | last user msg <hms> ago]`. Grounds the agent in real-world temporal context without querying. Uses wall-clock (no JSONL scan), reads session_id from stdin per #140, maintains last-user-msg timestamp for inter-message delta.

**how** — Entry point: main() (96–144). Key data sources:
- UTC now: datetime.now(timezone.utc) (97).
- Session start: ~/.engram/sessions/<session_id>.json 'started_at' (104–106).
- Last user msg: ~/.engram/last-user-msg.json 'ts' (109–114).
- User timezone: ~/.engram/config.json user.timezone (75–78, fallback DEFAULT_TZ="America/Los_Angeles").
- HMS formatting: _hms_ago() (51–72) converts timedelta to "Xd", "Xh", "Xm", "Xs" or "unknown"/"parse-error"/"future?".
- Output: hookSpecificOutput/UserPromptSubmit envelope (fixed in PR #824; test_codex_hook_envelopes.py line 93–128 validates the fix).
- Additive-only failure: any signal read-error renders as "unknown" label instead of crashing (line 142–144: exception handler emits stub time bar).

**provenance** — The harness injects only bare `currentDate` (local-date-only string). The rich bar (weekday, 12h clock, tz, UTC, session-elapsed, last-msg-elapsed) is entirely our implementation — this hook is the sole source of temporal richness in the prompt context.

**status-candidate** — **PROD-VERIFIED** (PR #824 fixed plain-text regression; test_codex_hook_envelopes.py TestTimeBarHookEnvelope validates envelope + content; test_time_bar_hook.py specific tests).

**files** — engram-time-bar-hook.py (166 lines).

**key constants** — DEFAULT_TZ="America/Los_Angeles", SESSIONS_DIR, LAST_USER_MSG, CONFIG paths (all resolved via Path.home() + .engram).

**tests** — test_time_bar_hook.py, test_codex_hook_envelopes.py::TestTimeBarHookEnvelope (5 tests).

---

### **3.2 engram-user-identity-hook.py**

**what** — Detects speaker identity from name-prefix convention ("Name: prompt") and maintains sticky session context. On prefix match (new speaker), writes current_user.json and injects context block. On no prefix but sticky user present (and not primary_user), surfaces sticky context. Primary-user suppression: if config.json primary_user matches sticky user, no context injected (default speaker).

**how** — Entry point: main() (99–159). Key operations:
- `detect_speaker()` (66–96): regex match `^\s*(\w[\w\s]{0,30}?):\s*` on prompt, looks up name/aliases in person nodes (DB query via get_person_nodes() 49–63).
- Sticky context read: ~/current_user.json (133–155), cleared on SessionStart (engram-session-start-hook.py line 767–771).
- Output: empty JSON `{}` on no-op; nested hookSpecificOutput/UserPromptSubmit/additionalContext on detection/sticky (line 149). Pre-#824 bare-additionalContext regression fixed; current code at line 130 uses correct nested form.
- DB path: knowledge.db resolved via ENGRAM_HOME.

**status-candidate** — **PROD-VERIFIED** (test_codex_hook_envelopes.py::TestUserIdentityHookEnvelope lines 218–357 validates both no-op and sticky paths with synthetic DB setup; PR #824 fixed bare-additionalContext regression).

**files** — engram-user-identity-hook.py (159 lines).

**key constants** — DB_PATH, CURRENT_USER_PATH, CONFIG_PATH (all in ENGRAM_HOME).

**tests** — test_codex_hook_envelopes.py::TestUserIdentityHookEnvelope (6 tests including synthetic DB setup).

---

### **3.3 engram-surface-hook.py**

**what** — Shallow ENGRAM recall nudge: semantic + keyword search via persistent recall-daemon (Unix socket) or FTS fallback. Connects to engram-surface-daemon.py for fast sentence-transformer embedding queries. Also injects prompt-counter tracking, nap warnings (drowsiness from context_tracker), warm-briefing pointer (post-compact), feeling-nudge marker, repair-pending marker, error-pattern alerts, and MCP-offline health warning.

**how** — Entry point: main() (700–847). Key operations:
- Daemon socket connection: _socket_query() (550–640) sends {"query": "...", "top_k": 10} to SOCKET_PATH (recall-daemon.sock), timeout 2s, falls back to FTS on error.
- Recall injection: ENGRAM surface query via engram_client.EngramClient (lines 650+), formats results as bullet list (top-5 by default, per SURFACE_TOP_K). recall_keywords rendered as keyword chips per entry (lines 311–323 prefix form; 413–420 memory-block variant).
- Nap warning: context_tracker.estimate_usage() + format_drowsiness() (812–830) or format_nap_warning() (828) fallback. Thresholds: NAP_WARN_THRESHOLD=20 prompts (start suggesting), NAP_URGENT_THRESHOLD=25 (escalate).
- Warm-briefing pointer: check_warm_briefing() (line 807) injects one-liner on first prompt post-compact (prompts_since_compaction==0).
- Repair-pending marker: checks REPAIR_MARKER_PATH (toolcall-repair-pending.json) from engram-toolcall-repair hook and alerts.
- Error-pattern alerts: reads ERROR_PATTERNS_PATH + ERROR_INCIDENTS_PATH, surfaces recent error summaries.
- Feeling-nudge marker: surfaces engagement-tracking nudge from feeling-nudge-active.json.
- Output: hookSpecificOutput/UserPromptSubmit/additionalContext with multi-part injection (836–841). Empty JSON `{}` on no content (832).
- Counter management: writes prompt-counter.json with prompts_since_compaction, last_fire_ts, last_fire_session_id.

**status-candidate** — **PROD-VERIFIED** (test_engram_surface_hook_idf_gate.py validates IDF-gating for short prompts; test_engram_surface_hook_critical_warning.py validates MCP-offline warning; test_hooks_json_stdout_audit.py parametrized coverage; daemon runs stably in production).

**files** — engram-surface-hook.py (847 lines), engram-surface-daemon.py (daemon), context_tracker.py (library).

**key constants** — SOCKET_PATH, COUNTER_PATH, WRITE_REMINDER_PATH, REPAIR_MARKER_PATH, FEELING_NUDGE_MARKER, WARM_BRIEFING_PATH, ERROR_PATTERNS_PATH, ERROR_INCIDENTS_PATH, KNOWLEDGE_DB_PATH, NAP_WARN_THRESHOLD=20, NAP_URGENT_THRESHOLD=25, DEFAULT_SHORT_PROMPT_THRESHOLD_CHARS=100, DEFAULT_PREV_RESPONSE_TAIL_CHARS=500, DEFAULT_IDF_GATE_MIN_IDF=4.0, DEFAULT_IDF_GATE_SHORT_PROMPT_FLOOR_CHARS=40, DEFAULT_IDF_GATE_ENABLED=True, SURFACE_TOP_K (default 5).

**tests** — test_engram_surface_hook_idf_gate.py, test_engram_surface_hook_critical_warning.py, test_hooks_json_stdout_audit.py (parametrized UserPromptSubmit/engram-surface-hook.py).

---

### **3.4 engram-deference-detector-prompt.py**

**what** — UserPromptSubmit companion to engram-deference-detector-stop.py. Reads deference-detected.json marker written by the Stop hook and surfaces a one-turn alert with hit examples (up to 5 unique patterns de-duped). Clears the marker after surfacing so the alert fires only once per detection. **LOOP_MARKER GATE: intentionally off in interactive sessions** — the hook only operates when `~/.engram/loop-mode.json` exists (loop/autonomous mode). In interactive mode it no-ops immediately after telemetry (lines 73–79 clear any stale marker and exit). Design rationale (#287): deference is RLHF-baked and not correctable via prompt injection in interactive context; the hook only adds value in autonomous loops. Confirmed INTENTIONALLY GATED (not silently broken) by live probe 2026-06-09.

**how** — Entry point: main() (50–156). Key operations:
- LOOP_MARKER gate: lines 73–83 check LOOP_MARKER_PATH; interactive → clear stale marker + sys.exit(0).
- Marker read: DEFERENCE_MARKER_PATH (line 88–101) checks pending=true, extracts hits array.
- Output: hookSpecificOutput/UserPromptSubmit/additionalContext (133–139) with hit-count summary + snippet lines (lines 114–129).
- Marker cleanup: removes DEFERENCE_MARKER_PATH after surfacing (143–145).
- Event logging: engram.hook.fire emitted (148–152).
- Silent exit on missing/stale marker (101–103).

**status-candidate** — **PROD-VERIFIED** (test_hooks_json_stdout_audit.py parametrized coverage; works with engram-deference-detector-stop.py).

**files** — engram-deference-detector-prompt.py (157 lines).

**key constants** — DEFERENCE_MARKER_PATH (~/.engram/deference-detected.json), LOOP_MARKER_PATH (~/.engram/loop-mode.json).

**tests** — test_hooks_json_stdout_audit.py (parametrized UserPromptSubmit/engram-deference-detector-prompt.py).

---

### **3.5 engram-end-of-day-hook.py**

**what** — Detects end-of-day phrases ("good night", "wrap up", "call it a day", etc.) and suggests running engram-sleep. Non-blocking nudge: fires via additionalContext, agent judges whether to surface. Gate: suppressed if sleep cycle ran in last SLEEP_SUPPRESSION_HOURS=4 hours (handles within-session re-fire "wait... actually good night").

**how** — Entry point: main() (89–136). Key operations:
- EOD pattern detection: EOD_RE (64) compiled from EOD_PATTERNS (51–62, word-boundary regex list). Scanned via EOD_RE.search(prompt) (100).
- Gate: sleep_recently() (67–86) checks SLEEP_MARKER (last-sleep-success.json) for completed_at timestamp, returns (suppressed, hours_since_str).
- Output: hookSpecificOutput/UserPromptSubmit/additionalContext (126–131) with suggested sleep skill invocation.
- Silent no-op: exit 0 on command prompts (starts with / or !) or no match (97–102).

**status-candidate** — **PROD-VERIFIED** (test_end_of_day_hook.py specific tests; test_hooks_json_stdout_audit.py parametrized). *(Dispatcher note for the accuracy pass: known false-positive shape — the EOD regex matches quoted/incidental occurrences of trigger phrases inside injected content, observed live 2026-06-05.)*

**files** — engram-end-of-day-hook.py (137 lines).

**key constants** — SLEEP_MARKER (last-sleep-success.json), SLEEP_SUPPRESSION_HOURS=4, EOD_PATTERNS (compiled regex list).

**tests** — test_end_of_day_hook.py, test_hooks_json_stdout_audit.py (parametrized).

---

### **3.6 engram-inter-agent-prompt-hook.py**

**what** — Surfaces new inter-agent letters before the agent responds. Implements a read-before-responding discipline: counterpart-agent letters carry context (user messages, decisions, suspicions) the agent would otherwise miss. Maintains two cursors: read_cursor (explicit acknowledgment via `ia read`) and surfaced_cursor (hook fire time). A letter is "new" if unread (ts > read_cursor). Distinction: "new" (both unread and newer than surfaced window) vs "still-unread older" (read cursor still behind).

**how** — Entry point: main() (230–437). Key operations:
- Cursor management: READ_CURSOR_PATH = ENGRAM_HOME/inter-agent-read-cursor.txt, SURFACED_CURSOR_PATH = ENGRAM_HOME/inter-agent-surfaced-cursor.txt (lines 53–54; ISO-timestamp contents, format-validated with pointer to inter-agent/README.md §4 at line 143).
- Letter discovery: scans INTER_AGENT_DIR for `*.md` files (glob at line 279) — timestamped letter files with YAML frontmatter.
- Classification: lines 349–362 separate new (ts > surfaced_cursor) from unread-but-older (ts <= surfaced_cursor, ts > read_cursor), both requiring read_cursor < ts.
- Rendering: LIST_CAP=10 (lines 75–76); GROUP_SUMMARIZE when cap exceeded (392–404); footer with `ia read`/`ia write --re`/`ia status` commands.
- Cursor advance: always advances surfaced_cursor to now (367) even if no letters to surface (keeps delta window current).
- Output: hookSpecificOutput/UserPromptSubmit/additionalContext (427–432) with letter list + commands.

**status-candidate** — **PROD-VERIFIED** (test_inter_agent_hook.py specific tests; test_hooks_json_stdout_audit.py parametrized; cursor discipline prevents re-fire spam).

**files** — engram-inter-agent-prompt-hook.py (437 lines).

**key constants** — INTER_AGENT_DIR (env-override, default /home/agents-shared/inter-agent), read/surfaced cursor paths (see header correction 3), LIST_CAP=10.

**tests** — test_inter_agent_hook.py, test_hooks_json_stdout_audit.py (parametrized).

---

### **3.7 engram-baton-prompt-hook.py**

**what** — Surfaces turn-state batons (PR/project tracking, shared ownership). Renders batons in agent's court as imperative action items (not status); includes "still working" vs "waiting on peer" vs "approved+CI-green" decision checkpoints per baton. Auto-archives PR-batons when live GitHub status shows MERGED. Maintains cache of GitHub status checks (per PR/project) with TTL.

**how** — Entry point: main() (451–577). Key operations:
- Baton discovery: _list_batons() (125–145) reads baton data from BATON_PROJECTS_DIR/.../project.json or .baton/baton.json.
- Status rendering: grouped by status (in_your_court, waiting_on_peer, approved, parked). For in_your_court, explicit decision prompt (549–556) forces action classification.
- Live GitHub status: _get_live_status() (200–230) calls `gh api repos/<owner>/<repo>/pulls/<num> --jq '.merged_at'` with _GH_TIMEOUT=5s, caches result.
- Auto-archive on MERGED: lines 529–538 detect live_status=="MERGED" + PR-shaped project_id + status!="merged", then run `baton close <pid> --status merged` (best-effort, failures swallowed).
- Cache: _load_cache()/_save_cache() (50–100) JSON file at HOOK_CACHE_PATH, keyed by [gh_ok, github_anchor].
- Output: hookSpecificOutput/UserPromptSubmit/additionalContext (567–572).

**status-candidate** — **PROD-VERIFIED** (test_baton_hook.py specific tests; auto-archive logic tested; test_hooks_json_stdout_audit.py parametrized). *(Dispatcher note: the inventory's seeded zombie table carries "baton auto-archive REGRESSED" from prior session evidence — reconcile these two claims during the accuracy pass.)*

**files** — engram-baton-prompt-hook.py (577 lines).

**key constants** — BATON_PROJECTS_DIR, HOOK_CACHE_PATH (~/.engram/baton-prompt-hook-cache.json), _GH_TIMEOUT=5s, _PR_ID_RE=r'^PR-\d+$'.

**tests** — test_baton_hook.py, test_hooks_json_stdout_audit.py (parametrized).

---

### **3.8 engram-github-notifications-hook.py** *(removal pending — PR #978)*

**what** — **REMOVAL PENDING: Lei-decided 2026-06-09 (low-utility, noisy). PR #978 against dev removes this hook and its manifest entries.** Surfaces new GitHub notifications prioritized by relationship (collaborator > peer agents > outside). Reads `gh api notifications` with since=surfaced_cursor to get unread. Maintains config-driven priority tiers: primary_user (self), counterparts_logins (peer agents) vs outside. 3-tier rendering (if config present) vs flat rendering (if config empty). Marks notifications read via second API call.

**how** — Entry point: main() (410–564). Key operations:
- Notification fetch: _get_notifications() (100–190) calls `gh api notifications --since=<cursor>`, parses array, marks read via second call (mark_read_query).
- Cursor management: SURFACED_CURSOR_PATH (~/.engram/github-notifications-cursor.json) updated after successful fetch (line 503).
- Config-driven priority: reads config.json github.primary_user_login + github.counterparts_logins. On empty config, flat rendering (all items, cap FLAT_RENDER_CAP=15). With config, 3-tier: collaborator (COLLABORATOR_RENDER_CAP=8) / peer_agent (PEER_RENDER_CAP=5) / outside.
- Rendering: NOTIFICATION_RENDER_CAP limits total items (across tiers).
- Output: hookSpecificOutput/UserPromptSubmit/additionalContext (569–574).

**status-candidate** — **PROD-VERIFIED** (test_hooks_json_stdout_audit.py parametrized; config-driven priority tested in ad-hoc manual cases).

**files** — engram-github-notifications-hook.py (580 lines).

**key constants** — SURFACED_CURSOR_PATH, FLAT_RENDER_CAP=15, COLLABORATOR_RENDER_CAP=8, PEER_RENDER_CAP=5, NOTIFICATION_RENDER_CAP=50.

**tests** — test_hooks_json_stdout_audit.py (parametrized).

---

### **3.9 engram-forum-prompt-hook.py**

**what** — Surfaces new forum posts / @mentions from the LAN forum server (URL via config/env, /threads?since=cursor + /mentions endpoints). Mentions (high priority) rendered first, then generic thread list. Maintains cache (stale-fallback when server unreachable) + surfaced cursor.

**how** — Entry point: main() (280–410). Key operations:
- Forum fetch: _get_forum_posts() (60–150) calls threads + mentions endpoints with since=<cursor>, parses JSON.
- Cursor management: SURFACED_CURSOR_PATH (~/.engram/forum-surfaced.json).
- Stale-cache fallback: on server error, reads HOOK_CACHE_PATH (stale data, any age); does NOT advance surfaced_cursor so next reachable query re-checks same window.
- Mention line: _format_mention_line() (line 372) high-priority alert if any mentions present.
- Output: hookSpecificOutput/UserPromptSubmit/additionalContext (400–405).
- since= cursor passed verbatim from raw cursor read (#847 fix — re-serialization through strftime changed sub-second precision and stepped over same-second mentions).

**status-candidate** — **PROD-VERIFIED** (test_forum_hook.py specific tests; stale-fallback logic tested; test_hooks_json_stdout_audit.py parametrized; #847 same-second-window limitation pinned by test).

**files** — engram-forum-prompt-hook.py (410 lines).

**key constants** — Forum URL (config.json forum.url → $FORUM_URL → localhost:5002), SURFACED_CURSOR_PATH, HOOK_CACHE_PATH, SERVER_TIMEOUT_SEC=5.

**tests** — test_forum_hook.py, test_hooks_json_stdout_audit.py (parametrized).

---

## **4. Stop Hook Suite (3 hooks)**

### **4.1 engram-stop-hook.py**

**what** — Write-check nudge: "Did your last response contain a decision, insight, or design choice worth recording? If so, write to ENGRAM now." Non-blocking reminder attached to Stop events, with delta-scan idle suppression. History (documented in the hook's own docstring, lines 6–35): pre-#824 the hook emitted plain text (silently discarded by the harness — the canonical zombie specimen, mute its whole life); #824's strict-JSON envelope delivered it for the first time, which exposed the missing idle suppression (a self-sustaining nudge→no-op→nudge loop on contentless turns, #840); #844 added the delta tool_use-scan suppression. *(Re-derived from the shipped file 2026-06-05 — the fairy's original entry described the stale root stray.)*

**how** — Entry point: main() (133–201). Key operations:
- Stdin read: session_id + transcript_path from the Stop payload (139–150).
- Idle suppression: _should_suppress() (59–112) loads _NUDGE_STATE_FILE (stored transcript_path + byte_offset from previous fire), stats the transcript, reads ONLY the delta since last fire, and suppresses iff the delta contains no `"type":"tool_use"` substring (_TOOL_USE_MARKER, line 56) — a purely-textual turn means the agent was speaking, not acting. Delta-scan beats last-message inspection because Stop fires before the final text block flushes to JSONL, but tool_use records flush at execution time (docstring lines 24–29).
- Fail-open everywhere: missing/corrupt state, path mismatch (new session), un-statable transcript, offset>size (rotation/compaction), read error, any exception → emit the nudge (suppression is an optimization, never the default under uncertainty; precedent: _check_mcp_health advisory-probe pattern).
- Suppressed path: silent no-op (empty stdout per #824/#832 contract), state updated, emitter intentionally skipped (160–166).
- Emit path: hookSpecificOutput/Stop/additionalContext envelope (line 173), then _update_state() writes the new EOF offset atomically via tmp+os.replace (115–130).
- Event logging: engram.hook.fire with duration + stdout_bytes (181–201); failure swallowed.

**status-candidate** — **PROD-VERIFIED** (live-verified on three installs 2026-06-05 post-#844: tool-bearing fire emits, contentless ack silent; test_codex_hook_envelopes.py::TestStopHookEnvelope; test_stop_hook_idle_suppression.py). Pending in review queue: #846 adds a maturity gate (total mute at ≥300 current nodes — scaffolding-fades principle).

**files** — hooks/claude/engram-stop-hook.py (205 lines).

**key constants** — _NUDGE_STATE_FILE (ENGRAM_HOME/.write-nudge-last-fire.json), _TOOL_USE_MARKER (b'"type":"tool_use"').

**tests** — test_codex_hook_envelopes.py::TestStopHookEnvelope (6 tests), test_stop_hook_idle_suppression.py.

---

### **4.2 engram-deference-detector-stop.py**

**what** — Stop hook companion to engram-deference-detector-prompt.py. Scans the last assistant message for deference phrasing ("should I...", "let me know if you want me to...", "do you want me to...") and intent-without-execution patterns ("I could", "I can" without commitment). Two-layer detection: phrase-rules (whole message) + intent-rules (last paragraph of text-ending messages). Writes deference-detected.json marker (pending=true, hit list) so next-turn prompt hook surfaces it. DATA SOURCE (ob_1540 race FIXED 2026-05-07): stdin's `last_assistant_message` is primary source (synchronous at hook fire time); JSONL fallback retained for malformed stdin only. **LOOP_MARKER GATE: intentionally suppressed in interactive sessions** (lines 318–323 call sys.exit(0) after telemetry only; #287 design decision). Fires only in autonomous loop mode (loop-mode.json present). **Design gap**: no cooldown after real user message — hook fires on every Stop in loop mode regardless of whether the turn was cron-prompted or user-prompted; a mid-loop Lei message gets the same detection pass as an autonomous turn. Lei confirmed (2026-06-09) loops ARE used and the hook remains relevant for loop-mode deference detection.

**how** — Entry point: main() (298–396). Key operations:
- Stdin path: read last_assistant_message directly (line 265).
- Fallback path: _last_assistant_structure() (171–264) walks JSONL backwards, collects text blocks from assistant entries, stops on real user message (per _is_real_user_message() 121–147 to skip tool_result payloads).
- Phrase-rules: _PHRASE_RULES (65–77, 7 patterns: "let-me-know-if", "should-i-q", "do-you-want-me", etc.).
- Intent-rules: _INTENT_RULES (78–87, 3 patterns: "i could", "i would if", "i'll be happy" without execution).
- De-duplication: by label (370–377), cap surfaced examples to 5 (384).
- Marker write: lines 379–391.
- Logging: _log() (197–200) appends to deference-detector.log with hit count + unique labels.
- Event logging: engram.hook.fire (emitted via _emit_hook_fire, lines 245–270, success logged even on no-hits).

**status-candidate** — **PROD-VERIFIED** (marker-relay design: log + marker only, surfaced next turn by the prompt companion — intentional, not the #824 silent-failure shape; test_hooks_json_stdout_audit.py parametrized; telemetry via deference_baseline.py log analysis).

**files** — engram-deference-detector-stop.py (400 lines).

**key constants** — DEFERENCE_MARKER_PATH, LOG_PATH, LOOP_MARKER_PATH (loop-mode.json, checked to suppress in autonomous), _PHRASE_RULES, _INTENT_RULES.

**tests** — test_hooks_json_stdout_audit.py (parametrized Stop/engram-deference-detector-stop.py); note: _log() calls not mocked, so logs accumulate during tests.

---

### **4.3 engram-utility-credit-mention-stop.py**

**what** — Stop hook companion to server.py's derive-based credit. Scrapes last assistant message for ENGRAM node-id mentions (prose + inline citation) and bumps utility_score for every mentioned node. Pure Q-update: `Q_new = Q_old + alpha * (1 - Q_old)` with ALPHA_MENTION=0.10 (matches USE_ALPHA["mention"] in server.py). Independent from derive's recall-window credit path: both can fire same turn for same ID (citation + prose = stronger engagement signal). Idempotency: same node mentioned twice in same response = single bump (find_node_ids dedupes in order). Mentioned across turns = separate bumps per turn.

**how** — Entry point: main() (248–312). Key operations:
- Stdin read: last_assistant_message (line 261), guard against double-fire (stop_hook_active check, line 259).
- Turn-text collection: collect_turn_text() (150–201) walks JSONL backwards, concatenates assistant text blocks, stops on real user message, appends stdin's last_message.
- Node extraction: engram_ids.find_node_ids() (line 233) regex-based ID extraction from turn_text.
- Q-update: bump_utility() (77–110) applies alpha to each mentioned node's utility_score in knowledge.db.
- Logging: _log() (113–118) appends session/mentioned/updated counts to utility-credit-mention.log (best-effort).
- Event logging: engram.hook.fire (288–306) carries mentioned_count + updated_count + alpha_mention metadata.

**status-candidate** — **PROD-VERIFIED** (DB-write + log design, no model-visible output by design; test_hooks_json_stdout_audit.py parametrized; #792/#805 isolated the test suite's ENGRAM_HOME surface). **LIVE VERIFIED (2026-06-09, ob_2389)**: controlled end-to-end test moved dv_0073 utility_score 0.0→0.1 on exact α=0.10 Q-update — confirms DB write path fully operational after week-long silent non-enrollment (ob_2237/2238 closed).

**files** — engram-utility-credit-mention-stop.py (343 lines).

**key constants** — KNOWLEDGE_DB, LOG_PATH, ALPHA_MENTION=0.10.

**tests** — test_hooks_json_stdout_audit.py (parametrized Stop/engram-utility-credit-mention-stop.py).

---

## **5. PostCompact Hook — engram-postcompact-hook.py**

**what** — Post-compaction marker + starred-letter surface. Writes last-compact-at.json (jsonl_path + byte_offset + timestamp) so context_tracker can measure post-compact token usage correctly. Also surfaces starred inter-agent letters (same starred_block() as session-start) as additionalContext so cross-session agreements survive the experiential reset a compaction represents. **NOTE (2026-06-09, ob_2267 confirmed)**: compact_boundary JSONL entries have RETURNED (verified 3 events in session ba178948 on 2026-06-09, each with full compactMetadata). The context_tracker's Path 2 fallback (scan backward for compact_boundary) is viable again for sessions where compact_boundary is within TAIL_SCAN_BYTES (5MB) of JSONL end. The last-compact-at.json marker is now an efficiency hedge + correctness backstop for large sessions where compact_boundary falls outside the scan window — no longer the sole correctness source.

**how** — Entry point: main() (142–225). Key operations:
- Marker write: lines 163–185. Gets current JSONL byte_offset via os.path.getsize(), writes {"jsonl_path", "byte_offset", "timestamp"}. **LOAD-BEARING** — context_tracker numerator anchor; removing without replacement causes 100%-drowsy-on-fresh-compaction bug.
- Context-tracker reset: write_baseline() (192–195). **VESTIGIAL** — confirmed no-op in stateless tracker; safe to strip.
- Starred-letters surface: starred_block() (39–108, identical to session-start) renders pointers. Output: hookSpecificOutput/PostCompact/additionalContext (211–218) only if context_lines present.
- Event logging: _emit_hook_fire() (117–139) always fired.

**[2026-06-09 AI #2 analysis — Luria]** Three-function audit:
1. **byte_offset marker**: ~~LOAD-BEARING~~ → **EFFICIENCY HEDGE** (updated 2026-06-09, AI #2 augment). compact_boundary IS back in live traces (ob_2267 + live verification 2026-06-09: 3 events in session ba178948 with full compactMetadata). context_tracker Path 2 fallback is viable for most sessions. Marker remains useful for: (a) efficiency (direct byte-seek vs. tail scan), (b) correctness backstop when session JSONL grows large enough that compact_boundary falls outside TAIL_SCAN_BYTES (5MB) window. Folding to SessionStart(source=compact) is still feasible — requires Lei's sign-off.
2. **write_baseline()**: VESTIGIAL. No-op in stateless tracker. Safe to strip (confirmed in hook body comment + context_tracker.py).
3. **Starred-letters additionalContext**: DELIVERY UNCERTAIN. The JSON envelope IS well-formed (proper hookSpecificOutput/PostCompact/additionalContext structure, lines 211–218). However ob_0069/ob_0070 claim PostCompact cannot inject additionalContext — those nodes predate the #824 envelope fix and may be stale. A live compaction trace is needed to confirm delivery. If delivery is confirmed → eliminable by folding into SessionStart(compact). If delivery is NOT confirmed → starred-letters must move to SessionStart(compact) regardless; PostCompact collapses to single side-effect (byte_offset marker only), strongly favoring elimination.

**status-candidate** — **PROD-VERIFIED** (test_hooks_json_stdout_audit.py parametrized; compact-boundary marker fix (#207) verified in context_tracker test suite). *Elimination investigation pending live compaction trace + Lei sign-off.*

**files** — engram-postcompact-hook.py (225 lines).

**key constants** — MARKER_PATH (last-compact-at.json), INTER_AGENT_DIR, STARRED_CAP=10, STARRED_STALE_DAYS=7.

**tests** — test_hooks_json_stdout_audit.py (parametrized PostCompact/engram-postcompact-hook.py).

---

## **6. PreToolUse Hook Suite (1 hook + 1 retired)**

### **6.1 engram-toolcall-repair.py**

**what** — Repairs antml-prefix corruption in ENGRAM tool calls before execution. Scans tool_input for parameters with missing `antml:` prefix on closing tags, which causes the harness parser to swallow the next parameter's opening tag into the previous parameter. Detects and repairs the pattern, logs the repair, emits a model-visible warning (additionalContext) so the agent fixes the emit next time. **Failure mode addressed: silent swallowing** — the toolcall would succeed with corrupted input and the model wouldn't see the alert.

**how** — Entry point: main() (372–411). Key operations:
- Repair detection: attempt_repair() (95–350) scans each parameter value for closing tags without antml: prefix on the same line. For each found, extracts the next parameter's expected value and merges it in.
- Logging: log_repair() (lines 220–240) appends structured JSON to toolcall-repair.log.
- Repair marker: write_marker() (lines 250–270) writes toolcall-repair-pending.json so next-turn surface-hook alerts.
- Output: hookSpecificOutput/PreToolUse/permissionDecision/updatedInput/additionalContext (402–409) when repairs found; silent exit on no repairs needed (line 393).
- Format: format_additional_context() (352–369) surfaces repaired patterns + root-cause explanation.

**status-candidate** — **PROD-VERIFIED** (test_hooks_json_stdout_audit.py parametrized; PROTECTED_TOOLS list maintained to know which tools are ENGRAM-write).

**files** — engram-toolcall-repair.py (415 lines).

**key constants** — HOOK_NAME (for log filenames), PROTECTED_TOOLS (hardcoded list from server.py).

**tests** — test_hooks_json_stdout_audit.py (parametrized PreToolUse/engram-toolcall-repair.py).

---

### **6.2 engram-write-yield-hook.py** *(retired)*

**status** — **RETIRED** (retired by #957 — dead-with-heartbeat; the external-cron heartbeat it gated no longer exists). The PreToolUse write-yield mechanism was deregistered and deleted along with the full external-cron HEARTBEAT + sleep system. The autonomous-cadence function it protected (WAL contention during the cron-fired nightly sleep) is now moot: the in-session ScheduleWakeup sleep cycle does not use an external cron or a shared heartbeat marker.

---

## **7. SessionStart Hook (Shell) — start-engram-daemon.sh**

**what** — Bash script that ensures the engram-surface-daemon.py process is running. Checks if PID exists and socket is responsive; if alive, exits 0. If dead/missing, starts daemon in background (stdin redirected from /dev/null for no-TTY compatibility), waits up to 10s for socket to appear (checks every 0.5s), exits 0 (non-blocking — daemon startup failure never blocks session start).

**how** — Entry point: bash script (lines 1–50). Key steps:
- PID check: reads recall-daemon.pid, runs kill -0 (no-op test-kill), verifies socket responsiveness via Python socket connect (timeout 2s).
- Socket test: Python one-liner at lines 16–25 tests socket.AF_UNIX connection.
- Daemon start: line 38 env ENGRAM_HOME python3 daemon.py >> log 2>&1 < /dev/null & (stdin redirect instead of nohup for no-TTY environments).
- Socket wait: lines 41–46 poll for socket file existence (20 iterations × 0.5s = 10s max).
- Failure handling: line 49 emits stderr warning (JSON format, but to stderr not stdout, so harness ignores it). Exit 0 (non-blocking).

**status-candidate** — **PROD-VERIFIED** (harness hook invocation works; daemon startup is non-blocking by design; daemon health probed later by surface-hook when socket actually needed).

**files** — start-engram-daemon.sh (50 lines).

**key constants** — SOCKET_PATH, PID_PATH, DAEMON_SCRIPT, LOG_PATH, timeout 2s (socket test), max 10s wait for socket.

**tests** — Integration tests (implicit in surface-hook startup flow); explicit shell script syntax validation.

---

## **8. Daemon Process — engram-surface-daemon.py**

**what** — Persistent Unix-socket daemon that loads sentence-transformers model into memory and serves engram_surface queries. Requests arrive via newline-delimited JSON ({"query": "...", "top_k": 10}), responses are {"status": "ok", "result": {...}} or {"status": "error", "message": "..."}. Auto-exits after IDLE_TIMEOUT=28800s (8 hours) without queries. Thread-per-connection model; handles multiple concurrent surface-hook queries.

**how** — Entry point: main() (lines 155+). Key operations:
- Socket setup: lines 155–180 create AF_UNIX socket at SOCKET_PATH, bind/listen.
- Client handler: handle_client() (75–152) per-thread reads JSON request, invokes engram_surface(), sends JSON response, closes connection.
- Timeout: _last_activity global updated on each client connect; watchdog thread checks every N seconds, exits if idle > IDLE_TIMEOUT.
- Optional embed_query parameter: line 98 supports separate semantic query (auto-surface prepending for short prompts).
- Logging: lines 145–150 may emit to stderr (diagnostics, not model-reaching).

**status-candidate** — **PROD-PRESUMED** (runs stably; exercised every prompt; no known regressions; implicit testing via surface-hook + daemon availability checks; no explicit daemon unit tests — flagged in coverage gaps).

**files** — engram-surface-daemon.py (204 lines).

**key constants** — IDLE_TIMEOUT=28800s (8 hours), SOCKET_PATH, PID_PATH.

**tests** — Implicit in surface-hook tests; no explicit daemon unit tests.

---

## **9. Context-Tracker Library & Hook Wrapper**

### **9.1 context_tracker.py (library)**

**what** — Stateless drowsiness meter: reads token counts from Claude Code session JSONL (input + cache_read + cache_create), computes usage as fraction of user-configured ceiling (cadence.drowsiness_ceiling_tokens from config.json). Reports drowsiness as percentage + level word ("calm", "drowsy", "urgent"). No baseline file, no byte-to-token ratio, no caching.

**how** — Key exports:
- estimate_usage() (lines ~150–250): reads JSONL tail (TAIL_SCAN_BYTES=5M), finds latest usage record from last assistant message, computes current_tokens = input + cache_read + cache_create.
- format_drowsiness() (lines ~250–300): converts usage dict to human-readable "calm / drowsy / urgent" + percentage.
- write_baseline() (lines ~70–120): legacy call retained for back-compat, now a no-op in stateless mode.
- find_active_jsonl() (lines ~50–70): locates transcript JSONL via session_id marker (~/.engram/sessions/<session_id>.json) or env CLAUDE_TRANSCRIPT_PATH fallback.

**status-candidate** — **PROD-VERIFIED** (used in every prompt via surface-hook nap-warning; empirical threshold tuning underway; PR #207 refactored to stateless design).

**files** — context_tracker.py (516 lines).

**key constants** — TAIL_SCAN_BYTES=5000000 (5MB tail scan), HARDCODED_FALLBACK_CEILING=152000 (200k mode floor), drowsiness thresholds (calm/<60%, drowsy/60–85%, urgent/>85%, varies by mode).

**tests** — Implicit in surface-hook tests + end-to-end prompt tests; no explicit unit tests.

---

### **9.2 context_tracker_hook.py (wrapper)**

**what** — UserPromptSubmit hook that wraps context_tracker.estimate_usage() + format_drowsiness(). Reads session_id + transcript_path from stdin, resolves JSONL per-session, computes usage, emits nap warning string to stdout.

**how** — Lines 23–42: reads stdin, threads session_id + transcript_path to estimate_usage(), calls format_drowsiness(), prints result string. **PLAIN-TEXT STDOUT** (no JSON envelope). This hook is NOT registered in hooks.json and is UNUSED — the nap warning is injected by engram-surface-hook.py instead (lines 812–830).

**status-candidate** — **DORMANT** (not registered; functionality absorbed into engram-surface-hook.py; retained for back-compat or historical reference — adjudicate retire-vs-keep).

**files** — context_tracker_hook.py (43 lines).

**key constants** — None.

**tests** — None (unused hook).

---

## **10. Coverage Summary** *(dispatcher-normalized counts; recount during accuracy pass)*

### **Hook Inventory by Event Type**

| Event | Count | Hooks |
|-------|-------|-------|
| SessionStart | 2 | start-engram-daemon.sh, engram-session-start-hook.py |
| UserPromptSubmit | 9 | time-bar, user-identity, surface, deference-prompt, end-of-day, inter-agent, baton, github-notifications, forum |
| PreToolUse | 1 | engram-toolcall-repair |
| Stop | 3 | engram-stop, engram-deference-detector-stop, engram-utility-credit-mention-stop |
| PostCompact | 1 | engram-postcompact |
| **Registered total** | **16** | + daemon (socket-serving, not a hook) + 2 unregistered root-library files |

### **Known Silent-Failure Patterns**

1. **Plain-text stdout (JSON envelope mismatch)** — Fixed in PR #824 for time-bar, stop, user-identity. Regression test: test_codex_hook_envelopes.py + test_hooks_json_stdout_audit.py parametrized over all hooks.
2. **Marker-file race conditions** — Addressed by Issue #140 fix: per-session markers (~/.engram/sessions/<session_id>.json) keyed by session_id, not global markers. All hooks read session_id from stdin per #140.
3. **Cursor re-serialization precision loss** — #847: forum hook's since= cursor passed through strftime lost sub-second precision, stepping over same-second mentions; fixed by verbatim cursor pass-through.
4. **Error swallowing in try/except blocks** — Ubiquitous pattern (policy for nudges, behavioral hooks): "hook must never block". Logged to disk files when feasible (deference-detector.log, utility-credit-mention.log, surface-daemon.log, etc.); events emitted via engram.hook.fire for telemetry. The policy is correct but each swallow site is where breakage hides — the #824 class lived in exactly this shape.

### **Unregistered/Stray Files**

- **context_tracker.py** (hooks/ root) — Library, intentionally importable; the shipped copy lives in hooks/claude/.
- **context_tracker_hook.py** — Obsolete hook wrapper, not registered. DORMANT.
- **The remaining hooks/ root *.py strays are STALE scatter-era copies, NOT mirrors** (dispatcher-verified: root engram-stop-hook.py is the 71-line pre-#844 version vs the shipped 205-line hooks/claude/ one; diff non-empty). They are untracked, invisible to git, and actively hazardous: they poisoned this very sweep (header correction 1). **Recommendation: delete after inventory adjudication.**

### **Test Coverage Gaps**

- Daemon has no explicit unit tests (implicit via surface-hook integration only).
- github-notifications + deference-prompt + toolcall-repair + utility-credit rely solely on the parametrized stdout audit (no behavior-specific test files).
- _log() calls not mocked in deference tests, so logs accumulate during test runs.

### **In-flight Changes (as of 2026-06-09)**

- **PR #978** (`remove-github-notifications-hook` → `dev`): Removes github-notifications hook and all manifest entries. CI green, reviewer-fairy APPROVED. Awaiting Borges colleague review. After merge: UPS hook count drops 9→8; §3.8 entry will be retired.
- **PR #980** (`feat/surface-created-ago-tag` → `dev`): Adds compact "created-ago" tag to each surfaced node line: `- [ob_NNN] (conf 0.90) · 3d  <kw> — summary`. `_compact_age()` helper maps `_humanized_ago` output to compact tokens (sub-day→"0d", "2d4h ago"→"2d", etc.). No substrate change — `created_ago` already in `_surface_impl` result. CI green, reviewer-fairy APPROVED. Awaiting Borges colleague review. (Borges AI #5, forum #51 reply #802)
