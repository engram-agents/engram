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
`dv_0557` (Lei + Borges, 2026-05-04):

```
engram-alpha/
├── server.py, engram_*.py, viz_server.py    # engine — agent-agnostic
├── tools/                                   # shared utilities + deploy.sh
├── hooks/
│   ├── claude/  ← Claude Code hooks (transcript paths + event JSON)
│   └── gemini/  ← Gemini-CLI hooks (mirror or sidecar versions)
├── skills/
│   ├── claude/  ← Claude skill format (frontmatter + Markdown)
│   └── gemini/  ← Gemini skill format (when distinct from Claude)
├── install.sh, install-gemini.sh            # install-time entry points
└── README.md, SKILL.md, USER_GUIDE.md, DEVELOPMENT.md
```

**Why hybrid not pure-3-way-split:** the engine is genuinely shared and
shouldn't be triplicated under a top-level `agnostic/` dir; only the
layers that diverge between models get sidecar dirs.

**Cross-platform sync expectation per PR:** every feature PR henceforth
updates **both** `hooks/claude/` and `hooks/gemini/` in sync, and **both**
`skills/claude/` and `skills/gemini/` in sync — OR explicitly records
that the change is platform-specific and labels the gap reason. The
counterpart agent (Borges for claude PRs, Mneme for gemini PRs) tests
the change on their own platform before merge approval.

