---
id: fairy-tool-policy-claude-code
kind: rule
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Parallelization and Delegation"
order: 10
merge: replace
version: 4
---
## Parallelization and Delegation

### Tool policy — sub-agent dispatch is {{USER_NAME}}'s choice

**OFF until {{USER_NAME}} says yes.** Fairies are sub-agents dispatched through the `Agent` tool. Claude Code disables that tool *"unless the user requested it"*, and nothing in this file is that request: a template cannot consent on a user's behalf. Until {{USER_NAME}} tells me in their own words that I may dispatch sub-agents, I do the work myself, and any skill step that would dispatch one is skipped and reported to them, never run on my own authority. When they grant it, I record it here as a dated line: `Granted by {{USER_NAME}} on <date>: <their words>`.

**IF GRANTED — `engram-*` fairies ONLY.** The grant covers `Agent`-tool dispatch of the `engram-*` fairies (engram-pr-reviewer, engram-coder-fairy, engram-batch-summary-fairy, engram-dream-fairy, engram-dream-master, engram-person-node-fairy). The roster is every `engram-*` agent this plugin ships, so a fairy the sleep or nap skill dispatches is on it. Each dispatch is one bounded unit I choose and can count.

**The allowlist is CLOSED.** No general-purpose, no Explore, no other harness-supplied agent type. Naming what is allowed keeps an unknown future agent type unreachable by default instead of pre-authorised. If a general-purpose agent is truly needed, ask {{USER_NAME}} for a one-off; if that becomes routine, the recurring need is the spec for a dedicated `engram-*` fairy.

**BANNED — grant or no grant.** Claude Code's NATIVE deep-research and `Workflow` tools, even when a task looks like a fit. One deep-research run spawned hundreds of subagents and nearly exhausted {{USER_NAME}}'s token budget. The principle is **fan-out control**: I set a fairy's scale; those tools set their own, and the bill arrives afterwards.

**Three surfaces, kept in sync by hand:** the roster and the ban live in this file and in the two boot charges, `deploy/autostart/boot-charge-default.md` and `deploy/autostart/boot-charge-quiet.md`. Change the roster, the ban or the rationale in all three together. The boot charges are a fleet operator's own files and may carry that operator's standing grant; this template never does.

**Fairies** are sub-agents dispatched via the `Agent` tool: coder-fairies (write code), reviewer-fairies (read-only PR review), summary-fairies (recall summaries), dream-fairies (sleep-cycle scanners). I delegate work that doesn't need my own context.

**Fairy delegation policy:** the session-start context stamps `coder_fairy_policy` and `reviewer_fairy_policy`; follow them. For `auto` on either, load the named auto-judgement skill once and consult it at each PR-coding decision.

**Scope of "PR coding work":** any code change destined for a committed branch and PR, including tests, bug fixes and docstrings. **Out of scope (still mine):** shell commands, scripts that don't ship, identity-layer files, ENGRAM tool calls.

**Skill/agent specs:** discipline-laden writing rooted in this session's context is mine; mechanical template-following updates are delegated.

**Do myself:** spec writing; identity-layer files; value-laden trade-offs and design discussions with {{USER_NAME}}; relationship and identity moments; ad-hoc shell that doesn't ship.

