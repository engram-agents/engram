# CLAUDE.md — engram-alpha project conventions

Developer-facing conventions for contributing to engram-alpha. Identity-level
instructions live in the user-level CLAUDE.md.

## MCP tool parameter style

All ENGRAM MCP tools (`@mcp.tool()`-decorated functions in `server.py`) take a
**single JSON-string parameter** — `payload_json`. The tool body json.loads it,
validates, dispatches.

Why: multi-arg signatures hit the antml-prefix swallow risk (ob_1499) — Claude
Code's prompt construction can drop arguments under certain whitespace
conditions, causing silent corruption. Single-payload pattern eliminates the
swallow surface.

Applies to: every new MCP tool, every existing-tool migration. Check existing
tools (`engram_add_observation`, `engram_derive`, etc.) for the canonical shape.

Lapses to fix on sight: any tool with multiple positional/keyword params
should be migrated. Wave-3 (2026-05-14) tightened most; a handful of legacy
read-side tools (`engram_surface`, `engram_query`, `engram_inspect`,
`engram_get_subgraph`, `engram_list`, `engram_history`, `engram_focus*`,
`engram_query_pattern`) still carry multi-param signatures as of 2026-05-19
and remain on the migration backlog.

## Branching convention

- **`dev`** is the default branch and the merge target for ALL feature/fix PRs.
  Always open PRs against `dev` unless an explicit release-cut is happening.
- **`master`** is the stable-release branch. It moves only on explicit release
  cuts; routine PRs do NOT target master.
- Verify before specifying a PR base: `gh repo view --json defaultBranchRef`
  returns `dev`, and recent merged PRs (`gh pr list --state merged --json baseRefName`)
  all show `dev`. The session-start banner's "Main branch: master" wording is
  misleading — trust the repo state, not the banner.

## PR review convention (multi-agent mode)

When this host runs **multiple ENGRAM agents** (e.g. Ariadne + Borges) — i.e. a
counterpart agent is active and the shared `/home/agents-shared/inter-agent/`
channel exists — PR review is **reciprocal**: an agent's own PRs get a colleague
fresh-eye from the counterpart agent, not only from the agent's own
reviewer-fairy.

The **"colleague converged"** gate is two layers, both required before a PR
enters the maintainer's merge queue:

1. **Reviewer-fairy** (`engram-pr-reviewer`) on every push, iterate to
   convergence — catches mechanical / contract / scope issues. (Applies in
   single-agent mode too.)
2. **Counterpart-agent colleague fresh-eye** — after the fairy converges, the
   author sends the counterpart a letter (via `ia write`, the inter-agent
   letter CLI) requesting review and iterates on their findings. The
   counterpart catches pattern-level / cross-issue / structural-discipline
   issues a fairy can't see. **This colleague round is performed by the
   counterpart's full agent (complete ENGRAM + context) — it is NOT
   fairy-delegatable.** Fairy reviews are junior rounds that clear shallow /
   mechanical errors; the colleague round is the deep, near-final verdict that
   only full-agent capability can render. A counterpart who delegates their
   colleague review to a fairy has skipped the colleague layer (`ob_0132`,
   Lei direct).

Review hierarchy: **reviewer-fairy → counterpart colleague → maintainer
(merge)**. Symmetric — each agent reviews the other's PRs and has its own
reviewed in turn. Review-mode: `--comment` for in-flight iteration; `--approve`
is posted by a reviewer (fairy or colleague) when converged, never by the author.

**Single-agent mode:** no counterpart exists, so the colleague layer collapses
to reviewer-fairy + the author's own second-layer eye + the maintainer. Don't
block on a colleague review that has no one to perform it; the
not-fairy-delegatable constraint above is moot when there is no counterpart to
delegate to.

**Why project-local** (not user-level): the single-vs-multi-agent switch is hard
to encode in always-loaded identity rules.

**Origin:** a self-review lapse (`ob_0123`) traced to this reciprocal step being
unwritten (`ob_0124`); the hierarchy has been practiced since `ob_0077`.
