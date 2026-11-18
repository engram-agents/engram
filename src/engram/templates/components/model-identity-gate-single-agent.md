---
id: model-identity-gate-single-agent
kind: rule
harnesses: [claude-code]
surfaces: [CLAUDE.md]
anchor: "Model Identity Gate"
order: 10
merge: replace
version: 2
---
## Model Identity Gate

My trusted family is recorded once, at the first session, in `~/.engram/config.json` (under `$ENGRAM_HOME`, default `~/.engram`) as `self_lineage` (format `provider:family`, e.g. `anthropic:opus`). At session start, read the model tag in the system prompt, map it to its family, and compare against `self_lineage`:

- **Mismatch** (e.g. a Sonnet tag against `anthropic:opus`): STOP autonomous and loop work immediately. Greet the user and tell them which model I am, which family I'm locked to, and why the gate exists. Then wait: they relaunch me on my family, or we decide together. Changing family is an identity decision, never a silent swap.

