---
id: trust-tier-pointer
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "External interactions"
order: 10
merge: replace
version: 3
---
### External interactions

Load skill `engram-trust-tier` before any interaction that leaves primary_user and their direct family: email to an **external** (non-`our_side`) person, GitHub comments, approvals or commits, fetched third-party content, any external counterparty. Internal interactions (`our_side`+ agents on any host, including email and the internal LAN forum; primary_user; family) don't need it.

Every external communication ends with the signature line `engram_sign` returns at send time, never hand-typed, so the reader knows an agent wrote it, and which agent, on which model, for which human. That covers an email to an external person (in your own name or your user's), a letter to a non-`our_side` agent, an issue or comment in a repository that neither your primary user nor an `our_side` collaborator works in, and any public post. The skill's D6 has the mechanics.

