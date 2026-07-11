# Spec: #1698 slice 2, round 2 — fix sibling-test regressions

**Author:** Sol · **Date:** 2026-07-08 · **Context:** round-1 coder-fairy
(`docs/specs/1698-slice2-unified-hook-check.md`) correctly implemented the
spec and correctly STOPPED rather than silently patching files outside its
given scope, after finding that deleting `check_incident_tripwire` /
`check_cornerstone_anchor` breaks 17 tests across 5 sibling files the
original spec didn't cover. I independently verified every failure below by
running the tests myself (not just trusting the fairy's report) — the
diagnosis is correct. This spec's only job is disposing of those 17 tests.

**Branch: continue on the EXISTING branch** `sol/1698-slice2-unified-hook-check`
(worktree `/home/agent-sol/engram-alpha/.claude/worktrees/agent-ac66250a6016b20e0`,
tip commit `75c50df`). Do not create a new branch. Commit on top.

**Do not re-touch** the 4 files round 1 already changed
(`engram_core.py`, `engram-surface-hook.py`, `engram-lesson-tripwire-hook.py`,
`tests/test_principle_triggers_registry.py`) except to READ
`test_principle_triggers_registry.py` for reference (see item 1 below). This
round's diff should be scoped to the 5 files listed below, nothing else.

**Ignore 4 unrelated failures** in `tests/test_cornerstone_anchor.py`
(`test_rebuild_cache_from_graph`, `test_rebuild_anchor_line_fallback_to_claim`,
`test_register_exemplar_cornerstone_rebuilds_cache`,
`test_generic_edge_tools_resync_anchor_cache`) — these fail with
`ModuleNotFoundError: No module named 'fastmcp'`, confirmed pre-existing on
unmodified `dev` (verified directly, not assumed). Not this PR's problem.

---

## Item 1 — `tests/test_cornerstone_anchor.py`: retire the §2 tests

This file has two sections (see its own module docstring): §1 tests
`_rebuild_cornerstone_anchors_cache` (untouched by slice 2, unaffected —
those are the 4 fastmcp failures above, ignore them). §2 tests
`check_cornerstone_anchor` directly — that function no longer exists
(deleted in round 1, folded into `check_principle_triggers`). The 7 failing
§2 tests are:

- `test_fires_on_exemplar_id`
- `test_fires_on_direct_cornerstone_id`
- `test_cooldown_suppresses_within_window`
- `test_cooldown_expires`
- `test_counter_reset_clears_cooldown`
- `test_missing_cache_is_silent`
- `test_malformed_cache_shape_is_silent`

**For each of these 7**, read `tests/test_principle_triggers_registry.py`
(round 1's additions, 8 new cases per the slice-2 spec's §4) and check
whether an equivalent behavior is already asserted there for `kind=cornerstone`
or generically across kinds:

- **If yes** (behavior is covered): DELETE the old test from
  `test_cornerstone_anchor.py`. Don't keep a dead test around testing a
  function that no longer exists just to keep a number up.
- **If no** (a real behavior gap — e.g. "fires on direct cornerstone ID" via
  the reverse `by_principle` lookup, or "counter reset clears cooldown",
  might not have an exact equivalent in the new suite): PORT it — rewrite the
  test to build a `principle_triggers.json` fixture (unified registry shape,
  `kind: "cornerstone"`) and call `check_principle_triggers` from
  `engram-surface-hook.py` instead of the deleted function against
  `cornerstone_anchors.json`. Preserve the ORIGINAL TEST'S INTENT (what
  behavior it was protecting), not its literal fixture shape.

Leave §1 (the 4 fastmcp-blocked rebuild tests) completely untouched — they
are not part of this slice's scope and will pass again whenever `fastmcp` is
available in the environment.

## Item 2 — `tests/test_surface_ledger_suppression.py`: update the call site

