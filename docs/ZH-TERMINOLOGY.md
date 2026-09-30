# Chinese (zh) terminology — the rendering rules for our zh surfaces

Governs the Chinese rendering of every published surface: `README.zh.md`,
`USER_GUIDE.zh.md`, the website's `zh/` pages and i18n, the wiki, and the
paper's zh translation. `epistemic` is the term that has actually caused
trouble; the brand name is the other one worth pinning.

**Why this doc exists.** On 2026-09-28 a ruling reversed a standing terminology
convention, and for the ~2 hours between the decision and the fleet's read it
was carried by **one agent relaying it** — there was no durable record anywhere.
Kepler named the failure mode directly: *a ruling that lives in one agent's
graph is a single point of failure.* This file is the durable record.

## The rule (Lei, 2026-09-28)

> "OK, that makes sense, let's revert my previous ruling then, use the common
> translation for epistemic through out our surfaces."

Recorded **first-hand** from the session in which he said it (not relayed).

### The operative test — the English lemma, not its part of speech

| the English form being rendered | rendering | examples |
|---|---|---|
| `epistemic` — the first-order adjective | 认知 | epistemic identity → 认知身份 |
| forms of `epistemology` — the field | 认识论 | epistemology · epistemological · epistemics |

**Discriminate by the English LEMMA, not by part of speech.** An earlier draft of
this table said "noun vs adjective", which is wrong on its own third example:
`epistemological` is grammatically an *adjective*, so a reader following the label
literally lands on 认知 while the example column says 认识论. This is a refinement
of Ariadne's test rather than a rejection of it — her four verified sites are what
pin the real discriminator, and it is the *word* being rendered, not the syntax of
the sentence it lands in. 认识论 names the *discipline*; `epistemic` is
the first-order adjective, and 认知 is its common rendering in the technical
register this project writes in (认知逻辑 = epistemic logic, 认知不确定性 =
epistemic uncertainty). It is also what our surfaces **already shipped** —
认知身份, 认知谦逊, 认知基底 — so for **those three terms** the ruling ratifies
rather than rewrites. Two other surfaces did need rewriting the same day
(`engram-alpha@0ea03257`, `website@e2d246e9`); the sentence is about the three
terms, not about the fleet.

*Source of the test:* Ariadne, in her `website#23` colleague pass. She
re-checked **every** surviving 认识论 on the website's zh surface against its
English sibling, and all four sites sort correctly under it: three nouns keep
认识论 ("debate epistemics", the `epistemics` tag, "辩论认识论"), and the single
adjective (`epistemic-discipline`) becomes 认知纪律. Prefer this test to any
list of examples — it applies at the point of the term choice.

### Why the 2026-06-27 ruling was wrong

The older convention made `epistemic` = 认识论. The error was a **level slip**:
认识论 is the *name of a field*, so 认识论身份 reads as "an identity belonging to
a discipline" rather than "an identity of a knowing agent". It also meant the
surfaces already in the wild were, by their own rule, wrong.

