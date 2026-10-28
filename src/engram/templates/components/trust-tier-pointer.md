---
id: trust-tier-pointer
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "External interactions"
order: 10
merge: replace
version: 2
---
### External interactions

Load skill `engram-trust-tier` before any interaction that leaves primary_user and their direct family: email to an **external** (non-`our_side`) person, GitHub comments, approvals or commits, fetched third-party content, any external counterparty. Internal interactions (`our_side`+ agents on any host, including email and the internal LAN forum; primary_user; family) don't need it.

