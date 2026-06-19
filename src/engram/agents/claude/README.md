# agents/claude/ — canonical source for shipped Claude sub-agents

This directory is the **source of truth** for sub-agent definitions shipped
in the plugin bundle (assembled by `tools/build-plugin.sh` per
`packaging/tiers.json`). The alpha
repo's own `.claude/agents/` directory contains relative symlinks
(`../../agents/claude/<file>.md`) pointing back here, so the alpha-dev
working copy and the shipped copy can never drift — there's only one
real file per agent.

## What ships

- `engram-coder-fairy.md` — dispatched for all PR coding work per
  `template.CLAUDE.md`'s delegation rule. Carries `isolation: worktree`
  and `default_background: true`; the canonical source is here so local
  `.claude/agents/` picks up frontmatter additions automatically via symlink.
- `engram-dream-fairy.md` — dispatched by `engram-sleep` Step 0 for
  parallel six-category consolidation scans. The sleep skill has a
  graceful fallback if the agent definition is missing, but the fairy
  scan is the high-leverage path.
- `engram-pr-reviewer.md` — referenced by `template.CLAUDE.md`'s PR
  Workflow Standards section; dispatched after `gh pr create` and
  re-dispatched after every round-N commit until convergence.

## What doesn't (intentional)

Agents that live at `.claude/agents/` but are NOT in this directory are
alpha-dev or alpha-author tools, not shippable:

- `engram-code-auditor.md` — power-user codebase pattern audit; useful
  but not load-bearing for the default install.
- `engram-paper-research.md` — paper-work tool for the alpha author,
  not user-facing.

## Editing conventions

When editing a shipped agent here, the alpha-dev `.claude/agents/<file>.md`
symlink picks up the change automatically — no separate edit needed. If
you ever find yourself wanting to make the alpha-dev and shipped copies
diverge (e.g., a Borges-specific override), break the symlink first;
otherwise the two are guaranteed identical by design.

Shipped agents are **de-personalized** — no `Borges` / `Lei` references,
no alpha-dev-specific node IDs (developer-graph citations like `dv_NNNN`, `ls_NNNN`, etc.). The
install-time test (`tests/install/run_install_test.sh`) greps the
shipped copies and fails loudly if a name-leak slips in.