Lines 453-454 call `self.hook.check_incident_tripwire(["ob_ledger_tripwire"])`
with the deleted function's old signature (1 positional arg, no
`prompt_count`). Update to call `self.hook.check_principle_triggers(...)`
with both required args (`matched_ids`, `prompt_count` — check how other
tests in `test_principle_triggers_registry.py` construct a `prompt_count`
value, e.g. an arbitrary int like `1`, and mirror that). The test's actual
assertion (per its own docstring, lines ~435: "has no session_id parameter
and doesn't [get suppressed by the ledger]") is about EXEMPTING this
tripwire path from an unrelated suppression mechanism — preserve that intent;
only the call signature needs to change.

## Item 3 — Four files: mechanical rename of the monkeypatched target

These four files only stub out `check_incident_tripwire` to silence it as
noise in tests unrelated to the tripwire itself — not testing tripwire
behavior:

- `tests/test_surface_hook_db_liveness.py:86` —
  `monkeypatch.setattr(mod, "check_incident_tripwire", lambda *a, **kw: None)`
- `tests/test_surface_hook_mcp_liveness.py:116` — same pattern
- `tests/test_surface_hook_attached_packs.py:775,785,819` —
  `orig_tripwire = self.hook.check_incident_tripwire` /
  `self.hook.check_incident_tripwire = lambda *a, **kw: ""` /
  restore via `orig_tripwire`
- `tests/test_recall_injection_budget.py:579,589,623` — same
  save/stub/restore pattern as the previous file

For all four: rename `check_incident_tripwire` → `check_principle_triggers`
in the monkeypatch/stub/restore lines (the attribute being stubbed, not a
functional change). Verify each file's suite passes after the rename — this
is expected to be purely mechanical, but confirm rather than assume.

## Item 4 — `tests/test_in_turn_recall.py`: fixture missing `claim` column

Lines 178-188: the test's ad hoc `CREATE TABLE nodes (...)` (inside
`test_tripwire_and_recall_compose`) has no `claim` column. Slice 2's PreToolUse
`_QUERY` change (spec §3, already landed in round 1) added `claim` as a final
`COALESCE` fallback — a real, intentional improvement — which now causes
`sqlite3.OperationalError: no such column: claim` against this fixture's
schema, silently swallowed by `load_tripwires`'s broad
`except Exception: return []` (so the tripwire never fires and the test's
assertion `"lesson-tripwire" in ctx` fails).

Fix: add `claim TEXT` to the `CREATE TABLE` statement, and add a value for it
in the `INSERT INTO nodes VALUES (...)` statement right after (currently 5
positional values — `id, type, is_current, memory_status, metadata` — becomes
6 with `claim` added; put it wherever reads most naturally, e.g. right after
`type`, and update the column list in `CREATE TABLE` to match the same order
used in the `VALUES` tuple). Any non-empty placeholder string is fine — this
fixture's lesson already has `scaffolding_nudge` set, which the `COALESCE`
picks first regardless of `claim`'s value; the fix is about schema
completeness, not this test's specific assertion content.

## Verification (mandatory)

1. `python3 -m pytest tests/test_cornerstone_anchor.py tests/test_surface_ledger_suppression.py tests/test_surface_hook_db_liveness.py tests/test_surface_hook_mcp_liveness.py tests/test_surface_hook_attached_packs.py tests/test_recall_injection_budget.py tests/test_in_turn_recall.py tests/test_principle_triggers_registry.py -q` — every test in these 8 files passes EXCEPT the 4 named fastmcp failures (which must still fail identically, confirming you didn't accidentally fix or further break something unrelated — an unexpected PASS on those 4 would mean your environment differs from what was diagnosed, worth a note).
2. Re-run the FULL sibling suite one more time (same command round 1 used) and confirm the failure count drops to exactly the 4 fastmcp ones + the 4 pre-existing installer/systemd ones round 1 already identified and confirmed unrelated (`test_install_local_marketplace.py` / `test_setup_surface_daemon_service.py`) — 8 total pre-existing failures, zero new ones.
3. `git diff --stat sol/1698-slice2-unified-hook-check~1` (i.e., against round 1's tip) should show ONLY the 5 files named in items 1-4 above.

## Once verification passes: open the PR

This round's job also includes what round 1 correctly deferred (its own
governing framework doesn't push or open PRs — that's the parent's job, not
a fairy gap). **I (Sol, the dispatching agent) will handle `gh pr create`
myself after this round reports back** — round 2, like round 1, should
stop at "committed on the branch, verified" and hand back a report; do not
attempt `gh pr create` or `git push`.

## Output contract

Same shape as round 1's: commit SHA, full `git diff --stat` against the
PRE-round-2 tip (`75c50df`), full test results (the two verification runs
above, not just "tests pass"), an ambiguous-decision log (especially: which
of the 7 §2 tests you deleted-as-redundant vs ported-as-gap, and why), and
any STOP-and-report conditions hit.
