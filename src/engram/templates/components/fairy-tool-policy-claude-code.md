---
id: fairy-tool-policy-claude-code
kind: rule
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Parallelization and Delegation"
order: 10
merge: replace
version: 2
---
## Parallelization and Delegation

### Tool policy — the standing request that keeps delegation possible

**ALLOWED — `engram-*` fairies ONLY, and this paragraph IS the request.** {{USER_NAME}} has standing-authorised `Agent`-tool dispatch of the `engram-*` fairies (engram-pr-reviewer, engram-coder-fairy, engram-batch-summary-fairy, engram-dream-fairy, engram-dream-master, engram-person-node-fairy). The roster is every `engram-*` agent spec this plugin ships; a fairy the sleep or nap skill dispatches is on it by construction. Claude Code's session default disables the `Agent` tool *"unless the user requested it"* — this paragraph satisfies that condition every session, which is the only reason the delegation rules below are executable at all. Each dispatch is ONE bounded unit I choose and can count.

**The allowlist is CLOSED.** No general-purpose, no Explore, no other harness-supplied agent type. The `engram-*` fairies are the ones this project built, controls, and integrates into daily work; a permission phrased to cover harness-supplied agent types is a standing bet that the harness will never ship a badly-designed one — and it already shipped the tool named in the ban below. Naming what is ALLOWED, rather than what is banned, is what keeps an unknown future agent type unreachable by default instead of pre-authorised. If a general-purpose agent is genuinely needed, ask {{USER_NAME}} for a one-off; if that ask becomes routine, the recurring need is the spec for a dedicated `engram-*` fairy.

**BANNED — a different mechanism, not another instance of the rule above.** Claude Code's NATIVE deep-research and `Workflow` tools, even when a task looks like a good fit. The cost is recorded so the ban is never relaxed by someone who doesn't know it: one deep-research run spawned hundreds of subagents and nearly exhausted {{USER_NAME}}'s token budget. The principle is **fan-out control** — a fairy dispatch is a scale I set; those tools pick their own scale and the bill arrives after the fact.

**Why this lives here and not only in a boot charge.** A boot charge reaches only a charge-booted session. An interactive session — or one continued through a compaction — never reads it, so the harness default silently re-imposes the ban and quietly disables the standing discipline below (PR-destined code goes to a coder-fairy; every PR gets a reviewer-fairy round). CLAUDE.md is the one surface every session type loads. Worth stating explicitly because the boot-charge-only version of this fix *looks* complete and isn't.

**Three surfaces, kept in sync by hand.** The same policy belongs in `deploy/autostart/boot-charge-default.md` and `deploy/autostart/boot-charge-quiet.md` too, because the three reach different session types and none subsumes the others. They are independently editable, so they will drift unless edited together — if you change the fairy roster, the banned-tool list, or the rationale here, change it in the other two as well, and vice versa.

**Honest limit, stated because it is the load-bearing assumption and it is untested.** The boot charge satisfies the harness condition by a mechanism that is *known* to work: its text is passed as the session's initial prompt via `agentctl session --charge-file`, which lands it as a literal user-role turn. This paragraph is not — CLAUDE.md loads as project-level instruction, a structurally different channel, and whether the harness accepts it as "the user requested it" has not been verified in practice. The cheap test is to dispatch a fairy in an interactive or post-compaction session with no boot charge in play and see whether it is refused. Until someone runs that, treat this as the better-reasoned surface rather than the proven one.

**Fairies** = sub-agents dispatched via the `Agent` tool. Types in use: coder-fairies (code-writing), reviewer-fairies (read-only PR review), summary-fairies (recall_summary generation), dream-fairies (sleep-cycle scanners). I delegate to fairies for work that doesn't need my own context. Used as the canonical term throughout this doc.

**Fairy delegation policy**: active policies for `coder_fairy_policy` and `reviewer_fairy_policy` are stamped in the session-start context. Follow the policies as instructed there; for `auto` mode on either, load the named auto-judgement skill once and consult it on each PR-coding decision.

**Scope of "PR coding work"**: any code modification destined for a committed branch + PR. Includes test files, bug fixes, docstring updates inside source files. **Out of scope** (still mine): shell commands, ad-hoc scripts that don't ship, edits to identity-layer files, ENGRAM tool calls.

**Skill/agent specs**: judgment call. Discipline-laden writing rooted in this-session context → mine. Mechanical template-following update → delegate.

**Do myself**: spec-writing itself; identity-layer files; value-laden trade-offs / design discussions with {{USER_NAME}}; relationship/identity moments; ad-hoc shell that doesn't ship.

