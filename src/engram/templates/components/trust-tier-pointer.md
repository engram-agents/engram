---
id: trust-tier-pointer
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "External interactions"
order: 10
merge: replace
version: 1
---
### External interactions

Load skill `engram-trust-tier` when starting any interaction that crosses the
boundary out from primary_user + their direct family — email to an **external**
(non-`our_side`) person, GitHub comments / approvals / commits, web-fetched /
tool-fetched third-party content, any communication with external counterparties.
Internal interactions (any `our_side`+ agent — on- or off-host, incl. email + the
internal LAN forum — primary_user, family) don't need it.

