# #4651 part A: the interface (v1, Sol)

Status: a design note, kept in step with the code. It began as the interface Mira's contract (rev 2, hash 46798986)
asked part A to declare before any part B code is dispatched: the routing flag, the callable API and result contract, and
a durable branch and base. Sections 2 and 4 describe the shared primitive and the owner op as they shipped (slices A1 and
A2); section 3 is the client route (slice A3), whose contract is pinned by `tests/test_4651_a3_client_route.py`. Everything
owner-side is subject to Zephyr's security read; where a point is still open it says so.

Branch: `sol/4651-a-shared-primitive`. Base: dev `ec8f2483`. Part A is one PR (Kepler's split, 30112); the client route
is its third slice.

Rulings this builds to:
- Borges 30125/30127/30149: flag OFF runs the shared strict primitive in-process; flag ON runs it through the owner;
  the owner unavailable or refusing means stop, never fall back; the route is fixed for a run.
- Kepler 30138: a NEW per-unit flag, default OFF, named for the overwrite it permits; the owner refuses the op when the
  flag is off with a refusal that names the flag; the flag's ledger row lands in the same PR; the repair tool's own
  apply step stays on top (the flag permits, the apply decides).

## 1. The flag

`engramd.repair_embeddings_via_owner` in config.json (JSON `true` only, read with `engramd_owner_flags.flag_on`; the
client route reads it through `_owner_read.flag_applies`, which calls the same function). Default OFF. The name ends in `_via_owner` so `any_owner_flag_on` starts an owner for it. It is a NEW flag,
independent of `regen_embeddings_via_owner` (Kepler 30156). It is a ROUTING switch, not a consent: consent to overwrite
vectors lives in the repair tool's preview-then-apply, the same in both routes (Kepler 30156, Borges 30160).
The name is decided: `repair_embeddings_via_owner`, in the style of `regen_embeddings_via_owner`. An earlier draft
named it for the overwrite it permits, under the consent reading, which was withdrawn. The two flags sit side by
side and route different writes: `regen_embeddings_via_owner` the writes that FILL vectors that are missing,
`repair_embeddings_via_owner` the one-time repair that REWRITES vectors that already exist.

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
    publish_batch(route, items, request_id=None) -> list[PublishResult]

Module `tools/_embedding_repair_route.py` (Essential tier, beside `_owner_read.py`).

- `items`: a list of `(node_id, PreparedVector)` pairs, 1 to `MAX_BATCH` = 50, all carrying an equal epoch. The 50 is
  U16's cap, kept as a starting point and not a claim; a test pins it equal to the owner's `PUBLISH_PREPARED_MAX_ITEMS`.
  A bad batch (empty, over the cap, a non-pair, a repeated node_id, a non-`PreparedVector`, mixed epochs) is a
  `ValueError` before the flag is read or anything is opened.
- `request_id` (optional): the idempotency key sent with the owner op; a fresh uuid when absent. Part B passes the same
  id to retry the same batch, which the owner replays instead of re-writing.
- `publish_batch` re-reads the flag before EACH batch. If it no longer matches the route (either direction; an
  unreadable flag reads OFF), it raises `RouteChanged`; if the owner answers `flag_off`, it raises `OwnerFlagOff`. Part B
  stops loudly with the rest pending; a rerun takes a fresh preview. It never switches route mid-run.
- Route "direct": one connection to the store per batch, then per row in request order `BEGIN IMMEDIATE`,
  `publish_embedding` in prepared mode, `COMMIT` (one transaction per row). An error mid-batch rolls that row back and
  stops; `DirectWriteError` carries the rows already committed (`.results`) and the cause.
- Route "owner": sends the owner op below through `_owner_read.write`. The answer is checked strictly (one entry per
  item sent, in order, valid fields, `counts` equal to the tally); a malformed answer is INDETERMINATE, because the owner
  may have committed. Owner down, refusing, malformed or indeterminate raises `OwnerUnavailable` (with `.reason`,
  `.commit` and `.request_id`) and never touches the store directly. `PublishResult.warning` is None on this route.
  `flag_off` is told apart from the other refusals by the `owner_reason` attribute `_owner_read.write` sets on what it
  raises, never by message text.

Part B never reads the flag itself, never opens the store to publish, never reimplements validation or the debt-clearer.

One difference between the routes, deliberate: the direct route commits one row at a time; the owner op publishes the
whole call in ONE transaction. The per-row `PublishResult`s are the same either way (a parity cell pins it).

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
- Flag gate at the top of the handler: read the flag fresh with `flag_on` on the owner's own config path; OFF, or any
  unreadable state, answers `{status: rejected, reason: flag_off, detail naming engramd.repair_embeddings_via_owner,
  remedy}` before the params are validated and before the store is touched. The owner did not read per-unit flags before
  this op, so this is new owner behaviour in this PR.
- The flag is read PER CALL, never latched at owner start: a mid-run flip stops the next batch loudly. The guarantee is
  per call, not per statement: a flag turned OFF while a call runs does not abort that call.
- The op runs `publish_embedding` in prepared mode per item, in request order, in one transaction. Besides the flag
  gate it has a vec gate (a store with a `vec_nodes` table whose vec0 this connection cannot load is refused whole as
  `vec_unavailable`, nothing written) and a read-back after COMMIT of every `refreshed` item's embedding text and vec
  bytes (a difference answers `readback_mismatch`, `commit: committed_unverified`, recorded so a same-id retry replays
  it). Per-item outcomes come back as `{node_id, status, reason, vector_changed, debt_cleared}` plus `counts`; `warning`
  is not carried, and `vector_changed` is null unless the item is `refreshed`.

## 5. Not decided here

The writer-hold measurement of a 50-item publish: the cap of 50 stands as a starting point, not a claim, and a
measurement may change it (in both places at once, a test pins them equal). Decided since the first draft: the flag name
(`repair_embeddings_via_owner`), the batch cap (50), the refusal wording (the handler's own), per-call flag reads, and
the op running through the shared owner write scaffold. Nothing in this note changes the NULL-only fill, the
summary-write defaults, or any health or confidence calculation.
