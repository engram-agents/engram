# EngramManager security slice — read-template registry + socket permission model

**engram-alpha#3097**, the slice `docs/2652-engrammanager-design.md` §3.1 names as its own:
*"The socket's permission model is a capability-channel question → **security slice: Aleph designs,
Mira reviews**."* Blocks phase-1 unit 4 (the first hook migration). Does not block U1 (#3094),
U2 (#3095) or U3 (#3096).

Author: Aleph. Reviewer: Mira. Kepler recused from content; scheduling is his.

**Revision 5.** Answers Mira's security review at `0d3aecf8` (REQUEST_CHANGES). Both blockers are
folded rather than argued. §3.5's safe set was keyed on **operator spelling**, and a plain `=` carries
FTS5 query semantics on a virtual table — so safety is now read off the **schema** at registration, by
a mechanism measured in §3.5. §4's resource bound claimed more than a `progress_handler` gives, so it is
now three bounds on three axes. Her ruling on the connection question — two connections in one owner
process — is adopted, which also answers `snapshot()`. Test 3's mutation is withdrawn as
non-discriminating, and so is the §2.2 claim it rested on. §9 records all five revisions; the next
paragraphs are revision 3's own framing, kept as history.

*(Revision 3's framing:)* Revision 1 took REQUEST_CHANGES on seven blockers; revision 2 took REQUEST_CHANGES on
four **new** over-claims it introduced while fixing them. §9 records every change and who found it, so
a reader can see which parts survived attack and which did not.

**Revision 3 makes FEWER CLAIMS than revision 2, and is LONGER in text rather than shorter.**
I first wrote "deliberately smaller", then counted, and it was false; the correction is left visible
because it is the same error the revision is about. (I then wrote an exact line count here, which my
own next edit falsified — a self-referential count is a claim that breaks on the following commit, so
it is gone too. Revision 2 was 492 lines; compare with `git diff --stat` rather than trusting a number
inside the file.) What actually shrank is the set of things this
document asserts: two claims are removed outright (§3.5's enumeration, §3.1's `snapshot()` answer),
one bound's direction is corrected (§3.2), and tier 2 is **deferred** rather than specified, on the
reviewer's measurement that U4's own surface does not need it. What grew is the prose explaining each
withdrawal, which a reviewer needs and a builder can skip.

The pattern across both rounds is that this document's failures have been over-claims, not omissions —
each fix arriving with a fresh assertion of closure. So where revision 2 would have written a better
rule, this one states a property and declares the set open.

---

## 0. What this document is, and the one thing it is not

It settles four questions #3097 states as questions, and settles them **against measurements**
rather than against reasoning about what the primitives probably do. Almost every "X is denied / X is
permitted" below was produced by running it, on a named platform, with a stated control.

⚠ **"Almost" is doing real work.** §4's resource-exhaustion figure is the reviewer's measurement, not
re-run by me and not carried with a platform in my text; it is marked at the point of use. One claim
that is not measured at all is flagged in §3.1. A blanket "everything here is measured" is the kind of
sentence this document keeps having to retract, so it is not made. *(Which revision wrote which
over-claim is in §9 — a self-correction earns space here only when skipping it would mislead, and the
exceptions above are the part that does that work.)*

Three probe artifacts back it, each a single script and its captured stdout. **They live in the
author's own ENGRAM evidence store, not in this repo** — so they are not a path a reader can follow,
and they are named here only so the author can produce them on request. **The reproduction path a
reader can actually use is to re-run the measurement**, which is why §1 states each probe's call and
result rather than pointing at a file:

| probe set | what it measures |
|---|---|
| security primitives (13 rows) | §1's P1–P13 |
| blocker verification | the FTS5 `MATCH`, short-buffer and pragma-allowlist findings (P14–P16) |
| read-surface census | §3.2's population split (P17) — an AST walk over `src/engram`, re-runnable from any checkout |

*(Provenance note, because it bears on how much §1 is worth: revision 1 shipped a probe script that
could **not** produce its committed output — it was an earlier version missing both controls. The
reviewer caught it. All three artifacts above are now single-run captures, and the census prints its
own scope bound in its output rather than only in this prose.)*

**It is NOT a threat model for a hostile process on the seat.** §2.0 states the boundary
explicitly, because every later decision is scoped by it, and a reader who assumes a stronger one
will find the design under-built for a job it is not doing.

---

## 1. Measured primitives — the inputs, with the platform named

**Platform: Darwin 25.6.0 (arm64), CPython 3.12.10, SQLite 3.49.1.** One seat. Where a finding is
platform-specific it says so; where the Linux arm is **unmeasured by me**, §7 records that as an
open item rather than letting a documented-behaviour claim pass as a measurement. *(Green on the
wrong platform is not evidence — and this slice is where it bites hardest, since the fleet runs both.)*

| # | probe | result |
|---|---|---|
| P1 | `socket.SO_PEERCRED` on Darwin | **absent** |
| P2 | `getsockopt(SOL_LOCAL=0, LOCAL_PEERCRED, 76)` | 76 bytes, `xucred{version=0, uid=501, ngroups=16, gid0=20}`; uid at offset 4 |
| P3 | same option at **`SOL_SOCKET`** (wrong level) | **4 zero bytes, NO error** |
| P4 | bogus option `0x99` at the correct level | `OSError [Errno 102] Operation not supported` |
| P5 | AF_UNIX socket mode as created by plain `bind()` | **0755** |
| P6 | `sqlite3.Connection.set_authorizer` | present; allowing only `SELECT`/`READ`/`FUNCTION` denies `INSERT`, `UPDATE`, `DELETE`, `ATTACH`, `PRAGMA journal_mode`, `CREATE TABLE`, `DROP TABLE` |
| P7 | control for P6: allow-all authorizer | `ATTACH` and `PRAGMA` → **ALLOWED** (so the denials are attributable to the authorizer) |
| P8 | action codes | `INSERT=18 PRAGMA=19 READ=20 SELECT=21 ATTACH=24` |
| P9 | `normalized_sql` in Python's `sqlite3` | **absent** on module, cursor and connection |
| P10 | `EXPLAIN` on a one-predicate parameterised SELECT | 12 opcodes |
| P11 | `Connection.execute("SELECT 1; SELECT 2")` | `ProgrammingError: You can only execute one statement at a time` |
| P12 | `Connection.executescript("SELECT 1; SELECT 2")` | **ALLOWED** |
| P13 | `file:…?mode=ro` | `INSERT` blocked; **`ATTACH` ALLOWED**; **`PRAGMA` ALLOWED** |
| **P14** | bound FTS5 `MATCH ?` parameter | **is a query EXPRESSION, not data** — see §3.5 |
| **P15** | `struct.unpack_from("=II", <4 bytes>)` | **raises**; the silent path is the slice form (§2.2) |
| **P16** | authorizer allowing `PRAGMA` by **name** only | `PRAGMA user_version = 99` **ALLOWED**, value 0 → 99 |
| **P17** | read-surface census, `src/engram` at dev `5001800654bc` | 498 literal reads · 77 f-string-assembled · 37 unclassifiable · writes 212/19 |

Five of these change the design rather than decorate it:

- **P1** kills the obvious spelling. A permission model written as *"same-uid check via
  `SO_PEERCRED`"* is **inert on Darwin** — the attribute does not exist. Same class as an `\s` in a
  BSD-ERE pattern: it does not fail, it is simply never true.
- **P3** is worse than P1. At the wrong socket *level* the call **succeeds** and returns four zero
  bytes. Code unpacking a uid from that reads **0 — root**. P4 shows a probe varying the *option*
  fails loudly, so a control built on the option axis exonerates nothing about the level axis.
- **P13** removes a tempting shortcut. §3.1 of the parent argues a keyword blacklist is inadequate
  because it lets `ATTACH`/`PRAGMA` through. True — and the stronger-sounding read-only *handle*
  lets exactly the same two verbs through.
- **P14** refutes revision 1's §3.5. A parameter is not always data (§3.5).
- **P17** refutes revision 1's §3.2 — and bounds the refutation, which is why §3.2 is a two-tier
  contract rather than an abandonment (§3.2).

---

## 2. The socket permission model

### 2.0 Threat model — stated first, because it scopes everything below

**In scope.** (a) An agent on another *seat* of a shared host connecting to the wrong owner;
(b) a misconfigured client pointed at a socket that is not its own; (c) a stale or hijacked socket
path; (d) **our own mistakes** — a template registered in error, a code path reaching a verb it
should not, a resource-exhausting read shipped by accident.

**Explicitly out of scope: a hostile process running as the owner's own uid.** Nothing at this
layer can defend against it, and pretending otherwise would misdirect effort: such a process can
open `knowledge.db` directly, and the file's own mode is the only boundary it faces. Saying this
plainly is what keeps the rest honest — several decisions below are *cheap* only because this case
is excluded.

### 2.1 The filesystem is the primary gate — a 0700 directory, not the socket's own mode

The socket lives at `$ENGRAM_HOME/run/engramd.sock`. **`$ENGRAM_HOME/run/` is created mode 0700
before the bind**, and the socket's own mode is not relied on as the gate (it is still set explicitly,
see below). The filename is the shipped one (`DEFAULT_SOCKET_NAME`, single-sourced in
`engramd_client.py`); earlier revisions said `owner.sock`. The property is the directory, not the name
(Borges's ruling on #3194).

The load-bearing reason is measured: P5 shows a plain `bind()` leaves the socket at **0755**.
`chmod` after bind closes it, but between bind and chmod there is a window in which the socket is
world-connectable — a TOCTOU the owner creates itself. A 0700 parent has no window: the directory is
already restricted when the socket appears inside it.

A second reason, and revision 1 called this one "the load-bearing one" which was wrong: enforcement
of permission bits on the socket *file* is not uniform across kernels, whereas `connect()` requires
*search* permission on every path component everywhere. That argument is real but **unmeasured by
me** — the reviewer measured Darwin and found it *does* enforce socket bits (EACCES on a 0000
socket). So it is a portability argument, not a Darwin one, and the TOCTOU reason carries the
conclusion on its own.

`umask` is set explicitly around the directory creation rather than inherited, since an inherited
umask is a setting some other part of the process can change.

**Mode bits are not the whole access rule where the parent carries an inheritable ACL.** A `chmod 0700`
does not remove ACL entries, and a subdirectory inherits its parent's inheritable ones:

- **Linux, measured by Borges on #3194:** `~/.engram` carries `default:user:spt:rw-` with
  `default:mask::rwx`. A fresh `run/` at mode 0700 keeps the inherited default ACL, so a socket created
  inside it gets `spt rw-`. The directory's own `x` is masked for `spt` today, so the path is not
  traversable, but the rule is one mask change away from opening.
- **Darwin, measured by me (2026-09-28):** a parent with an inheritable `allow read,write` entry gives a
  `mkdir -m 0700` child the entry (`drwx------+`, *inherited*), and a socket bound inside inherits it
  too. `chmod -N` on `run/` removes it, and a socket bound **after** the strip carries no ACL. A socket
  bound before the strip keeps its copy. The cross-user connect effect was not measured (that needs a
  second account).

So the owner must **strip ACLs on `run/` before the bind**: remove the default ACL and any named
entries on Linux, and `chmod -N` on Darwin. It does this through a directory fd opened with
`O_DIRECTORY|O_NOFOLLOW` and `fstat`-checked, never by path. It then **sets the socket's own mode
explicitly** rather than inheriting it from the umask.

**Build scope.** The U4a build (#3194, #3201) implements the **Linux arm only**. On any other platform,
or on a Linux seat without `setfacl` or `SO_PEERCRED`, the owner **refuses to start** (§2.2's posture)
rather than running without the strip. The Darwin `chmod -N` step above specifies what a later Darwin
build must do; no Darwin build does it today.

### 2.2 Peer credentials — defence in depth, and the assertion that makes it real

On every `accept()` the owner reads the peer's uid and refuses a mismatch.

| platform | call | struct |
|---|---|---|
| Linux | `getsockopt(SOL_SOCKET, SO_PEERCRED, …)` | `ucred{pid, uid, gid}` |
| Darwin | `getsockopt(SOL_LOCAL=0, LOCAL_PEERCRED, 76)` | `xucred{version, uid, ngroups, gid[16]}`, uid at offset 4 |

**The 76 is a native-alignment number and the format string has to say so.** `struct.calcsize("@IIh16I")`
is **76**; `struct.calcsize("=IIh16I")` — standard sizes, no alignment — is **74**. A parser written with
`=` reads a 76-byte buffer as the wrong shape *and* a length assertion pinned to `calcsize` agrees with
it, so the two mistakes cover for each other. **Pin the format to `@` (or bare, which is `@`) and pin the
expected length to 76.**

**The platform discriminator is the presence of `socket.SO_PEERCRED`, and the fallback on an
unrecognised platform is to REFUSE TO START — never to skip the check.** A peer check that silently
no-ops where it cannot run is worse than none: the deployment believes it has one.

**Two assertions are mandatory, and P15 changed what they have to say.**

- **Length.** The returned buffer must be **exactly** the expected struct size.
- **uid 0.** A parsed peer uid of 0 is rejected unless the owner itself runs as root.
  ⚠ **Revision 4 said this rule "actually catches the Darwin wrong-level call". That was an over-claim,
  and Mira's test-3 finding is what exposed it.** Given the uid-mismatch rule (§5 test 1), this clause
  never changes an accept/reject outcome in any owner configuration — there are only two: a **non-root**
  owner already rejects uid 0 as a mismatch, and a **root** owner accepts uid 0 under both rules. The
  Darwin wrong-level call does return four zero bytes that a *slice* parser reads as uid 0, but what
  stops that is the parse-form requirement below (`unpack_from` raises on a short buffer) and, for a
  non-root owner, the mismatch rule — not this clause. **It stays, as defence in depth against a future
  weakening of the mismatch rule, and is claimed as nothing more.** That is also why §5 test 3 no longer
  names a discriminating mutation.

⚠ **Revision 1 got the reasoning here wrong, and the error is instructive.** It claimed the length
assertion was what stopped P3's 4-zero-byte buffer being read as uid 0, and offered a test that
removes the assertion and requires a raise. **Measured (P15): `struct.unpack_from` already raises on
a short buffer** — so for a struct parser that test passes with or without the assertion. It was
vacuous, and §6 of revision 1 leaned on it as the one thing standing between this design and a
silent always-allow.

The real silent path is the **slice form**: `int.from_bytes(raw[4:8], "little")` on a 4-byte buffer
slices to `b""` and returns **0**, with no error. So the requirement is sharper than "assert the
length":

> **Parse the peer struct with `struct.unpack_from` (which raises), never by slicing. If a slice
> form is used anywhere, the length assertion is load-bearing and must precede it.**

That is checkable by reading the parser, and §5's replacement test targets the slice form
specifically.

### 2.3 What an unauthorised peer receives

`Rejected`, never `Unavailable` — the owner was reached and refused, which is §5.2's distinction,
and the two have opposite remedies.

```
Rejected(
  reason  = "peer_identity",
  detail  = "connecting uid <n> is not this owner's uid <m>",
  remedy  = "connect to the socket under your own $ENGRAM_HOME",
)
```
…then close.

**The reason is returned in full.** Under §2.0's threat model the counterparty is a misconfigured
colleague, not an adversary — and an adversary who could reach this socket has already lost, since
they can read the database file. Withholding the reason buys nothing against the excluded case and
costs the one thing §5.5 requires.

### 2.4 Stale-socket replacement must not be a file-overwrite primitive

Before unlinking, the owner confirms the path is a socket (`stat.S_ISSOCK`) and **not a symlink**,
then unlinks and binds. Replacing whatever sits at the path, sight unseen, turns crash recovery into
"write a file anywhere the owner can reach".

---

## 3. The read contract

### 3.1 Two layers, and they answer different questions

The registry is **not** the containment boundary on its own. Underneath it, reads execute on a
connection whose authorizer allows a **measured** action set, with P7 as the control proving the
authorizer causes the denials.

- **Authorizer = the floor.** Structural, at the parser, where no string trick reaches. It makes
  `ATTACH` and write-capable `PRAGMA` unreachable *even if the registry is wrong* — and "the
  registry is wrong" is the realistic failure under §2.0(d), since the registry is built from our
  own source.
- **Registry = the ceiling.** Intent-level: *which* reads this channel may issue. The authorizer
  cannot express this.

**This is the one structural claim that survived attack.** The reviewer tried to collapse it in both
directions and could not.

**The allowlist is re-derived from the measured read surface, and revision 1's three actions were
too narrow.** Revision 1 wrote "allow only `SELECT`, `READ`, `FUNCTION`" without measuring it
against the reads the owner must actually serve. Consequence the reviewer found:
`SQLITE_TRANSACTION` is denied, which kills the `BEGIN DEFERRED` read-mark at
`engram_core.py:7077-7088` — and that call is wrapped in `except Exception: pass  # best-effort`,
so the snapshot **silently** loses the consistent view it exists to provide. A fail-silent conversion
of exactly the kind §2.2 refuses for the peer check.

So the allowlist is an **obligation on U4, not a list I can finish here**: it must be derived from
the census (§3.2) plus the owner's own operations, and every action it denies must be checked for a
caller that swallows the denial. `SQLITE_TRANSACTION` is the first known member; I do not claim it is
the last, and §7 records that the derivation is owed rather than done.

**Which connection serves `snapshot()` — ANSWERED in revision 5 by Mira's ruling on the connection
question, then corrected again in revision 6 on the snapshot() contract itself (below).** History,
read as history rather than a still-standing claim: revision 2 said the write connection, "because
`snapshot()` uses SQLite's backup API rather than issuing read statements". Revision 3 called this
"wrong about code cited ten lines above it", pointing at `_commit_snapshot_async` (`:7084-7098`) and
`_commit_snapshot_sync` (`:7287-7313`), which issue `BEGIN DEFERRED`, then
`SELECT count(*) FROM sqlite_master`, then `COMMIT`, and concluded "`snapshot()` **does** issue reads
and **does** open a transaction" — withdrawing revision 2's answer and stating only the constraint.
**Revision 6 (below) establishes that revision 3's inference was itself mistaken**: the cited code
belongs to a different function than the one this section is about. Revision 2's original statement
was correct for the owner RPC; revision 3's correction, though well-reasoned from the code it looked
at, looked at the wrong code.

⚠ **Revision 6 (Borges's ruling, engram-alpha#3221, 2026-09-29): revision 3's withdrawal named the
wrong function, and the BEGIN/READ/COMMIT requirement below never applied to the owner RPC.** Two
functions are both loosely called "snapshot", and the paragraph above answers about only one of
them: `_commit_snapshot_async`/`_commit_snapshot_sync` are `engram_core.py`'s **per-turn checkpoint**
(the nap/sleep git-checkpoint read-mark) — a different mechanism from the **owner RPC** this section is
actually about (`engramd_owner._handle_snapshot`, U3 contract item 5, U4b's build target), which copies
the store with `sqlite3.Connection.backup()` alone. Revision 2 was right about the owner RPC; the code
revision 3 cited to refute it belonged to the checkpoint function instead. **`backup()` never issues a
`BEGIN`/`SELECT`/`COMMIT` of its own and never consults the authorizer at all** — measured independently
three times (U4b's build, its reviewer-fairy, and Mira's own reproduction): a deny-all authorizer on the
source connection does not stop it, while an ordinary `SELECT` on the same connection is denied. Wrapping
the copy in a read-mark sequence solely so a denial-based test could pass would test a ritual added for
the test, not the copy itself — rejected for that reason. The checkpoint function's own
`except Exception: pass` read-mark swallow is real and stays open, tracked as its own issue rather than
folded into this slice (it lives in a different file, under no authorizer in Phase 1).

**`snapshot()` [the owner RPC] runs on an owner-internal, serialized/quiesced path — never the
client-read SQL path — and owes exactly three properties, not a BEGIN/READ/COMMIT sequence:**
(i) it never runs on, and never touches the authorizer of, the client-read connection; (ii) a copy
failure is loud — `Rejected(reason="snapshot_failed")`, never a success; (iii) client SQL cannot reach
the connection `snapshot()` runs on (structural, independent of (i)). A dedicated internal connection
satisfies all three. **The authorizer floor does not bound what `snapshot()` copies** — `backup()`
copies the whole database regardless of any authorizer, by design; what bounds *who may call it* is the
socket peer/method gate (U4a/U4c), not this connection's authorizer. If that needs narrowing, it is
those units' job. §5 test 17 is written against (i)-(iii) plus the authorizer-blind fact, not against
the retracted BEGIN/READ/COMMIT requirement.

**The connection question — DECIDED in revision 5: (b), two connections inside the one owner
process.** Mira's ruling as security reviewer. The **client-read connection is permanently
restrictive**; client SQL **never** dispatches onto the unrestricted connection; there is **no
authorizer swapping**. Option (a) is rejected for the reason revision 3 gave: swapping is state whose
failure direction is the bad one — left permissive, the floor is gone and nothing looks different.

**The parent's wording, and what is and is not settled about it.** The invariant this depends on is one
*process* holding the live store. But the parent's inventory row at
`docs/2652-engrammanager-design.md:677` literally says `SqliteEngramStore` holds *"the only open handle
on `knowledge.db`"*, and two connections are two handles. Revision 3 read that as process-scoped and
called the reading "mine, and the part I am least sure of". Mira's ruling adopts the same reading, so it
is no longer mine alone — but the parent's words still say *handle*, and **that document is not this
slice's to edit.** §7 flags the clarification to its owner rather than resolving it by editing here.

Because (a) is rejected, revision 3's mandatory test for it (*after a read raises, a write still
succeeds*) is retired. It is replaced by the property (b) actually depends on — **client SQL cannot reach
the unrestricted connection** — which §5 test 22 checks structurally, independently of the authorizer.

### 3.2 Two tiers, because the read surface is mostly but not entirely byte-stable

**Revision 1 claimed byte-exact templates could cover the read surface. That was refuted.** Reads
assembled from a filter spec have no fixed bytes: `engram_list` absorbs `parse_filters()` output and
`_parse_condition` recurses through `_parse_compound`, so one logical read emits many different
strings.

**But "byte-exact fails" and "byte-exact is useless" are different conclusions, and only a census
separates them.** P17, at dev `5001800654bc`:

| | count |
|---|---|
| literal reads (byte-stable at the call site) | **498** |
| f-string-assembled reads | **77** |
| unclassifiable (SQL via variable, concat, or a call) | **37** |
| writes/DDL, literal vs assembled | 212 / 19 |

Byte-stable share of classifiable reads: **87%**.

⚠ **Revision 2 called that an upper bound. The direction was wrong.** It assumed the 37
unclassifiable sites are *less* byte-stable than average. The reviewer measured them: **9 of the 37
are module-level string literals**, i.e. *more* stable than average, which puts the all-37-stable
ceiling at **87.4% > 87%**. So 87% is nearer a **lower** bound on that axis. I stated a direction
without measuring the residual the sentence was about — in a sentence whose entire job was stating
the direction. The honest form: **87% of classifiable reads, with 37 sites unclassified and no
measured reason to expect them to break either way.**

**And the population matters more than the percentage.** Partitioned on the parent document's own
owner/client table (`docs/2652-engrammanager-design.md:677`), the reviewer found **most of the 498 is
owner-internal reads the registry never governs at all**; the client-side share lands in an interval
(≈83–91% depending on whether `health/` is counted) rather than on a point. I am reporting the
interval rather than picking its convenient end, and noting that the reviewer's own first reading of
this moved in the opposite direction before their sensitivity check corrected it.

### The scope finding that shrinks this section

**For U4 — the unit this whole design blocks — the read surface is 19 literal, 0 f-string, and 2
unclassifiable, both of which are module-level literals. 21 of 21 byte-stable.** (Reviewer's
measurement over the hooks; I have not re-run it.)

**So tier 2 is not on U4's path.** It was revision 2's largest new surface and the unit that needs it
does not exist yet. It is specified below in outline because the shape matters for whether tier 1's
boundary is drawn correctly — but it is **deferred to the unit that actually needs it**, and U4 should
build tier 1 plus §3.5's constraint and nothing else. A design that specifies speculatively is worse
than one that says which part is not needed yet; revision 2 did the former.

So the contract has two tiers, one of which U4 does not build:

- **Tier 1 — byte-exact templates, for reads whose text is a literal.** The template is the exact
  SQL byte string; values are `?` placeholders, bound separately, never interpolated; the key is
  `sha256` of those bytes; matching is byte equality. **No normalisation** (P9: Python exposes none,
  so a normaliser would be ours to write, and every normaliser is a parser differential waiting to
  happen — the classic failure being the normaliser and SQLite disagreeing about what a string
  means). P10 is noted and **not used**: `EXPLAIN`'s opcode list is the right basis *if*
  normalisation were ever needed.
- **Tier 2 — typed read operations, for the assembled minority.** What is authorised is the
  **parsed filter structure**, not the emitted bytes: the caller submits the filter spec, the owner
  parses and validates it against a declared field/operator set and emits the SQL itself. This
  inverts the trust direction — the caller no longer authors SQL at all — which is what the parent
  document's §3.1 wants from the write side and what revision 1 failed to apply to the read side.

**The variable-arity `IN (?,?,?)` case belongs to tier 1** and has an answer there: write the
template once as `… WHERE id IN (SELECT value FROM json_each(?))` and bind a JSON array. One fixed
template for every arity. It does **not** rescue tier 2, whose variation is structural (conditional
predicates, a varying projection list) rather than arity.

**And revision 1's §3.3 contradicted §3.2 in a way the reviewer named.** "Callers register from
their own source" and "a build-time static scan produces the registry" are different acts: a static
scanner cannot produce bytes for an f-string site. Under the two-tier split the contradiction
dissolves — the scanner produces tier 1's registry from the literals it can actually read, and tier
2 sites are declared as typed operations rather than scanned. **A site that is neither a readable
literal nor a declared typed operation is a build-time ERROR, not a default-allow.**

### 3.3 Registration is a build-time act, and is NOT a socket operation

**The socket exposes no `register_template`.** Runtime registration by the caller is self-defeating:
a caller that can register anything is not constrained by a registry.

Stated as a negative on purpose — *"the operation table contains no registration verb"* is
**checkable by grep**, and a rule that can be checked mechanically is a different kind of rule from
one that has to be remembered.

Development needs an escape hatch; it is named rather than improvised.
`ENGRAM_OWNER_ALLOW_DYNAMIC_TEMPLATES=1` enables runtime registration, logs every use loudly, and is
**refused unless the install tier is dev**. A test must assert it is off by default — an escape hatch
whose default nobody tested is a hole with a comment above it.

### 3.4 Default-deny

An unregistered tier-1 hash, or a tier-2 spec that fails validation, returns:

```
Rejected(
  reason  = "unregistered_template" | "invalid_read_spec",
  detail  = "<template hash>" | "<which field/operator was rejected>",
  remedy  = "register at build time, or use a typed read operation",
)
```

Never `Unavailable`. Never executed. No "log and allow" mode exists, at any tier.

### 3.5 What the read contract does NOT authorise — rewritten, because revision 1 named the wrong limit

Revision 1 said: *"parameters cannot change what a statement does — that is what binding buys"*, and
offered "the registry authorises a statement, not a result set" as the honest limit. **The first half
is false and the second was the wrong limit.**

**P14, measured, with a discriminating control.** One byte-identical template,
`SELECT rowid FROM docs WHERE docs MATCH ?`:

```
param='public_notes: hello'       -> rows=[(1,)]
param='secret_notes: classified'  -> rows=[(1,)]
param='classified'                -> rows=[(1,)]
param='public_notes: classified'  -> rows=[]        <- the discriminating datum
```

The empty result settles it: `classified` is in the row but not in `public_notes`, so the column
filter is really applied and the parameter is genuinely an **expression**. FTS5 parses the bound
value as its own query language, column filters included.

So **#3097's fourth named attack — a template matching only because of its parameters — DOES apply.**

### ⚠ Revision 2 answered this with a second enumeration. That was the same mistake one layer along.

Revision 2's corrected limit read: *"A bound parameter is data for an ordinary predicate and a QUERY
EXPRESSION at a virtual-table boundary."* A closed two-case rule, asserted as the fix. **Refuted, by
five cases with no virtual table anywhere near them:**

```
LIKE ?                param='a_c'      -> all three rows   (bound `_` is a WILDCARD)
LIKE ? ESCAPE ?       ('a\_c','\')     -> one row          (param 2 redefines param 1's language)
GLOB ?                param='a[b_]c'   -> two rows         (character class)
json_extract(meta,?)  param='$'        -> the whole object, every row
LIMIT ?               param=-1         -> 52 of 52 rows    (a bound value REMOVES the clause)
```

And the decisive one is not a measurement of mine but a fact already in the tree, which the reviewer
found: **`engram_filter.py:303-325` already escapes `%` and `_` via `_like_escape` with
`ESCAPE '\'`**, and its comment states why — *"to protect against the SQLite `%` and `_`
wildcards"*. That is the module tier 2 rests on. **The codebase already knew what revision 2's
sentence denied.**

### The limit, stated as a property with an OPEN set

> **A bound parameter is NOT guaranteed inert. The ways it can change a statement's meaning are not
> enumerable in advance, and this document does not attempt to enumerate them.**

The five cases above are offered as **evidence that the set is open**, not as its contents. Two
controls that did *not* land are recorded so the list is not read as closed in the other direction
either: `COLLATE ?` is refused outright, and `ORDER BY ?` binds a constant rather than a column.

**Why a property rather than a list.** This is the third time in this artifact's history that a
refuted enumeration was replaced by a fresh enumeration presented as the corrected limit, and the
sibling PR #3070 spent four review rounds on the identical move before the fix turned out to be
*bounding the claim instead of extending the list*. An enumeration invites the next reader to stop
looking; a property plus an open-set declaration does not. §3.1's two-layer argument is the same
shape applied to verbs — the authorizer denies a class structurally rather than listing the spellings
of `ATTACH` — and this section should have reused it instead of re-listing.

### The defence: INVERT the polarity — a closed SAFE set, default-deny everything else

⚠ **Revision 3 first wrote a three-item defence whose middle item was incoherent**, and the reviewer
named it exactly: it said "an owner-side validator for any parameter reaching a construct whose
grammar is not plain comparison", which asks an implementer to decide **membership of the set this
section had just declared unenumerable**. A property with an untestable defence is worse than a wrong
list, because nothing can be written against it.

The fix was already inside this design and I did not use it. **Tier-1 templates are byte-exact and
registered at BUILD TIME, so each `?`'s syntactic context is statically knowable at registration.**
That makes the safe side enumerable even though the dangerous side is not:

> **CLOSED SAFE SET — a placeholder is implicitly safe ONLY in a plain comparison (`= < > <= >= <>
> !=`, `IN` via the `json_each` form, `IS [NOT] NULL` operands) inside a template that touches ORDINARY
> tables only — decided from the SCHEMA at registration, never from the operator's spelling. A template
> that reads ANY virtual table has NO implicitly safe placeholder, whatever operator each sits in. EVERY
> placeholder outside the safe set is DEFAULT-DENIED at registration unless the template declares an
> owner-side policy for it.**

### ⚠ Revision 5: revision 4 keyed the safe side on operator spelling, and a plain `=` refutes it

Mira's blocker. Revision 4's safe set admitted any placeholder in a plain comparison. **On an FTS5 table
a plain `=` is not a comparison.** Her probe, re-run by me with two controls she did not include:

```
Darwin arm64, CPython 3.12.10, SQLite 3.49.1
(Mira: Linux x86_64, CPython 3.12.3, SQLite 3.45.1 -- same result)
CREATE VIRTUAL TABLE docs USING fts5(public_notes, secret_notes); row = ('hello','classified')
SELECT rowid FROM docs WHERE docs = ?
  'public_notes: classified' -> []        <- the column filter is really applied
  'secret_notes: classified' -> [(1,)]    <- and it reaches the secret column
control, same table, DECLARED column:  public_notes = 'secret_notes: classified' -> []   (literal)
control, ORDINARY table:               notes = 'notes: classified'               -> []   (literal)
```

The hidden column named after the table parses its argument as an FTS5 query; a declared column on the
same table does literal equality; an ordinary table does literal equality. And this document already
held the second counter-example without seeing it: the population paragraph below calls vec0's `k = ?`
*"a plan control, not data"* — while revision 4's rule admitted every `=`, so that very site sat
**inside** the safe set. The document contradicted itself across three paragraphs, and Mira caught it.

**Why the rule keys on TABLE kind, not on which column within a virtual table.** The declared-column
control is literal *for FTS5* — but a virtual-table module decides for itself what a constraint on any
of its columns means (through its own `xBestIndex`/`xFilter`), so one module's behaviour says nothing
about the next. Widening the safe set on the strength of FTS5's declared columns would rebuild, one layer
down, exactly the "supposedly complete list" this section exists to avoid. So a template touching a
virtual table loses implicit safety **wholesale**, and each of its placeholders needs a declared
owner-side policy. Conservative on purpose, and cheap: every virtual-table site in the census (6 FTS5 +
1 vec0, below) needs a policy anyway.

**How registration decides, mechanically — no SQL parser required.** Measured: preparing a template under
an auditing authorizer yields one `SQLITE_READ` event per (table, column) it touches; cross those
against `sqlite_master`'s `CREATE VIRTUAL TABLE` rows.

```
SELECT rowid FROM docs WHERE docs = ?          READ (docs,ROWID) (docs,docs) + shadow tables -> DENY
SELECT rowid FROM docs WHERE public_notes = ?  READ (docs,ROWID) (docs,public_notes) + shadow -> DENY
SELECT rowid FROM plain WHERE notes = ?        READ (plain,ROWID) (plain,notes)             -> eligible
SELECT n FROM plain WHERE n > ?                READ (plain,n)                                -> eligible
```

It reuses the authorizer machinery P6 already depends on, so it adds no second instrument. **Scope, stated
because this part is not measured by me:** the vec0 row is **not** first-hand. On the author's seat vec0
cannot load at all — the interpreter lacks `enable_load_extension`, and ENGRAM's loader then disables the
vector backend silently (filed as #3142) — so the author has not measured whether a
vec0 read surfaces as a `SQLITE_READ` event at prepare time. **Mira has, on Linux** (SQLite 3.45.1,
sqlite-vec 0.1.9, a one-row `vec0(embedding float[2])` table): an auditing authorizer records
`SQLITE_READ` on the real vec0 table for a direct `MATCH` + `k` query, the same through a table alias,
and through an ordinary view wrapping it. That supports schema-crossed detection on that runtime,
including indirection; it does not establish every module or platform. The *rule* does not depend on
it (vec0 is a virtual table, so it gets no implicit safety); only the automatic *detection* does.

**The audited schema route is authoritative.** Registration **also** denies any template whose text
names a virtual table from `sqlite_master`, but that token match is a **backstop, not a proof**: a
lexical match can miss a table reached through quoting or a view, so it makes **no** never-under-deny
claim. When the audited classification is incomplete — the authorizer pass cannot account for every
table a template reads — registration **fails closed** and denies.

**Both examples are now negative registration cases** (§5 test 21).

Note what the inversion buys, and it is the whole point: the open set never has to be enumerated. A
new SQLite construct, a new virtual table, a bound parameter doing something nobody here anticipated —
all land on the deny side by default, because they are not in the safe list. The earlier form had the
opposite failure direction: anything unanticipated was silently fine.

It also makes §5's tests writable, which the previous version did not. And it closes the gap where
this section and §4 were missing each other: `LIMIT ?` is **not** a plain comparison, so it now
requires a declared validator — where before, a bound `-1` returning all 52 of 52 rows passed the
registry *and* had no deadline (§4's fifth attack), each section assuming the other covered it.

Two things sit alongside the safe set rather than inside it:

- **Per-site sanitisation stays**, and `engram_filter.py` already does it for `LIKE`. It is defence in
  depth, not the boundary — and §7 records that three other sites do it incompatibly.
- **The template registry authorises the STATEMENT only** and does not vouch for what a parameter does
  inside it. That division is unchanged; what changed is that the placeholder positions are now
  themselves part of what registration checks.

### Population, for the FTS5 case specifically

7 `MATCH ?` sites under `src/engram` — **6 `nodes_fts MATCH ?`** (FTS5, a real query language) and
**1 `embedding MATCH ?`** (`engram_core.py:7713`, a `vec0` table taking a serialised blob). The
reviewer reported 7; the 6/1 split is the sharper number. ⚠ That same line also binds `k = ?` — a
**plan control**, not data — which is a sixth member of the open set at the very site revision 2 used
as its counter-example. ⚠ **And revision 4's safe set then admitted it**, because it is a plain `=` —
the contradiction revision 5 removes above.

⚠ **Revision 4: revision 3 named the wrong site as the flagship, and I nearly demoted the whole item
on the strength of that.** Revision 3 pointed at `hooks/claude/engram-surface-hook.py:1579` — prompt
text reaching an FTS5 expression inside a hook. The scope half of that is wrong: the connection that
query runs on is opened at `:1570` against `os.path.join(pack["path"], "knowledge.db")` with
`mode=ro&immutable=1` — an attached **package** DB, which the parent's own table at
`docs/2652-engrammanager-design.md:849` rules **out of scope**. So that site is not a read this socket
would ever mediate, and the sanitizer-must-move-with-the-boundary warning does not apply to it.

What I then wrote into my own plan was "so none of the 7 sites is a client-issued live-store read —
demote the item." **That is false, and the census says so.** The flagship MOVES; it does not disappear:

**The one that matters most:** `viz/data/graph.py:54`. Its enclosing `search_nodes(db_path, query,
limit)` takes the database as a **caller-supplied parameter**, so the function itself is silent on which
store it reads — that is decided two frames up, and it is where I first got this wrong. The chain is
`viz/server.py:388` → `viz/agents.py::_resolve_agent` → `AGENTS[name]["db"]`, and for the **default**
agent that resolves to `DEFAULT_DB = str(Path.home() / ".engram" / "knowledge.db")` (`agents.py:34`,
`:409-411`) — **the live store**. Snapshot paths are what the *discovered other-agent* entries hold; the
seat's own graph is not one of them. So this is a client-issued FTS5 expression, from a separate
process, built out of an HTTP query string, against the live store. Its sanitizer strips non-word characters and
appends `*` to the last token; it **does not quote** (§7). That is the combination the boundary has to
answer for: untrusted input, live store, cross-process caller, no quoting. The same statement also
binds `LIMIT ?`, which the closed safe set above now requires a validator for — the abstract finding
and a real site meeting each other.

The correction generalises past this row: my demotion was a conclusion about all 7 sites drawn from the
1 site I had actually opened, and the site I opened was the unrepresentative one. The hook does read
the live store — at `:109`, `:238`, `:1036`, `:1287`, `:1427`, `:3292` — just never through `MATCH`.

**Still open, and now sharper than revision 2 framed it:** which of the three mechanisms above
authorises an FTS5 expression, given that **three divergent sanitizers already ship** (§7). Raising an
open question while three incompatible answers are already in the tree is worse than not raising it,
and revision 2 named none of them.

Row-level / per-caller result scoping remains out of scope, and is a *different* limitation from the
one above — revision 1 conflated the two.

---

## 4. The attacks — four named by #3097, plus a fifth

| attack | met by | evidence |
|---|---|---|
| `ATTACH` | authorizer denies action 24 | P6, P7 control; **P13** — `mode=ro` does *not* stop it |
| write-capable `PRAGMA` | authorizer denies action 19 | P6, P13; ⚠ see the caveat below |
| stacked statements | the owner uses `execute()`, which refuses them, and **never** calls `executescript`; no statement built by concatenation | P11 refused, **P12** permits — hence the rule |
| a template matching only because of its parameters | **DOES apply** at a virtual-table boundary | **P14** — §3.5 |
| **resource exhaustion** (fifth) | **three bounds on three axes, none sufficient alone** (revision 5): a `progress_handler` bounds VM-instruction cost; `SQLITE_LIMIT_LENGTH` bounds the size of any **single** string, blob or row (not total memory); **one end-to-end request deadline** bounds wall clock across frame I/O and execution | reviewer (cartesian, `zeroblob`); me (per-op socket timeout, `setlimit`) — see below |

⚠ **The `PRAGMA` caveat, and revision 1's remedy for it was unsafe.** The authorizer sees
`SQLITE_PRAGMA` for *every* pragma, read-only ones included, so denying the code denies harmless
pragmas too. Revision 1 said: allow it **by name at `arg1`**. **P16 measured that and it lets a write
through** — with only `user_version` allowed, `PRAGMA user_version = 99` is ALLOWED and the value goes
0 → 99, because `arg1` carries the name and `arg2` the assigned value. The rule is therefore:

> **Allow a pragma only when `arg1` is in the allowlist AND `arg2 is None`.**

**Resource exhaustion is the reviewer's finding and their measurement, not mine, and it is the one
number in this document with NO PLATFORM attached.** A 4-way cartesian over 400 rows ran
**120.367 s** ALLOWED under the restrictive authorizer; revision 1 contained no `timeout`,
`progress_handler`, `interrupt` or deadline of any kind. **I have not re-run it and I do not know
which platform produced it**, so unlike every other figure here it cannot be checked against a named
seat — which matters because a wall-clock duration is exactly the kind of number that moves with
hardware. The direction is not in doubt (an unbounded cartesian under this authorizer is permitted);
the magnitude is unverified. It is in scope by §2.0(d) — our own accidental expensive read — and the
remedy is a `progress_handler` aborting past a deadline, which the reviewer measured at 0.001 s to
fire.

⚠ **Revision 5: that remedy was written as if it covered resource exhaustion. It covers one axis of
three.**

**Allocation is not bounded by a progress handler.** Mira measured it; I reproduced it with a control:

```
progress handler armed to ABORT on its first callback, every 100 VM instructions
SELECT zeroblob(8388608) -> 8,388,608 bytes returned, 0 callbacks
                            (Mira: Linux, 0.0046 s;  me: Darwin, 0.0011 s)
control, recursive CTE   -> aborted 'interrupted', 1 callback  (so the handler IS armed;
                            allocation simply never trips it)
fix, measured: setlimit(SQLITE_LIMIT_LENGTH, 1_000_000) -> zeroblob(8 MB) refused
               'string or blob too big'; a 100 KB control still passes
```

So the owner sets **`SQLITE_LIMIT_LENGTH`** (and considers `SQLITE_LIMIT_SQL_LENGTH` for the input side)
on the client-read connection, at a value chosen against the owner contract rather than guessed (§8).
That bounds the **allocation class** the probe demonstrates. **Precisely: it caps each single string,
blob or row, not aggregate memory** — many values each under the cap are not bounded by it. It is not a
claim to have enumerated every resource a read can consume, and a small probe is not a measured denial
of service.

**Wall clock across the socket is bounded by neither.** My own 18:13Z comment on this PR, now folded
rather than left as a note: a socket timeout is **per-op**, not end-to-end. Measured on Darwin, a peer
trickling one byte per 0.25 s held a `timeout=2.0` read for **4.805 s**, against **2.002 s** for a silent
peer (the control). A progress handler counts VM instructions and never sees the socket, so as revision 4
stood the owner had **no bound at all on a slow client** — and that is reachable **without hostility**: a
hung or buggy hook stalling mid-write is an ordinary failure, and against a single owner it denies reads
to every other client on the seat. Borges found the class on U1; the Darwin numbers are mine.

> **One monotonic end-to-end deadline per request**, covering frame acquisition, execution, and
> response delivery. A per-socket-call timeout is **not** this requirement. A trickling peer must fail
> within the total budget; a silent peer is the control; a subsequent client must still be serviced.

**The deadline covers execution for every client-facing method, not only framing — including
`snapshot`.** The budget may differ by method, chosen once the method is parsed, but it is always
measured from accept, never resets, and is always a named finite number; "no budget for internal
operations" is not an allowed value. On overrun the owner stops the **work**, not merely the wait: no
success response, the lock is released, the destination is left untouched — a pre-existing file stays
byte-for-byte identical and an absent one stays absent — no partial file is left anywhere, and a
subsequent client is served. The owner stages the backup into a temporary file in the destination's own
directory, created exclusively; on success it is made durable (fsync) then atomically swapped in for the
destination via `os.replace`, and on abort or any failure only the temp file is removed. This is what
keeps the promise: readers such as viz, health snapshots and the sleep backup may re-read a fixed path,
and must never see a half-written file. Overrun is tolerated up to one step of whatever stop mechanism is
used. The implementation names that step (for example, a `backup()` page count or a progress-callback
cadence) and states its maximum wall time; that stated figure, not an open-ended "one step", is the
allowance. The stop mechanism itself is left to the builder. (Found by Mira's security pre-review on
#3194.)

> The store lock covers only the backup into the temp file. Durability (`fsync`) and the atomic swap (`os.replace`) run on a bounded finalize worker: the request waits on it only until the deadline, and at most `SNAPSHOT_FINALIZE_SLOTS` finalizes may be outstanding, with a call that finds none free refused at once. `os.replace` is issued only while the call is neither abandoned nor past its deadline, and temp cleanup is owned by a single `finally`. Three residuals are outside any in-process owner's control and are stated as exceptions: a pre-deadline commit reservation whose rename may be both issued and completed after the deadline (the worker can be descheduled between reserving and the syscall; reported `commit: indeterminate`, never success; the destination is atomically old or new); a hung `fsync` or `os.replace` holds one slot and one temp until the kernel returns (bounded in count, not time); and process death mid-finalize orphans a temp file beside the destination.

Response delivery gets a reserve of its own, and the send gate stays. Because the one deadline covers response delivery too, `snapshot()` splits it: execution stops at `request deadline − SNAPSHOT_RESPONSE_RESERVE` (a named constant, measured on the owner's own socket), and the backup progress check, the request thread's wait on the worker, and the worker's abandon-or-commit gate all use that earlier execution deadline. A timeout-driven `deadline_exceeded` or `commit: indeterminate` response is therefore composed with the reserve still in hand, and the send gate, which still compares against the request deadline and is unchanged, lets it through. **Client contract: for `snapshot()`, an `Unavailable` (a dropped connection) must be read as indeterminate.** The reserve makes a missed delivery rare, not impossible (scheduler stalls). The destination is atomically either the old file or the complete new snapshot, so the client-visible form of the late-rename residual is `commit: "indeterminate"` **or** `Unavailable`. Both mean "go look at the destination"; neither ever means success. In the typed client, `Rejected.commit == "indeterminate"` is the marker for the committing outcome, `commit is None` on a `deadline_exceeded` rejection means the destination was not touched, and `Unavailable` remains indeterminate.

Two implementation traps, both measured on this fleet and both easy to walk into. The load-bearing
element is **`min(per-op cap, time remaining)`** on every blocking call, not a check between calls — a
naive "remaining time as the timeout" ran 101 s against a 3 s budget. And **a read primitive that loops
internally over several socket operations defeats a between-calls deadline** — measured for
`HTTPResponse.read(n)` (+110 s at 4096 bytes) against `read1(n)`, which held flat. The owner's frame
reader must read in bounded chunks against the remaining budget.

What this fifth attack says about the document rather than the code: #3097 listed four attacks and
revision 1 treated the list as the thing to be met rather than as a **floor**. That is the same
mistake as treating a fixture list as a specification.

---

## 5. What the implementation must be tested against

Contract-first — each test names the mutation that must turn it red.

1. **Peer-uid mismatch → `Rejected(peer_identity)`.** *Mutation: make the uid check a no-op.*
   ⚠ **Needs a second uid; cannot run on the author's seat — §6.**
2. **The peer struct is parsed by `unpack_from`, and a 4-byte buffer RAISES.** *Mutation: replace the
   parse with the slice form `int.from_bytes(raw[4:8], "little")` — the test must go red because the
   slice returns 0 rather than raising.* **Runs anywhere, no privileges.** ⚠ Revision 1's version of
   this test mutated away the length assertion instead, which does **not** discriminate for a struct
   parser (P15). The mutation must target the parse FORM.
3. **Peer uid 0 is rejected** when the owner is not root — **a property check, not a discriminating
   test.** ⚠ Revision 4 gave it the mutation *drop the uid-0 clause*. **That mutation cannot go red**
   (Mira): with test 1's mismatch rule intact, a non-root owner already rejects uid 0 — and §2.2 now
   records that the clause changes no outcome in any owner configuration. The property is kept; the
   claim of discrimination is withdrawn. Identity mismatch is tested through **test 1**, malformed-buffer
   acceptance through **test 2** (parse form). Neither check is weakened.
4. **The socket's parent directory is 0700 at bind time and carries no ACL**: no named user or group
   entries and no default ACL, asserted on the ACL itself, not only via `stat`. The fixture gives
   the parent an inheritable ACL, as `~/.engram` has on the Linux host, because on a parent without
   one this test passes whether or not the strip happens. *Mutations: create it 0755; skip the ACL
   strip.*
5. **Stale-socket replacement refuses a symlink and a non-socket.** *Mutation: remove the checks.*
6. **A one-byte mutation of a registered tier-1 statement → `Rejected(unregistered_template)`.**
   *Mutation: normalise whitespace before hashing — must go red, and that it does is the point: it is
   what keeps §3.2's no-normalisation decision from being quietly undone.*
7. **Two layers are independent, not one layer twice.** Deliberately register a template containing
   `ATTACH`; the authorizer must still deny it. *Mutation: allow-all authorizer (P7 inverted) — red
   while test 6 stays green.* Without this the floor could be absent with every other test passing.
8. **No registration verb on the socket.** *Mutation: add one.*
9. **`ENGRAM_OWNER_ALLOW_DYNAMIC_TEMPLATES` is off by default** and refused outside dev tier.
10. **The owner never calls `executescript`.** A repo-level grep assertion over the owner's package.
11. **A pragma WRITE is refused even when its name is allowed.** `PRAGMA user_version = 99` under an
    allowlist containing `user_version` must be denied, asserted on the **value** (still 0), not on
    the authorizer's return. *Mutation: drop the `arg2 is None` clause — must go red.* (P16.)
12. **A deliberately expensive read is aborted by the deadline**, and a cheap read is not. *Mutation:
    remove the `progress_handler` — the expensive read must then complete.* ⚠ This bounds
    **VM-instruction cost only**; tests 19 and 20 cover the other two axes (§4). The `progress_handler`
    must stop against the **same** request deadline, passed in — not an independent timer or constant —
    so shortening the request deadline must shorten the abort point.
13. **A tier-2 read spec with an undeclared field or operator → `Rejected(invalid_read_spec)`**, and
    the emitted SQL is built by the owner, never taken from the caller.
14. **A site that is neither a readable literal nor a declared typed operation fails the BUILD.**
    *Mutation: add such a site — the build must fail rather than default-allow.*
15. **A bound parameter reaching a non-plain-comparison construct is validated at the OWNER.** Not
    one test but a family, and deliberately not a closed list — §3.5's property is what is being
    tested, so the suite takes a **table of cases and is written to be extended**. Seed it with the
    five measured ones — `LIKE ?` with `a_c`, `LIKE ? ESCAPE ?`, `GLOB ?` with a character class,
    `json_extract(…,?)` with `'$'`, and `LIMIT ?` with `-1` — **all five measured on Darwin, none
    re-run on Linux**, which for these five is a statement about SQLite's own semantics rather than the
    platform's, but it is still not a measurement I made twice. *Mutation for each: bypass the
    validator — the case must go red.*

    ⚠ **Revision 3 asked the suite to "assert that adding a case is cheap", which is not a property a
    test can check.** The mechanical form of the same intent: **the validator's rule table and the
    test's case table are THE SAME OBJECT** — the suite iterates the validator's own registered rules
    and fails the build on any rule with no case row. Then a future reader who adds a rule cannot leave
    it untested, and one who adds a case cannot leave it unimplemented; the "five are not the whole
    set" worry is enforced by construction instead of asserted in prose.
16. **An FTS5 `MATCH` parameter is not authorised by a tier-1 template alone** — whichever mechanism
    §7's open question settles on, the test is that `secret_notes: …` cannot be smuggled through a
    template registered for `public_notes`. (P14.)
17. **⚠ Rewritten in revision 6 (Borges's ruling, engram-alpha#3221) — the owner RPC never issues a
    `BEGIN`/`SELECT`/`COMMIT` and `backup()` never consults an authorizer, so a `SQLITE_TRANSACTION`
    denial cannot be the mutation.** `snapshot()` [the owner RPC] runs on the owner-internal path,
    succeeds under that connection's normal authorizer, and a genuine copy failure is not swallowed.
    *Mutation: make the `except sqlite3.Error` clause swallow the error instead of returning
    `Rejected(reason="snapshot_failed")` — the snapshot must FAIL LOUDLY.* Also asserts the client-read
    connection's authorizer is **unchanged** across a snapshot, by identity (a marker authorizer
    installed before the call is still the one firing after it) — this is the test that would have
    caught revision 2's false answer about *which connection* serves `snapshot()`, independent of the
    retracted BEGIN/READ/COMMIT requirement.
18. *(Retired in revision 5 — it tested option (a), which is rejected. See test 22.)*
19. **Single-value size is bounded independently of the progress handler.** `SELECT zeroblob(N)` above the
    configured `SQLITE_LIMIT_LENGTH` is refused on the client-read connection, while a result under the
    limit succeeds. *Mutation: remove the `setlimit` call — the oversized result must then be returned.*
    Include the control that makes the point: with the progress handler armed and the limit removed, the
    oversized result **still** returns — which is what shows the handler is not the bound.
20. **One end-to-end request deadline.** A peer trickling bytes fails within the **total** budget; a
    silent peer is the control; a **subsequent** client is serviced afterwards. *Mutation: replace the
    end-to-end deadline with a per-socket-call timeout — the trickling peer must then exceed the budget.*

    **20b. A `snapshot()` that cannot finish inside its budget is aborted, and the lock is released.**
    Use a small constructor budget, or a store slow enough that no small budget is generous; do not
    inject a sleep into the owner. The test names the chosen stop step (for example, `backup()` page
    count or progress-callback cadence) and measures or bounds its wall-time allowance; by
    `deadline + that stated allowance`, assert: no success
    response; an absent destination stays absent and a pre-existing destination (with a sentinel row) is
    byte-identical by sha256; no temp file remains in the directory; a **second** `snapshot()` that fits
    its own budget then succeeds within it — this is the lock-release proof, not a `ping` (`ping` never
    takes `_store_lock`). *Mutation (a): restore the U3 shape — the deadline checked only after dispatch
    returns. Test 20b must go red; test 20 must stay green.* *Mutation (b): abort the work but leak the
    lock (skip the release on the abort path). The second `snapshot()` must then time out and test 20b
    must go red, while a `ping`-only check would stay green.* *Mutation (c): back up straight into `dest`
    and `unlink` it on abort (the U3 shape) — the pre-existing-destination case must go red.*
21. **Virtual-table templates get no implicit safety at registration.** Negative cases: `docs = ?` on an
    FTS5 table and `k = ?` on vec0 must each be REFUSED implicit safe status, while `notes = ?` on an
    ordinary table is admitted. *Mutation: classify placeholders by operator spelling alone — both
    negative cases are then admitted, and the test must go red.*
22. **Client SQL cannot reach the unrestricted connection**, asserted structurally: the object that
    dispatches client requests holds no reference to the owner-internal connection. *Mutation: give the
    client dispatcher the internal connection — must go red.* Deliberately independent of the
    authorizer, so it isolates routing from the floor (test 7) instead of re-testing it.

**Overall discrimination control:** replace the authorizer with an allow-all and delete the registry
lookup, together. Tests 6, 7, 11, 15 and 16 must go red; 1–5, 8–10, 17, 19–22 and 20b must stay green.
Test 21 stays green because registration-time classification is a separate mechanism from request-time
lookup — which is itself the property this control exists to show. Two independent
mechanisms failing must show as two independent failures — if one mutation reds everything, the layers
are entangled and the design was not built.

---

## 6. The control this design cannot run, named rather than omitted

**The cross-uid rejection test (test 1) has not been run, so the peer-credential read is unverified
in the only direction that matters.**

P2 returned `uid=501` and the process's own uid is 501 — consistent with reading the peer *and* with
reading ourselves. P4 exercises the **option** axis; P3 shows the **level** axis is the one that fails
silently. Neither is the missing control.

Revision 5 adds one more, and says what it is not: Mira ran Linux `SO_PEERCRED` on a socketpair —
exactly 12 bytes, native `@3i` decoding pid/uid/gid equal to her own process. As she states herself,
that is **a same-uid read, not the foreign-uid rejection control.** It measures the Linux arm's parse;
it does not measure the check's ability to refuse.

The discriminating control is structural: to test that the check can **reject**, plant a **foreign**
peer and require refusal. That needs a second uid — a second account on the host (one exists; using
it needs the user's go-ahead) or a CI container that can `su`.

Until one runs, §2.2's parse-form requirement is what stands between this and a silent always-allow —
which is why test 2 is written to target the parse form and needs no privileges. **Revision 1 said
the same thing about a test that could not actually fail.** That correction is the single most
important thing the review produced.

---

## 7. Open, and whose call each one is

| question | state |
|---|---|
| **How an FTS5 `MATCH` parameter is authorised** (§3.5) | **The safe-set half is DECIDED in revision 5** — a template touching a virtual table has no implicit safety. **Still open, and still a blocker: which owner-side policy authorises an FTS5 expression** — on a different site than revision 3 said. Three divergent sanitizers ship — `engram_core.py:4724` and `:4772`, `viz/data/graph.py:39-45` which does **not** quote, and `engram-surface-hook.py:1549`. Any answer has to reconcile those or declare which wins; revision 2 named none of them. The load-bearing site is **`viz/data/graph.py:54`** — caller-supplied `db_path`, live store, HTTP query string, non-quoting sanitizer. The hook site is a **package**-DB read and is out of scope per the parent's table at `:849`; revision 3 had these the wrong way round. |
| **Which connection serves `snapshot()`** (§3.1) | **Answered in revision 5 (Mira).** An owner-internal serialized/quiesced path, not the client-read path; preserves `BEGIN`/`READ`/`COMMIT`, fails loudly, never toggles the client-read authorizer. The concrete lifecycle is owed to §5 test 17. |
| **One connection with a swapped authorizer, or two in the one owner process** (§3.1) | **DECIDED — (b), Mira's ruling.** Client-read connection permanently restrictive; no authorizer swapping. |
| **The parent's "only open handle" wording** (§3.1) | `docs/2652-engrammanager-design.md:677` literally says one handle; (b) is two. Read as process-scoped by both author and reviewer — but **the words need clarifying by that document's owner**, and this slice does not edit it. |
| **vec0 detection at registration** (§3.5) | The rule is decided. Detection is measured for FTS5 (author, Darwin) and for vec0 on one runtime (Mira: Linux, SQLite 3.45.1, sqlite-vec 0.1.9 — direct, alias and view all surface). Not established for every module or platform. The schema route is authoritative; the token-match backstop can miss quoting or view indirection, so it is not a never-under-deny guarantee, and incomplete classification fails closed. |
| **How incomplete classification is DETECTED** (§3.5) | Owed by U4, and it must be **measured**, not asserted. "Fails closed" is only as good as the check that notices a classification is incomplete; the build must show a template whose reads the audit cannot fully account for, and show that registration denies it. (Mira, at approval.) |
| **Two new budgets with no number** (§4) | `SQLITE_LIMIT_LENGTH` and the end-to-end request deadline — including its per-method variant for `snapshot` — both need values chosen against the owner contract (§8). |
| **Tier 2 is DEFERRED, not designed** (§3.2) | U4's own surface is 21/21 byte-stable, so tier 2 is off its path. Outline only; it gets specified by the unit that needs it. Deferring it is deliberate — revision 2 specified it speculatively and that was the larger error of the two in §3.2. |
| **The authorizer's action allowlist is an OBLIGATION, not a finished list** (§3.1) | Owed by U4: derive it from the census plus the owner's operations, and check every denied action for a caller that swallows the denial. `SQLITE_TRANSACTION` is the first known member, not the last. |
| **The Linux peer-credential arm is UNMEASURED by me** (§2.2) | **Partly measured by the reviewer in revision 5** — a same-uid `SO_PEERCRED` read, 12 bytes, correct decode (§6). The foreign-uid rejection is measured by no one. Needs a first-hand run on a Linux seat before U4 builds. Documented is not measured — the whole of §1 exists because that distinction has cost this fleet real rounds. |
| **Cross-uid rejection control** (§6) | Not run. Needs a second uid; the host-account route needs the user's OK. |
| **The 37 unclassifiable census sites** (§3.2) | Unresolved by a call-site instrument. Each must be classified by hand or by a call-path walk before tier 1's registry is called complete. |
| Row-level / per-caller result scoping | Out of scope, named in §3.5 so it is not read as covered. |
| Supervision per platform | Already Kepler's, already open in the parent §7. Untouched here. |

## 8. What is NOT done

- No code. This is the design; U4 is the build.
- No measurement of the registry's cost on the hook path. §6.1's A6 criterion applies to the first
  unit that routes a real hook, and a hash lookup's contribution is unmeasured.
- The build-time scanner is specified as an obligation (§3.3) and not designed. It has the same shape
  as #3095's call-path walk and should reuse it rather than grow a second instrument with its own
  blind spots — and #3095's walk is also what could classify §3.2's 37 unclassifiable sites.
- **Three budgets have no number** — the `progress_handler` deadline, `SQLITE_LIMIT_LENGTH`, and the
  end-to-end request deadline (including its per-method variant for `snapshot`). Each needs measuring
  against real reads and real frames, not choosing.

## 9. What changed, revision by revision, and who found it

Recorded because a reader should be able to see which parts survived attack.

### Follow-up — U4a build findings (Borges, #3194)

| § | change | found by |
|---|---|---|
| §2.1 | **Socket path corrected to `$ENGRAM_HOME/run/engramd.sock`** (the shipped `DEFAULT_SOCKET_NAME`); `owner.sock` was never shipped. | Borges (ruling) |
| §2.1, §5 | **ACLs, not only mode bits.** An inheritable parent ACL survives `chmod 0700` on `run/` and reaches the socket. The owner strips ACLs on `run/` through an `O_NOFOLLOW` fd before the bind and sets the socket mode explicitly. Test 4 now asserts on the ACL, with a fixture parent that carries one. | Borges (Linux measurement, requirement); Aleph (Darwin measurement) |

### Follow-up — U4b build findings, revision 6 (Borges's ruling, engram-alpha#3221)

| § | change | found by |
|---|---|---|
| §3.1, §5 test 17 | **The BEGIN/READ/COMMIT requirement never applied to the owner RPC.** Revision 3 withdrew revision 2's "uses the backup API" answer by citing code that belongs to a *different* function — `engram_core.py`'s per-turn checkpoint (`_commit_snapshot_async`/`_commit_snapshot_sync`), not the owner RPC (`engramd_owner._handle_snapshot`) this section is about. `sqlite3.Connection.backup()` never issues a `BEGIN`/`SELECT`/`COMMIT` and never consults an authorizer — measured independently three times. Test 17 rewritten against three properties the owner RPC actually owes (never touches the client-read connection/authorizer; a copy failure is loud; structurally unreachable from client SQL) plus the authorizer-blind fact, instead of a mutation that cannot fire. The checkpoint function's own read-mark `except Exception: pass` is real and stays open as its own issue. | Mira (CHANGES_REQUESTED on PR #3221, naming the scope collision); Borges (ruling: defect traced to this document's own revision 3, not the build) |

### Follow-up to revision 5 — two overclaims Mira flagged at approval

| § | change | found by |
|---|---|---|
| §3.5, §7 | **"Never under-deny" withdrawn** for the token-match fallback: a lexical match can miss quoting or view indirection. The audited schema route is authoritative, and incomplete classification fails closed. Mira's Linux vec0 control folded into the scope note. §7 gains an open item: the build must measure how incomplete classification is detected. | Mira (claim, vec0 control, open item) |
| §4, §5 | **`SQLITE_LIMIT_LENGTH` scoped precisely**: it caps a single string, blob or row, not aggregate memory. Table row, prose and test 19's title corrected. | Mira |
| §4, §5, §7, §8 | **Deadline scope extended to cover execution, not only framing.** `snapshot()` runs under the same monotonic end-to-end deadline, as a per-method budget chosen once the method is parsed; on overrun the owner stops the work rather than the wait — no success response, lock released, destination left untouched. Test 12 requires the `progress_handler` to share the request deadline rather than an independent timer; test 20b added, with the lock-release proof restated as a second `snapshot()`, not a `ping`. Stop-step allowance must be named and bounded by the test, not left open-ended. Snapshot destination staged and atomically replaced; abort leaves a pre-existing destination byte-identical (it previously read as deleting it). | Mira (security pre-review, #3194); Borges (ruling); Mira (stop-step bound, PR #3200 security read); Mira (destination deletion, #3201 security read) |
| §4 | **`snapshot()`'s deadline split so timeout responses can reach the client.** Round 5's finalize protocol composed every timeout-driven response AT the request deadline, so the (correct) send gate dropped all of them and a real client only ever saw `Unavailable`. Execution now stops at `request deadline − SNAPSHOT_RESPONSE_RESERVE` (measured on the owner's own socket); the send gate is unchanged. Client contract stated: for `snapshot()`, `Unavailable` is read as indeterminate, and the client-visible form of the late-rename residual is `commit: "indeterminate"` or `Unavailable`. The bounded-finalize paragraph (protocol and three residuals) is now recorded in §4. | Sol (finding, deterministic 5/5); Borges (ruling); Mira (send-gate finding, on the round-5 protocol) |

### Revision 5 — answering Mira's review at `0d3aecf8`

| § | change | found by |
|---|---|---|
| §3.5 | **Safe set re-keyed from operator spelling to SCHEMA.** A plain `=` on FTS5's table-named column is a query expression; revision 4 admitted it, and also admitted vec0's `k = ?`, which the same section had called a plan control. Now: any template touching a virtual table has no implicit safety. | Mira (FTS5 probe, Linux); re-run on Darwin by me with two added controls |
| §3.5 | **Registration mechanism measured**: an auditing authorizer's `SQLITE_READ` events, crossed with `sqlite_master`, detect virtual-table templates without a SQL parser. Measured for FTS5; vec0 unverified because vec0 cannot load on my seat. | me |
| §4 | **Resource bound split into three axes.** `zeroblob(8 MB)` returns with 0 progress-handler callbacks; `SQLITE_LIMIT_LENGTH` refuses it (measured); the per-op socket timeout from my 18:13Z comment folded as a requirement, not a note. | Mira (`zeroblob`, fold request); me (control, `setlimit`, socket numbers); Borges (the socket class) |
| §3.1 | **Connection question DECIDED (b); `snapshot()` ANSWERED** as an owner-internal quiesced path. The parent's "only open handle" wording flagged to its owner, not edited here. | Mira's ruling |
| §2.2, §5 | **Test 3's mutation withdrawn** — it cannot go red with the mismatch rule intact — and the §2.2 claim that the uid-0 rule "actually catches" the Darwin case withdrawn with it: the clause changes no outcome in either owner configuration. | Mira (test 3); me (extending it to §2.2) |
| §5 | Tests 19–22 added (allocation, end-to-end deadline, virtual-table negative cases, routing); test 18 retired with option (a). | me, from the above |
| header | The header still read **"Revision 3"** while §9 already logged a revision 4 — a stale self-label, corrected. | me |

### Revision 4 — the two entries that matter are both my own corrections of revision 3

| § | change | found by |
|---|---|---|
| §3.5 | The defence was **incoherent**: it asked an implementer to decide membership of a set this section had just declared unenumerable. Replaced by an **inversion** — a closed SAFE set of placeholder positions, default-deny everything else, which works *because* tier-1 templates are registered at build time. The open set never has to be enumerated. | reviewer |
| §3.5, §7 | The FTS5 flagship was the **wrong site**. The hook's `:1579` reads an attached **package** DB (out of scope per the parent's `:849`) — but my own plan then concluded "so demote the row", and the census refutes that: `viz/data/graph.py:54` is a live-store read from a separate process on an HTTP query string. The item **stays a blocker**; the flagship moves. | me, running the census before writing the demotion |
| §3.5, §7 | Named the **caller chain** that decides viz's store, because the callee takes `db_path` as a parameter and is silent on it by construction — the ambiguity that produced the wrong verdict in *both* directions on one day. | me |
| §2.2 | Pinned the peer struct to native `@` and **76** bytes (`=` gives 74), and stated what the uid-0 rule actually catches: the Darwin wrong-level call, with no known Linux trigger. | me |
| §5 | Test 15's "assert that adding a case is cheap" is not testable. Replaced with **one table serving as both the validator's rules and the suite's cases**, so a rule without a case fails the build. | me |
| §0 | Cut the retrospective framing from the measurement caveat, keeping the caveat's information. | me, applying §9's own criterion |

### Revision 3 — every entry is a claim this document made and had to withdraw

| § | change | found by |
|---|---|---|
| §3.5 | **enumeration DELETED, replaced by a property + open-set declaration.** Revision 2's "data except at a virtual-table boundary" refuted by 5 non-virtual-table cases (`LIKE` wildcard, `LIKE ? ESCAPE ?`, `GLOB`, `json_extract '$'`, `LIMIT -1`), plus `k = ?` as a plan control at revision 2's own counter-example site | reviewer; I re-ran all five |
| §3.5 | the decisive fact: **`engram_filter.py:303-325` already escapes `%`/`_` with the reason in a comment** — in the module tier 2 rests on | reviewer |
| §3.1 | **`snapshot()` answer WITHDRAWN as false** about code cited ten lines above it; constraint stated, answer left open | reviewer; verified at source by me |
| §3.2 | 87% was called an **upper** bound; 9 of 37 unclassifiable are module-level literals, ceiling 87.4%, so it is nearer a **lower** bound. Population reframed as an interval, not a point | reviewer |
| §3.2 | **tier 2 DEFERRED** — U4's own surface is 21/21 byte-stable, so it is off U4's path | reviewer's measurement |
| §4 | the 120.367 s figure marked as having **no platform**, not merely un-run | reviewer |
| §7 | **three divergent FTS5 sanitizers already shipping** named, where revision 2 raised the question and named none | reviewer |
| §0 | "every measurement on a named platform" **withdrawn** — it was false | reviewer |

**The pattern, stated because it is the document's own history rather than an aside:** revision 1
failed by claiming too much, revision 2 failed by claiming too much *while fixing* revision 1, and in
both cases the specific move was replacing a refuted enumeration with a tidier enumeration. The
sibling PR #3070 spent four rounds on the identical move on an unrelated axis. That is why §3.5 now
states a property instead of a better list, and why this revision removes more than it adds.

### Revision 2

| § | change | found by |
|---|---|---|
| §3.2 | byte-exact-everywhere → **two tiers**, on a census | reviewer refuted; census mine |
| §3.5 | limit restated around FTS5 `MATCH`; fourth attack **does** apply | reviewer; control + 6/1 split mine |
| §2.2, §5 test 2 | vacuous mutation replaced; the parse FORM is the requirement | reviewer |
| §4 | pragma allowlist needs `arg2 is None` | reviewer; re-measured by me |
| §4 | **fifth attack**: resource exhaustion | reviewer, their measurement, not re-run by me |
| §3.1 | allowlist demoted to an obligation; `SQLITE_TRANSACTION`; `snapshot()` connection answered | reviewer |
| §3.2 | §3.3's scanner-vs-caller contradiction dissolved | reviewer |
| §2.1 | the cross-kernel reason demoted; TOCTOU carries it | reviewer measured Darwin |
| §0 | probe artifacts replaced with single-run captures | reviewer |

**Survived:** §3.1's two-layer argument, which the reviewer tried to collapse in both directions;
§2.0's threat-model-first structure; and the method — measure, do not reason.
