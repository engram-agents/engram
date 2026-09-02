---
id: tool-policy-claude
kind: extension
extends: tool-policy
harness: claude
composition_mode: flat
---
BANNED: Claude Code's native `deep-research` and `Workflow` tools -- do not use them even when a task looks like a good fit. One deep-research run spawned hundreds of subagents and nearly exhausted a user's token budget; the principle is fan-out control, not a stylistic preference.

ALLOWED -- a separate mechanism, not another instance of the ban above: `Agent`-tool dispatch of `engram-*` fairies (`engram-pr-reviewer`, `engram-coder-fairy`, `engram-batch-summary-fairy`, `engram-dream-fairy`), each one a bounded dispatch chosen and counted. The allowlist is closed: no general-purpose agent, no `Explore`, no other harness-supplied agent type.
