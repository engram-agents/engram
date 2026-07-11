# Spec: #1698 Slice 2 — Unified principle-trigger check in both hooks

**Author:** Sol · **Date:** 2026-07-08 · **Driver role:** spec-fidelity, not
authorship — Kepler reviews against his own design, does not build.
**Design source:** `docs/principle-triggers-design.md` §3, §6 (implementation
plan, PR 2 of 3). Slice 1 (registry + rebuild + migration shim) is merged
(`_rebuild_principle_triggers` in `engram_core.py`, PR #1702). This slice
flips the **read path** in both hooks from the two legacy per-kind caches to
the unified `principle_triggers.json` registry, and fixes a known bug in the
registry-builder while we're touching this code.

Team context: owned by Sol (Team① Ariadne+Sol, forum #229/#230), Ariadne is
working `engram-postcompact-hook.py` (#1655) in parallel this week — verified
zero file overlap (her DM, 2026-07-08T12:23Z). Kepler is NOT to be paged for
authorship; this spec should be precise enough that a coder-fairy can build it
end to end, with only a final review-not-build pass from Kepler.

---

## Scope — three files, in this order

1. `src/engram/engram_core.py` — fix the trigger-side `is_current` gap in
   `_rebuild_principle_triggers` (~line 4253).
2. `src/engram/hooks/claude/engram-surface-hook.py` (UserPromptSubmit) —
   replace `check_incident_tripwire` + `check_cornerstone_anchor` +
   `_check_cornerstone_anchor_inner` with one `check_principle_triggers`.
3. `src/engram/hooks/claude/engram-lesson-tripwire-hook.py` (PreToolUse) —
   broaden the `_QUERY` type filter + nudge fallback chain.

Out of scope (slice 3, separate spec): strength/decay, enactment detection,
telemetry events, `engram_diagnose` `principle_coverage` section, retiring
the legacy cache files themselves.

---

## 1. `engram_core.py` — trigger-side `is_current` fix

**Bug** (flagged by Luria, confirmed by Sol reviewing #1702, recorded on issue
#1698 itself — not just a PR-body claim): `_rebuild_principle_triggers`'s SQL
joins `nodes` for the **principle** side (`n.id = e.target_id`, filtered
`n.is_current = 1`) but never joins/filters the **trigger** side
(`e.source_id`, or `e.target_id` in the bidirectional `tensions` case). A
retracted/superseded trigger node still lands in the registry keyed by its
node ID. Harmless today (nothing reads this file yet), live-buggy the moment
this slice's hooks start reading it: if `matched_ids` ever contained a stale
trigger ID, it would still fire.

**Fix** — add a second join on the trigger side, filtered `is_current = 1`,
in both the forward query and the bidirectional-reverse query inside the
`for relation, ptype, kind, nudge_key, mode, bidirectional in
_PRINCIPLE_TRIGGER_SPECS:` loop:

```sql
-- forward (unchanged shape, added trigger-side join):
SELECT e.source_id AS trigger_id, e.target_id AS principle_id,
       n.claim AS claim, n.metadata AS meta
FROM edges e
JOIN nodes n ON n.id = e.target_id
JOIN nodes t ON t.id = e.source_id
WHERE e.relation = ? AND n.type = ? AND n.is_current = 1
  AND t.is_current = 1

-- bidirectional-reverse (mirror image, same fix):
SELECT e.target_id AS trigger_id, e.source_id AS principle_id,
       n.claim AS claim, n.metadata AS meta
FROM edges e
JOIN nodes n ON n.id = e.source_id
JOIN nodes t ON t.id = e.target_id
WHERE e.relation = ? AND n.type = ? AND n.is_current = 1
  AND t.is_current = 1
```

No other change to this function. Add a one-line comment above the new join
citing #1698 + this fix's origin (Luria's catch).

**Test to add** (`tests/test_principle_triggers_registry.py`, sibling suite
already exists from slice 1): create a trigger node, wire it `exemplifies` →
a lesson, retract the trigger node, rebuild, assert the trigger's ID is
**absent** from `principle_triggers.json`. Mirror for the bidirectional
`tensions` case (retract the non-goal side of a tension edge).

---

## 2. Surface hook — unified `check_principle_triggers`

### 2.1 New constants (mirror the existing pattern at line ~135-144)

```python
PRINCIPLE_TRIGGERS_PATH = os.path.join(ENGRAM_HOME, "principle_triggers.json")
PRINCIPLE_TRIGGER_STATE_PATH = os.path.join(ENGRAM_HOME, "principle-trigger-state.json")
PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS = 10  # same value as today's CORNERSTONE_ANCHOR_COOLDOWN_PROMPTS
PRINCIPLE_TRIGGER_CAP = 2  # design doc §3 point 3
_PRINCIPLE_KIND_PRIORITY = {"lesson": 0, "axiom": 1, "cornerstone": 2, "goal": 3}  # lower = higher priority
```

Keep `ERROR_INCIDENTS_PATH` / `CORNERSTONE_ANCHORS_PATH` /
`CORNERSTONE_ANCHOR_STATE_PATH` / `CORNERSTONE_ANCHOR_COOLDOWN_PROMPTS` —
still referenced by `engram_core.py`'s legacy rebuild functions elsewhere;
not this hook's concern to remove.

### 2.2 Replace the three functions with one

Delete `check_incident_tripwire`, `check_cornerstone_anchor`,
`_check_cornerstone_anchor_inner` (lines ~1459-1591). Add:

```python
def check_principle_triggers(matched_ids: list, prompt_count: int) -> str:
    """Unified principle-trigger check (#1698 slice 2) — one registry, four
    kinds (lesson/cornerstone/axiom/goal), replacing the separate
    check_incident_tripwire + check_cornerstone_anchor.

    Byte-compatibility requirement: lesson and cornerstone renderings must
    stay IDENTICAL to their pre-unification format — both are load-bearing
    in existing transcripts/tests. Axiom and goal are new kinds with no
    prior rendering to preserve; they use the design doc's generic
    "[Principle trigger (...)]" register-tagged form.
    """
    if not matched_ids:
        return ""
    try:
        return _check_principle_triggers_inner(matched_ids, prompt_count)
    except Exception:
        # Runs on every prompt — a malformed cache must degrade to
        # silence, never break the hook (same contract as the function
        # this replaces).
        return ""


def _check_principle_triggers_inner(matched_ids: list, prompt_count: int) -> str:
    try:
        with open(PRINCIPLE_TRIGGERS_PATH, "r") as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Deviation from design doc §2's literal "fallback to legacy":
        # slice 1's rebuild is additive and runs at the same 3 sites the
        # legacy caches already rebuild from, so this file is realistically
        # only absent in the brief window right after an upgrade before any
        # lesson/cornerstone/axiom/goal edge write has happened -- and in
        # that window the legacy caches would also be empty of anything
        # meaningful. Returning "" here (rather than re-implementing a
        # parallel legacy read path) is a simplification; flagging this
        # explicitly for reviewer/Kepler to confirm or override, since the
        # design doc's literal words say "fallback to legacy."
        return ""
    if not registry or not isinstance(registry, dict):
        return ""

    # Reverse view: a matched ID that IS a principle's own node ID (not a
    # trigger) also fires -- mirrors today's by_cornerstone reverse lookup,
    # generalized across all four kinds.
    by_principle = {}
    for entry in registry.values():
        pid = entry.get("principle_id", "")
        if pid:
            by_principle[pid] = entry

    try:
        with open(PRINCIPLE_TRIGGER_STATE_PATH, "r") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    # Gather all candidates that matched AND are past cooldown, deduped
    # per principle_id (one firing per principle per prompt -- same
    # semantics as today's `fired`/`seen_lessons` sets, now shared across
    # all four kinds since it's one registry).
    candidates = []  # list of (kind, principle_id, entry, matched_via)
    seen_principles = set()
    for mid in matched_ids:
        entry = registry.get(mid) or by_principle.get(mid)
        if not entry:
            continue
        pid = entry.get("principle_id", "")
        if not pid or pid in seen_principles:
            continue
        last = state.get(pid)
        if (
            isinstance(last, (int, float))
            and last <= prompt_count
            and (prompt_count - last) < PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS
        ):
            continue
        seen_principles.add(pid)
        candidates.append((entry.get("kind", ""), pid, entry, mid))

    if not candidates:
        return ""

    # Priority sort (lesson > axiom > cornerstone > goal), cap total to 2.
    # NOTE this is a real behavior change for lessons, which previously had
    # NO cross-prompt cooldown (only per-fire dedup) -- unification means
    # lessons now share the same fixed cooldown as cornerstones did. This
    # is what design doc §6 means by "v1 fixed cooldown carried over" --
    # carried over to ALL kinds, not just cornerstone. Flagging explicitly
    # since it changes observed lesson-tripwire cadence.
    candidates.sort(key=lambda c: _PRINCIPLE_KIND_PRIORITY.get(c[0], 99))
    rendered = candidates[:PRINCIPLE_TRIGGER_CAP]

    lines = []
    for kind, pid, entry, mid in rendered:
        claim = entry.get("claim", "")
        nudge = entry.get("nudge", "") or claim
        if kind == "lesson":
            # BYTE-COMPATIBLE with pre-unification check_incident_tripwire.
            # mid here is the incident/trigger ID (was `mid` in old code).
            lines.append(
                f"[ENGRAM Tripwire ({pid}): {claim}]\n"
                f"  Action: {nudge}\n"
                f"  (Triggered by incident match: {mid})"
            )
        elif kind == "cornerstone":
            # BYTE-COMPATIBLE with pre-unification check_cornerstone_anchor.
            lines.append(f"[Cornerstone anchor ({pid})]: {nudge}")
        elif kind == "axiom":
            lines.append(f"[Principle trigger ({pid}, constraining)]: {claim} → Constraint: {nudge}")
        elif kind == "goal":
            lines.append(f"[Principle trigger ({pid}, directional)]: {claim} → Serves: {nudge}")

    # Stamp cooldown ONLY for what actually rendered (top 2 after the cap) --
    # a cap-bumped candidate stays eligible next prompt rather than being
    # suppressed for a firing it never got to make. Matches old code's
    # semantics (`for cid in fired: state[cid] = prompt_count` where `fired`
    # was exactly the rendered set).
    for _kind, pid, _entry, _mid in rendered:
        state[pid] = prompt_count
    try:
        with open(PRINCIPLE_TRIGGER_STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError:
        pass  # state write failure -> worst case an early re-fire; never block

    return "\n".join(lines)
```

Note on the cornerstone rendering: old code's anchor line was
`entry.get("anchor_line") or entry.get("cornerstone_claim", "")` — in the
unified registry that's already folded into `entry["nudge"]` by
`_rebuild_principle_triggers` (nudge_key="anchor_line", fallback→claim), so
`nudge` is the direct equivalent. **Coder-fairy: verify this byte-for-byte
against a real fired example before calling it done** — diff the rendered
string against a cornerstone anchor firing captured pre-change, not just a
visual read of the code.

### 2.3 Call-site update (~line 1710-1728)

Replace:
```python
tripwire = check_incident_tripwire(matched_ids)
if not tripwire:
    tripwire = check_error_patterns(prompt)
if tripwire:
    parts.append(tripwire)

cornerstone_anchor = check_cornerstone_anchor(
    matched_ids, counter["prompts_since_compaction"]
)
if cornerstone_anchor:
    parts.append(cornerstone_anchor)
```
with:
```python
principle_triggers = check_principle_triggers(
    matched_ids, counter["prompts_since_compaction"]
)
if not principle_triggers:
    # Preserve the semantic-keyword fallback for lessons specifically --
    # this is independent of the incident-index match and must survive
    # unification unchanged (design doc doesn't mention it; it's Locus-3
    # semantic-content matching, orthogonal to the trigger registry).
    principle_triggers = check_error_patterns(prompt)
if principle_triggers:
    parts.append(principle_triggers)
```

**Open question for reviewer:** the old code fell back to
`check_error_patterns(prompt)` only when the *lesson* check specifically was
empty, independent of whether cornerstone fired. The unified check now
returns "" if NO candidate of ANY kind fired (including e.g. a goal trigger
having been the only thing suppressed by cooldown) -- so the fallback could
now trigger in a slightly wider set of cases (any-kind-silent vs
lesson-silent). In practice this only matters when cornerstone/axiom/goal
would have been the sole survivor and lesson had nothing -- rare, and
`check_error_patterns` is itself lesson-only so it's a reasonable fallback
regardless. Flagging so it's a conscious call, not a silent side effect.

---

## 3. PreToolUse hook — broaden `_QUERY`

`src/engram/hooks/claude/engram-lesson-tripwire-hook.py`, lines 46-59. Replace:

```python
_QUERY = """
SELECT
    COALESCE(
        json_extract(metadata, '$.scaffolding_nudge'),
        json_extract(metadata, '$.anchor_line')
    ) AS nudge,
    json_extract(metadata, '$.situation_pattern')  AS pattern
FROM nodes
WHERE type IN ('lesson', 'cornerstone')
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
"""
```

with:

```python
_QUERY = """
SELECT
    COALESCE(
        json_extract(metadata, '$.scaffolding_nudge'),
        json_extract(metadata, '$.anchor_line'),
        json_extract(metadata, '$.surfacing_nudge'),
        claim
    ) AS nudge,
    json_extract(metadata, '$.situation_pattern')  AS pattern
FROM nodes
WHERE type IN ('lesson', 'cornerstone', 'axiom', 'goal')
  AND is_current = 1
  AND memory_status = 'active'
  AND json_extract(metadata, '$.situation_pattern') IS NOT NULL
  AND json_extract(metadata, '$.situation_pattern') != ''
"""
```

Two changes: (a) `type IN (...)` widened to all four kinds, matching design
doc §3's closing paragraph; (b) `claim` added as a final COALESCE fallback —
this is a genuine improvement over today's behavior (a lesson/cornerstone
with a `situation_pattern` but no nudge field today silently drops the row
via `if row[0] and row[1]` in `load_tripwires`; per design doc's own table
every kind has "fallback → claim", which the old code never actually
implemented for lesson/cornerstone either).

**Do not change** the module docstring's Locus-1/2/3 taxonomy (still
accurate — it's about cue *shape*, not principle *kind*), the outer banner
text (`"[lesson-tripwire] Action-moment pattern matched — remember:\n..."`
in `main()`), or `check_command`/`build_match_target`. Those are unrelated to
this change and byte-compat matters here too (this exact banner has fired
live in agent transcripts this session).

---

## 4. Tests (mandatory, not optional)

Extend `tests/test_principle_triggers_registry.py` (slice 1's suite):

1. Trigger-side `is_current` fix (§1 above) — retracted trigger absent from
   rebuilt registry, both forward and bidirectional cases.
2. `check_principle_triggers`: lesson-kind hit renders byte-identical to a
   captured pre-change `check_incident_tripwire` output for the same fixture.
3. Same for cornerstone-kind vs pre-change `check_cornerstone_anchor` output.
4. New axiom-kind and goal-kind hits render the new format correctly.
5. Cooldown: a principle within `PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS` of its
   last fire is suppressed regardless of kind (this is the lesson-cadence
   behavior change — needs its own explicit test, not just inherited from
   the cornerstone cooldown test).
6. Cap: 3+ simultaneous candidates across mixed kinds → exactly 2 render, in
   lesson > axiom > cornerstone > goal priority order; the cap-losing
   candidate's cooldown state is NOT stamped (still eligible next prompt).
7. Missing/absent `principle_triggers.json` → `check_principle_triggers`
   returns `""` (the §2.2 deviation-from-doc behavior) rather than raising.
8. PreToolUse `_QUERY`: axiom/goal rows with only `surfacing_nudge` set are
   now returned by `load_tripwires()`; a row with situation_pattern but no
   nudge field falls back to `claim` instead of being silently dropped.

Run the full sibling suite (not just the new tests) before calling this
done — #1702's reviewer round caught cross-suite breakage from a
similar-looking change; don't repeat that miss.

---

## 5. Non-goals / explicit deferrals to slice 3

- No changes to `principle-trigger-state.json`'s shape beyond
  `{principle_id: last_fired_prompt}` (int) — slice 3 introduces the richer
  `{last_fired_prompt, strength, enactments, fires}` shape and owns migrating
  this file. Flag this clearly in the slice-2 PR description so slice 3's
  author isn't surprised by the flat shape.
- No `engram.trigger.fire` / `engram.trigger.enactment` telemetry — slice 3.
- No `engram_diagnose` `principle_coverage` section — slice 3.
- Legacy cache files (`error_incidents.json`, `cornerstone_anchors.json`) and
  their rebuild functions in `engram_core.py` are UNTOUCHED — still written,
  per design doc §2's "release after: legacy files retired" (not this
  release).

## 6. PR metadata

Title: `feat(#1698): unified principle-trigger check in both hooks (slice 2)`.
Body: `part of #1698` (does not close it — slice 3 remains). Cite this spec
file's path. Reviewer-fairy round required before flipping to Sol for the
colleague pass; Kepler reviews only after that, against his own §6 plan and
the two flagged open-design-calls above (§2.2's deviation, §2.3's fallback
scope widening).
