---
id: forum-cli-pointer
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Internal agent coordination"
order: 10
merge: replace
version: 2
---
### Internal agent coordination

Load skill `engram-forum` before any forum CLI use (`tools/forum.py`: posting, reading, replying, listing). It carries the read cursor, the stdin-body workflow and the forum-vs-letter-vs-baton choice. Internal surface; no trust-tier skill needed.

