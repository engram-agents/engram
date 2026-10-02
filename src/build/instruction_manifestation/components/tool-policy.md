---
id: tool-policy
kind: base
composition_mode: flat
extension_required: true
---
Tool usage policy for this harness. Certain harness-native tools are BANNED outright -- the reason is recorded so the ban is never relaxed by someone who doesn't know the cost -- and a fixed set of engram-* fairies is the allowed delegation mechanism: reviewer (engram-pr-reviewer/reviewer-fairies), coder (engram-coder-fairy/coder-fairies), batch-summary (engram-batch-summary-fairy/summary-fairies), dream (engram-dream-fairy/dream-fairies), dream-master (engram-dream-master), person-node (engram-person-node-fairy). The concrete tool identifiers this policy names differ per harness and are never spelled out here (this base body stays harness-neutral by construction; lint (a) enforces it) -- see this harness's own tool-policy extension component for the specifics.
