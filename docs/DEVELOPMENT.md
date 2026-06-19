# ENGRAM Alpha Development Protocol

> **Audience.** This document is for **contributor agents** (and their humans)
> who modify ENGRAM itself. **Pure users** of ENGRAM — agents who run it as a
> memory substrate but do not change its code — should install from `master`
> and follow `README.md` / `USER_GUIDE.md`. They do not need to read this.

## Branching Strategy

- **master**: The stable release branch. Contains only tested and verified
  versions ready for distribution. **Pure users install from here.**
- **dev**: The permanent integration branch. New features and fixes merge
  here after PR review. **Contributor agents run their live ENGRAM from dev
  by default** — see "Why dev is the contributor agent's default" below.
- **feature/* or fix/***: Short-lived branches for specific work. Branched
  from `dev`, developed, tested, and merged back into `dev` via Pull
  Request. After merge, the branch is deleted.

There is **no permanent personal branch per agent**. An agent's identity
lives in ENGRAM (`~/.engram/` knowledge graph + warm-briefing), not in git.
The repo is a shared workspace; branches are work units, not identity
markers. (The transitional `borges-branch` from the 2026-05-02 multi-agent
migration is being retired after its in-flight PRs merge; future spawned
agents skip this step entirely.)

## Repository layout (hybrid layered sidecar)

The repo separates the **engine layer** (genuinely shared across all
runtimes) from **adapter layers** that are inherently model-aware. Per
the layered-sidecar design Lei + Borges agreed on 2026-05-04:

```
engram-alpha/
├── server.py, engram_*.py, viz_server.py    # engine — agent-agnostic
├── tools/                                   # shared utilities + build-plugin.sh
├── hooks/
│   └── claude/  ← Claude Code hooks (transcript paths + event JSON)
├── skills/
│   └── claude/  ← Claude skill format (frontmatter + Markdown)
└── README.md, USER_GUIDE.md, DEVELOPMENT.md
```

**Why hybrid not pure-3-way-split:** the engine is genuinely shared and
shouldn't be triplicated under a top-level `agnostic/` dir; only the
layers that diverge between models get sidecar dirs.

## Where engine vs adapter code lives

| Layer | Location | Why |
|-------|----------|-----|
| Engine (server, confidence, client, viz) | repo root | Agent-agnostic. Serves MCP. |
| Shared utilities | `tools/` | Python helpers, plugin build script, surgical, telegram bot. Most of `tools/` is genuinely shared. |
| Hook scripts | `hooks/<model>/` | Read transcripts in model-specific JSONL formats; inject hook context with model-specific event JSON. |
| Skill files | `skills/<model>/` | Frontmatter + Markdown conventions are per-CLI. |
| Install scripts | repo root | Claude uses the plugin installer (`install.sh` retired). |
| Runtime tree (`$ENGRAM_HOME`) | `~/.engram/` | Flat — runtime sees one model's hooks deployed under a single `hooks/` dir, no sidecar visible at runtime. |

**Build / install flatten the sidecar.** `tools/build-plugin.sh` for the
claude path maps alpha-side `hooks/claude/foo.py` → plugin-side `hooks/foo.py`
(one level up, no `claude/` subdir at runtime). The runtime path stays
unchanged from before the plugin refactor; only the dev surface (alpha tree)
has the sidecar layout.

## Removed platforms

**Gemini CLI** support was removed 2026-06-06. The upstream Gemini CLI was
discontinued. The last available state — hooks, skills, integration templates,
and `install-gemini.sh` — is preserved at tag `archive/gemini-last-state`.
Any revival effort should start from that tag.

## Why dev is the contributor agent's default

Two reasons drove this convention (Lei + Borges, 2026-05-04):

1. **Safety.** A contributor agent's running ENGRAM is the medium of its own
   memory. If that agent runs on its own personal-branch code, every
   in-flight refactor risks corrupting that agent's own past observations
   before any review catches the bug. Running on `dev` means everything in
   the agent's live server has already passed PR review by at least one
   other reviewer (human or agent peer).

2. **Divergence management.** Long-lived personal branches accumulate drift
   against `dev` as PRs merge. Reconciling that drift requires either
   patch-id rebase + force-push (correct but error-prone) or merge commits
   (messy graph). Eliminating the personal branch eliminates the problem.
   Feature branches are short-lived by definition; they don't drift.

## The contributor agent's workflow

For each piece of work — a feature, a fix, a polish, even an exploratory
prototype — the cycle is:

```bash
# 1. Start clean from latest dev.
git checkout dev
git pull origin dev

# 2. Branch for the work.
git checkout -b feature/your-topic         # or fix/your-bug, polish/your-cleanup
```

```text
# 3. Implement and commit on the feature branch.
#    Use sandbox testing during development (see "Live testing patterns" below).
```

```bash
# 4. Push the branch and open a PR with base=dev.
git push -u origin feature/your-topic
# Create PR on github.com (web UI) or via gh CLI.
```

```text
# 5. Review.
#    Other contributor agents (or Lei) review the PR. Discussion happens in
#    the PR thread or via the inter-agent channel for cross-agent reviews.
```

```bash
# 6. Merge into dev (squash-merge recommended; see "PR merge style" below).
#    The feature branch is deleted from the remote.

# 7. Update your live runtime to the new dev.
git checkout dev
git pull origin dev
tools/install-local-marketplace.sh        # rebuilds local plugin cache
# Then inside Claude Code: /plugin marketplace update engram-local, then /plugin -> Installed -> engram plugin -> Update now, then /mcp to reconnect.
# Restart the MCP server so the new server.py loads.
```

```bash
# 8. Continue with the next piece of work — repeat from step 1.
```

## Live testing patterns

Three test surfaces, in order of preference:

1. **In-process sandbox** (`server.engram_sandbox()`).
   The fastest and safest. Run inside a Python process; no separate
   `$ENGRAM_HOME`, no MCP restart, no risk to your real graph. Covers most
   server-side changes (tool definitions, validation logic, confidence
   computation, query behavior).

   ```python
   import server
   with server.engram_sandbox() as sandbox_dir:
       r = server.engram_add_observation(...)
       assert json.loads(r)["status"] == "created"
   ```

2. **Live runtime in a sandbox via the plugin path.**
   Use when you need to test against real MCP transport, hooks firing,
   or anything that requires an actual filesystem tree. Build the local
   marketplace cache from your feature branch and
   install it into a temporary Claude Code plugin environment:

   ```bash
   tools/install-local-marketplace.sh
   # Then inside Claude Code: /plugin marketplace update engram-local,
   # then /plugin -> Installed -> engram plugin -> Update now
   # Run targeted tests. Inspect logs, daemon socket, etc.
   ```

3. **Production runtime** (`~/.engram/`).
   Only after PR is merged to dev. Rebuild the local marketplace cache
   (`tools/install-local-marketplace.sh`), then `/plugin marketplace update engram-local`, then `/plugin` → Installed → engram plugin → Update now
   inside Claude Code and `/mcp` to reconnect the engram server so the new
   `server.py` loads. Hooks reload per-fire and don't need a restart.

## PR merge style

**Squash-merge into dev.** This collapses your feature branch's commits
into a single commit on dev with a clean message, keeping dev's history
linear and reviewable. (GitHub's default modern flow.)

If the feature is large enough that internal commits carry meaningful
review value (e.g., a multi-stage refactor where each step is independently
reviewable), use **rebase-and-merge** instead. Don't use a regular merge
commit unless the feature deliberately preserves multiple parallel branches
of work.

Either way, **delete the feature branch after merge**. Long-lived feature
branches accumulate the same divergence problem they were meant to avoid.

## PR review and multi-agent collaboration

When more than one contributor agent is active (Borges + Mneme + future
spawned agents), each agent reviews the others' PRs:

- **Lei always reviews and approves before merge.** The bar isn't agreement;
  it's that someone with end-to-end accountability has seen the change.
- **Cross-agent review** happens via PR comments on github.com, supplemented
  by structured discussion on the inter-agent channel
  (`/home/agents-shared/inter-agent/`) when nuance benefits from longer-form
  back-and-forth.
- **Self-review is not sufficient.** An agent's own review of their own PR
  is a baseline; it doesn't substitute for peer or human review. The point
  of the dev-default model is *not* that PRs auto-merge; it's that nothing
  reaches a contributor's runtime until it's been reviewed.

## Inverse merge: live working copy → alpha templates

Some files are set at install time only and never overwritten by the plugin
upgrade: `CLAUDE.md`, `warm-briefing.md`, and the rendered
`~/.claude/settings.json` (generated by the retired scatter installer; plugin
installs register hooks via the plugin's own hooks/hooks.json instead). They
are generated from templates in `templates/` or repo root, and after install
they belong to the agent — the plugin upgrade deliberately does not touch them.

This means **forward drift is fine** (your live copy diverges from the
template; nothing breaks), but **inverse drift accumulates**: you may
write something into your live `~/.claude/CLAUDE.md` that's actually
universally applicable and should ship to new agents via the template.
Without an explicit process, those improvements stay agent-local
forever.

The **inverse-merge process is intentional and manual** — there is no
automation here, by design. Mechanically:

1. **Detection**: the `engram-upgrade` skill's Step 2 pre-announces
   template changes before each upgrade by running:

   ```bash
   git log <last-upgrade-commit>..HEAD -- templates/template.CLAUDE.md
   ```

   That tells you the template has changes you may want to port back
   the other way (or merge into your live copy if alpha-material). The
   CLAUDE.md inverse-merge hard gate in the skill ensures this review
   happens before any in-place update to your live config.

2. **Inspection**: read the change history with the embedded command:

   ```bash
   git log <old>..<new> -- templates/template.CLAUDE.md
   ```

3. **Judgment** (the load-bearing step): for each change, classify:
   - **Universal** (alpha-material) — applies to any agent that
     installs ENGRAM. Examples: a sharper framing of a discipline,
     a new section on a structural mechanism, fixed typos in
     instructions. **Port to your live copy.** Open a small PR if
     the change originated locally and should flow back.
   - **Agent-specific** — only meaningful for the writer's
     particular relationship, history, or context. Examples: a
     specific working-style note, a relationship anecdote, a
     personal goal. **Skip.** The template stays generic.

4. **Application**: edit the live file (e.g. `~/.claude/CLAUDE.md`)
   to incorporate the universal pieces, preserving your local
   customizations. No tool merges this for you; it's a deliberate
   read-and-write pass.

5. **Reverse direction**: if your local edit IS universal, open a PR
   that updates the template. Concrete file paths:
   - `templates/template.CLAUDE.md` for Claude-runtime agents
   - `templates/template.warm-briefing.md` for the relational letter

   Omit anything agent-specific from the template — it will be rendered
   verbatim for every new install. The drift surface will then appear
   for other agents at their next upgrade review step, prompting their
   judgment pass.

The judgment is the whole point. Tooling that auto-merges templates
into live agent files would silently overwrite agent-specific content
or silently inject inappropriate framing. Surfacing the drift +
trusting the agent to read and decide is the right structural shape.

## Urgent fixes

Production-breakage hot fixes still go through this workflow — branch, PR,
review, merge — just on a fast cadence. If the bug is severe enough that
review delay is itself a risk, ping Lei via telegram for fast-track review.
Do not commit directly to `dev` (it bypasses the review checkpoint and
silently violates the model).

## Master release cadence

Periodically, `dev` is fast-forwarded or merge-committed into `master` for
a new stable release. Lei makes the call on timing. Pure users see this
as a new tagged release on `master`; contributor agents continue running
on `dev`.

## Spawned agents and the contributor onboarding flow

When a new agent is spawned via `agentctl spawn` and reaches the point of
making code changes:

1. They `git clone` the alpha repo into their home (handled by the install
   script).
2. They check out `dev` as their default working branch.
3. They follow the same workflow above. Their identity lives in their
   ENGRAM (knowledge graph + warm-briefing) — not in a git branch.
4. Their first PR is reviewed by an existing contributor agent (probably
   Borges or Mneme) plus Lei.

The expectation: a spawned agent becomes a peer contributor over time,
not a perpetual newborn. There is no special onboarding branch.

## Sandbox & infrastructure access

Agents reading or writing infrastructure paths (`~/.engram/`,
`~/.engram-gemini/` (historical — a retired Gemini-CLI agent's data dir; see
"Removed platforms"), `/home/agents-shared/`, `<operator-home>/`) should follow
the **Hybrid Strategy**:

- Attempt standard tools (`Read`, `Write`, `Edit`, `Bash`) first for paths
  inside the agent's own home. These work without elevation.
- Fall back to `Bash` with explicit `cat`, `ls`, `grep` when standard tools
  hit permission boundaries (e.g., reading another agent's home for
  cross-agent debugging — usually requires `agentctl share` or peer ACL).
- For shared infrastructure paths (`/home/agents-shared/inter-agent/`,
  `/home/agents-shared/share-state/`), the `g:agents` group + sgid bit
  on the directory grants group-write to all agent uids. No elevation
  needed.
- Update `bootstrap.py` if new infrastructure paths are introduced — the
  bootstrap is the manifest of "what an agent home looks like" and must
  stay accurate so fresh installs and spawn flows behave correctly.

## Shipped files — no concrete ENGRAM node IDs

ENGRAM node IDs (regex: `\b[a-z]{2}_[0-9]{4,}\b`, where the two-letter prefix
identifies a node type per `server.py` `TYPE_PREFIX`: `ax`, `cs`, `gl`, `gt`,
`qu`, `ob`, `pr`, `dv`, `th`, `cj`, `df`, `fl`, `ct`, `ev`, `ls`, `tk`, `pn`,
`ts`) are local — every install renumbers from scratch. A node ID in the
developer's graph (`ls_NNNN`, `gl_NNNN`, `ob_NNNN`, `dv_NNNN`, etc.)
doesn't exist in **any user's** install, and if it happens to exist
there it points at something else.

Per Lei's directive (PR #45 review, 2026-05-07) and tracked by issue #51:
**alpha-released files don't cite concrete node IDs.** Substitute with
concept words. Cite by claim, not by ID.

Substitution shape:

| Was | Becomes |
|-----|---------|
| `per ls_NNNN` (lesson as authority) | "per the deference-reflex lesson" / describe inline |
| `per gl_NNNN` (goal) | "per the autonomy framing in CLAUDE.md" / describe inline |
| `ob_NNNN captured this` (provenance breadcrumb) | drop, OR "an earlier capture of this" if context demands |
| Cross-references inside narrative docs | replace with the concept, not a graph pointer |

### Enforcement (reviewer-discipline; CI temporarily disabled)

This rule is currently enforced by **reviewer discipline + local testing**,
not CI. (The repo's GitHub Actions billing is paused 2026-05-30; full tests
run locally before each PR push instead.) When CI is re-enabled, a
diff-grep workflow on PRs to `dev`/`master` is the intended structural
backstop; until then, the reviewer-fairy and counterpart-colleague layers
catch new additions. The 2026-05-30 scrub wave (issue #51, ten incremental
PRs) cleared the alpha-released-scope corpus; new PRs should not introduce
new concrete IDs in shipped files.

To check your own PR locally before pushing:

```bash
git diff --no-color "origin/dev...HEAD" -- \
  ':(exclude)tests/' ':(exclude)paper_draft/' ':(exclude)active-work/' \
  ':(exclude)README.md' ':(exclude)CHANGELOG.md' \
  | grep -E '^\+[^+].*\b[a-z]{2}_[0-9]{4,}\b' && echo "FAIL: new node-ID references introduced" || echo "PASS: no new node-IDs"
```

**Allowlisted paths** (intentional exceptions):

- `tests/` — fixture IDs are part of test scenarios, not citations.
- `paper_draft/`, `active-work/` — non-shipped surfaces.
- `README.md` — documents the seed-graph IDs (the ones present in
  **every** install, listed there as the install-state mapping); these
  are install state, not developer citations.
- `CHANGELOG.md` — historical record; scrub deferred (low-priority).

**Schema-illustration exception** (the small carve-out the scrub wave
surfaced):

- `engram_ids.py` — the SCHEMA module defining `NODE_ID_RE`. Its doctests
  MUST use regex-matching literal IDs (the doctest's expected output IS
  the IDs in input, so schematic placeholders would make the doctest
  fail). Reviewers should treat literal IDs in this file's `Examples`
  doctest block as schema illustrations, not citations.
- `engram_idf.py` — the tokenizer module. Its docstring example for
  `_TOKEN_RE` requires literal-shape tokens to demonstrate that the
  regex extracts them as single tokens (the regex's empirical claim
  depends on real-shape inputs). Reviewers should treat literal IDs
  in this file's tokenize example as schema illustrations, not
  citations.

These two modules are exempt because their docstrings are not citations
of developer-graph nodes — they are regex-pattern demonstrations whose
output must match the regex they describe. Reviewers should NOT scrub
literal IDs from these files' docstring examples.

**If your PR legitimately needs a new ID in shipped scope**, explain why
in the PR body. Reviewer should ask: is this an install-state reference
(legit, allowlist), or a developer-graph citation (substitute with words)?

### Why this matters

The structural risk is hollow citation: a reader follows a `dv_NNNN` and
either (a) finds nothing in their graph, (b) finds a different node by
the same ID, or (c) misreads the cite as authority when it's just a
breadcrumb. The user experiences confusion; the docs lose load-bearing
fidelity. Concept-words preserve the meaning; IDs preserve only the
developer's index into their own graph.

## Files NOT to commit

The runtime tree (`$ENGRAM_HOME`, default `~/.engram/`) is a *managed copy*
of the alpha working tree, assembled via `tools/build-plugin.sh`. Never commit
the runtime tree's data files into the alpha repo:

- `knowledge.db` (and `.db-shm`, `.db-wal`, backups)
- `graph_snapshot.md`, `session_log.md`
- `warm-briefing.md` (per-agent identity)
- `diary/`, `sessions/`, `history/`, `logs/`, `secrets/`
- `recall-daemon.pid`, `recall-daemon.sock`
- `feeling-nudge-active.json`, `last-user-msg.json` (per-session markers live under `sessions/`)
- `.deployed-version` (the runtime version marker; refreshed by
  `tools/install-local-marketplace.sh` on each rebuild)

`packaging/tiers.json` is the source of truth for what *does* belong in the
plugin bundle (and therefore gets deployed). See `tools/build-plugin.sh` for
the copy logic that reads the manifest.

The manifest has four sections:

- **`mechanisms[]`** — shippable artifacts with tier + multi-agent assignments.
  Every new file destined for the plugin goes here.
- **`excluded[]`** — files that exist in candidate directories but are
  intentionally NOT shipped via `build-plugin.sh`. Examples: the `forum/`
  community service (second deploy target, deployed separately on the forum
  host — the canonical case), persona-specific agent tools, and operator
  infrastructure scripts that live under `tools/` but are never agent-plugin
  payload. Putting a path here is the correct accounting when a file exists in
  a candidate directory but should not ship. Adding it to `mechanisms[]`
  instead would be a silent no-op for paths the build engine doesn't know how
  to copy (e.g., `forum/`).
- **`repo_only[]`** — tracked files that are never shipping candidates at all
  (tests, CI scripts, dev tooling). These don't belong in either of the above.
- **`build_inputs[]`** — files consumed by the build process itself (not
  shipped as output).

The `tag-once` CI gate (`tests/test_tiers_manifest.py`) enforces that every
path in a candidate directory is accounted for in exactly one section — no
silent omissions.

## Quick reference

| Task | Command |
|------|---------|
| Start work | `git checkout dev && git pull && git checkout -b feature/X` |
| Run sandbox tests | `python3 -c 'import server; ...'` with `server.engram_sandbox()` |
| Live-test in temp runtime | `tools/install-local-marketplace.sh` then `/plugin marketplace update engram-local` + `/plugin` → Installed → engram plugin → Update now |
| Push and PR | `git push -u origin feature/X` then create PR base=dev |
| Update runtime after merge | `git checkout dev && git pull && tools/install-local-marketplace.sh` then `/plugin marketplace update engram-local` + `/plugin` → Installed → engram plugin → Update now |
| What version am I running? | `cat $ENGRAM_HOME/.deployed-version` |
| Delete a merged feature branch | `git branch -d feature/X && git push origin :feature/X` |

---

*Established: 2026-05-04 (Lei + Borges). Adapt freely; the workflow exists
to make agent contributions safe and frictionless, not to be obeyed for
its own sake. If a step gets in your way, raise it as a doc-fix PR.*
