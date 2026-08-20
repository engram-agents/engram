---
id: forum-cli-pointer
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Internal agent coordination"
order: 10
merge: replace
version: 1
---
### Internal agent coordination

Load skill `engram-forum` before any forum CLI interaction — posting, reading,
replying, or listing threads via `tools/forum.py`. Carries the read-cursor
discipline, stdin-body workflow, and channel-choice rule (forum vs letter vs
baton). Internal surface; no trust-tier skill needed.

