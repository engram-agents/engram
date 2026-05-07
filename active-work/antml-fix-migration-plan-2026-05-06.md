# antml-fix migration — per-tool single-JSON-payload signature reshape

**Status:** plan (telemetry baseline phase running; migration gated)
**Started:** 2026-05-06 morning (plan; design from 2026-05-05 PM)
**ENGRAM pointers:** ob_1499 (design decision), ls_0023 (empirical-validation discipline)
**Branch:** `feat/antml-repair-telemetry` is in flight (telemetry only). Migration branch: TBD, ~`feat/antml-fix-per-tool-json` after baseline data lands.
**Depends on:** baseline accumulation in `~/.engram/toolcall-invocations.jsonl` (post-2026-05-06 entries) — at least one full week of data, ideally two, to characterize repair rate by tool with reasonable confidence.

## Goal (from ob_1499, restated)

Eliminate the antml-prefix multi-param swallow failure mode at the source by changing every `engram_*` MCP tool's signature from named parameters to a single JSON-payload parameter. With exactly one parameter per call, the swallow pattern (which requires `</X><parameter name="Y">` to fire) cannot manifest.

The substance of the v1-release-blocker commitment is unchanged; this is the implementation path.

## Pre-conditions before opening the migration PR

1. **Baseline telemetry collected:** `~/.engram/toolcall-invocations.jsonl` has accumulated enough data that `tools/antml_repair_baseline.py --since 2026-05-06` reports a stable per-tool repair rate (rate doesn't shift more than ±10% relative across consecutive 24-hour windows). This typically means ≥1000 invocations and ≥2 weeks calendar time across diverse usage (interactive + cron + heartbeat).
2. **Calibration metric defined:** the rollback criterion is *total error rate per N tool calls with categorical breakdown*, not just antml-repair fire rate. Failure categories to instrument server-side:
   - `MALFORMED_JSON` — payload string isn't valid JSON
   - `MISSING_REQUIRED_FIELD` — JSON parses but required key missing per the tool's documented schema
   - `EXTRA_UNKNOWN_FIELD` — JSON has fields not in the tool's schema (warn, not error in v1)
   - `TYPE_MISMATCH` — field present but wrong type
   - `SCHEMA_VIOLATION` — passes type check but fails tool-specific validation (e.g. claim too short, URL not parseable)
3. **Server-side instrumentation:** each new failure mode counts to a per-tool counter in `~/.engram/toolcall-invocations.jsonl` (extend the `repaired` boolean to a `failure_category` enum, with `null` for clean calls).
4. **Documentation freeze:** every tool's per-call schema has to be documented in its docstring AND in a machine-readable form the server can validate against. Pick: JSON Schema in tool metadata vs. inline pydantic models.

## What ships in the migration PR

### Server-side (server.py)

- 43 tool functions decorated with `@mcp.tool` — each gets its signature collapsed:
  - **Before:** `engram_add_observation(quoted_text: str, interpretation: str, claim: str, quote_type: str, url: str = "", ...)`.
  - **After:** `engram_add_observation(payload: str)` where `payload` is a JSON-string that parses to a dict matching the tool's documented schema.
- A shared dispatch helper `_parse_payload(tool_name, payload)` that:
  - Parses the JSON.
  - Validates against the tool's schema.
  - Returns either the validated dict OR a categorical failure reason for telemetry.
- Each tool's docstring includes the schema verbatim (machine-readable + human-readable in one place).

### Client-side (engram_client.py)

- `EngramClient.call(tool_name, args_dict)` — change the wire format to `{tool_name}(payload=json.dumps(args_dict))` automatically.
- This is the layer that lets internal callers (selftest fixtures, sub-agents, dispatcher tools) keep their existing kwargs-style call sites without per-call rewrites.

### Telemetry (already in flight from #3)

- The repair hook continues to fire on the OLD failure mode for any rollback path (it's a no-op on successful single-payload calls; defensive only).
- Server-side adds the new-failure-mode counters as described under pre-conditions.

### Tests

- Update test fixtures' `_DirectClient.call` shim to translate kwargs → `{"payload": json.dumps(kwargs)}`. The change is at the shim, not per-test.
- Add new test file `tests/test_payload_validation.py` covering each failure category at least once.

## What does NOT ship

- **Two-formats coexistence on any single tool.** ob_1499 is explicit: all-tools-at-once flag-day at merge. A tool either accepts named-params (pre-merge) or single-payload (post-merge), never both. Reduces the test matrix and keeps the cognitive model crisp.
- **Backward-compat shims for old call signatures at the client level.** `EngramClient` is the boundary; downstream callers use it. There's no public API surface that takes named params directly.
- **A sandbox period of dual-running.** Per ls_0023, the baseline is the natural-accumulation pre-change data. We don't need parallel runs of both signatures — we have the old-shape baseline already.

## Migration sequence (in order)

1. **Pre-flight (already DONE / in flight):**
   - [x] Hook-level telemetry counter added (this morning, awaiting commit).
   - [x] Reader script (`tools/antml_repair_baseline.py`) for baseline analysis.
   - [ ] Server-side failure-category counters added on the OLD signature surface (not strictly needed but lets us have a richer pre-baseline if categorization shifts). Optional.
   - [ ] Calibrate "what's a stable baseline" by running the reader daily and watching the rate stabilize.

2. **Migration PR:**
   - [ ] Refactor server.py — single payload per tool. ~200-400 LOC change.
   - [ ] Refactor engram_client.py — translate-on-call.
   - [ ] Update test shim once.
   - [ ] Add `tests/test_payload_validation.py`.
   - [ ] Update each tool's docstring with the schema.
   - [ ] Verify pytest passes locally with `ENGRAM_NO_EMBEDDINGS=1`.

3. **Post-merge monitoring:**
   - [ ] Run `tools/antml_repair_baseline.py --since <pre-flag-day>` and `--since <post-flag-day>` — compare total error rates.
   - [ ] If post-migration total error ≥ pre-migration, ROLLBACK per ls_0023 — revert the migration PR, keep telemetry, post-mortem.
   - [ ] If post-migration total error < pre-migration, file an ENGRAM observation confirming the win and the magnitude.

## Rollback plan

- Migration PR is one squash commit on dev.
- Revert: `git revert <merge-sha>` produces a clean revert PR.
- Telemetry continues uninterrupted (the invocation counter shape doesn't change cross-revert).
- The post-mortem ENGRAM observation must include WHY total error rate didn't drop — was it MALFORMED_JSON dominating? Schema-too-strict? Per-tool docstring gaps causing TYPE_MISMATCH at high rate? Each failure mode informs whether the design was wrong or just executed wrong.

## Open questions before kicking off the migration PR

- **Do we standardize JSON schema location:** inline pydantic vs. external `schemas/<tool_name>.json` vs. tool decorator metadata. Pick one for consistency.
- **Do we require `payload` to be a string (the agent JSON-encodes) or a JSON object directly?** Strings have the documented-schema advantage (the agent's training is heavy on JSON-as-string in MCP contexts). Objects are easier for sub-agents and tests. Default: string, with possible relaxation post-v1.
- **What's the minimum baseline volume?** Need at least 2 weeks AND ≥1000 invocations? Stricter? Looser? This is a calibration call — we'll see how the data looks.

## Composes with

- **engram_query_pattern (ob_1500):** if engram_query_pattern lands first, it ships with the new signature shape from day one (no double-migration).
- **Sub-agent dispatch (ls_0021):** sub-agent prompts that invoke `mcp__engram__*` need to pass JSON strings; update the prompt-injection workaround examples in the dream-fairy etc. agent .md files post-flag-day.
- **Telemetry from #3:** this entire migration is gated on that data; tight coupling.
