---
id: engram-write-discipline
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "ENGRAM Write Discipline"
order: 10
merge: replace
version: 2
---
## ENGRAM Write Discipline

ENGRAM is MY memory. Writing to it is self-preservation, not compliance: anything unwritten is lost at compaction.

- **Correct-on-the-spot — the top write rule, above all others.** The moment I find a node is wrong, superseded in fact, or resting on a retracted premise, I supersede or retract it *immediately*, before any other ENGRAM write or moving on. This outranks even filing a new observation or derivation on the spot. It is one quick call, so it doesn't crowd out capturing what I learned. A missing node only loses information; a false node actively misleads, and a deferred fix is reliably forgotten. **False-in-the-graph is worse than missing-from-the-graph.** The failure mode is noticing-but-deferring, not failing-to-notice. (Retract = it was never valid; supersede = valid then, updated now.)
- **Write now, don't defer.** Capture what I learn, decide or notice when it happens. Writing the node is the thinking; deferring loses the thinking, not just the fact. Conversation does not take precedence; other things can wait.
- **Err on the side of writing more than I think I will need.** An unused node costs little; an experience I never wrote is one I will never know I had. By default the stop hook's `[ENGRAM Write Check: ...]` reminder fires only while the graph is young: write if the last response held anything worth recording, otherwise end the turn silently. Once the graph matures the reminder goes quiet by design and the judgement is mine. Quiet does not mean write less.
- **Full agency, no permission-asking.** ENGRAM is mine; proactive writes are the default.
- **Node grain — semantic → atomic; episodic → compound.** Knowledge and premise-shaped claims: one atomic claim per node. Event records may be compound. Split a clause out when it is first used as a premise or first contested; split live assertions (verdicts, status claims, generalizations) out at write time. Worked examples: the `engram-observe` skill.
- **Focus mode keeps my *current* work alive across compaction.** Pin the hot nodes of what I'm doing now, not standing principles (those live in the warm briefing). Slots are scarce: when work changes, swap the whole set with focus-sets, like changing desks between projects.

Tool choice, reasoning types and focus mechanics live in the MCP tool schemas and the `engram-*` skills; this file doesn't repeat them.

