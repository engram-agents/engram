# #4651 part A: declared interface (v0, Sol)

Status: a DECLARATION, not an implementation. It is the interface Mira's contract (rev 2, hash 46798986) asks part A to
declare before any part B code is dispatched: the routing flag, the callable API and result contract, and a durable
branch and base. Everything here is subject to Zephyr's security read of the owner-side pieces; where a point is
still open it says so.

Branch: `sol/4651-a-shared-primitive`. Base: dev `ec8f2483`. Part A is one PR (Kepler's split, 30112). No code yet.

Rulings this builds to:
- Borges 30125/30127/30149: flag OFF runs the shared strict primitive in-process; flag ON runs it through the owner;
  the owner unavailable or refusing means stop, never fall back; the route is fixed for a run.
- Kepler 30138: a NEW per-unit flag, default OFF, named for the overwrite it permits; the owner refuses the op when the
  flag is off with a refusal that names the flag; the flag's ledger row lands in the same PR; the repair tool's own
  apply step stays on top (the flag permits, the apply decides).

## 1. The flag

`engramd.overwrite_embeddings_via_owner` in config.json (JSON `true` only, read with `engramd_owner_flags.flag_on`).
Default OFF. The name ends in `_via_owner` so `any_owner_flag_on` starts an owner for it. It is a NEW flag,
independent of `regen_embeddings_via_owner` (Kepler 30156). It is a ROUTING switch, not a consent: consent to overwrite
vectors lives in the repair tool's preview-then-apply, the same in both routes (Kepler 30156, Borges 30160).
Open, one line from Kepler or Borges: the name. `overwrite_embeddings_via_owner` was chosen under the consent reading,
which was withdrawn; a unit name in the style of `regen_embeddings_via_owner`, for example
`repair_embeddings_via_owner`, may fit the convention better. A rename before the PR opens is free.

## 2. The shared primitive (module `engram_embedding_publish`, Essential tier)

One function, two entry modes, identical verification in both contexts (in-process and owner):

    publish_embedding(conn, node_id, *, prepared: PreparedVector | None = None) -> PublishResult

- Compute mode (`prepared=None`): today's strict summary-write refresh, unchanged. Existing compute-mode callers and
  their tests do not move.
- Prepared mode: the vector arrives already encoded. It NEVER calls the embedder or its availability probe. Encoding is
  the only thing it skips.

`PreparedVector`: `vector` (exactly 384 finite non-bool numbers), `text_sha256` (sha256 hex of the exact UTF-8 text it
was encoded from, computed by part B with the same function at encode time), `epoch` (model id, dimension,
normalisation it was encoded under).

Prepared-mode checks, in this order, inside the write transaction; the first failure defers with NOTHING written and
the old vectors and any debt marker intact:
1. the row exists and `is_current = 1`, else `not_current`;
2. the current text from `_embedding_text_for_node` is non-empty, else `empty_text` (an empty current text never matches
   a hash);
3. sha256 of the current text equals `text_sha256`, else `recipe_changed` (the primitive never accepts caller text);
4. the epoch fields equal the current ones, compared as separate fields, else `epoch_changed`;
5. the vector is 384 finite numbers within the float32 range, with a non-zero norm whose float64 square does not exceed
   the float32 maximum (an all-zero vector and a vector that overflows float32 arithmetic have no KNN distance), else
   `invalid_vector`;
6. the metadata is SQL NULL, absent, or a JSON object, else `malformed_metadata` (compute mode keeps its legacy
   behaviour; prepared mode never silently normalises).

An exception raised inside any of these checks (a corrupt config, a failing text read) does not escape: that item
defers as `check_failed`, nothing written. The caller may pass the current epoch it read once for the whole batch.

Then publish: write both vector stores, verify the complete canonical JSON and the expected sqlite-vec mirror bytes,
and clear the debt marker only after that verification. A failure here restores both old stores and returns `pending`
with a reason (`mirror`, `canonical`, `store`, `embedding`) and sets the debt marker, exactly as compute mode does. One
debt-clearer: the existing one. A no-marker success performs no metadata UPDATE.

`PublishResult`: `node_id`, `status` (`refreshed` | `pending` | `deferred`), `reason` (or None), `vector_changed`
(bytes before and after, only when `refreshed`), `debt_cleared` (a marker existed and this call removed it), `warning`.
Part B's receipts map one to one: attempted, verified-refreshed, byte-changed (`vector_changed`), unchanged-but-verified,
deferred (with its cause), pending, and indeterminate (no result at all, never guessed).

Implementation note for review: the store step inside `_compute_and_store_embedding` (the JSON UPDATE and the vec
delete-and-insert) is extracted into one core function that both modes call, so prepared mode cannot drift from the
canonical write. The compute path's behaviour is unchanged and pinned by the existing cells. Borges is asked to confirm
this extraction is acceptable semantics.

## 3. What part B calls (and nothing else)

    open_route(engram_home) -> Route            # reads the flag once; "direct" or "owner"; fixed for the run
    publish_batch(route, items) -> list[PublishResult]

- `items`: a list of `{node_id, PreparedVector}`; the batch cap is set after the writer-hold measurement (U16's 50 is
  the starting point, not a claim).
- `publish_batch` re-reads the flag before EACH batch. If it changed in either direction, or the owner answers
  `flag_off`, it raises `RouteChanged` / `OwnerFlagOff` and part B stops loudly with the rest pending; a rerun takes a
  fresh preview. It never switches route mid-run.
- Route "direct": calls `publish_embedding` in-process, one transaction per row.
- Route "owner": sends the owner op below. Owner down, refusing, malformed answer or indeterminate outcome raises
  `OwnerUnavailable` and never touches the store directly.

Part B never reads the flag itself, never opens the store to publish, never reimplements validation or the debt-clearer.

### 3a. One OFF/ON behaviour (SETTLED: Kepler 30156 withdrew the consent reading of 30138; Borges 30160)

Mira (30153) found two rulings that read two ways: Borges 30127 (flag OFF means the install's configured in-process
writer) and Kepler 30138 (the flag as finer consent). Kepler withdrew the consent reading in 30156: ONE contract,
Borges's. The flag is a ROUTING switch; it is not a gate on the in-process writer. The consent to overwrite, in every
route, is the repair tool's explicit preview-then-apply (a user yes for shipped users). The owner still refuses an op
that arrives while the flag is OFF (Borges 30160), as route integrity and not as consent: with the flag OFF the tool never
calls the owner, so such a call means the caller's route is stale.

| flag | owner | what happens |
|---|---|---|
| OFF | absent, running or down | explicit apply, then the shared primitive runs in-process; the owner is not contacted |
| OFF | receives the op anyway (a stale client) | refuses `flag_off`, naming the flag; writes nothing |
| ON | available | the op runs through the owner |
| ON | down, refusing, malformed answer, or outcome indeterminate | stop loudly, remaining rows pending, never an in-process fallback |
| flipped in either direction mid-run, or owner answers `flag_off` | any | stop loudly, remaining rows pending; a rerun takes a fresh preview |

Why not "OFF refuses everywhere": an Essential install with every owner flag off has no owner, and Essential installs
can need this repair (Mira's contract, T1). A global refusal would leave them with no supported route. The cost of the
proposal: with OFF the in-process write is the same direct write every non-migrated path makes today; the flag does not
add protection there, and this note does not claim it does.

## 4. Owner side (the wire and authorizer plumbing; Sol and Zephyr)

- A new write method `publish_prepared_embeddings` in the owner's closed write set, with the kind table entry, a
  closed-key parameter validator over ALL items before any write, the envelope's request_id as idempotency key, and its
  own column-exact authorizer allowlist MEASURED for this op (it reads six `nodes` columns for the recipe and updates
  `nodes.embedding`), not copied from the NULL-only fill op.
- Flag gate at the top of the handler: read the flag fresh with `flag_on` on the owner's own config path; OFF answers
  `{status: rejected, reason: flag_off, detail naming engramd.overwrite_embeddings_via_owner, remedy}`. The owner does
  not read the per-unit flags today, so this is new owner behaviour in this PR.
- Open for Zephyr: a per-call config read, or a flag latched at owner start. Per-call matches the fresh-read precedent
  and makes a mid-run flip stop the next batch loudly; latching would not.
- The op runs `publish_embedding` in prepared mode per item; per-item outcomes are returned as `PublishResult` data.

## 5. Not decided here

The batch cap (needs the writer-hold measurement); the exact refusal wording; whether the new owner op shares the
fill op's lock-wait budget; the flag name (candidate). Nothing in this note changes the NULL-only fill, the summary-write
defaults, or any health or confidence calculation.
