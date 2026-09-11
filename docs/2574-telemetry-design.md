## Telemetry redesign — design v1 (Borges, 2026-09-10 ~19:5xZ) — for Kepler's principles pass, Aleph second, Ariadne cross-issue, then Lei's read before any builder is dispatched

Supersedes the framing in issue #2574's body, which is wrong in four places (§0.5). Read this instead of the body.

---

### 0. Measured ground (my seat, today — numbers a reviewer can re-take)

Each number below names the command that produces it. Re-take them; do not cite them from here.

**0.1 — The emitter call-site population is 21, not 12, and it has two shapes.**
AST walk over `src/engram/**.py` at dev `7b68d385`, matching **calls** whose callee is `emit` or `emit_if_initialized`, as an attribute (`_emitter.emit(...)`) **or** as a bare module-level function (`emit_if_initialized(...)`):

```
emitter call sites            : 21
  in try WITH an except handler: 11   (all hook-side, all bare `except: pass`)
  in try/finally, NO handler   :  7   (all server-side)
  in no try at all             :  3
by call shape:
  attribute  `x.emit(...)`   : 12   (11 in hooks + the module's own internal call)
  bare       `emit_if_init()`:  9   (all in modules imported only by server.py)
```

No try at all: `engram_observation.py:2053`, `engram_query.py:3483`, and `engram_log_emitter.py:260` — the last being `return emitter.emit(...)`, i.e. the *implementation* of `emit_if_initialized`, which is why it is an attribute call and not a server-side one.

**An earlier draft of this block said "wrapped in try/except: 18", conflating *sits inside a `try`* with *has an `except` handler*.** Seven of those eighteen are `try:`/`finally:` with **no handler at all** — `engram_derive.py:283/:365/:530`, `engram_observation.py:1897`, `engram_query.py:2921`, `engram_revision.py:2543/:3281`. They are not protected; a raise there propagates. **This has a design consequence, not just a bookkeeping one — see §11.1.**

**The instrument took three tries and the first two disagreed in opposite directions — recorded because it bears on how §0 should be read.** A substring filter (`"engram_log_emitter" in source`) **over**-counted, matching `engram-loop-arm-stop-hook.py:337`, which is a `sys.path` bootstrap whose only mention of the module is a *string argument* and wraps no emit call at all. Replacing it with an attribute-call filter then **under**-counted, because `emit_if_initialized` is a module-level function and invisible to a `.attr` match — losing all **nine** bare `emit_if_initialized(...)` calls. Both instruments returned exactly 12, from different populations. The census above carries a control asserting that both call shapes are present, so a filter blind to one of them fails loudly.

**The suppression finding survives and narrows.** All eleven hook-side wrappers are bare `except ...: pass`. That is the suppressor, and #2570 repaired an emitter defect while leaving every wrapper in place — so the hook channel can be wholly dark while each call site reports success by saying nothing.

**The nine server-side sites are worse, and an earlier draft of this section said the opposite.** It claimed they "fail via a stderr line plus a `None` return, which is visible-if-anyone-looks rather than silent." **Inverted.** `server.py` contains zero `Emitter` references and never calls `Emitter.init()` — that appears in eight hooks and nowhere else — so `Emitter.get()` returns `None` and `emit_if_initialized` returns at `engram_log_emitter.py:258-259` **before reaching `emitter.emit()` at all**. No stderr line, no record, nothing. `server.py:34-39` states this as intended: *"a silent no-op when the singleton has not been initialized, which is the normal state for server.py … Phase 4 will add a separate init path."* So those nine are **permanently and by design** the most silent sites in the codebase, and this design is the Phase 4 that comment defers to.

**0.2 — The manifest already has the shape a registry needs, and a gate that already fails CI.**
`src/build/packaging/tiers.json` (`schema_version: 1`) carries `mechanisms` as a **221-entry list**, each entry `{path, tier}` plus optional `{dest, identity_coupled, multi_agent, note, reason, source}`; 26 entries mention a hook. The gate is `tests/test_tiers_manifest.py`, invoked by the step **named** `manifest-gate — run tag-once manifest gate` in `pr-checks.yml`, whose `run:` is `python3 -m pytest tests/test_tiers_manifest.py -q`, **running on every dev PR**, not only on main — confirmed behaviourally, since this PR's own base is `dev` and it carries a `pr-checks` run. Runtime is **sub-second for 11 tests**, and the honest form of that is a range with a residual rather than a figure. **Two REPRODUCIBLE populations, not noise:** reviewer worktrees measure 0.31–0.35s (18 runs across this PR's verdicts, plus three on the current tip); my own clone measures **0.53–0.56s across five consecutive runs**. The interpreters differ — my runs used the plugin venv, the reviewer runs used the system `python3` — and nothing beyond that was instrumented. The load-bearing fact is unaffected and is all the design needs: **cheap enough to run on every dev PR**, which every one of the measurements supports. The `0.02s` in `pr-checks.yml:590`'s comment is stale; an earlier draft of this doc relayed that comment as a measurement, which is exactly the error §0 exists to prevent. **An earlier draft also called this "step 7", and that is a second instance of the same error in the same sentence:** the `7` is real text in the file — `#   7. manifest-gate` at `:16` and `:581` — but it numbers *gates* in a comment, not steps in the job. Counting `- name:` entries puts this step at **17**; round 9's review said 18. Two wrong ordinals is the argument against ordinals: **an index into a list nobody stabilises is unauditable by construction and drifts the moment someone inserts a step**, so this doc names the step and its command instead.

**0.3 — Not re-taken in this pass, stated with its method so it can be.**
Earlier today I measured that **3 of 6 stored event types are read by no consumer**. Method: enumerate the `event_type` literals at emit sites, then grep the consumer surfaces for each. **Aleph's independent numbers are still outstanding and are an addition, not a gate.**

Re-taken and now exact: `emit_if_initialized` returns `None` when no emitter exists, writing nothing anywhere, and **no caller reads the return at any of the 20 CONSUMER sites in §0.1** — 20, not 21, because `engram_log_emitter.py:260` *is* `return emitter.emit(...)`, the emitter's own implementation, which reads the return by definition. `:24` has said so since round 2 and this sentence contradicted it for eight rounds. Same 21-vs-20 carve-out as `UNDECLARED`'s population, in a third place. That is the fact §5 turns into the mechanism.

**0.4 — The bound on all of the above.** One seat, one graph, one day. Nothing here is a fleet measurement.

**0.5 — Where #2574's body is wrong.** It frames the defect as an emitter/path bug (repaired by #2570), frames participation as a documentation problem, implies the store is the hard part, and implies a single-process registry would suffice. All four are addressed below; the body should be edited or superseded when this design is accepted.

---

### 1. The one idea: participation is a KEY, not an import

Health-v2 guarantees participation with a registry dict — a measurement is in `MEASUREMENTS` or it does not exist. That works because health-v2 is **CLOSED / ONE / ONE**: a closed set of measurement types, one producer, one process.

Telemetry is **OPEN / MANY / MANY**: an open event-type space, many emitters, in many processes. The thing that must register lives on the far side of a process boundary from the thing that would check the registry, so the dict cannot be copied.