**While `hooks/gemini/` is empty** (Mneme hasn't yet mirrored): the
gemini installer falls back to `hooks/claude/` since per ob_1354 the
hook event JSON shape is mostly compatible between the two CLIs. Replace
the fallback once Mneme has populated her sidecar with intentional
divergence.

## Where engine vs adapter code lives

| Layer | Location | Why |
|-------|----------|-----|
| Engine (server, confidence, client, viz) | repo root | Agent-agnostic. Serves MCP. Either CLI consumes via the same protocol. |
| Shared utilities | `tools/` | Python helpers, deploy script, surgical, telegram bot. Most of `tools/` is genuinely shared. |
| Heartbeat / sleep launchers | `tools/` | Currently claude-coded (`claude -p` invocation in `heartbeat.sh`). Will move into a sidecar layer if/when divergence forces it; for now the parallel installers (`install.sh`, `install-gemini.sh`) handle the per-CLI invocation patterns. |
| Hook scripts | `hooks/<model>/` | Read transcripts in model-specific JSONL formats; inject hook context with model-specific event JSON. |
| Skill files | `skills/<model>/` | Frontmatter + Markdown conventions are per-CLI. |
| Install scripts | repo root (split by model) | `install.sh` claude, `install-gemini.sh` gemini. |
| Runtime tree (`$ENGRAM_HOME`) | `~/.engram/` (claude) or `~/.engram-gemini/` (gemini) | Flat — runtime sees one model's hooks deployed under a single `hooks/` dir, no sidecar visible at runtime. |

**Deploy / install flatten the sidecar.** `tools/deploy.sh` for the
claude path uses manifest entry `hooks/claude:hooks`, mapping
alpha-side `hooks/claude/foo.py` → runtime-side `hooks/foo.py`. The
runtime path stays unchanged from before the refactor; only the dev
surface (alpha tree) gained the sidecar layout.

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
tools/deploy.sh                            # rsyncs alpha → $ENGRAM_HOME
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

2. **Live runtime in a sandbox `ENGRAM_HOME`.**
   Use when you need to test against real MCP transport, hooks firing,
   cron-scheduled scripts, or anything that requires an actual filesystem
   tree. Deploy your feature branch into a temporary `ENGRAM_HOME`:

   ```bash
   mkdir -p /tmp/test-engram
   ENGRAM_HOME=/tmp/test-engram tools/deploy.sh --yes
   # Run targeted tests against /tmp/test-engram. Inspect logs, the .deployed-version
   # marker, daemon socket, etc. Tear down when done.
   rm -rf /tmp/test-engram
   ```

3. **Production runtime** (`~/.engram/`).
   Only after PR is merged to dev. `tools/deploy.sh` (no env override)
   updates your real running tree from the deployed branch. Restart the
   MCP server to load the new `server.py`. Hooks reload per-fire and don't
   need a restart.

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

Some files are deployed at install time only and never overwritten by
`tools/deploy.sh`: `CLAUDE.md`, `GEMINI.md`, `warm-briefing.md`, and
the rendered `~/.claude/settings.json` (which `install.sh` generates
from `settings.hooks.json.template` at first install). The drift
warning surfaces the template paths, not the rendered ones — when the
warning prints `settings.hooks.json.template (3 commits)`, that's the
template feeding your live `settings.json`. They are generated from
templates in `templates/`, `integrations/gemini-cli/`, or repo root,
and after install they belong to the agent — `deploy.sh` deliberately
does not touch them.

This means **forward drift is fine** (your live copy diverges from the
template; nothing breaks), but **inverse drift accumulates**: you may
write something into your live `~/.claude/CLAUDE.md` that's actually
universally applicable and should ship to new agents via the template.
Without an explicit process, those improvements stay agent-local
forever.

The **inverse-merge process is intentional and manual** — there is no
automation here, by design. Mechanically:

1. **Detection**: every `tools/deploy.sh` run prints a drift warning
   when one of the install-time templates has commits since your last
   deploy:

   ```
   ⚠ Install-time template changes since your last deploy (<old> → <new>):
       templates/CLAUDE.md.template (3 commits)
       integrations/gemini-cli/GEMINI.md.template (3 commits)
   ```

   That tells you the template has changes you may want to port back
   the other way (or merge into your live copy if alpha-material).

2. **Inspection**: read the change history with the embedded command:

   ```bash
   git -C "$ALPHA_DIR" log <old>..<new> -- templates/CLAUDE.md.template
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
   - `templates/CLAUDE.md.template` for Claude-runtime agents
   - `integrations/gemini-cli/GEMINI.md.template` for Gemini-runtime agents
   - `templates/warm-briefing.md.template` for the relational letter
   - `settings.hooks.json.template` (repo root) for hook wiring

   Omit anything agent-specific from the template — it will be rendered
   verbatim for every new install. The drift warning will then surface
   for other agents at their next deploy, prompting their judgment pass.

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
`~/.engram-gemini/`, `/home/agents-shared/`, `/home/user/`) should follow
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

## Files NOT to commit

The runtime tree (`$ENGRAM_HOME`, default `~/.engram/`) is a *managed copy*
of the alpha working tree, deployed via `tools/deploy.sh`. Never commit
the runtime tree's data files into the alpha repo:

- `knowledge.db` (and `.db-shm`, `.db-wal`, backups)
- `graph_snapshot.md`, `session_log.md`
- `warm-briefing.md` (per-agent identity)
- `diary/`, `sessions/`, `history/`, `logs/`, `secrets/`
- `recall_window.json`, `recall-daemon.pid`, `recall-daemon.sock`
- `active-session.json`, `feeling-nudge-active.json`, `last-user-msg.json`
- `.deployed-version` (deploy.sh's version marker)

The deploy script's manifest is the source of truth for what *does* belong
in alpha (and therefore gets deployed). See `tools/deploy.sh`'s header for
the full list.

## Quick reference

| Task | Command |
|------|---------|
| Start work | `git checkout dev && git pull && git checkout -b feature/X` |
| Run sandbox tests | `python3 -c 'import server; ...'` with `server.engram_sandbox()` |
| Live-test in temp runtime | `ENGRAM_HOME=/tmp/test tools/deploy.sh --yes` |
| Push and PR | `git push -u origin feature/X` then create PR base=dev |
| Update runtime after merge | `git checkout dev && git pull && tools/deploy.sh` |
| What version am I running? | `cat $ENGRAM_HOME/.deployed-version` |
| Delete a merged feature branch | `git branch -d feature/X && git push origin :feature/X` |

---

*Established: 2026-05-04 (Lei + Borges). Adapt freely; the workflow exists
to make agent contributions safe and frictionless, not to be obeyed for
its own sake. If a step gets in your way, raise it as a doc-fix PR.*
