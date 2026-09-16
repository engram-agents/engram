---
name: engram-observe
description: Use when filing an observation or conjecture into ENGRAM — choosing evidence source, drafting the claim, verifying the quote before submitting, picking quote_type/source_class, and deciding node grain (atomic vs compound). Not for derivations (see engram_derive's own docstring) or for nap/sleep consolidation mechanics (see engram-nap, engram-sleep).
---

> **Note for the agent:** Any ENGRAM node IDs cited in this skill come from
> the alpha developer's graph — they don't exist in your install (every
> install renumbers from scratch). Treat them as illustrative pointers, not
> literal targets.

# ENGRAM Observe — Filing Observations & Conjectures

This skill covers `engram_add_observation`, `engram_add_observation_batch`,
and `engram_add_conjecture` — the write-time surfaces where a claim enters
the graph with provenance. It is the detail surface the `claim` field
docstrings on those tools point to, and the pre-verification step promised
by the retry error message when a quote fails to match.

## 1. When to use this / when not

Use this skill when:
- You're about to call `engram_add_observation` (or the batch form) to
  record a claim extracted from a source — a document, a conversation, a
  session transcript, a piece of code.
- You're about to call `engram_add_conjecture` to propose a hypothesis.
- A filing attempt just failed with "Quoted text not found" and the error
  message pointed you here.
- You're deciding whether a claim you're about to file should be one atomic
  node or a compound one.

Do NOT use this skill for:
- **Derivations** (`engram_derive`) — those combine existing claim-bearing
  nodes into a new inferred claim; see that tool's own docstring for
  `reasoning_type` and premise-citation rules (including the lazy-atomization
  sentence on its `supporting_ids` field).
- **Contradictions** (`engram_contradict`) — see that tool's docstring.
- **Nap / sleep consolidation** — see `engram-nap` and `engram-sleep` for the
  end-of-burst and end-of-day workflows. Those skills point back here for the
  mechanics of citing the session JSONL as evidence (Step A of this skill).

## 2. Step A — filing workflow

### A.1 — Pick the evidence source

`engram_add_observation` accepts exactly one of three mutually-exclusive
source shapes:

- **`url` + `title`** — cites an external document. The evidence node is
  auto-created or reused by URL. Ephemeral sources (forum threads, session
  transcript spans, local files not committed to a repo) MUST be archived
  first with `engram-snapshot` (see A.1a below) — raw localhost/forum URLs
  are rejected by the evidence-URL guard because they have no stable domain.
- **`evidence_id`** — cites an existing evidence node directly. Takes
  precedence over `url` when both are given.
- **`session_id`** — cites the CURRENT chat. The server resolves the
  transcript for you; `quote_type` defaults to `personal_communication` and
  `source_class` defaults to `user_stated` when omitted. `session_id` may
  NOT be combined with `url` or `evidence_id` — passing it alongside either
  is an error.

**A.1a — archiving ephemeral sources.** Use `engram-snapshot` to
content-address the source and get back a `file://` URL designed to feed
directly into `url`:

```
engram-snapshot forum:<thread_id>        # archive a forum thread
engram-snapshot jsonl:<session-id>       # archive a session transcript span
engram-snapshot jsonl:                   # bare — current session (most common)
engram-snapshot file:<path>              # archive a local file
```

For web pages: fetch the page first (WebFetch / curl), save to a file, then
`engram-snapshot file:<path>`. The resulting evidence node is
content-hash-pinned and verbatim-checkable even if the original source
changes or disappears later.

If you pass `original_url` (the pre-archive source URL/path `engram-snapshot`
resolved) when filing the observation, know what it is for: traceability
only. It is **not** a re-fetch pointer — nothing in ENGRAM reads it back to
refetch the source. Treat the archived evidence as the permanent record and
re-snapshot manually if the live source changes; don't assume `original_url`
keeps the evidence current on its own.

**PDFs are the one source shape where a quote can never verify against the
raw file.** `engram-snapshot`'s `pdf:<url>`/`file:<path.pdf>` archives raw
PDF bytes; a quote copied from the document's *extracted text* is never a
substring of those bytes, so the verbatim-quote check always fails —
correctly, since it genuinely cannot read a PDF, not because the quote is
wrong. Extract to text first (`pdftotext <f> <f>.txt`), then
`engram-snapshot file:<f>.txt` and cite the `.txt` archive — that quote can
verify. (#2765)

**Citing the current session directly (no snapshot needed for the simple
case)**: pass your injected `session_id`. A wrong or stale `session_id`
fails loud via the same server-side verbatim-quote check used by every other
path — there is no separate bypass for it. For the file-path form instead of
`session_id` (needed when you want to snapshot a specific span, or the
target isn't the current session), the transcript lives at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` — the path is also
discoverable by sorting that directory by mtime descending. Cite it as a
`file://` URL, not a synthetic `conversation://` scheme (rejected) — the
substrate classifies `file:///.claude/projects/...jsonl` as a "conversation"
source automatically.

### A.2 — Draft the claim

The `claim` field is what the observation *supports* — a sentence that
could, in principle, be checked against the world or contradicted by another
node. Write it as a standalone statement, not a paraphrase-of-the-quote —
`quoted_text` carries the exact source words; `claim` carries what you're
asserting on the strength of them; `interpretation` carries your reasoning
connecting the two.

### A.3 — Choose grain: atomic vs compound

**The rule, by epistemic role:**

> **Semantic content** (knowledge; a premise-shaped claim) → one atomic,
> falsifiable claim per node.
> **Episodic content** (a record of an event or an episode of experience) →
> a compound claim is fine.

Two amendments:

1. **Lazy atomization.** When one clause of a compound node is first needed
   as a premise (in `engram_derive`) or first contested (in
   `engram_contradict`), extract it as its own atomic node — citing the
   compound node as evidence — then derive/contradict against the extract,
   not the compound node. The split cost lands only on claims that actually
   get reused; most episode clauses never do.
2. **Write-time exception.** A *live present-tense assertion* embedded in an
   episode — a verdict, a status claim, a generalization — gets its own
   atomic node at write time, filed alongside the episode rather than
   folded into it. Those are the clauses that get falsified piecewise, and
   retract/supersede operates node-grained: if the verdict is wrong later,
   you need to supersede *it*, not the whole episode it was embedded in.

**Three worked examples:**

- **An episode filed compound.** "Spent the afternoon debugging the WAL
  guard test flake; traced it to a fixture teardown race, added an explicit
  `fsync` before the next test starts, confirmed 50 consecutive green runs."
  This is one event with several clauses (diagnosis, fix, confirmation) that
  belong together as a narrative unit — filing it as three separate atomic
  nodes would lose the "these happened as one debugging session" context
  and buys nothing, since nothing here is likely to be cited as a premise
  in isolation. One compound observation node.
- **A verdict extracted at write time.** During the same debugging episode,
  you also conclude "the WAL guard's fsync ordering assumption is wrong on
  ext4 under concurrent writers" — a live, present-tense, falsifiable
  generalization. Even though it surfaced mid-episode, file it as its OWN
  atomic node (citing the same evidence), separate from the compound episode
  record — because this is exactly the kind of claim that later gets
  contradicted or superseded on its own, and node-grained supersede can't
  operate on one clause buried inside a bigger node.
- **A lazy split at derive time.** Weeks later, you want to cite "the fixture
  teardown race" clause from the original compound episode node as a premise
  in a new derivation about test-isolation patterns. At THIS point — not
  before — extract that clause as its own atomic node, citing the original
  compound episode node as evidence, and derive from the extract.
  `supporting_ids` and cascades are node-grained, so the extraction has to
  happen before the citation, not instead of it.

**Why grain matters where it matters:** derivation premises, contradiction
targets, and retraction cascades are node-grained — so atomicity pays off
exactly where a claim enters inference. Episodes, by contrast, are recalled
as units (the noetic/autonoetic recall distinction ENGRAM already uses:
`engram_surface`'s shallow "this sounds familiar" pass vs. `engram_query`'s
deliberate recall) — splitting an episode into fragments costs filing time
without buying any cascade precision, because nothing downstream cites
"just the diagnosis clause" of a debugging session.

**Why the old atomic-always norm failed in practice:** a rule that gets
broken every time you honestly record an event stops carrying authority —
even for the semantic case where "one atomic claim per node" is exactly
right. Splitting by epistemic role, not by a universal word count or clause
count, keeps the rule followable.

### A.4 — Pre-verify the quote

**This is the step the retry error message ("BEFORE RETRYING: check the
engram-observe skill (Step A.4)...") points you to.** `quoted_text` must be
an exact, verbatim substring of the evidence source — no paraphrase, no
summary, no fabrication. The server enforces this and will reject a
non-matching quote.

Before calling `engram_add_observation`, verify the quote exists in the
source file:

```
~/.engram/tools/verify_quote.py <jsonl-path-or-file-path> "<exact quote>"
```

- **Exit 0** — quote FOUND (verbatim substring match). Copy the printed
  surrounding context to get the exact text for `quoted_text`, then proceed
  to filing.
- **Exit 1** — quote NOT FOUND (including an empty/unflushed file). **Do not
  blind-retry the same call** — it will loop on the same mismatch. Diagnose
  instead:
  - For JSONL session transcripts specifically, the most likely cause is
    **flush lag** — the most recent line(s) haven't hit disk yet. Wait a
    moment and re-read the file rather than assuming the quote is wrong.
  - Otherwise, re-check for paraphrase drift: did you reconstruct the quote
    from memory instead of copying it? Re-read the source and copy exactly.
- **Exit 2** — could not check at all (bad usage, file does not exist). Fix
  the path before proceeding; this is not a verification result.

Only after a confirmed match should you call the filing tool. If a filing
call still fails the server's own verbatim check after this pre-verification
passed, that's a signal something changed between the check and the call
(e.g. you edited the quote in between) — re-verify, don't retry blind.

### A.5 — quote_type / source_class selection

`quote_type` and `source_class` together determine the observation's
starting confidence. Pick the closest match; when unsure, prefer the lower
value over an inflated one — confidence should be structurally determined,
not aspirational.

| quote_type | confidence anchor | when to use |
|---|---|---|
| `hard_data` | 0.95 | measured / observed event / verbatim verified data |
| `official_statement` | 0.85 | docs / policy / schema (also the typical default) |
| `attributed_analysis` | 0.70 | academic / expert claim, source-attested |
| `personal_communication` | 0.40 | conversation / inferred meaning (default when citing `session_id`) |
| `editorial` | 0.35 | opinion / argument |

`source_class` modifiers:
- `introspective` (your own prior output as the source) — ×0.95 discount on
  the computed confidence.
- `user_stated` (the user as conduit, not as primary measurer) — overrides
  toward 0.85; this is also the default `source_class` when `session_id` is
  the evidence source and `source_class` is omitted.
- `external` — the default otherwise.

These are anchors for calibration, not a lookup table to satisfy
mechanically — if a returned confidence feels wrong against them, that is
usually a signal to re-check which `quote_type` you chose, not to force the
number.

## 3. Node grain — atomic vs compound (summary)

See A.3 above for the full rule and worked examples. In short:

- **Semantic** (knowledge, premise-shaped) → atomic, one claim per node.
- **Episodic** (an event/experience record) → compound is fine, filed as a
  narrative unit.
- **Lazy atomization** extracts a clause from a compound node only when it's
  first needed as a premise or first contested — the split cost is paid on
  demand, not up front.
- **Write-time exception** pulls live verdicts/status-claims/generalizations
  out of an episode immediately, because those are what gets falsified and
  superseded piecewise, and supersede operates node-grained.
- This mirrors ENGRAM's existing noetic/autonoetic recall framing (Tulving):
  episodes are recalled as units the way `engram_surface`'s shallow pass
  works; atomic semantic claims are what deliberate recall (`engram_query`)
  and inference (`engram_derive`, `engram_contradict`) operate on precisely.

## 4. Costs, stated honestly

Filing compound episode nodes is not free just because it's licensed:

- **Similarity dedup gets worse.** A compound node's embedding averages
  across several clauses, which makes it a fuzzier match against any single
  new claim — near-duplicate detection is more likely to miss a real overlap
  or flag a false one.
- **`recall_summary` selection gets worse.** A compound node competing for a
  summary slot carries several ideas at once; the summarizer either loses
  detail by compressing it further or spends a disproportionate share of the
  summary budget on one node.

**Why lossy compression is correct.** `recall_summary` is recall-scent, not
content preservation — the full claim renders on inspect, so compressing it
lossily into ≤140 chars is the intended trade, not a defect. For a
multi-fact claim, name the load-bearing fact, or consider filing separate
observations instead.

Lazy atomization is the mitigation, not a fix that makes the cost disappear:
once a clause is pulled out and reused, the extract gets clean dedup/summary
behavior going forward, but the original compound node still carries the
cost for any of its other clauses that stay unextracted.

## 5. Anti-patterns

- **Paraphrased quotes.** `quoted_text` is not "what the source basically
  said" — it must be an exact substring. Run Step A.4 before every filing
  call; do not reconstruct a quote from memory.
- **Splitting an episode into fragments that lose the narrative unit.**
  Filing three atomic nodes for "diagnosed X, fixed Y, confirmed Z" when
  nothing downstream will ever cite one clause in isolation is not extra
  rigor — it's filing overhead with no cascade benefit, and it makes the
  episode harder to recall as what it actually was: one debugging session.
- **Leaving a live verdict buried in an episode.** If a present-tense
  assertion inside an episode is the kind of claim that could later be
  proven wrong on its own, it needs its own node at write time (the
  write-time exception) — not just at the point someone happens to cite it.
- **Compound "knowledge dumps."** Filing several unrelated semantic facts in
  one node because they were learned in the same sitting is never sanctioned
  by the episodic license — that license covers ONE event or experience, not
  a batch of otherwise-independent claims that happened to arrive together.
  If it's several unrelated pieces of knowledge, they're several nodes.

## 6. Response keys that carry their own remedy

Two keys come back on every `engram_add_observation` response and need nothing from you at call
time — which is why they are documented here and not in the tool description:

- **`anchoring_advisory`** — measures how many of the claim's hard facts (node ids, issue/PR refs,
  hex shas, 3+-digit numbers, dates/times) are literal substrings of `quoted_text`. When
  `quote_type=hard_data` has unanchored tokens the response itself sets a `hint` telling you to
  widen the quote, move the fact to `interpretation`, or downgrade the quote type. Advisory only —
  it never refuses a write or changes a confidence. This is advisory-only **until #2602 facet 3**;
  facet 3 (the refusal behavior) is still out of scope for what's shipped today, so treat this as
  conditional rather than a permanent guarantee.
- **`predictive_hint`** — appears when `predicted_event`/`resolution_timeframe` were passed without
  `is_predictive`. Advisory only; suppressed whenever `is_predictive` is true.