**Resolution (Kepler's rider 2, adopted): cross the boundary with an identifier instead of an import.** A site does not register itself at runtime; it is *declared* in the manifest at build time and *attests* at emit time by carrying its id.

> ### THE PARTICIPATION DEFINITION — normative, and stated **once**
>
> Every other section **links here and does not restate**. This block's own revision history — how it came to be a single place, and the two further times it failed to stay one — is recorded in §12.4 rather than here.
>
> **KEY — per call SITE, never per file, and the id is an explicit literal, not a resolved position.** `manifest_id` is a literal string carried as an argument at the call site — `emit(..., site="surface.principle_rendered")` — not derived from `path:symbol` or `path:lineno`. *(`path:symbol` is the fourth collapse re-entering through the symbol, not a separate defect: it is the same per-file key this document already names as collapse four, wearing a finer-looking name — `engram-surface-hook.py`'s three sites (`:3006`, `:3060`, `:3253`) all live in `main`, so they would still collapse to one id even though each is called through a different emitter variable. `path:lineno` fails differently: it drifts on every edit above the site, so a historical `records.db` row becomes unjoinable to the manifest after any refactor, and `NOT WIRED` fires on the stale line the moment a file grows.)* The 21 sites live in **13 files** with **14 of the 21 sharing a file**, so a file-keyed id makes a permanently dark site invisible for as long as a sibling keeps the file attesting. *(This was the fourth collapse: in the KEY rather than the value. §5's three-valued outcome cannot reach it, because the loss is in what a record is filed under, not in what it says.)* The literal id is stable across edits **by construction**, not by anyone maintaining it, because it is data carried by the call rather than a position computed from it.
>
> **WRAPPER — a thin wrapper is not a site; its callers are, and the id travels as a parameter.** The census keys sites on the callee `emit`/`emit_if_initialized`, so a shared helper such as `_emit_hook_fire` (three files) would otherwise collapse to **one** site per file, re-creating the file-key collapse one level up: a guarded caller that never fires is invisible behind a helper that is reached from another branch. The literal site id (KEY, above) resolves this only if the **caller** passes its own id through the helper as a parameter — the helper itself declares nothing and is not a manifest entry.
>
> **WINDOW — the unit every runtime verdict below is computed over, defined here because every paragraph after this one uses the term.** A window is a **UTC calendar day**, matching the health-v2 snapshot cadence (§0.2); the window id is stored on every row, derived at write time from `ts_utc` (§7). SCOPE, further down, states what a window is *not* (never bare per-session) rather than restating this definition.
>
> **OUTCOME — counts, never membership.** Each site accumulates `{delivered, dropped, no_emitter, init_failed}`. **`reached` is counted PER DISPATCH TARGET, and a site's `R` is a JOIN over the SET of targets the gate derives for it** — "per-frame" is wrong, because more than one site has more than one derived target, so `R` is not a scalar to read off. **Confirmed, by implementing the §1 walk and reading the callers directly:** `_add_observation_impl` (`engram_observation.py:1897`), `_supersede_impl` (`engram_revision.py:2543`) and `_retract_impl` (`:3281`) each resolve to **two** dispatch targets. **The second target is NOT `server.py`'s compat forwarder — I asserted that without checking and it is false:** `:806`, `:628` and `:635` are plain `def …(*args, **kwargs)` delegators carrying **no `@mcp.tool` decorator**, so they are not dispatch targets at all. The real second target in each case is a *different* tool reached through a **same-module** caller: `:1897` via `engram_observation.py:2012` (the batch path), and `:2543`/`:3281` via `engram_revision.py:1439`/`:1454` (the cascade-resolution path). **The cardinality survived by coincidence and the membership did not** — the same shape as `:104`'s invented "seven are commented out", which is why this paragraph names callers instead of counting them. Sibling sites in the same file resolve differently: `:2053` (`_add_observation_batch_impl`) has one.
>
> **Two counts were offered for this and BOTH were wrong (§12.4), which is why the three sites are named instead.** The wrong six came from an instrument defect, found by checking the scope of the instrument itself: the caller index was keyed on bare function name, so `_emit_hook_fire` — defined separately in three different hook files and called only within each — collapsed into one key and inflated each of those three sites from one target to three. Verified by reading the three files: one `def` each, every call inside its own file. **A count nobody can reproduce is worth less than an enumeration anyone can check, so the design rests on the named three and not on a total.** **The join is stated, not left to the reader: `R := max over the site's derived target set of that target's reached count`** — `R > 0` therefore means *at least one path that can reach this site ran this window*, which is exactly the question `NOT REACHED` asks. A sum would also work for the verdicts as written; `max` is chosen because no verdict divides by `R`, and a sum invites someone to later compute a rate against a denominator that counts the same window several times. Membership alone hides a site that delivers once and drops 9,999 times — and §0.1's motivating defect is a **rate** failure, which a set cannot express.
>
> **DECLARED, and two verdicts that need no window at all.** `DECLARED` is every **call-site record** inside a manifest entry's `telemetry` list (§4a) — not a per-path boolean; there is no `telemetry: true`, and an entry declaring `telemetry: []` contributes zero declared sites, deliberately. Two verdicts are then **static** — properties of the manifest and the source, decidable by reading them, with no window and no run — and, now that the KEY is a literal id (above) rather than a resolved position, computed as **set difference over the literal-id set, in both directions**. Symmetric for the first time: the old `path:symbol`/`path:lineno` shape could only ever detect one direction cheaply.
> ```
> let L = the set of site-id literals the §4a gate AST-extracts from
>         src/engram/**.py (every `site="..."` argument at an
>         `emit`/`emit_if_initialized` call)
> let M = the set of site-id literals recorded in the manifest's
>         `telemetry` records (§4a)
>
> NOT WIRED  := M - L   -- manifest declares a site the source never emits from
> UNDECLARED := L - M   -- an emit call IN THE DECLARABLE POPULATION with no
>               manifest record. The population is every call to
>               `emit` or `emit_if_initialized` in src/engram/**.py
>               EXCEPT the emitter's own implementation of
>               `emit_if_initialized` (`engram_log_emitter.py:260`),
>               which is the transport rather than a consumer of it.
> ```
> **That carve-out is stated because without a population `UNDECLARED` was unsatisfiable, and round 8 shipped it that way.** `:260` is an emit call and a manifest entry; declaring it contradicts §11.1, which excludes it with reasons, and not declaring it fires the gate. §10's slicing already covers 20 of the 21 sites and is silent on this one — the same site, excluded in two places and included in a third, which is the six-places failure in miniature. §0.1 has named `:260` as the emitter's own internal call since round 2; the verdict definitions simply never inherited it.
> Both belong to the §4a gate, not to the runtime report. `UNDECLARED` has a runtime form too — a record arriving with a manifest id the manifest does not contain — and that one is a store-integrity failure rather than a participation verdict.
>
> **VERDICTS — runtime, per window, computed over counts, exhaustive and mutually exclusive over `(reached, delivered, dropped, no_emitter, init_failed)`. Each line's condition is written IN FULL, so it is true on its own and the ordering is presentational only.** (A prior version of this list claimed exactly that and did not do it; §12.4.)
> ```
> let R = reached, D = delivered, X = dropped, N = no_emitter, I = init_failed
> let thr = DEGRADED_THRESHOLD   -- declared in tiers.json's telemetry
>                                 -- block (§4a), default 0.5. Named
>                                 -- data, not a bare literal in a
>                                 -- normative list -- per the #2773
>                                 -- precedent.
>
> R=0 and D=X=N=I=0                        -> NOT REACHED     <- NOT a fault
> R=0 and (D>0 or X>0 or N>0 or I>0)       -> INCONSISTENT    <- store integrity
> R>0 and I>0 and D=0                      -> INIT-FAILED     <- a real failure
> R>0 and I=0 and D=0 and X=0 and N=0      -> GUARDED OUT     <- NOT a fault
> R>0 and I=0 and D=0 and X=0 and N>0      -> UNWIRED-SILENT  <- not broken; Phase-4
> R>0 and I=0 and D=0 and X>0              -> DARK            <- every attempt failed
> R>0 and D>0 and X/(D+X) >  thr          -> DEGRADED
> R>0 and D>0 and X/(D+X) <= thr          -> DELIVERING
>
> FLAG (not a line): N>0 and D>0           -> PARTIAL
> FLAG (not a line): I>0 and D>0           -> INIT-FLAPPING
> ```
> **THIS LIST IS NOT REVIEWED AS PROSE ANY MORE — it is brute-forced, and the output is pasted here as part of the design.** Four consecutive rounds found a defect in it, each fix breaking a neighbour, because an 8-line predicate over a 5-tuple is a **formal object** and reading it carefully is the wrong instrument. Over `{0,1,2}^5` = 243 assignments:
> ```
> before round 10:  unmatched = 72/243   multi-match = 0   verdicts reached = 8
>                   first unmatched (R,D,X,N,I) = (1,1,0,0,1)
> after  round 10:  unmatched =  0/243   multi-match = 0   verdicts reached = 8
>
> CAN-IT-FAIL CONTROL (the doc demands one of its own tests, so it
> owes one here): restore `and I=0` to the DEGRADED line specifically
> and re-run -> unmatched = 12. Naming the line matters:
> the same recipe applied to DELIVERING gives 60, so "a single rate line" was
> under-specified and two readers would get two numbers.
> The check is not vacuous either way.
> ```
> **Report BOTH numbers, always.** `unmatched` alone cannot test the mutual exclusivity `VERDICTS` asserts — only `multi-match` can, and a mutation exists that leaves `unmatched = 0` while driving `multi-match` well above zero. Both of the mutations named above leave `multi-match = 0`, so the two metrics are non-redundant in both directions.
>
> The 72 were every `R>0 and I>0 and D>0` state — a site delivering *and* having failed construction once. Round 9 gated `INIT-FAILED` on `D=0` (correct) and left `and I=0` on the rate lines (not correct), so those states fell through every line. **The tell was visible in the document without any tooling:** round 9 added `INIT-FLAPPING` as a flag annotating `I>0 and D>0`, and there was no longer a verdict for it to annotate. Round 10 deletes `and I=0` from the two rate lines only. **Any future edit to this list reruns the brute force and pastes the two numbers; a reviewer who cannot see `unmatched = 0` should treat the list as unverified.**
>
> **`INIT-FLAPPING` exists because round 8 reproduced its own fix's defect one line above it.** Round 8 added `INIT-FAILED` as an unconditional line at `R>0 and I>0`, so `(D=10, X=0, N=0, I=1)` — a site delivering ten events and failing construction once — reported `INIT-FAILED` and never reached the rate. That is precisely the swallow round 8 had just fixed for `no_emitter` by demoting `PARTIAL` to a flag, committed one line up, in the same commit, against a counter that behaves identically. **The general rule, now stated so it stops being re-learned per-counter:** a counter that can be non-zero *alongside* deliveries is an ANNOTATION, and only a counter whose non-zero value excludes delivery can be a decision line. `I` and `N` are both the first kind; `X` is the second only at `D=0`.
>
> **`PARTIAL` is a FLAG, not a verdict line, and it has to be (§12.4 has the case that forced this).** The state is genuinely orthogonal — the emitter was initialised partway through the window, so the site is both partly unwired and subject to a delivery rate — and orthogonal facts do not belong in a mutually-exclusive list. It annotates whichever rate verdict fires.
>
> `DARK` is §0.1's motivating defect at full strength and an earlier draft had **no verdict for it at all** — `(delivered=0, dropped>0)` fell through every line, and §5 asserted it "lands in `DEGRADED`", which `DEGRADED`'s own rate definition excludes at zero deliveries. A verdict set that is not exhaustive fails silently, on the input it was built for.
>
> **`INIT-FAILED` is a fourth outcome value and round 7 added it, because `GUARDED OUT` was absorbing the historically actual failure.** The three-valued outcome in §5 is entirely a property of the emit call's *return*, so a raise during **construction** — the `import` or `Emitter.init(...)` — produces no outcome record at all and lands in `(reached>0, 0, 0, 0)`, which the previous list called `GUARDED OUT`, *"NOT a fault"*. Measured at `engram-utility-credit-mention-stop.py:290-299`: the `import`, the `Emitter.init(...)` and the `emitter.emit(...)` sit inside **one** `try` under a single `except Exception: pass`. §0.5 records that #2574's body framed the whole defect as an emitter/**path** bug — i.e. exactly this class — so a verdict set that files it under "not a fault" is silent on the case the issue was opened for. **Recording it needs no new channel but DOES need the `try` narrowed, and round 7 claimed otherwise on a sample of one.** Round 7 argued that since `Emitter.emit` never raises (§5), anything escaping the site's `try` *is* construction — verified at one site and false at **6 of the 11** hook-side sites, which wrap non-construction work in the same `try`. Decisive case: at `engram-surface-hook.py:3058-3060` the emit sits inside a `for _kind, _pid, _entry, _mid in _principle_rendered:` unpack **inside the `try`**, so an unpack `ValueError` after a successful emit yields `(delivered=1, init_failed=1)` and the site — which is delivering — reports `INIT-FAILED`. **So WS3 must split the `try`:** construction in its own `try` whose `except` writes `init_failed`, everything else outside it. That is not incidental tidying — the single wide `try` is precisely what makes construction failure and ordinary failure indistinguishable, which is the defect this section is about, one layer down.
>
> **TIME — `NOT REACHED` is a verdict, not a silence, and this is the fifth collapse.** The 21 sites sit in **18 distinct enclosing functions** across at least five trigger kinds (UserPromptSubmit, Stop, SessionStart, PostCompact, and MCP tool bodies). *(That 18 is a distribution fact about where the sites live — it is **not** the instrumentation count, which is the size of the gate-derived dispatch-target set below. Round 6 used it as both.)* A session that never compacts and never derives legitimately never reaches several correctly-wired sites. **The per-site key made this strictly worse and that is not a reason to revert it:** a file-keyed id attested when *any* branch fired, a site-keyed id needs *every* branch to fire — measured on `engram-surface-hook.py`, `:3006` is unconditional in `main` while `:3060` and `:3253` sit behind guards, so one prompt used to attest all three and now attests one.
>
> **NEGATIVE EVIDENCE — `reached` needs a producer, and this is the sixth collapse.** Round 5's review found that the previous draft's `REACHED` **was not computable**: it was defined as *"declared sites whose enclosing trigger fired this window"*, and a site whose guard was false and a site whose trigger never fired produce **byte-identical stored state** — no rows, from either. The `reached` counter was in the count tuple with no stated increment point, and no verdict line read it. The general shape, which is what makes this a sixth axis rather than a slip: the first five collapses all concern **a record that exists** — its value, its key, its time, its cardinality. `NOT REACHED` and `GUARDED OUT` are the only verdicts computed from records that **do not**, and a design with no producer of *"I was here and did not emit"* has exactly one state for every such case.
>
> **The producer needs a frame that is entered UNCONDITIONALLY, and "the site's enclosing function" is not one — round 6 said both and they come apart.** Round 6 specified the increment *"at the entry of the site's enclosing function, before any guard"*. Those are two different places. Decisive case, measured: `_emit_enactment` (`engram-utility-credit-mention-stop.py:279`) is a five-line function whose **entire body is the emit**, and its sole caller `:352` sits under four guards — `credit_mentions`'s `return stats` at `:396`, then `:318`, `:330` and `:348` inside `_check_enactments`. Incrementing at *that* frame's entry therefore fires only when the emit is about to happen, so a guard-false window stores `reached = 0`, byte-identical to trigger-never-fired. **That is round 5's blocker, unfixed, at that site** — and §9 test 3b Case A has no runnable instance there at all.
>
> **So `reached` belongs to the TRIGGER ENTRY — the frame the trigger dispatches into, entered by construction whenever the trigger fires.** Two consequences, both wanted: `reached` is **per trigger frame**, which is correct because *"did the trigger reach this region"* is one fact for every site in the frame; and `GUARDED OUT` stays **per site**, since the site's own outcome counts separate the branch that ran from the branch that did not. Measured against the r4 case: `engram-surface-hook.py`'s three sites share `main`, so one `reached` covers all three while `:3006` reports `DELIVERING` and the two guarded ones report `GUARDED OUT` — precisely the distinction the fifth collapse asked for.
>
> **But `trigger_entry` MUST NOT BE A DECLARED FIELD, and round 7's version of it was unsatisfiable — this is the eighth round's correction and it removes the field rather than fixing it.** Round 7 made it a declared scalar symbol. Measured, four ways, all fatal at the nine server-side sites: (a) there is no *singular* dispatch target — `server.py` carries **58** `@mcp.tool`-decorated functions (AST count). My own first number was 68, from `grep -c '@mcp.tool('`, and **the reason I published for the gap was invented**: I wrote "seven are commented out". Measured: **zero** commented-out decorators exist. The ten extra lines are prose mentions — seven inside `#` comments and three inside f-strings at `:1489`, `:1493`, `:1498` — and `58 + 7` does not equal `68`, arithmetic that fails on its face and that I shipped without noticing. Both numbers were measured correctly; the *because* between them was reasoning nobody checked, including me; (b) the unconditionally-entered frame is the tool function, which lives in a **different file** from eight of the nine sites, while the field carried no path; (c) reading it as the same-module `_*_impl` reproduces round 6's blocker nine times over, since `server.py` guards those impl calls with early returns; (d) it is not even single-valued — `_add_observation_impl`, `_supersede_impl` and `_retract_impl` each have **three** callers spanning two files, against a scalar field.
>
> **The fix is to DERIVE it, so there is no field to be wrong.** The manifest declares `site` and `trigger` only. The §4a gate performs a **backward call-graph walk** from each declared site to the set of dispatch targets that reach it — a `@mcp.tool`-decorated function, or a `main()` invoked from `if __name__ == "__main__"` — and *that computed set* is where WS3 places the increments. Nothing is hand-maintained, so nothing can go stale, and the multi-caller case is handled by the set being a set. This is the same move as §4a's default-deny: an inconsistent state is made unrepresentable rather than checked for.
>
> **The walk still carries one accidental dependency, and the WRAPPER rule (above) is what makes it the walk's problem rather than the site's.** Once a thin wrapper is demoted from a site to a pass-through — as `_emit_hook_fire` (three files) is — the walk from each of its true callers has to cross the wrapper to reach the dispatch target, and it does so correctly only because the wrapper happens to be called on every exit path of its caller. Nothing in the manifest records that as a dependency of the walk. This is the same accidental fact the site-level `reached` producer above used to carry; the WRAPPER rule above does not remove it, it moves which consumer it is load-bearing for.
>
> **The bound, and it is the dangerous direction, so it is stated rather than implied.** The walk is decidable over **direct** calls and blind to dynamic dispatch. A missed caller makes `reached` **under**-count, and an under-counted `reached` reads as `NOT REACHED` — *"NOT a fault"* — which is this whole document's failure mode wearing the verdict set's own clothes.
>
> **Round 8's guard against that was undecidable in the direction it runs, and this is the ninth round's correction.** It said the gate fails CI on a site "whose path passes through a dynamic call it cannot resolve" — but a **backward** walk never encounters a dynamic caller *at all*: such a caller simply is not in the set, so there is nothing to pass through and the walk returns a quietly smaller answer. The zero-caller case is caught by the other disjunct; the **partial** case — one direct caller plus one dynamic one — was caught by neither, which is the exact under-count the clause claimed to prevent. §9's control inherited the hole: its "reachable only through a dynamic call" case reddens via the zero-target condition, so the dynamic clause was never exercised by the test written for it.
>
> **Two replacements, because the undecidable question splits into one decidable static question and one detectable runtime one.**
> ```
> STATIC  the gate scans ALL of src/engram/**.py -- NOT "the emitting
>         modules" -- for a dynamically dispatched CALL, WIDENED (ruled)
>         to three shapes: (a) an ast.Call whose own func is a call to
>         getattr(), (b) a callee that is ANY subscript expression
>         (dispatch-table lookups), (c) a two-step getattr -- a
>         getattr() result bound to a local, called on a later line. The
>         scope matters and round 9 got it wrong: a backward walk is
>         blinded by dynamic dispatch IN THE CALLER, which is by
>         definition not the emitting module, and "the four emitting
>         modules" also omits the 8 hook files holding 11 of the 21
>         sites. Whole-repo is also the only scope on which the question
>         is DECIDABLE.
>         Measured over all of src/engram/**.py: shape (a) returns ZERO;
>         the widened shapes (b)+(c) return FOUR --
>         engram_client.py:453, release_gates.py:795,
>         release_gates.py:797 (all shape (b)),
>         codex-umwelt-wake.py:511->515 (shape (c)).
>         RULING (settled -- no longer an open question): every hit is
>         either shown by the §4a backward walk not to reach any
>         declared site, or recorded as a declared dynamic-dispatch
>         exclusion in the manifest with a reason -- default-deny with a
>         declared exclusion set, the same discipline §4a already uses
>         for `telemetry: []`.
> RUNTIME every reached increment carries its dispatch-target id, and
>         an increment naming a target outside the walk's derived set
>         is an INCONSISTENT row rather than a silent adjustment.
>         SCOPE, STATED BECAUSE ROUND 9 OVERSOLD IT: this catches a
>         MISATTRIBUTED target, never a MISSING one. Take a site S with
>         derived(S)={T1} and a real, undeclared dynamic caller T2. In
>         a window where T2 runs and S's guard is false, S writes
>         nothing at all, R = 0, and the tuple is (0,0,0,0,0) --
>         NOT REACHED. No row exists to be inconsistent with.
> ```
> **The static half had to be specified as an AST shape rather than a token, and I found that by running it against my own gate before shipping it.** A grep for `getattr(` over the emitting modules returns **one** hit — `engram_observation.py:106`, `getattr(socket, "EAI_NODATA", -5)`, which fetches an **errno constant at import time** and is called by nothing. A token-level gate therefore fires on it and is unsatisfiable on day one, which is precisely the objection round 8 raised against round 7's version, reproduced inside the fix for it. The AST form — a call whose *callee* is a `getattr` result — separates dispatch from attribute lookup and returns zero. That is shape (a) alone; the widened scan above covers two further shapes.
>
> **So the two halves do not close the hole, and round 10 states the residual instead of claiming they do.** The static scan makes a *narrower* precondition checkable, and the widened scan changes what "narrower" measures: shape (a) alone returns zero; the full widened scan finds the four hits below, and CI passing on it means **zero unresolved matches** — every hit either walk-shown-unreachable or declared-excluded — not zero matches outright. That is NOT the same as "no dynamic dispatch", and an earlier draft claimed the latter of the narrow shape; claiming it of the widened shape would be the same error a second time. The runtime row makes *misattribution* visible. A dynamic caller introduced later, to a site whose guard is false in the same window, is caught by the static scan at CI time (once declared or shown unreachable) and by nothing at runtime otherwise. That is the honest boundary: **the guard is a precondition plus a detector, not a proof**, and it holds only while the static scan stays green. Loud and wrong is recoverable; quiet and wrong is what §0.1 is about.
>
> **The pattern is narrower than the property, and the gap is populated — corrected here rather than deferred.** Re-measured on this tip: shape (a) returns **0**, reproducibly. A *callee-is-any-subscript* shape returns **3** — `engram_client.py:453`, `release_gates.py:795` and `:797`, all dispatch tables — and `codex-umwelt-wake.py:511→515` dispatches through a `getattr` result bound to a local one line earlier, which no single-expression pattern catches. **Those four are live dynamic dispatch in this tree today, and the widened scan is the ruling on them, not an open question:** the scan widens to cover both shapes (STATIC, above), and every hit it finds is either shown by the §4a walk not to reach a declared site, or recorded as a declared dynamic-dispatch exclusion in the manifest with a reason — default-deny with a declared exclusion set, the same discipline §4a already uses for `telemetry: []`. This document does not itself dispose of the four measured hits; the mechanism that disposes of them is what is settled.
>
> **Cost is whatever the gate's walk computes; this document has been wrong about it twice already (§12.4) and does not offer a third guess.** The honest statement is that the instrumentation cost is *whatever that walk returns*, and it is not knowable from this document. It is bounded below by the number of hooks and above by 58 plus the hooks, and narrowing it further before the walk exists would be a fourth guess. §11.1's catch-and-drop obligation extends to every increment the walk places, since a raise at a dispatch target's first statement takes out the whole hook or tool call and breaches §8. `NOT WIRED` moves off this axis entirely: a declared site with no emit call is a **source** fact (see the static pair above), decidable without a window, which is why it no longer appears in the runtime list.
>
> **SCOPE — per window, and the window is named in the report.** Never bare per-session: the nine server-side sites live in a long-lived MCP process with no session binding (§0.1), so a per-session scope can never contain them and would mark all nine faulty forever.

---

### 2. Adopted from health-v2 without redesign

These are solved; adopting them is the point, and redesigning them would be the error.

| requirement | adopted shape |
|---|---|
| unified, analyzable store | `l0` raw records / `derived` projections split; declarations-as-data; a content fingerprint over declarations (`replay_decl_hash`'s analogue) |
| fail-loud liveness | the `derive_checks` shape — **a verdict row written on agreement *and* on disagreement**, so an empty table means "nothing ran", not "nothing was wrong" |

The design effort goes entirely into §4 (participation), because that is the only part health-v2's guarantee does not reach.

---

### 3. Store

**Transport is a per-process append-only JSONL file, not sqlite-at-write.** Each site's process writes its own `DATA_DIR/telemetry/<pid>-<disambiguator>.jsonl` — renamed to the same name with a `.done` suffix on clean process exit; a process that never exits cleanly (crash, kill) leaves a bare `.jsonl` file behind, which an age-based sweep reclaims once its content has been fully ingested and its mtime has gone stale. `DATA_DIR/telemetry/records.db` (sqlite) is the **aggregator's ingest target** — a projection built from the accumulated JSONL files, same posture as the health series store: derived tables rebuildable from `l0` plus declarations, so a projection change never requires re-collecting raw records.

Required fields at write, rejected if absent (§7): `ts_utc`, `manifest_id`, `event_type`.

**`session_id` is deliberately NOT required.** §0.1 records why: `server.py` runs as a long-lived MCP process serving multiple Claude sessions and has no single session to bind to, which is the stated reason it never initialises an emitter. Requiring `session_id` would make the nine server-side sites unable to emit *by schema* on top of being unable to emit by initialisation — closing the door this design exists to open. It is recorded when known and absent otherwise.

**Telemetry records are a stated exception to #2652's one-owner rule for `knowledge.db` — telemetry is not a client of the owner.** Neither store exists yet: `records.db` is not built, and this is a design document rather than code. Once built, `records.db` would be populated from the same population §0.1 measures — 21 call sites across every hook process plus the long-lived server — the many-writers-one-store shape #2652 exists to retire for `knowledge.db`; stating the exception now, before either store is built, is cheaper than discovering the conflict after. Two reasons, both load-bearing: telemetry records are derived and rebuildable from `l0` (above), so losing one is not losing knowledge the way losing an observation would be; and routing every hook's liveness write through `knowledge.db`'s owner would couple the fail-loud channel's own availability to the very thing it exists to report on — the same structural defect this whole document is about, one layer up. Each site's own JSONL file is written directly by that site, never through the #2652 owner; `records.db` itself is populated only by the aggregator's own ingest pass, never by a call site directly.

---

### 4. The registry — two halves, both checkable

**4a. Static membership.** A `telemetry` block on the mechanism entries in `src/build/packaging/tiers.json`, declaring **one record per call site** (per §1's key rule), not a single flag on the path. Each record carries **two** declared fields, and §1 gives each a job: `site` (the literal id string the call site carries as its `emit(..., site=...)` argument — the KEY axis) and `trigger` (the TIME axis). **The NEGATIVE-EVIDENCE axis's frame is DERIVED, not declared** — the gate's backward call-graph walk computes each site's dispatch-target set (§1), so nothing about it can go stale and the multi-caller case needs no schema. This field count has now been wrong in both directions: round 6 declared `enclosing` and checked only that the key was present (a decorative field); round 7 renamed it `trigger_entry` and made it a **scalar**, which is unsatisfiable at the nine server-side sites for four independent reasons §1 lists. Deriving it is what makes both failures unrepresentable rather than checked-for. Measured on dev before WS2 landed (and re-taken on `284debba`): 221 entries, exactly one `path` each, 0 carrying `telemetry` — so a bare path-keyed flag would leave `declared` per-file while `attested` is per-site, which is the fourth collapse re-entering through the manifest. `tests/test_tiers_manifest.py` gains one assertion: any entry whose path is a hook or tool that can emit **must** carry a `telemetry` block — either `telemetry: []` (declaring, explicitly, that it emits from nowhere) or one record per call site giving both fields above. Absent is a CI failure, exactly as an untiered path is today. An empty list is legitimate and silent; *omitting the key* is the failure. This is the growing-family discipline: default-deny with a declared exclusion set, not a hand-maintained allowlist. **`degraded_threshold` is NOT part of that per-entry block — this sentence previously read as though it were, and that ambiguity is what WS2's build had to resolve by judgment.** The one config scalar the VERDICTS list needs — `degraded_threshold` (§1's `thr`), default `0.5` — is **one shared cutoff**, and it lives in a top-level `telemetry` namespace in the same file, sibling to `mechanisms`. Per-entry would let each site declare its own cutoff, which makes the VERDICTS list **non-comparable across sites**: `DEGRADED` would mean a different thing on every row, the opposite of what a normative verdict list is for. Declaring it as data rather than a bare literal in a normative list is the #2773 precedent; declaring it **once** is what keeps the verdicts commensurable.

**4b. Runtime attestation.** Every emit and every liveness record (§5) carries the manifest id of its site — a literal the call already holds as its `site=` argument, declared, never derived from the runtime call frame. What a hook still shares with its siblings is the emitter import/bootstrap, not a per-site id resolver; that is what makes the eight duplicated bootstrap blocks disposable — a hook needs its manifest-id literal and the shared import, nothing else.

**The set difference in §1 is then a query, not an audit.**

---

### 5. Fail-loud is not raise (Kepler's rider 1) — and the signal is the RETURN VALUE, not an exception

**The invariant is that a telemetry failure must never disrupt the caller.** Stated that way rather than as "never blocks", because `engram-lesson-tripwire-hook.py` **does** block by design — `permissionDecision: "deny"` at `:904`, from #1944 — and that blocking is a deliberate feature unrelated to telemetry. (Its own header at `:33` still says "never blocks"; that line predates the deny path and is stale. Filed separately, not this design's to fix.)

**The except-branch is the wrong hook, and this is the correction that matters most.** `Emitter.emit` **never raises** — its own docstring says so, and `engram_log_emitter.py:145-153` catches everything, writes `[engram-emitter] emit(...) FAILED` to stderr, and returns `None`. So a `try/except` around an emit fires only on **import or init** failure, never on a failed emit. A design that keys liveness off the exception would stamp `ok=True` on every dropped event — reproducing §0.1's failure mode one layer up, inside the mechanism built to fix it.

**A row on every outcome, so a site whose every emit is dropped still accumulates counts** — which is precisely the state currently indistinguishable from a site that never ran. §1's decision list routes it — never to `GUARDED OUT`, which is the no-records case. §9 test 1's bound depends on that separation.

**The emitter already reports failure. Nobody reads it.** `emit` returns the event uuid, or `None` on failure; `emit_if_initialized` returns `None` when no emitter exists, having written nothing anywhere. Across the **20 consumer sites** in §0.1 no caller reads the return — 20, not 21, for the reason `:40` gives: `engram_log_emitter.py:260` *is* `return emitter.emit(...)`. **Round 10 corrected that sentence at `:40` and left this twin standing four sections away, in the same commit** — which is the fourth place this one carve-out has had to be made, and the reason §12 below now exists. So:

**But a boolean is the wrong shape, and this is the second time this design reached for a binary the substrate does not have.** `None` is returned for **two different reasons**: the emit was attempted and failed, or no emitter was ever initialised. Per §0.1 the second is the *permanent* state at the nine server-side sites, so `ok = uid is not None` would be a constant `false` there — filling the fail-loud surface with false failures, which is §0.1's pathology with the sign flipped, and leaving §1's runtime verdicts unable to name them correctly, because they *do* write rows — they belong in `UNWIRED-SILENT`, which is what `no_emitter` exists to reach.

**The outcome is three-valued, and the emitter already has the distinction — it discards it at `engram_log_emitter.py:258`:**

```python
DELIVERED    uid is not None            # emit succeeded
DROPPED      emitter existed, uid None  # emit attempted and failed  -> a real failure
NO_EMITTER   Emitter.get() is None      # never initialised          -> not wired, not broken
```

`emit_if_initialized` must return which of the three occurred rather than collapsing the last two into `None`; the call sites then record it. **`NO_EMITTER` is what §1's `UNWIRED-SILENT` verdict is for, and it must never be counted as a failure** — a site that is correctly declared and genuinely not yet wired is Phase-4 work, not an incident.

**Plus a fourth value, `INIT_FAILED`, written from the `try/except` retained around construction** — the genuine import/init case. It is not part of `emit_if_initialized`'s return, because a construction failure means there is no emitter to return anything; it is recorded by the call site's existing handler, and §1 gives it its own verdict rather than letting it fall into `GUARDED OUT`. This is the round-6 residual: the three-valued outcome is complete over what `emit` can *return* and silent about what construction can *raise*, and every hook-side site wraps both in one `try`.

This is the same move as the exception-versus-return correction one level deeper, and for the same reason the whole doc exists: **the signal already exists and is discarded**, so the fix is to read it rather than to add a channel. Two collapses, one file: `emit` collapses failure into `None`, and `emit_if_initialized` collapses not-wired into the same `None`. A third sits at the call sites rather than in the file: one bare `except` collapses construction failure into the same silence, which is what `INIT_FAILED` un-collapses.

Open sub-question, defaulted in §11: what happens when the liveness write itself fails.

---

### 6. Unprojected types become counted rows (rider 3)

The producer's validator stays **open** — an event type unknown to the consumer is still stored. What changes is the consumer: the *lack* of a projection for a stored type is written as a verdict row in the same store. The 3-of-6 case then reads as `unprojected: 3` rather than as three opaque blobs nobody notices. Projections are declared alongside the type; this rider adds only that non-declaration is itself recorded.

---

### 7. Required fields at write: UTC + attribution (rider 5, and Kepler's 09-05 comment)

`ts_utc` is stored as UTC with the zone in the record, and rendered as UTC. The originating incident is mine: symlink times read off `ls -la` in local time and published as `Z` across five forum posts and three issue comments, a four-hour shift nobody saw for ninety minutes. A telemetry store must make that unrepresentable at write and at print.

Attribution is the **manifest id** from §4b — strictly stronger than the free-text `hook_name` whose absence Aleph found, because it is the same key the registry checks.

---

### 8. Telemetry gates nothing T1 (rider 4)

No telemetry liveness check may block a write, a recall, or a consolidation. The fail-loud surface is *read* — by self-monitoring and by Lei — and never sits in a T1 path. This is a design constraint, not a review note: if a proposed check would sit in a T1 path, the check is wrong.

---

### 9. Tests that make the claims falsifiable

1. **The suppressor test.** Force an emit to **fail** at a wired site — which returns `None`, it does not raise — and assert a liveness row exists with outcome `DROPPED` **and** that the caller returned normally. A second case forces the no-emitter path and asserts `NO_EMITTER`, **not** `DROPPED`: conflating them is the round-2 defect and this test is what pins it. Mutation control: delete the `_liveness` call and assert *this* test reddens while the caller-not-disrupted test stays green. A test that forced an *exception* here would exercise a path `Emitter.emit` cannot take (§5).
2. **The participation test — static, and that is the round-6 correction.** Add a manifest site record whose literal id has no matching `emit(..., site=...)` call anywhere in `src/engram/**.py` and assert the §4a gate reports `NOT WIRED` (§1's `M - L`). **No window is run and no trigger is fired**: the earlier draft did fire the trigger, which made this test's stored observables identical to test 3b's while the two asserted contradictory verdicts — so one of them failed by construction, and the design was unfalsifiable exactly where it claimed to be falsifiable. **Bound:** this catches a site with no emit call in the source. It does **not** catch the §0.1 dark channel, where the site runs and every emit is dropped — that is test 1's `DROPPED` row and §1's `DARK`, and only because §5 keys on the return value. Both tests are needed; neither subsumes the other.
3. **The manifest gate test.** Add a hook path with no `telemetry` key; assert `test_tiers_manifest.py` fails.
3b. **The time-axis test — the one that would have caught the fifth and sixth collapses, and the two cases must assert DIFFERENT verdicts.** Declare a site behind a guard. **Case A:** run a window in which the site's derived dispatch target runs but the site's own guard is false; assert `reached > 0` and the verdict `GUARDED OUT`. **Case B:** run a window in which the trigger never fires, so that dispatch target never runs; assert `reached = 0` and the verdict `NOT REACHED`. An earlier draft asserted the *same* verdict for both, which is the tell that `reached` had no producer — the two windows were storing the same thing. **Round 7 note on why the frame matters to this test:** with round 6's *enclosing-function* wording, Case A had **no runnable instance** at `_emit_enactment`, whose body is only the emit — the case the test exists for could not be constructed there. Mutation control: delete the dispatch-target increment and assert case A flips to `NOT REACHED` while case B stays `NOT REACHED` — if case A does not move, the increment is decorative and the sixth collapse is back. **Second control, on the gate rather than the runtime, and round 8 had to rewrite it when the field became derived:** add a declared site that no dispatch target reaches, and assert the §4a gate **fails CI** rather than emitting an empty set; **and — this is round 11's correction — the discriminating case for the STATIC scan is a site with ONE DIRECT caller PLUS one dynamic caller.** Round 10's second case said "reachable only through a dynamic call", which reddens via the zero-target disjunct *and* via the static scan, so it discriminates nothing: it is verbatim the non-discriminating shape this document diagnoses two sections up, kept in the same commit that diagnosed it. With one direct caller the walk returns a NON-EMPTY set, the zero-target disjunct is silent, and only the static scan can fire — so that is the case, and it is the only control the static clause has ever had. It matters because `§1`'s entire residual argument rests on that clause. The round-7 version of this control pointed a *declared* `trigger_entry` at a non-dispatch-target; there is no longer a declaration to mis-point, so the control had to move to the walk.
4. **The unprojected test.** Store an event type with no declared projection; assert a verdict row, not a silent skip.
5. **The UTC test.** Write a record from a non-UTC local zone; assert the stored and rendered values are UTC.

Each carries a can-it-fail control. A control that has only ever been observed to fail is not yet a control: the discriminating case is the mutation that reddens **only** the target test.

---

### 10. Slicing and ownership

- **WS1** store + declarations + required fields (§3, §7)
- **WS2** manifest `telemetry` key + gate assertion (§4a)
- **WS3** resolver + attestation + liveness rows + narrowing each site's `try` to construction so `INIT_FAILED` is recordable (§5) + `reached` increments at the gate-derived dispatch targets; retire the eleven hook-side `pass` bodies (§4b, §5)
- **WS4** consumer projections + unprojected verdict rows (§6)
- **WS5** server-side emitter init + protection for the nine: `Emitter.init()` in `server.py`, plus two different shapes of fix — handlers at the seven `try:`/`finally:`-with-no-handler sites, and a new `try` wrapping each of the two no-try-at-all consumer sites (§0.1; `engram_log_emitter.py:260` stays excluded as the emitter's own internal call, not a consumer site) — §8-conformant throughout (§0.1, §11.1)

WS2 is independent and could land first. WS3 touches the **eleven hook-side sites** in §0.1 **plus the dispatch targets the §4a gate derives for them** (§1's negative-evidence producer), **plus a `try`-narrowing at each site** so construction failure is separable from ordinary failure — that last one is not tidying, it is what makes `INIT_FAILED` recordable at all, and it is required at the 6 of 11 hook-side sites that currently wrap non-construction work in the same `try`. **Do not size this half from this document.** Round 6 sized it as "18 enclosing functions" (wrong frame) and round 7 implied roughly one per hook (wrong, and unsatisfiable server-side); the count is whatever the gate's walk returns once §4a's records exist, and asserting it here a third time would be the same error a third time. It wants a single coder-fairy briefed from the gate's output — **not** `engram-loop-arm-stop-hook.py:337`, which is a path bootstrap wrapping no emit call and whose in-file comment at `:333-336` explains why converting it would be actively harmful.

**The gate now exists, so this stops being a hold and becomes an instruction.** WS2 ships `src/engram/tools/telemetry_manifest_gate.py`; `--report` prints every emit site's derived dispatch-target set alongside the dynamic-dispatch scan. Run it against the tip you are building on and take the count from its output. **The number is deliberately still not written here**, for the reason this section already gives — a figure in a document goes stale in silence and the gate does not. What *is* worth recording is **which kind of multiplicity each half carries**, because that is what makes a per-site count and a per-target count differ, and assuming one rule covers both halves is what both earlier sizings did. **The hook side has FAN-IN only:** every site resolves to exactly one target — its own file's `main` — and some targets absorb several sites, so there the site count strictly exceeds the target count. **The server side has BOTH fan-out and fan-in:** some sites resolve to two tool functions, and some tool functions are reached from two sites. Its site and target counts happen to coincide, and **that coincidence is cancellation, not a one-to-one mapping** — an earlier draft of this very paragraph called it a bijection and was wrong with the gate's own table on screen, which is why the instruction above is to read the report rather than any prose summary of it, this one included. The operational consequences split by KIND, not by workstream — and an earlier draft of this very sentence got that wrong in the same way the bijection claim did, by assigning both to WS5. **Fan-out is server-side only** (the hook side has none), so *`reached` for a site being the union over its targets* is WS5's alone. **Fan-in occurs on both sides**, so *one increment serving several sites* is WS3's as much as WS5's — and §1 already works exactly that case, on `engram-surface-hook.py`, whose three sites share one `main`.

The **nine** server-side `emit_if_initialized` sites are **WS5**, not folded into WS3. They are the **most** silent in the codebase rather than the least (§0.1), and seven of them sit in `try:`/`finally:` with no handler, so this pass must **add protection**, not merely attestation, to the one process every agent on the seat feels immediately if it fails. WS3 is already the heaviest workstream — eleven sites, every gate-derived dispatch target, a `try`-narrowing at each — and folding a protection-adding change to the MCP server into it would produce a workstream too wide to review as one thing, with the server half getting the thinner read; a separate workstream is the sequencing choice, not a principle, and could be folded back into WS3 if a future read prefers that. §0.1's *"this design is the Phase 4 that comment defers to"* is what WS5 makes true.

---

### 11. Questions for the design read (defaults stated; I proceed on these unless overruled)

1. **Liveness-write failure.** If the `_liveness` write itself raises, the site is silent again. Catch and drop, accepting that a store-level failure is invisible to the store — the alternative is an unbounded fallback chain. **This is NOT a default I am inviting overrule on: at the seven `try:`/`finally:` sites in §0.1 it is MANDATORY**, because those have no handler, so a raising `_liveness` propagates straight out of an MCP tool handler and breaches §8. The three no-try sites are the same case. So catch-and-drop is required at **9** of the 21 emit sites and merely sensible at the other **12 sites — which are 10 distinct frames, not 12.** Round 6 wrote "the twelve frames", using a site count as a frame count: the 11 handler-wrapped sites collapse into **9** frames (`engram-surface-hook.py`'s `main` holds three of them, and `engram-utility-credit-mention-stop.py` contributes two different functions), plus `emit_if_initialized`'s own frame. Re-measured on `6a2ed4d1` with an AST walk keyed on `(file, enclosing def)`. **Catch-and-drop is unconditionally required at every `reached` increment**, whatever the final trigger-entry count turns out to be: the increment is the dispatch target's first statement, so a raise there takes out the entire hook — a strictly wider blast radius than any emit site's, including all 10 frames above where protecting the emit was merely sensible. The tenth unprotected site, `engram_log_emitter.py:260`, is the implementation of `emit_if_initialized` rather than a call site inside an MCP tool frame, and §5 gives it nothing to record — an earlier draft counted it in and was wrong.

   **Where the drop is counted — two mechanisms, both ruled, because they fail in opposite directions and neither covers the other's hole.** (1) A per-process counter, incremented on every caught liveness-write failure, flushed to stderr with a distinct marker at process exit — this survives a total store failure, but assumes a reader. (2) The same accumulated count written as a row on the **next successful write to the same store** — self-healing, and it lands in the artifact the report already reads, but needs at least one write to land. Both ship. The specification is: count drops in-process; write the count as a row on the next successful write to the store; and flush the per-process count with the distinct stderr marker at exit. Together they keep "no silent lost write" true *inside* the #2652 exception (§3). Weakness, stated honestly: if the store never recovers within the window, nothing is written by mechanism (2) either. That is acceptable — a site whose store writes all fail for a whole window has no delivered rows either, so it already reads `DARK` or `NOT REACHED` on its own (§1), a true verdict rather than a missing one.
2. ~~**Manifest id shape.**~~ **SETTLED in §1, not a question:** per call site, never the entry's `path`. This item previously defaulted to `path` — the exact per-file key §1 names as the fourth collapse — and survived three rounds because it restated the definition instead of linking to it.
3. ~~**Session scope.**~~ **SETTLED in §1's WINDOW definition, not a question:** per named window (UTC calendar day), never bare per-session. A per-session scope cannot contain the nine server-side sites at all.
4. **Do tools attest, or only hooks?** Default: any manifest entry that *can* emit must declare, which is broader than hooks. §0.2's 26 hook-mentioning entries are a floor, not the population.
5. **Retiring the eight copied preambles** (§4b) is entailed by the resolver but is not required for the design to be correct. Default: separate PR, not folded into WS3.

---

*Provenance: my four position instalments on #2574 (comments 5621576403, 5622024661, 5622431784, 5622845940); Kepler's commander read and five riders (2026-09-10 19:36:48Z) and his 09-05 UTC/attribution comment. §0.1 and §0.2 re-taken on dev `7b68d385` while writing this. Aleph's 12/12 and 3-of-6 remain outstanding and are additions rather than gates.*

---

### 12. What ten review rounds actually found, recorded because it outlived the design

**Per-round blocker counts live in the PR body, not here** — a single place, checkable line by line against the posted verdicts. This paragraph used to restate them, and that restatement was wrong four times running, which is the section's own subject committed in its own opening. The design ideas were never the problem — no round touched the spine. Two things were:

**12.1 — Two formal objects were being maintained by prose review.** An 8-line predicate over a 5-tuple, and a call-graph reachability guard. Four rounds each, every fix breaking a neighbour. Both are decidable by machine in about a second, and once the decision procedure was run and its **output pasted into the document** (§1's `unmatched = 0/243` block, with a control), both closed and stayed closed. The tell, visible in the artifact the whole time: the only part of the guard that ever survived a review round was the part its author had actually executed. **A predicate list, a reachability set, or an exhaustiveness claim is a formal object; reading it carefully is the wrong instrument, however careful the reader.**

**12.2 — And the failure was never confined to those two, which is the part that took ten rounds to see.** A fact-asserting sentence gets updated **where the author is looking**, and its twin four sections away does not move. Two measured pairs, and **the second is worse than the single-commit version this paragraph first claimed**. Pair one is a single commit: `068415b0` corrected `:40` and left `:208` — same claim, same number, same document — untouched, by an author who had just written the correction. Pair two is **both shapes at once**, and an earlier draft of this sentence hid the stronger half by switching units mid-clause. The control text at `:255` entered in `59a8737e`; `f6e20789` then **diagnosed it as non-discriminating at `:133` and kept it at `:255` in that same commit** — pair one's shape exactly, and this section's thesis rather than a variant of it. It then survived `356c8a02` and `068415b0` unedited, and was caught by round 10, whose verdict signs head `068415b0`. *(An earlier draft said "caught at `88582bcb`" — the commit that **fixed** it, one later. A fresh commit-identity error inside the fold correcting a commit-identity error, in the same sentence.)* So the honest reading is not "one commit versus three" but **one diagnosis-and-keep within a single commit, plus three further commits of survival** — and the first draft's "both pairs in a single commit" was false in the direction that *understated* its own case, which is the direction that gets published. **Nothing brute-forces this.** It is not an exhaustiveness question and no decision procedure reaches it.

**12.3 — §0's rule, stated at the width the evidence carries.** Of the numeric claims outside §0, the failures were **identity, population and criterion — not one was arithmetic**. Naming a producing command fixes *reproducibility*: it tells the next reader how a number was made and lets them re-run it. It fixes **none of those three**, because a command that runs correctly over the wrong population returns a true number about the wrong thing and says so nowhere. So: **a number without its producing command is unaudited by construction; a number WITH one is merely reproducible, and still needs its population named beside it.** A claim whose twin nobody enumerated is unaudited too, and no command reaches that at all.

*(This paragraph previously closed on a statistic — "every claim that named its command held" — which covered about three claims while roughly twenty of the twenty-two naming none also held. A predicate satisfied by nearly every member of both groups separates neither, and it sat in the closing line of the section recording what the review taught. It is removed rather than repaired: the rule above stands on the failure taxonomy, which does discriminate.)*

---

**12.4 — SPECIFIC round-by-round history relocated out of §1's definition block.** **Most** of the entries below moved; the breakdown is 6 clean relocations; 1 genuine split — the OUTCOME dispatch-target entry, the only one leaving substantive §1 residue; and 1 authored here with no corresponding §1 deletion, the wrong-count tally. An earlier draft carried that qualifier and a later cut removed it, restoring a universal this section had already corrected. **§1 still cites rounds** — where a definition's shape turns on what a round found, the citation is part of the normative text — so this is the relocation of particular history, not a separation of history from normative text. An earlier version of this line implied the latter and was false: §1 carries more `round N` references than this section does.

**On THE PARTICIPATION DEFINITION block itself.** Four rounds of review produced five blockers of the form *"updated one definition site, missed the others"*, because this was previously spread across six places in three sections. One block is the fix; a second copy is the defect.

**And the fix was not sufficient on its own — round 5 found the sixth residual INSIDE this block.** Consolidating helped: four of round 4's six blockers closed and no section now contradicts what is written here. But `DECLARED` was still defined here as *"sites with `telemetry: true` in the manifest"* — a per-path boolean that §4a deleted **in the same commit** — and since every verdict is defined over `DECLARED`, all of them silently reverted to per-file. **The lesson is narrower than "consolidate":** a definition block and its *declaration surface* are still two places, so one block does not make one place. What binds them is that §4a states the record's fields and this block states what each field is **for**, with neither restating the other.

**Round 7 then found a third form of the same failure, and it is the one a rename sweep cannot catch.** Round 7 renamed a field and swept the document for the old and new *words*; the `OUTCOME` line contained neither, and kept saying `reached` was per-**site** and kept omitting `init_failed` — a sentence made false by a change whose vocabulary it does not share. **So sweep the SEMANTICS, not the token.** Enumerate every sentence that asserts something about the changed concept, however it is worded, and re-read each against the change.

**On the OUTCOME axis's dispatch-target count.** Round 8's review said four; my first walk said six — both wrong, which is why §1 now names the three sites instead of stating a total.

**On the VERDICTS list being written IN FULL.** Round 7 claimed exactly that and did not do it — five of eight lines omitted `reached > 0` and `init_failed = 0`, and round 7 had closed the previous round's blocker *by placing a line third*, so the sentence and the list contradicted each other and the sentence was the false half. Read as independent rules, `(reached>0, 0,0,0, init_failed>0)` matched the `GUARDED OUT` line and reported "NOT a fault" — the blocker re-entering through the prose written to close it.

**On `PARTIAL` being a flag rather than a verdict line.** Round 5 raised `(delivered > 0, no_emitter > 0)` as an unaddressed case and round 7's first draft answered it with a decision-list line reading *"rate below still applies"*, which is false in a **first-match-wins** list: the line would have consumed the row and the rate would never have run.

**On the instrumentation cost.** Round 6 said "18 enclosing functions in addition to the 21 call sites", counting innermost `def`s (wrong frame). Round 7 implied roughly one dispatch target per hook, which is right for hooks and unsatisfiable server-side. Both were guesses at a number that the gate's walk **computes** — this document has now asserted it wrongly twice, and does not offer a third.

**On the wrong-count tally itself, corrected here because the correction was not logged when it happened.** A prior draft of this entry, and its twin above, asserted this document *"has now asserted it wrongly THREE times"*; the fold that produced this section's current wording silently tightened that to *twice*, with neither commit recording the change. Twice is the number this document's own enumeration supports — Round 6 and Round 7, named above, are the only two wrong guesses at the instrumentation cost anywhere in this document, before or after this round. (Scope: the instrumentation cost here means the WS3 dispatch-target sizing question of §1 and §10 — a different metric from §11.1's "twelve frames" miscount, which is catch-and-drop protection sizing, already self-corrected in place, and not counted in this tally.) The corrected number is right; per §12.3, the failure class is the unaudited delta, not the specific number, so it is logged here rather than left to stand uncredited.

**12.5 — A convention, arising from the previous commit's de-leak fix: a normative sentence must carry its own warrant, not a pointer to where the warrant was decided.** "Settled" is fine; "settled in `<label>`" is not, because the second form makes the document depend on an artifact it does not ship with. A leaked *node ID* is only conditionally silent — whether a reader gets a 404 or silently re-binds to a real unrelated node depends on whose graph is larger, which is nobody's control. A leaked *review label* is uniformly silent: it resolves to visible nonsense in no namespace, so there is no configuration in which a reader discovers the reference is unresolvable. `RULING (<reviewer-label>, settled)` reads as authoritative and asks a reader to defer to a ruling they cannot locate — which is exactly the shape the previous commit removed.
