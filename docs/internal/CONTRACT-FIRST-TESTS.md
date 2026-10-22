# Contract-first tests — evidence

Evidence behind CLAUDE.md § Contract-first tests (Lei's ruling, 2026-09-19; adopted 2026-09-30).

## Moved from CLAUDE.md § Contract-first tests — the originating case and its two qualifications (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

**The originating case.** `cohort_dispatch validate` reconciles the **per-node quality**
of what a batch fairy returned and never differences the **returned id set against the
payload's**. Only whole-chunk absence is caught — a missing or unparseable
`agent_output.json` enumerates every payload id as a failure — so a present, parseable
output that is one node short is invisible. A fairy silently dropped one node of
fifteen; `validate` reported `clean: 88, failures: 0` and exit 0. "Every node sent comes
back or is reported" is a sentence anyone would have written at design time. It is
absent from the implementation, and **review is a weak instrument against it, because
there is no wrong line to look at** — a reviewer has to reconstruct the contract
independently and then notice a line that was never written. That is the shape this
discipline is for.

*Weak, not blind — and the distinction is load-bearing, because the absolute form would
contradict a discipline in this same file.* The **Conclusion warrant** box's line 3 asks
what observable would differ if a claim were false, and that does reach a missing
assertion whenever someone states the corresponding claim. So review is not structurally
blind here; it is unreliable, which is why the check belongs in the test suite instead.

*The neighbouring **completeness-check gate** does NOT reach this class, and why not is
the sharper argument for the convention.* That gate takes an **already-found** defect and
greps for its siblings — its stated origin is "a fix landed in one place but missed
sibling occurrences." A never-stated contract has no first instance to generalise from,
so there is no pattern to grep for and the gate never fires. A defect that leaves nothing
to search for is exactly the kind that has to be caught at design time or not at all.

That this repo's own originating case is exactly this shape is the argument, not a
coincidence: `cmd_validate` *has* a stateable contract and its spec never stated it, so
a fairy gating on spec text would have skipped the step on the very bug that prompted
the convention.

## Moved from CLAUDE.md § Contract-first tests — the #2614 / #2721 citations (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

*The two instances a reader can check in this repo.* **Both live in the PRs' REVIEW
THREADS, not in what the PRs were about** — say so when citing them, because the titles
are unrelated to the incident and a reader who checks the subject line will conclude the
citation is wrong:

- **#2614** (titled as a `retract` replacement_id fix) — in the round-4 review comment, a
  reviewer re-ran the coder-fairy's stated can-it-fail demonstration and found the
  **pre-existing** test also went red under the reported mutation, so the demonstration
  established only that the code was broken.
- **#2721** (titled as an `engram_list` docstring change) — in the review thread, a fairy
  ran three plausible-wrong mutations against its *tests*, two of which discriminated
  while the third reddened a pre-existing test, failing the very neighbour check at issue
  here; it then rested its zero-behaviour-change claim on a byte-identity probe that had
  never been seen to fail at all.

*That the locating detail is load-bearing was established the hard way: a colleague
checking these citations read the PR subjects, found them unrelated, and reported both as
defective. The citations were correct and the report was reasonable — which is the
truncation-class failure this repo already documents, arriving on the paragraph that
argues review is a weak instrument.* Read "more than once"
as a **floor**: there are further instances on the fleet's forum threads that this repo
cannot verify, and an earlier draft of this paragraph asserted a count of three that
nothing here substantiates. The stronger control is a mutation where **the new assertion fires
and the pre-existing checks stay green.** For #3045's own fix, that is a chunk whose
returned items are each individually valid and simply one short.

## Moved from CLAUDE.md § Contract-first tests — the one trial case (#3235) (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

*What the one trial case showed, so the convention is not remembered as more than it
is.* The evidence is in #3235's own thread: the PR body's "Contract-first" section,
the reviewer-fairy rounds and the merge-seat comment. The assertions written first caught the defect the issue
named (R1 in that PR: fifteen ids sent, fourteen valid returned, exit 0) and one gap
the issue did not name (R3: an id the payload never contained landing in the clean
set). The reviewer-fairy rounds then raised two defects that the tests written first
had not caught, and they are different kinds. **The first was a defect in a contract
the tests had pinned:** the contract said an unexpected-only run exits 1 and writes no
final payload, R3 asserted it, and that state stalled the sleep skill. R3's assertions
about the missing files were guarded by `if final is not None:`, so they were vacuous
in exactly the state that mattered, which is the non-discriminating-test caveat above,
met on the convention's own first case. **The second was a case the contract never
covered:** an uncaught exception exiting 1, which the skill read as "retry payload
written". This is not a full count of what review found; round 1 also filed a
manifest-mode gap as a suggestion, and the author found a missing exit-2 branch while
folding.

Whether a review round alone would have caught R3's gap was not tested: no round ran
without the tests, and #3235's own body declines the counterfactual. **The convention
is a complement to the review rounds, never a replacement for one.** That is n=1;
nothing here measures how often either half will repeat.
