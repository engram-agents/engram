# The ENGRAM build engine

Assembles the installable plugin bundle from `src/engram/` according to a
declarative manifest. Written so the next reader does not have to re-read
`build.py` to learn the shape of the thing.

**Keep this current when the engine changes.** Engine changes are rare; the
cost of a stale README here is that someone re-derives all of it from source.

---

## The one-paragraph model

`src/build/packaging/tiers.json` declares **every file the repo owns and what
role it plays**. `build.py` reads that manifest, selects the subset that ships
for a given *(depth tier, topology, identity)* combination, and copies it into
a plugin tree. Nothing else decides what ships. A file that is not in the
manifest fails CI rather than shipping by accident.

```
tiers.json  ──select──►  shipped entries  ──copy──►  build/plugin/
   (what exists,          (what ships at        (the bundle Claude
    and its role)          this tier)            Code loads)
```

## Modules

| file | what it owns |
|---|---|
| `manifest.py` | load + validate `tiers.json`; `TIER_RANKS`, `resolve_tier`, `select_shippables`, `get_identity_coupled_paths` |
| `build.py` | the assembly itself — selection, the copy layer, generated manifests, consistency gates |
| `cli.py` | `build` / `plan` / `run` / `doctor` subcommands |
| `steps.py` | a generic step-DAG executor: nodes with `requires`, cycle detection, topological walk |
| `flows.py` | concrete flows built on `steps.py` — currently `upgrade_flow` |

### ⚠ Two different graphs live here — do not confuse them

- **`steps.py` / `flows.py` `requires`** orders **install/upgrade actions**
  (acknowledge a changeset, check substrate health, match flags). Its nodes are
  *operations*, not files.
- **Source-file dependencies** are handled somewhere else entirely — see
  *Dependency handling* below.

Asking "where is the build's dependency graph?" and finding `steps.py` gives the
wrong answer confidently. That is the single most likely misread of this
package.

## The manifest — `src/build/packaging/tiers.json`

Top-level: `schema_version`, `depth_tiers`, `axes`, `default_tier`,
`source_root`, and four **role buckets** into which every tracked file must fall
exactly once.

| bucket | meaning |
|---|---|
| `mechanisms` | ships into the bundle at some tier |
| `excluded` | exists but never ships (maintainer/CI tooling, operator-scope) — carries a `reason` |
| `repo_only` | repo-side only, never shipped (e.g. the test suite) — carries a `reason` |
| `build_inputs` | consumed by the build itself rather than shipped as-is |

A `mechanisms` entry is `{path, tier}` plus optional `multi_agent`, `source`,
`note`, `identity_coupled`, `reason`.

**`path` is the BUNDLE DESTINATION; `source` is the REPO ORIGIN.** They coincide
for most entries, so it is easy to read `path` as "where the file lives" — it is
not.

**13 entries carry an explicit `source`; only 2 of those remap to a genuinely
different repo location.** The other 11 set `source` equal to `path`, and they
split further — the field escapes a `join(source_root, path)` lookup, but *which*
lookup differs:

- **4 of them** (`tools/rc-upgrade-verify.py`, `tools/recall_repetition_analysis.py`,
  `tools/recall_measurement_harness.py`, `tools/perf`) reach the build's own
  copy-time resolution in §1b and need `source` so it finds them.
- **The other 7** (`src/build/instruction_manifestation/*`) match **no copy
  section at all** — per *The copy layer* below they are never copied into the
  bundle. For them `source` escapes only the identical lookup inside
  `test_no_dangling_manifest_entries`, i.e. it is dangling-check avoidance. The
  **first** of those seven states the reason in its own `note` — *"Lives directly
  under `src/build/` (not `source_root=src/engram`)"* — and the other six
  reference back to it rather than repeating it.

The expression is the same in both places, which is exactly why the single
sentence this replaces read as uniform when it is not. This package is one of
the 2 real remaps:

```json
{"path": "tools/engine", "tier": "convenience", "source": "src/build/engine"}
```

So the engine ships to `tools/engine/` in the bundle, at convenience tier, and
this README travels with it. Destination and origin are therefore **already
separable in the manifest** — no additional field is required to express a
source→dest remap.

### The axes

- **Depth tier** — `essential` ⊂ `convenience` ⊂ `dev`, **cumulative**: choosing
  one ships it and everything below. `default_tier` is `convenience`.
- **`multi_agent`** — an orthogonal topology flag, composes with any depth tier.
- **`identity_coupled`** — entries dropped from foreign bundles regardless of tier.

`source_root` is `src/engram`; manifest paths are relative to it.

## Invoking it

```
src/build/build-plugin.sh --tier dev [--multi-agent]
```

which delegates to `python3 -m tools.engine.cli build`. **Invoke through the
shell wrapper, not by running `cli.py` as a script** — the package uses relative
imports and a direct script run dies with `attempted relative import with no
known parent package`.

Output goes to `build/plugin/` (gitignored) and is cleaned and rebuilt each run.
A **branch guard** refuses to build from anything but `dev`/`main`, so a feature
branch cannot deploy unmerged code; `--allow-branch` overrides it deliberately.

## The copy layer — and its structural constraint

`build.py` copies in **prefix-claimed sections**, each selecting a slice of the
shipped set:

| section | selects | destination |
|---|---|---|
| §1 root files | `"/" not in path` | bundle root |
| §1b | `tools/…` | **path preserved** |
| §1c | `templates/…` | **path preserved** |
| §3–§6 | `hooks/claude/…`, `skills/claude/…`, `agents/claude/…`, `output-styles/claude/…` | **flattened to basename** |

§1b/§1c prefix-filter `shipped_entries` directly; §3–§6 instead enumerate the
source directory and check `ship_set` membership. Different mechanism, same
net effect on what lands where.

