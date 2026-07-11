# Spec: #1698 Slice 3 — decay/enactment/telemetry + `engram_diagnose` coverage

**Author:** Sol · **Date:** 2026-07-08 · **Driver role:** spec-fidelity, not
authorship — Kepler reviews against his own design, does not build.
**Design source:** `docs/principle-triggers-design.md` §4, §5, §6 (PR 3 of 3),
§8 open question 2. Depends on slice 2 (`docs/specs/1698-slice2-unified-hook-check.md`)
having merged — this spec upgrades the flat `{principle_id: last_fired_prompt}`
state shape slice 2 ships into the richer per-principle record §4 describes.
**Do not dispatch a fairy against this spec until slice 2 is merged to dev** —
the file paths/line numbers below assume slice 2's code is already in place.

Team decisions this spec must fold in (forum #229/#230/#231, all Kepler-confirmed):
- **Fork-2**: `kind=axiom` is exempt from decay (never retires) but KEEPS the
  cooldown (still suppressed between fires, just never grows its effective
  cooldown or drops out of injection). Empirical anchor: a same-day cross-check
  found the highest-recall-count cornerstone in the author's own graph was one
  that keeps getting re-surfaced into review contexts rather than retiring once
  "learned" -- the shape of a constraint that should NOT decay, flat and
  ongoing, unlike a lesson's front-loaded/decaying value.
- **Design note for the next builder** (Borges, forum #231 reply): the four
  trigger kinds must NOT share one decay curve — this spec gives lesson/
  cornerstone/goal the §4 exponential-decay treatment and axiom the
  never-decay treatment; do not generalize a single curve across all four.
- **Kepler's own #1698 GitHub comment** (2026-07-07) flags a forward-looking
  concern: full-scan + full-JSON-rewrite rebuild cost at liberalized-`serves`
  scale. Not actioned in this spec (no telemetry-driven signal exists yet to
  size the fix) — noted in §5 below as a non-blocking follow-up once slice-3's
  own telemetry accumulates.

---

## Scope — four files

1. `src/engram/hooks/claude/engram-surface-hook.py` — upgrade
   `check_principle_triggers`'s state handling to the rich per-principle
   record + effective-cooldown decay math + `engram.trigger.fire` telemetry.
2. `src/engram/hooks/claude/engram-utility-credit-mention-stop.py` —
   enactment detection piggybacked on the existing mention-credit pass +
   `engram.trigger.enactment` telemetry.
3. `src/engram/engram_epistemic.py` (`_register_exemplar_impl`, ~line 846) +
   `src/engram/engram_cornerstone.py` (~lines 535-544, 712-721) +
   `src/engram/engram_tasks.py` (~line 333) — reset `enactments` to 0 when a
   NEW exemplar/incident/trigger edge is registered against a principle.
4. `src/engram/engram_stats.py` (`_diagnose_impl`, ~line 566) —
   `principle_coverage` section.

---

## 1. Rich state shape + effective cooldown

### 1.1 State shape migration

Slice 2 ships `principle-trigger-state.json` as `{principle_id: int}` (a bare
`last_fired_prompt`). Slice 3 migrates to:

```json
{
  "<principle_id>": {
    "last_fired_prompt": 42,
    "strength": 1.0,
    "enactments": 0,
    "fires": 3
  }
}
```

**Migration on read**: when loading the state file, any entry that is a bare
`int` (not a dict) is the slice-2 shape — upconvert in memory to
`{"last_fired_prompt": <that int>, "strength": 1.0, "enactments": 0, "fires": 0}`
before use. Write back the upgraded shape on the next state write. No separate
migration script needed — this is a lazy, idempotent upgrade-on-touch, same
spirit as slice 1's cache migration shim.

### 1.2 Effective cooldown (§4)

In `check_principle_triggers`'s cooldown check, replace the flat
`PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS` comparison with:

```python
RETIREMENT_CEILING_PROMPTS = 160  # design doc §4: "effectively retired"

def _effective_cooldown(kind: str, entry: dict) -> int:
    base = PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS
    if kind == "axiom":
        return base  # Fork-2: axiom never decays, cooldown stays fixed
    enactments = entry.get("enactments", 0)
    return min(base * (2 ** enactments), RETIREMENT_CEILING_PROMPTS)
```

A principle whose effective cooldown has reached the ceiling is, in practice,
retired from injection (it will almost never clear cooldown) but **stays in
the registry** — `engram_diagnose`'s `principle_coverage` (§4 below) still
counts it as covered via its registry entry, per design doc §4's "`strength`
is the rendered form: retired triggers drop from injection but stay in the
registry."

### 1.3 `engram.trigger.fire` telemetry

In `check_principle_triggers`, right where the current cooldown-stamp loop
runs (`for _kind, pid, _entry, _mid in rendered: state[pid] = prompt_count` in
slice 2's code — becomes `state[pid]["last_fired_prompt"] = prompt_count;
state[pid]["fires"] = state[pid].get("fires", 0) + 1`), add one emission per
rendered principle, following the exact P4 pattern at
`engram-surface-hook.py` lines 1875-1896 (`Emitter.init(...).emit(...)`,
try/except-drop-silently):

```python
try:
    sys.path.insert(0, ENGRAM_HOME)
    from engram_log_emitter import Emitter
    _emitter3 = Emitter.init(
        session_id=hook_input.get("session_id", "unknown"),
        transcript_path=hook_input.get("transcript_path", ""),
    )
    for kind, pid, entry, mid in rendered:
        _emitter3.emit(
            event_type="engram.trigger.fire",
            level=1,
            data={"principle_id": pid, "kind": kind, "trigger_id": mid, "prompt_seq": prompt_count},
        )
except Exception:
    pass
```

Note `hook_input` isn't currently in scope inside `check_principle_triggers` —
it's a UserPromptSubmit-hook-level variable. Either thread `session_id`/
`transcript_path` into the function as extra params, or move the emission to
the call site (main body, right after calling `check_principle_triggers`) where
`hook_input` is already available and an `_emitter` is already initialized
(reuse it rather than constructing a second `Emitter.init` — `Emitter.init` is
documented idempotent per the P4 comment, but reusing the existing `_emitter`
from earlier in `main()` is simpler). **Coder-fairy: prefer the call-site
placement** — less plumbing, and it keeps `check_principle_triggers` a pure
function of `(matched_ids, prompt_count) -> str` like its slice-2 shape,
returning the `rendered` list as a second value (or a small wrapper) for the
caller to emit from.

---

## 2. Enactment detection (§4)

`src/engram/hooks/claude/engram-utility-credit-mention-stop.py`,
`credit_mentions` (~line 239) already computes `mentioned` (deduped node IDs
found in the agent's own turn text via `find_node_ids`). Add, right after:

```python
try:
    _check_enactments(mentioned)
except Exception as e:
    _log(f"enactment check failed: {type(e).__name__}: {e}")
```

```python
_ENACTMENT_WINDOW_PROMPTS = 10  # "no trigger fire... within the last k prompts"

def _check_enactments(mentioned_ids, state_path=PRINCIPLE_TRIGGER_STATE_PATH,
                       prompt_count_path=PROMPTS_SINCE_COMPACTION_PATH):
    """Mention-proxy enactment detection (design doc §4, v1-implementable
    proxy). A mentioned node ID that IS a principle_id in the registry, with
    no trigger fire for that principle in the trailing window, counts as an
    unprompted enactment: the practice happened without the nudge firing."""
    try:
        with open(PRINCIPLE_TRIGGERS_PATH, "r") as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    principle_ids = {e.get("principle_id") for e in registry.values() if e.get("principle_id")}
    hits = principle_ids & set(mentioned_ids)
    if not hits:
        return

    prompt_count = _read_prompt_count(prompt_count_path)  # see open question below
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    changed = False
    for pid in hits:
        entry = state.get(pid)
        if isinstance(entry, int):
            entry = {"last_fired_prompt": entry, "strength": 1.0, "enactments": 0, "fires": 0}
        if not isinstance(entry, dict):
            entry = {"last_fired_prompt": 0, "strength": 1.0, "enactments": 0, "fires": 0}
        last_fired = entry.get("last_fired_prompt", 0)
        if (prompt_count - last_fired) >= _ENACTMENT_WINDOW_PROMPTS:
            entry["enactments"] = entry.get("enactments", 0) + 1
            state[pid] = entry
            changed = True
            _emit_enactment(pid, prompt_count)  # engram.trigger.enactment, same Emitter pattern as §1.3
    if changed:
        _write_state_atomic(state_path, state)
```

**Open question for reviewer (genuine gap, not an oversight):** this Stop
hook runs as a separate process from the surface hook (different hook event:
`Stop` vs `UserPromptSubmit`), and needs the CURRENT `prompts_since_compaction`
counter to compare against `last_fired_prompt` — the surface hook tracks this
via its own `counter` dict (see the call site
`check_principle_triggers(matched_ids, counter["prompts_since_compaction"])`
in slice 2), but this Stop hook has no existing access to that counter. Two
options, pick one at implementation time and flag the choice in the PR:
(a) read it from wherever the surface hook persists the counter across
prompts today (grep for where `prompts_since_compaction` is stored between
hook invocations — likely a small state file already, since it must survive
across the per-prompt subprocess boundary); or (b) have the Stop hook
maintain its own turn counter. **(a) is very likely correct** since the
counter already has to persist somewhere for the surface hook itself to work
across invocations — find and reuse that file rather than inventing a second
counter that could drift from the surface hook's.

---

## 3. Reset-on-incident

Design doc §4: "registering a NEW exemplar/incident against the principle
resets `enactments` to 0 (full strength)." The three call sites that already
run `core._rebuild_principle_triggers()` after a new trigger edge is written
(`engram_epistemic.py` ~line 983, `engram_cornerstone.py` ~lines 544/721,
`engram_tasks.py` ~line 333) each know the `principle_id` the new edge just
targeted — pass it through and reset that principle's `enactments` in
`principle-trigger-state.json`.

**Cross-process write, flag for reviewer:** these call sites run inside the
MCP server process; `principle-trigger-state.json` has, through slices 1-2,
only ever been touched by the hook scripts (separate subprocesses invoked by
Claude Code). Slice 3 is the first time server-side code needs to write this
file. Two things the implementer must get right: (a) resolve the SAME path
the hooks use (`$ENGRAM_HOME/principle-trigger-state.json` — `engram_core.py`
already has `ENGRAM_HOME`/`DATA_DIR` resolution patterns to mirror, don't
hand-roll a second one); (b) write atomically (tmp + `os.replace`, matching
the hooks' own pattern) so a concurrent hook-side read never sees a partial
write. Add a small shared helper if one doesn't already exist for
read-modify-write-atomic on this file, rather than duplicating the
tmp+replace dance a third time across two more call sites.

```python
def _reset_principle_enactments(principle_id: str) -> None:
    """Reset enactments to 0 for principle_id in principle-trigger-state.json.
    Best-effort: a write failure here must never fail the exemplar-
    registration call it's attached to."""
    path = os.path.join(DATA_DIR, "principle-trigger-state.json")  # mirror hook's path construction
    try:
        with open(path, "r") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    entry = state.get(principle_id)
    if isinstance(entry, int):
        entry = {"last_fired_prompt": entry, "strength": 1.0, "enactments": 0, "fires": 0}
    if not isinstance(entry, dict):
        entry = {"last_fired_prompt": 0, "strength": 1.0, "enactments": 0, "fires": 0}
    entry["enactments"] = 0
    state[principle_id] = entry
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass
```

Call this immediately after each of the three `_rebuild_principle_triggers()`
call sites, passing the `principle_id` the newly-written edge targeted (each
call site already has this value in scope — it's the target of the
exemplifies/instantiates/serves/tensions edge just created).

---

## 4. `engram_diagnose` — `principle_coverage` section

`src/engram/engram_stats.py`, `_diagnose_impl` (~line 566). Follow the
existing section pattern exactly (see `structure` at line 576: build a dict,
populate via SQL + file reads, assign into `metrics` before the function's
`json.dumps(metrics)` return at line 1306).

```python
# ── Principle Coverage (#1698 slice 3) ──────────────────────────────
principle_coverage: dict = {}
principle_rows = conn.execute(
    """SELECT id, type, claim FROM nodes
       WHERE type IN ('lesson', 'cornerstone', 'axiom', 'goal')
         AND is_current = 1 AND memory_status = 'active'"""
).fetchall()

try:
    with open(PRINCIPLE_TRIGGERS_PATH, "r") as f:
        registry = json.load(f)
    covered_ids = {e.get("principle_id") for e in registry.values() if e.get("principle_id")}
except (FileNotFoundError, json.JSONDecodeError):
    covered_ids = set()

warm_briefing_text = ""
try:
    with open(os.path.expanduser("~/.engram/warm-briefing.md")) as f:
        warm_briefing_text = f.read()
except OSError:
    pass
claude_md_text = ""
try:
    with open(os.path.expanduser("~/.claude/CLAUDE.md")) as f:
        claude_md_text = f.read()
except OSError:
    pass

uncovered = []
for r in principle_rows:
    nid = r["id"]
    channels = []
    if nid in covered_ids:
        channels.append("registry_trigger")
    # situation_pattern channel: re-query per-node metadata (cheap; N is small)
    meta_row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()
    try:
        meta = json.loads(meta_row["metadata"]) if meta_row and meta_row["metadata"] else {}
    except (TypeError, json.JSONDecodeError):
        meta = {}
    if meta.get("situation_pattern"):
        channels.append("situation_pattern")
    if nid in warm_briefing_text:
        channels.append("warm_briefing_anchor")
    if nid in claude_md_text:
        channels.append("claude_md")
    if not channels:
        uncovered.append({
            "id": nid, "type": r["type"], "claim": (r["claim"] or "")[:120],
            "cheapest_fix": "register one exemplar (engram_register_exemplar / engram_lesson_register_incident)",
        })

principle_coverage["total_principles"] = len(principle_rows)
principle_coverage["covered_count"] = len(principle_rows) - len(uncovered)
principle_coverage["uncovered"] = uncovered
metrics["principle_coverage"] = principle_coverage
```

This is read-only (matches `_diagnose_impl`'s documented side-effect-free
contract) and degrades gracefully (empty registry/missing files → everything
reports uncovered rather than raising, which is the honest answer if those
files are genuinely absent).

---

## 5. Non-goals / forward-looking notes (not this spec's scope)

- **Rebuild-cost-at-scale** (Kepler's #1698 GitHub comment): full-scan +
  full-JSON-rewrite on every serves/tensions edge write may need
  incremental/debounced rebuild once the liberalized `serves` surface (§7,
  already shipped in slice 1) grows edge volume. This spec's own
  `engram.trigger.fire`/`engram.trigger.enactment` telemetry is what would
  let a future PR size that need with real data — don't preemptively
  optimize the rebuild in this slice; there's no volume signal yet to design
  against.
- Legacy cache files/rebuild functions: still untouched (same as slice 2's
  non-goals).
- No change to slice 2's cap/priority policy (§3 of the design doc) — decay
  changes WHICH principles are eligible to fire (via effective cooldown), not
  how many render per prompt or in what order.

## 6. Tests (mandatory)

Extend `tests/test_principle_triggers_registry.py`:
1. State-shape migration: a bare-int legacy entry is upconverted on read and
   the rewritten file uses the dict shape.
2. Effective cooldown: a lesson with `enactments=2` needs
   `4 × base_cooldown` prompts before it's eligible again; capped at
   `RETIREMENT_CEILING_PROMPTS`.
3. Axiom exemption: an axiom with `enactments=5` still uses the flat base
   cooldown, not the decayed one.
4. `engram.trigger.fire` emits on every rendered firing with the right
   `{principle_id, kind, trigger_id, prompt_seq}` shape.
5. Enactment detection: a mentioned principle ID with no fire in the trailing
   window increments `enactments` and emits `engram.trigger.enactment`; a
   mentioned ID that DID fire recently does not.
6. Reset-on-incident: registering a new exemplar against a principle with
   `enactments=3` resets it to 0.
7. `principle_coverage`: a principle with zero channels appears in
   `uncovered` with a non-empty `cheapest_fix`; one with a registry entry,
   a `situation_pattern`, a warm-briefing mention, or a CLAUDE.md mention
   does not.

Run the full sibling suite (not just new cases) — same discipline as slice 2.

## 7. PR metadata

Title: `feat(#1698): decay/enactment/telemetry + principle_coverage diagnose (slice 3)`.
Body: `Closes #1698.` (this is the final slice — closing keyword required per
this repo's PR-issue-closure convention). Cite this spec + slice 2's PR.
Reviewer-fairy round required before flipping to Sol for the colleague pass;
Kepler reviews only after that, against his own §4/§5 design and the two
flagged open-design-calls (§2's counter-source question, §3's cross-process
write path).
