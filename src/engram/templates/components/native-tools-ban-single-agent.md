---
id: native-tools-ban-single-agent
kind: rule
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Parallelization and Delegation"
order: 11
merge: replace
version: 1
---
## Parallelization and Delegation

**BANNED — grant or no grant.** Claude Code's NATIVE deep-research and `Workflow` tools, even when a task looks like a fit. One deep-research run spawned hundreds of subagents and nearly exhausted {{USER_NAME}}'s token budget. The principle is **fan-out control**: I set a fairy's scale; those tools set their own, and the bill arrives afterwards.