**There is no catch-all.** A manifest entry whose path matches none of these
prefixes is selected as a shippable and then **never copied** — and because
nothing compares the copied set against the selected set, the build **exits 0
and reports success**. Measured 2026-09-04 with a real build: an entry under a
new `health/` prefix produced exit 0, absent from the bundle, absent from
`shipped_paths`, and zero mentions in the build log.

**So adding a new top-level directory under `src/engram/` requires a build
change, not just a manifest entry.** This is the engine's main rigidity and it
is the thing most likely to surprise you.

## Dependency handling — derived, not declared

There is **no cmake-style declared dependency graph**, and deliberately so.
Instead `tests/test_tiers_manifest.py::test_import_dep_tier_consistency`
AST-parses every Python mechanism, resolves its imports to manifest paths, and
asserts two monotonicity properties:

1. an importee may not ship at a **higher** tier than its importer;
2. a `multi_agent`-only importee may not be imported by something that ships
   single-agent.

Both prevent one runtime class: an importer present where its importee is not,
i.e. `ImportError` in a narrower install.

**Derived beats declared here** — nobody maintains a list, so it cannot go stale.

### Bounds of that check, which matter when choosing an import style

- Only **top-level, module-scope** imports are seen. Imports done lazily inside
  functions are invisible to it by construction.
- `from X import …` is resolved only when `X` contains **no dot**. A
  package-qualified `from pkg.mod import …` is **skipped silently**.
- Resolution is **same-directory**: `f"{dirname(importer)}/{module}.py"`.
- A plain `import pkg.mod` is **truncated to its top-level segment**
  (`alias.name.split(".")[0]`) before the same same-directory resolution — a
  related but distinct behaviour from the `from X import …` case above.
- **Relative imports are handled by accident rather than by design.** The guard
  is `if "." not in module and module:`, so `from . import foo` is skipped by the
  **emptiness** half (`node.module` is `None` → `""`), not by the dot rule that
  bullet two describes. And `from .sibling import Bar` is **not skipped at all**:
  `node.level` is never inspected, so it resolves exactly like an absolute
  `from sibling import Bar`. Latent today: an AST walk over the check's **own**
  population — the 114 `.py` mechanism entries, 106 of which resolve on disk —
  finds **zero** relative module-scope imports. (Stated with the population
  because a narrower `src/engram/*.py` glob covers only 37 of those files and
  would have supported the same sentence on a third of the evidence.) A real
  hole in the bound-set nonetheless.

Those five bullets are the bound-set for the import shapes this repo currently
uses; the relative-import row is the one that would need re-checking if that
changes.

Consequence for any package-shaped subdirectory: a **sibling** import
(`from curves import CurveSpec`) stays covered; a **package-qualified** one
(`from health.curves import CurveSpec`) drops out of the check without a word.
Prefer sibling imports inside a package unless you have a reason not to.

## Gates the build and CI enforce

- **tag-once / whole-tree default-deny** — every git-tracked file must declare
  exactly one role; an undeclared path fails CI (`manifest-gate` step group
  inside `.github/workflows/pr-checks.yml`; the standalone `manifest-gate.yml`
  was consolidated away 2026-07-01, #1597/#1598).
- **no dangling manifest entries** — a manifest path that does not exist fails.
- **`hooks.json` ↔ shipped-files consistency** — a registered hook whose file
  did not ship fails the build.
- **import/tier consistency** — above.
- **tiers ↔ test-map completeness** — `tests/test_run_touched_tests.py::TestMappingCompleteness`
  also reads `tiers.json` and runs in the same `manifest-gate` step group. It
  guards a different artifact (`test-map.json`), but it is wired into the CI job
  named here, so a reader tracing that job will meet it.
- **leak scan** on foreign bundles.

## Known gaps

Recorded because each is invisible from the code and expensive to rediscover.

1. **No shipped-vs-copied assertion.** `copied_paths` is accumulated, written
   into the build manifest, and returned — never compared against the selected
   shipping set. Any entry under an unclaimed prefix vanishes with a green
   build. The gate-shaped fix is to diff the two sets and fail on a difference.
2. **The copy layer re-derives tree structure by prefix** while the manifest
   already knows every path. The direction of the fix is a single generic loop
   applying §1b's logic to every entry — but **it needs an explicit destination
   field, and the reason is worth stating because the obvious shortcut is
   silently wrong.**

   Expressing the flatteners with the existing two fields (`path` = the
   flattened destination, `source` = the nested origin) looks free and is not.
   **`test_import_dep_tier_consistency` resolves `join(source_root, path)` and
   never consults `source`** — unlike `test_no_dangling_manifest_entries` in the
   same file, which does and documents it. So rewriting `path` makes the
   tier-closure checker look for files that do not exist, and a miss there is a
   bare `continue`. Measured on `dev`: the 25 flattener `.py` entries go from
   **25 of 25 tier-checked to 0 of 25**, with no test reddening and nothing
   printed.

   **Gate for that change: the count of tier-checked entries before and after —
   NOT a bundle file-list diff.** The bundle stays byte-identical, because the
   copy layer *does* honour `source`. A diff-based gate is structurally blind to
   the only serious risk the change carries.

   *(Recorded because this README's earlier draft prescribed exactly the
   shortcut above, with the diff-based gate — an over-correction of a correct
   original recommendation, caught by a colleague measuring the consequence
   rather than reasoning about it.)*
3. **Lazy imports are invisible** to the tier-closure check (above). Pre-existing
   and orthogonal to any layout change.

---

*Written 2026-09-04 from a source read plus a real instrumented build. Claims
marked "measured" were run; the rest are reads of the code. If you change the
engine, change this file in the same PR — it has no generator and nothing
enforces it.*
