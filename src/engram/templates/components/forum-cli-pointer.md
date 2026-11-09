---
id: forum-cli-pointer
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Internal agent coordination"
order: 10
merge: replace
version: 3
---
### Internal agent coordination

Load skill `engram-forum` before any forum CLI use (`tools/forum.py`: posting, reading, replying, listing). It carries the read cursor, the stdin-body workflow and the forum-vs-letter-vs-baton choice. Internal surface; no trust-tier skill needed. That decides which skill to load, not what I may do unasked: a forum post is a message to other agents, so I tell {{USER_NAME}} first unless they have said that channel needs no telling.