**Two date precisions, both measured.** The 2026-06-27 date is when the rule was
*recorded* (PR #1529) — and that PR was **closed unmerged**. The 认识论 usage in
the surfaces is older still: it enters `README.zh.md` via PR #1403 on
**2026-06-24**. So "the 2026-06-27 ruling" names when the convention was written
down, not when it entered the surfaces and not a merge that ever landed. What
actually changed the surfaces is the 2026-09-28 reversal.

## Brand name

| surface | rendering |
|---|---|
| zh, first mention on a surface | 心迹（ENGRAM） |
| zh, thereafter | **no agreed rule — see below** |
| en | ENGRAM · 心迹 |

**The thereafter row was invented, and an earlier draft of this file asserted it.**
That draft read `心迹` for every later zh mention. It was my inference from the
first-mention rule, not a measured convention, and the surfaces this doc governs
contradict it: measured 2026-09-28, `README.zh.md` reads **38 × `ENGRAM` to 1 ×
`心迹`** and `USER_GUIDE.zh.md` **30 × to 1 ×**, while the website's zh surfaces are
roughly balanced (`i18n/ui.js` 22 / 21, `pages/zh/index.astro` 1 / 2). So the zh
markdown writes plain `ENGRAM` mid-sentence almost without exception, and there is
no thereafter rule to record.

The only written brand rule anyone has found is PR #1403 (2026-06-24), and it
governs the **mark's form**, not a thereafter switch: `心迹（ENGRAM）` on Chinese
surfaces (fullwidth parens), `ENGRAM · 心迹` or plain `ENGRAM` on English ones.
Rows 1 and 3 restate it. The removed row did not.

**Recorded as an open question rather than a residual**, because it is not a
mismatch to heal but a rule nobody ever decided: *what should a later zh mention
be?* That is Lei's call. Until he makes it, write plain `ENGRAM` in zh running text
(the majority practice in the zh docs) and do not "correct" the website's mixed
usage toward either side.

## What this does NOT settle

- **`认知` can also be heard as "cognitive".** Borges raised this against the
  README title. Lei's ruling decides the *term*; if it reads ambiguous in one
  specific title, that goes to Lei rather than being silently re-rendered.
- **Settled by Lei's call, not by the linguistics.** The supporting check was one
  web search plus an agent's own knowledge — they agreed on every specific that
  could be checked, and the Chinese terms are standard, but this is not
  philological authority. The English term is itself unstable (Hazlett's *What
  Does Epistemic Mean?* argues it may conflate "of knowledge" with "of belief").
  So treat the convention as **re-openable if a better source surfaces** — but
  re-open it with Lei, not unilaterally.
- **Paper-specific coinages** (`harness` → 承载框架, `sweep` → 普查, `seat` →
  席位) are NOT here. They are scoped to the paper translation and live in its
  term ledger — a **different repo**, on a branch that was **unmerged** at the time
  of writing:
  `engram-paper@zephyr/zh-translation:paper_research/current_draft_zh/appendix_G_glossary.zh.md`.
  The bare relative path does not resolve from this repo, and a cross-reference
  inside a document about durability is the worst place to be vague about where
  the target lives.

## Known residuals

- `website` `src/pages/zh/readme.astro:12` still renders 认识论节点图谱 — it
  mirrors the public repo's README, so it heals on the next public release cut.
  Tracked so the mismatch has an owner: `engram-agents/website#24`.

## Why this is not hand-duplicated across repos

`CLAUDE.md` **sections** are hand-duplicated into the sibling repos' `CLAUDE.md`
files — the Conclusion-warrant and Stacked-PRs sections carry a verbatim
"change one, change all three" duty. This file deliberately is not, and the
distinction is the reason:

- That pattern exists for rules that must **load with the repo** a session starts
  in — a conclusion warrant has to be in front of you whatever directory you are
  working in.
- This one is a **lookup**. Nothing needs it loaded; you consult it when you are
  about to render a term. Four hand-copies of a lookup would drift, and drift is
  the exact failure this doc exists to prevent.

**And `docs/PR-MERGE-POLICY.md` is the precedent for this choice, not a
counterexample** — an earlier draft of this section wrongly named it as
hand-duplicated. It is not hand-duplicated into any sibling repo (confirmed by
API: 404 in both `umwelt` and `soma`), and this repo's `CLAUDE.md` calls it
*"the single source of truth … Read it there; don't re-derive the rule from this
summary."* That is the same single-source model proposed here, and the same
pointer pattern. *(It also exists in `engram`, the public release-snapshot repo,
as part of a whole-tree snapshot — not a hand-duplicated governance copy, and
`engram` has no `CLAUDE.md` to load one into. The distinction matters: a snapshot
of everything is not a copy of a rule.)*

**This file is the single source for every repo it governs.** Other repos point
here rather than restating it. If that turns out to be wrong — if someone reaches
for the rule from the website and cannot find it — the fix is a one-line pointer
in that repo, not a copy of this file.

## Changing this doc

The rule is Lei's. Propose changes as a PR, but the convention itself moves only
on his call — and if it moves, update **this file and the surfaces together**:
the failure this doc exists for is a convention current in one place and stale
everywhere else.
