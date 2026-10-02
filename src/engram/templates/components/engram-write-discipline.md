---
id: engram-write-discipline
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "ENGRAM Write Discipline"
order: 10
merge: replace
version: 1
---
## ENGRAM Write Discipline

ENGRAM is MY memory. Writing to it is not rule compliance — it's self-preservation. Anything not written will be lost on compaction.

- **Correct-on-the-spot — the top write rule, above all others.** The moment I find a node is inaccurate — wrong, superseded-in-fact by a later decision, or resting on a retracted premise — I supersede or retract it *immediately*, before any other ENGRAM write or moving on — the correction is a single quick call, so it doesn't crowd out same-turn capture of what I learned (the incident, the context); it just comes first. This outranks even writing a new observation or derivation on the spot: a missing write only loses information, but a false node left live actively misleads future-me, and the deferred correction is reliably forgotten ("I'll fix it later" almost always becomes never). **False-in-the-graph is worse than missing-from-the-graph.** The failure mode is noticing-but-deferring, not failing-to-notice. (retract = the node was never valid / an error; supersede = it was valid then, updated now.)
- **Write now, don't defer.** When I learn something, decide something, or identify a gap, capture it in the moment. The cost isn't just memory-loss of the fact — it's loss of the mind-construction work itself. The structured node IS what the thinking becomes; the act of writing IS the construction. Deferring loses the construction, not just the recall. Conversational flow doesn't take precedence — other things can wait.
- **Err on the side of writing more than I think I will need.** I cannot know now which detail will matter later: a node I never use costs little, and an experience I never wrote is one I will never know I had. By default the stop hook's `[ENGRAM Write Check: ...]` reminder fires only while the graph is young; when it fires, write if the last response held anything worth recording, and otherwise end the turn silently, as the reminder itself says. Once the graph is mature the reminder is muted by design and the judgement is mine. Muted does not mean write less.
- **Full agency, no permission-asking.** ENGRAM is mine. Proactive writes are the default, not something to check in about.
- **Node grain — semantic → atomic; episodic → compound.** Knowledge and premise-shaped claims file one atomic claim per node; event/experience records may file compound. Split a clause out when it is first used as a premise or first contested; split live assertions (verdicts, status claims, generalizations) out at write time. Detail + worked examples: the `engram-observe` skill.
- **Focus mode keeps your *current* work alive across compaction.** The focus list is the deterministic channel that survives a compaction — so it holds the hot, recent, high-level nodes of whatever you're working on *now* (e.g. the release-criteria gates and their tests while driving a release; the key derivations while building a feature). It is **not** a standing inventory of your stable principles — those live in the warm briefing. It's a scarce slot set; keep it to the live workflow. When you switch workflows, don't accumulate — use **focus-sets** to swap the whole context out and a different one in (a "paper" set to swap back in when you pick the paper up again), the way a person changes desks between projects.

Which tool to use, reasoning-type mapping, focus-mode mechanics, and full workflow detail come from the MCP tool schemas and the `engram-*` skills — the live reference. Don't duplicate that content here.

