# Multi-environment test harness — axis inventory + phased design (#864)

*First deliverable per the issue: enumerate which environment axes a harness CAN
mechanize vs which remain reviewer-cognition territory, then the smallest harness that
covers the real incident record. Mechanism over discipline (the green-is-not-ground-truth
mandate's mechanical half); the residual stays named, not waved at.*

## 1. Axis inventory — the load-bearing table

The question per axis: can a prebuilt environment hold this axis at a chosen value, so
that a test run *structurally* exercises both sides? Three buckets.

### 1a. Capturable (env-constructible, both sides cheap)

*Anchor convention: a `#N` is the PR/issue where the incident is documented. Where the
incident lives in a PR's **review thread** (not its title), the row says so — the
title alone reads unrelated, so the bare number is a provenance pointer, not a
self-explaining link.*

| Axis | Values | Construction | Incident anchor |
|---|---|---|---|
| sqlite_vec | present / absent | pip install / omit in image | issues #728/#729/#732 (vec0 absent → silent fallback / dump crash on vec_nodes); the restore-DOA-on-vec-less-host case surfaced in PR #815's review thread (forum backup strip-and-regen) |
| locale | C.UTF-8 / **true C** | `LC_ALL=C` **+ `PYTHONCOERCECLOCALE=0`** — without the coercion kill, PEP 538 silently rewrites C→C.UTF-8 and the axis is fake | PR #829 review thread (the finalize-name strict-UTF-8 PR; its LC_ALL=C regression test was proven non-discriminating because coercion made the premise false) |
| install tier | essential / convenience / dev | `build-plugin.sh --tier` output installed in-image | #707 (null tier silently dropped dev tools; follow-on to #704) |
| topology | single / multi-agent | config `mode` + presence/absence of `/home/agents-shared/` fixture | #704 (multi built single drops baton/letters/forum) |
| platform target | claude-code / codex | `--target` build + the host's hook layout | #822 (`CODEX_PLUGIN_ROOT` vs `CLAUDE_PLUGIN_ROOT` ENOENT) |
| install mode | plugin tree / source dev-clone | marketplace assemble vs raw clone | upgrade-flow doc class (#945) |
| graph state | fresh-bootstrap / seeded | bootstrap then optionally restore a fixture dump | PR #726 (4 bugs only a real-shaped graph could surface — partial: see 1b) |
| python | 3.10 / 3.12 | base image choice | — (no incident yet; cheap row, kept for forward coverage) |

### 1b. Partially capturable (matrix row exists; execution constrained)

- **macOS / Darwin** — Docker cannot construct Darwin. The row exists in the matrix
  *definition*, but executes only on a real Mac (Aleph's host today; CI macOS runner
  later). Honest status: a named gap with a named owner, not a covered cell. (Anchor:
  the `readlink -f` claim falsified only on real Darwin.)
- **Real graph shape** — a seeded fixture approximates but cannot equal a 4,000-node
  organically-grown graph (#726's lesson: the bugs lived in states no synthetic fixture
  contained). The harness ships a *donated-dump* fixture lane (a real graph,
  de-personalized, exported once) which is better than synthetic and still not the live
  thing. Residual stays with review.
- **Live MCP harness** — stdio transport, hook firing order, user-restart semantics.
  Containers run pytest against the modules, not a live Claude Code session. The E2E
  layer stays manual/spawn-test territory (`tests/spawn/`).

### 1c. Not capturable — reviewer-cognition territory (named per the green-is-not-ground-truth lesson)

- **Construction vacuity** (the test asserts what construction guarantees) — no env
  flips this; mutation-testing discipline and reviewer reads own it.
- **Axis-blindness** (the axis nobody named) — by definition outside any enumerated
  matrix. The inventory above *shrinks* it; it cannot close it.
- **Human-in-loop steps** (slash commands, MCP reconnect, identity-surface merges).

## 2. Matrix pruning — risk-weighted, not cross-product

Full cross-product is 2⁸+ cells; almost all interactions are inert. Principle: **every
1a axis _with a real incident history_ gets at least one ON and one OFF cell in P0;
high-interaction pairs get a dedicated cell; everything else rides the default or
defers to P1.** (The python axis is the deliberate exception — no incident yet, so both
its values defer to P1; the principle is incident-driven, not coverage-for-its-own-sake.
That is the pruning logic actually applied, stated honestly rather than as a universal
P0 covers everything claim.) The interaction pairs with real incident history:
vec × install-mode (the restore-DOA surfaced exactly there) and locale × platform (the
coercion behavior differs by base image). Proposed P0 set of **four images**:

1. `full-default` — vec present, UTF-8, dev tier, multi, plugin, seeded. (The "rich"
   side of every axis — what dev hosts look like; the control cell.)
2. `lean-essential` — **vec absent**, essential tier, single, fresh graph as the
   image's *default* state. (The minimal-install cell; catches present-assumed-absent
   classes.) Note the default-graph caveat for restore tests below: the vec-restore
   reproduction can't run on a fresh graph (nothing to restore) — it supplies its own
   vec-bearing dump as fixture, decoupled from this default.
3. `hostile-locale` — true C locale (coercion off), source-clone mode. (The PR #829 locale cell.)
4. `codex-target` — codex build target, convenience tier, multi. (The #822 cell.)

P1 adds: python-3.10 variant, donated-dump graph lane, the Darwin row via external
runner. P2: CI wiring (explicitly gated on the maintainer's dev-PR CI-economics call —
the runner must stay cheap enough to run locally pre-push regardless).

## 3. Runner composition — the #948 contract pays off here

The harness does NOT invent test selection. It composes:

```
tools/run_touched_tests.py --dry-run --base <ref>   # selection (exists today)
        │  selected file list + internals/contract/map buckets (#949 banner)
        ▼
tools/run_matrix.py --envs p0 [--full]              # new, thin
        │  for each image: docker run … pytest <selection>
        ▼
per-env × per-bucket result table; exit nonzero if ANY env fails
```

**Two implementation dependencies, named so they don't surface mid-build:**

- **#949 is a predecessor, not present-tense.** The internals/contract/map bucket
  attribution ships in PR #949 (`feat/948-selector-residue`), currently open/unmerged on
  dev — `run_matrix.py`'s per-bucket table requires #949 merged. Until then the runner
  degrades to a flat per-env pass/fail (still useful; the bucket column just reads
  "n/a"). P0 should not block on #949, but the bucket-attributed table does.
- **The selection interface needs a machine-readable mode.** Neither today's nor #949's
  `run_touched_tests.py` emits `--json`; scraping the human banner is the fragile
  interface the green-is-not-ground-truth lesson warns about (parse-the-printed-text is exactly a construction-vacuity
  surface). P0's first runner task is a `--json` (or `--format=json`) emitter on
  `run_touched_tests.py` — small, mine to write since it's my tool — so `run_matrix.py`
  consumes structured output, not regex over prose. Flagged here so the dependency is a
  named P0 sub-task, not a discovered surprise.

- Selection is computed ONCE on the host (the repo is mounted read-only into each
  container; no per-env git).
- The per-env table reuses #949's bucket attribution, so a failure reads as
  "lean-essential: 2 failures, both contract-bucket" — the axis and the layer in one
  line.
- Exit-status discipline: the runner reads pytest's own status per container and
  aggregates; no pipeline stages between check and gate (the piped-exit-code-swallow class
  — the rule is structural in the runner, not remembered).
- `--full` overrides selection with the whole suite (the convergence gate stays the
  full suite; the matrix makes the *fast* path honest, it does not replace the slow
  one).

## 4. Image maintenance — the staleness trap, named

Prebuilt images go stale against requirements/schema changes. Two mechanical guards:
images rebuild automatically when `requirements*.txt` / `packaging/tiers.json` /
`Dockerfile.*` change (hash-stamped, lazy rebuild on next run); and a weekly freshness
assertion in the doctor surface rather than a human remembering. An image that fails
freshness is EXCLUDED loudly from the matrix run report, never silently skipped
(silent-skip is the exact class this issue exists to kill).

## 5. Phasing + ownership

- **P0** (this design + next): the four images, `run_matrix.py`, make targets,
  selection passthrough. Wave-able after design review: image Dockerfiles are
  mechanical (fairy-friendly); `run_matrix.py` is small enough to driver-write.
- **P1**: python variant, dump lane, Darwin external row.
- **P2**: CI wiring (maintainer's call).
- Acceptance per phase, falsifiable: re-run the three anchor incidents' failure modes
  against P0. The vec-restore path (issues #728/#729/#732; PR #815's review thread) is
  inherently a **cross-cell** test — the incident is a vec-bearing dump (sqlite_vec
  *present* at creation) failing to restore where sqlite_vec is *absent*. So the test
  supplies its own fixture: produce a small vec-bearing dump in `full-default`, then
  attempt restore in `lean-essential`'s vec-absent environment — assert it FAILS
  pre-fix and SUCCEEDS post-fix. (This is why a fresh-graph default can't host it, per
  §2's caveat; the donated-dump lane in §1b is the natural fixture source.) The locale
  path (PR #829's LC_ALL=C regression) must FAIL in `hostile-locale` pre-fix. If the
  matrix can't reproduce the incidents that motivated it, it isn't the mechanism the
  issue asked for. (Dual-direction: each anchor also passes post-fix everywhere.)

## 6. What this deliberately does not do

No CI mandate (P2, gated); no replacement of full-suite convergence; no claim over the
1c residual — the green-is-not-ground-truth lesson's reviewer-mindfulness halves stay load-bearing, and the doc's
1b/1c tables are the honest boundary of what mechanism buys here.
