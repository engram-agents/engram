# EngramManager security slice — read-template registry + socket permission model

**engram-alpha#3097**, the slice `docs/internal/2652-engrammanager-design.md` §3.1 names as its own:
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
open item rather than letting a documented-behaviour claim pass as a measurement. *(As-built, 2026-10-31: the peer-credential cross-uid rejection is now measured on both Linux and Darwin; see §6 and §7. Other platform-specific findings keep their own status.)* *(Green on the
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

**Build scope.** The U4a build (#3194, #3201) implemented the **Linux arm**, and the Darwin arm followed
in #3666 (PR #3703), with build notes in §2.5. On any other platform, on a Linux seat without `setfacl`,
`getfacl` or `SO_PEERCRED`, or on a Darwin seat without `socket.LOCAL_PEERCRED`, `/bin/chmod` or
`/dev/fd`, the owner **refuses to start** (§2.2's posture) rather than running without the strip.

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

### 2.5 Darwin arm — build notes measured for #3666 (2026-10-10/11)

§2.1 and §2.2 already decide the Darwin mechanisms: `chmod -N` for the `run/` strip and `LOCAL_PEERCRED`
for the peer check. This section adds only what the Darwin build needs and those sections do not say.
Every new row was measured on one seat: macOS 26.6.2, Apple M4, the plugin venv's Python 3.12.10, on
2026-10-10/11. Rows that restate §2.1/§2.2 come from that section's own 09-26/09-28 measurements on the
same Mac. Nothing here was measured on another Mac.

**The fd-relative strip on Darwin is `chmod -N /dev/fd/<fd>`.** §2.1 requires the strip to go through the
`O_DIRECTORY|O_NOFOLLOW` fd, never the path. Linux does that with `setfacl -b /proc/self/fd/<fd>`.
Darwin has no `/proc`, but `/dev/fd/<fd>` serves the same purpose. With the fd passed via
`pass_fds`, `chmod -N /dev/fd/<fd>` returned 0 and removed both explicit and inherited entries.
- **Swap control (the property the Linux code relies on):** open `run/` (A), rename it away, create a new
  `run/` (B) with its own entry, then strip through the fd. A was stripped and B was untouched, so the
  strip acts on the fd's target, not on what the path names now. The test could have shown the
  opposite, and was built to tell the two apart.
- `O_DIRECTORY|O_NOFOLLOW` on a symlink to a directory refuses with `ENOTDIR` (errno 20), the same errno
  the Linux docstring records.
- **Open for the build: verify the strip by reading the ACL back.** `chmod -N` returned 0 in every case
  above, and nothing in the Linux arm re-reads the ACL after `setfacl -b`. A strip that reports success
  without verification is the silent-gap shape §2.2 refuses elsewhere. `acl_get_fd_np` and `acl_free`
  are present in libc via ctypes (an fd-native read-back), but that call was NOT exercised. The build
  must measure it, or use `ls -led /dev/fd/<fd>`, and refuse if any entry remains.
  *Resolved in the build (#3666, PR #3703):* `acl_get_fd_np` works as the read-back, and NULL with ENOENT
  means no ACL. `ls -led /dev/fd/<fd>` does NOT show a directory's ACL entry, so it cannot serve. The
  build reads the ACL and the mode back through the strip's fd and refuses with `OwnerAclNotStripped`.
  Measured: `chmod -N` leaves mode bits alone. The Linux arm reads back too since #3698.

**Peer check: two facts §2.2's assertions already cover, stated so the build tests them.**
- With a buffer length of 8, `getsockopt(0, LOCAL_PEERCRED, 8)` returned 8 bytes with no error, so the
  kernel truncates silently rather than refusing. §2.2's "exactly 76 bytes" assertion and the
  `unpack_from` parse form are what catch this; the build's test should pass a short buffer and
  require a refusal.
- **`cr_version == 0` is an added requirement, not in §2.2.** `XUCRED_VERSION` is 0 and the measured
  buffers carried 0. A parser that ignores the field would read a future layout as this one, so the
  Darwin parser must assert `cr_version == 0` alongside the length, and refuse otherwise.
- `cr_ngroups` is capped at 16: this account has 20 groups and `xucred` carried the first 16. The owner
  uses only the uid, so nothing is lost today. **Bound:** no future rule may rely on the group list
  being complete on Darwin.
- `getsockopt(0, LOCAL_PEERPID=2, 4)` returns the peer pid if a later unit wants it for logging. Python
  exports neither `SOL_LOCAL` nor `LOCAL_PEERPID`, so they are literals with a comment.
- Control: the same call on an unconnected socket fails with `ENOTCONN` (errno 57).

**Socket path length: a gap on BOTH arms, found while measuring Darwin.**
- On macOS `bind()` on a 129-byte path fails with `OSError: AF_UNIX path too long`. That failure is
  the only measurement. The Darwin `sun_path` is 104 bytes **per the platform header, not measured
  here**, and the usable boundary (103 vs 104) was not measured. Linux's 108 was not measured either.
  *Measured since:* Darwin's usable limit is 103 bytes (103 binds, 104 fails; macOS 26.6.2, #3666),
  and Linux's is 107 (107 binds, 108 fails; Linux 6.6, #3696).
- The owner has no length check. `_bind_socket_or_raise` translates only `EADDRINUSE`; any other
  `OSError` is re-raised raw. And `start()` runs `_harden_run_dir()` before the bind, so an over-long
  path leaves `run/` created and hardened, then a bare traceback.
- Reach: the owner builds the path only from `engram_home` (`self.socket_path = self.engram_home /
  SOCKET_DIR_NAME / DEFAULT_SOCKET_NAME`, set in `__init__`). The default
  `$HOME/.engram/run/engramd.sock` is 37 bytes on my seat, so only a long `ENGRAM_HOME` hits it.
- **Requirement:** a length pre-check against the platform's limit, with a named refusal, placed
  right after `_check_platform_support()` and computed from `self.socket_path`. It must precede the
  FIRST filesystem mutation in `start()`, which is `self.engram_home.mkdir(...)`, not
  `_harden_run_dir()`. The Darwin build includes it; the Linux half is #3696, against the same
  contract. *As built:* #3696 landed first and holds the one check for both platforms
  (`engramd_client.SOCKET_PATH_MAX_BYTES` plus one reason text, shared by owner and client); #3666
  adds Darwin's measured 103 to that constant rather than a second check.

**The refusal gains a Darwin branch, and the test marker must move with it.** Today
`platform_unsupported_reason()` refuses anything that is not Linux, then checks for `setfacl`, then for
`socket.SO_PEERCRED`. The Darwin branch checks the Darwin prerequisites instead:
`socket.LOCAL_PEERCRED` present, `/bin/chmod` present (called by absolute path, never found through
`PATH`: a GNU-coreutils-first `PATH` resolves a `chmod` that may lack `-N`), and `/dev/fd` present.
It refuses, naming the
missing one, if any is absent. An unrecognised platform still refuses. The `requires_linux_owner` marker
keys on the same function (#3375), and that shared function is what keeps the refusal and the skip in
agreement. The marker should still be renamed (to something like "owner-supported platform"), so the
label says what it now means. It must never be re-keyed on `platform.system()`: #3375 showed a
simulated-Darwin run that patched only `platform.system` missing the `SO_PEERCRED` absence a real
Mac run found.

**Consequence the build must handle: every marked test starts running on a Mac.** Once Darwin passes
the refusal check, the marker no longer skips there. Some marked tests name Linux APIs directly, for
example `tests/test_engramd_owner.py` reads `socket.SO_PEERCRED`. So the build needs a per-test audit
of the marked tests, and a second marker for tests that are genuinely Linux-only (Linux API, `/proc`,
`setfacl`). Tests with their own module-level Linux skip are already safe.

**What Darwin adds nothing to, stated so a reader does not re-derive it.**
- **File flags and xattrs.** `~/.engram` and `knowledge.db` carry no BSD flags, no ACL entries and no
  xattrs on my seat (control: `ls -ldO /` shows `restricted`, so the column can report a flag).
  Darwin's flags (`uchg`, `uappnd`, ...) only restrict, and a different uid cannot set them on this
  directory. That is reasoning, not a measurement of every flag, but no flag grants access.
- **launchd and TCC.** A temporary user LaunchAgent (parent pid 1, the plugin venv's python) listed
  `~/.engram` and read `knowledge.db`'s header with no TCC prompt and no denial. It was unloaded and
  removed after the run. **Bound, not measured:** an `ENGRAM_HOME` under a TCC-protected folder
  (`~/Documents`, `~/Desktop`). Probing it would raise a consent dialog on the person's screen. The
  launcher unit should refuse such a home, or document the Full Disk Access requirement, rather than
  hang on an unanswerable prompt.

**What the Darwin build must be tested against, on a real Mac** (beside §5's cells, which apply as
written):
1. Strip through the fd, with the swap control above: the fd's target is stripped, a replacement at
   the path is not.
2. A strip that leaves an entry behind is refused (read-back), not reported as success. A real
   `chmod -N` never leaves one, so the fixture replaces the strip command with a no-op on a `run/`
   that carries an entry. This cell is mutation-shaped by necessity; say so in the test.
3. Short `LOCAL_PEERCRED` buffer → refusal. Wrong `cr_version` → refusal. Wrong level (`SOL_SOCKET`)
   → refusal, never uid 0 accepted.
4. An over-long socket path → named refusal, with NEITHER `engram_home` NOR `run/` created by this
   start. A test that checks only `run/` would pass with the check wrongly placed between the two
   mkdirs.
5. The refusal and the test marker agree on a host missing each Darwin prerequisite in turn. On one
   Mac this can only be simulated, and per #3375 the simulation must patch EACH primitive the
   function reads (`socket.LOCAL_PEERCRED`, `shutil.which("chmod")`, the `/dev/fd` check), never
   `platform.system` alone.

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

*(As-built, 2026-10-31: the allowlist is built and the swallow check is done; see §3.7 and §7.)*

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
`docs/internal/2652-engrammanager-design.md:677` literally says `SqliteEngramStore` holds *"the only open handle
on `knowledge.db`"*, and two connections are two handles. Revision 3 read that as process-scoped and
called the reading "mine, and the part I am least sure of". Mira's ruling adopts the same reading, so it
is no longer mine alone — but the parent's words still say *handle*, and **that document is not this
slice's to edit.** §7 flags the clarification to its owner rather than resolving it by editing here.

*(As-built, 2026-10-31: the parent's wording was changed by `4bf1f164` (#3198); the paragraph above is
kept as history. See §7.)*

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
owner/client table (`docs/internal/2652-engrammanager-design.md:677`), the reviewer found **most of the 498 is
owner-internal reads the registry never governs at all**; the client-side share lands in an interval
(≈83–91% depending on whether `health/` is counted) rather than on a point. I am reporting the
interval rather than picking its convenient end, and noting that the reviewer's own first reading of
this moved in the opposite direction before their sensitivity check corrected it.

### The scope finding that shrinks this section

⚠ **Corrected after Sol's U4c re-measure (engram-alpha#3196, dev `8c52a989`).** Revision 3 said:
*"For U4 — the unit this whole design blocks — the read surface is 19 literal, 0 f-string, and 2
unclassifiable, both of which are module-level literals. 21 of 21 byte-stable."* That figure counted statements written **inside the hook files**,
and I had relayed it without re-running it. As a statement of U4's read surface it was wrong in three
ways:

- **The 21 are 19 SELECTs + 2 PRAGMAs.** One PRAGMA is `journal_mode=WAL`
  (`engram-utility-credit-mention-stop.py:153`), a mode *write* on a read-write connection. One
  SELECT (`engram-surface-hook.py:1573`) reads the package DB, which §3.5 rules out of scope.
- **A read registry governs what the hooks REACH, not what the hook files contain.**
  `engram-lesson-tripwire-hook.py` imports `engram_core.generalization_chain` (`:324`) and calls it
  on the hook's own connection (`:460`). Walking U2's call graph from the hook entry points, the hooks reach **30 SELECTs, 2 of
  them f-strings** (`engram_core.py:1530` and `:1541`, both `WHERE id IN ({_ph})`).
- **That 30 is "found by this instrument", neither an upper nor a lower bound.** U2's edges match
  calls by bare name (which over-reaches) and produce no edge for attribute calls (which
  under-reaches). Which effect dominates is not measured.

**What survives:** both f-strings are variable-arity `IN`, which this section already assigns to
tier 1 through `json_each` (below). **What does not:** "0 f-string" and "21 of 21". The two sites
must be rewritten before U4c's real-tree scan can pass. That rewrite is its own precursor unit
(U4c-0), not folded into U4c, because `engram_core` is shared with the write path and the change
needs its own equivalence test, the empty list included (#3196).

**So tier 2 is still not on U4's path.** It was revision 2's largest new surface and the unit that needs it
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
  depth, not the boundary — and §7 records that three other sites do it incompatibly. *(As-built, 2026-10-31: the owner admits no FTS5 read, so these sanitizers stay outside this slice; §7, FTS5 row.)*
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
`docs/internal/2652-engrammanager-design.md:849` rules **out of scope**. So that site is not a read this socket
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

*(As-built, 2026-10-31: closed by deny. The registry refuses every virtual-table read, so no FTS5
expression reaches an owner-side policy. See §7, first row. The paragraph above is kept as history.)*

Row-level / per-caller result scoping remains out of scope, and is a *different* limitation from the
one above — revision 1 conflated the two.

### 3.6 Typed-op input bounds

**Typed-op input bounds.** An owner op may **clamp** an out-of-range input only when it is an upper bound on magnitude **and the reply already reports what the clamp cut off**: the chain op's `max_depth` above `CHAIN_MAX_DEPTH_CAP` qualifies, because any cut it causes comes back as `truncated: true`. Every other out-of-range input is **refused by name**, before any statement: a wrong type, a negative value, an unknown enum member, a duplicate, and **any over-limit list**, because clamping a list silently drops an item the caller asked for and nothing in the reply says so. A new op that wants to clamp names the reply field that reports the cut; if it can't, it refuses.

### 3.7 The authorizer swallow sweep (as-built, 2026-10-31, #3097)

This is in §3, not §5, because it discharges the §3.1 obligation: check every action the authorizer denies for a caller that swallows the denial. §7 points here.

**What it is.** A read-only analysis of dev `b7f963f3`. No code was changed. It follows a denial from the authorizer to every caller of the client-read path. It asks one question per caller: does the caller surface the denial, or turn it into an empty or default answer? The owner leg (3.7.2) was also run once, in a scratch script that is not committed. Each caller was read in the code at the cited line. No caller was run against a live owner in this pass.

**Terms.** The *client-read path* is the four owner methods that run on `_client_read_conn`: `read`, `df`, `chain` and `confidence_by_type` (method table `src/engram/engramd_owner.py:2981-2986`; handlers `:5294`, `:5391`, `:5446`, `:5521`). `ping` touches no store (`:4583`). `snapshot` runs on `_owner_conn` (`:5587`, `.backup()` at `:5873`). Neither is on the client-read path.

**3.7.1 What the authorizer denies.** `_client_read_authorizer`, `src/engram/engramd_owner.py:1158-1193`:

| condition | answer | pointer |
|---|---|---|
| action `SELECT`, `READ` or `FUNCTION` | OK | `:1132-1138`, `:1165-1166` |
| `PRAGMA` with a value (arg2 is not None), a write form | DENY | `:1177-1178` |
| `PRAGMA` with no value, name `user_version` or `data_version` | OK | `:1155`, `:1179-1180` |
| `PRAGMA` with no value, any other name | DENY | `:1181` |
| `TRANSACTION` | DENY | `:1183-1191` |
| every other action code (the writes, the DDL, `ATTACH`, `DETACH`, `SAVEPOINT`, `RECURSIVE`, and the rest) | DENY, by the final default | `:1193` |

The function never returns `SQLITE_IGNORE`. Measured: action codes 0 to 39, six argument shapes, no result other than OK or DENY. This matters because IGNORE would turn a denied column read into NULL inside the owner. On this seat an IGNORE authorizer made `select a from t` return `[(None,)]`. The owner leg cannot do that. The codes in the last row are not each tested; the default `return sqlite3.SQLITE_DENY` is what denies them.

**3.7.2 How a denial travels.**

1. `_client_read_authorizer` returns DENY. SQLite does not prepare the statement.
2. Python raises `sqlite3.DatabaseError`, message "not authorized", `sqlite_errorcode` 23 (`SQLITE_AUTH`). It is not an `OperationalError`.
3. `_ClientReadGate._execute_admitted` (`:1689-1702`) lets it through and clears the progress handler in its `finally`. `_ClientReadGate.execute` (`:1583-1612`) converts only an `OperationalError` with `sqlite_errorcode == 1` into a `Rejected`; this error is neither, so it propagates. `execute_typed` (`:1614`), `run_chain` (`:1630`) and `run_confidence_by_type` (`:1667`) catch nothing.
4. `EngramdOwner._run_client_read` (`:5351-5389`) catches it at `except sqlite3.Error` (`:5382`). An `OperationalError` would be caught at `:5374` and end in the same place, unless it is an interrupt.
5. `_read_failed` (`:5577-5585`) logs a warning in the owner log and returns `{"status": "rejected", "reason": "read_failed", "detail": "DatabaseError: not authorized", "remedy": ...}`.
6. The handler returns that dict as the reply: `_handle_read` `:5329-5330`, `_handle_df` `:5439-5440`, `_handle_chain` `:5497-5498`, `_handle_confidence_by_type` `:5563-5564`.
7. On the client, `engramd_client._call_mapped` (`src/engram/engramd_client.py:965-972`) maps `status == "rejected"` to `Rejected(reason="read_failed", ...)`.
8. The shim `OwnerReadConnection` raises `OwnerReadError("rejected", result)`: `execute` `:1225`, `generalization_chain` `:1262`, `confidence_by_type` `:1297`. A hook that calls `ec.read("read", ...)` itself gets the `Rejected` object.

**Owner-side result: no path was found where a denial becomes an empty row set or an ok-with-nothing.** Evidence, measured in a scratch script on this seat (macOS arm64, Python 3.12.10, SQLite 3.49.1). It ran the real `_client_read_authorizer`, `_ClientReadGate._execute_admitted` and `EngramdOwner._run_client_read`:

| statement through the owner tail | answer |
|---|---|
| `insert`, `pragma user_version=5`, `pragma journal_mode`, `begin`, `create table`, `attach database` | each: `rejected`, `read_failed`, detail `DatabaseError: not authorized` |
| `pragma user_version`, `select id from nodes` | rows, no failure |

The typed walks add no catch of their own. `generalization_chain` (`src/engram/engram_core.py:2437-2730`), `_resolve_to_current` (`:2414`), `_current_successor` (`:10665`), `dag_edge_set` (`:2227`), `confidence_by_type` and `_confidence_by_type` (`src/engram/engram_stats.py:133`, `:98`) were read; none has a `try` around its SQL. Not read: `core._get_current_turn()`, called in `run_confidence_by_type` (`:1685`); it reads `config.json`, not the store.

Two limits. Other Python and SQLite builds were not run. And **no committed test feeds an authorizer DENY through `_run_client_read`** (a grep of `tests/` for "not authorized" and "prohibited" found two unrelated hits). The committed cells feed other SQLite errors through the same tail: `tests/test_engramd_owner_read_3197.py::test_o7_oversized_cell_is_a_loud_rejection_not_a_dropped_connection` (`:313`, a `DataError`) and `tests/test_engramd_chain_3740.py::test_chain_sqlite_errors_map_to_their_named_reasons` (`:810`). The gap is named here. Filling it is a code change, outside this doc-only pass.

**3.7.3 How the callers were enumerated.** Run at the repo root on dev `b7f963f3`. Counts are lines, then files; they include docstring and comment mentions.

```
git grep -n -E 'OwnerReadConnection' -- src tools                    # 15 lines, 7 files
git grep -n -E '(ec|client|engramd_client)\.read\(' -- src tools     # 12 lines, 8 files
git grep -n -E 'open_reader\(' -- src tools                          # 14 lines, 10 files
git grep -l -E 'engramd_client' -- src tools                         # 20 files
git grep -n -E '_via_owner' -- src tools                             # 127 lines, 29 files
git grep -n -E 'owner_read\.(ping|snapshot|write)\(' -- src tools    # 14 lines
git grep -n -E '(ec|client|engramd_client)\.write\(' -- src tools    # 3 lines
git grep -n -E "\"method\": *\"(read|df|chain|confidence_by_type)\"|'method': *'(read|df|chain|confidence_by_type)'" -- src tools   # 0 lines
git grep -l AF_UNIX -- src tools                                     # 8 files
```

How each was used. `OwnerReadConnection` finds the shim and its users. `.read(` finds direct calls: of the 12 lines, 6 are `ping`, 1 is `snapshot`, 3 are docstrings, and 2 send `read` (`engram-stop-hook.py:574`, `engram-user-identity-hook.py:227`). `open_reader(` finds the tool opener and its nine callers. The 20 files that mention `engramd_client` are the superset of files that can talk to the owner. Each is the owner itself (`engramd_owner.py`), is in a row below, is in the exclusion list, or has no hit in the first three greps. The last group is five files that only name the module (`src/build/packaging/tiers.json`, `engram_home_resolver.py`, `engram_pidfile.py`, `hooks/claude/_hooklib.py`, `tools/engramd_read_scan.config.json`); they were grepped, not read through. The `_via_owner` names map flags to files: the read flags are the `*_reads_via_owner` keys, `regen_embeddings_via_owner` covers one tool's reads and writes, and `*_writes_via_owner` are writes. The raw-method grep looks for a client that writes the request itself and so bypasses the shim: none (0 lines; the same pattern finds raw requests in `tests/`, so it can match). Of the 8 `AF_UNIX` files, two are the owner and its client. The other six talk to the recall daemon (`engram-lesson-tripwire-hook.py`, `engram-surface-hook.py`, `engram-surface-daemon.py`, `start-engram-daemon.sh`), the Codex app server (`codex-umwelt-wake.py`), or only name the socket in a comment (`tools/engramd_stage_profile.py`). None sends a client read to the owner.

**3.7.4 The table.** Every denial reaches every caller as the same answer: `Rejected(reason="read_failed")`, because all four methods end in `_run_client_read` (3.7.2). So there is one row per caller, not one per denial. The callers do not branch on the reason, with these exceptions, each stated in its row: `tools/_owner_read.py` words its message by reason; `_check_db_liveness` in the surface hook reads two reasons; `OwnerReadConnection.execute` reads one reason of `df`.

| caller (file:function:line) | what it does with a non-Ok answer | verdict |
|---|---|---|
| `src/engram/engramd_client.py` `OwnerReadConnection.execute` `:1178-1226`, `.generalization_chain` `:1228-1263`, `.confidence_by_type` `:1265-1298` | Raises `OwnerReadError` for Unavailable, Rejected, an unusable answer, a client crash and an exhausted budget. Two empty cursors exist. One is sent when no valid `df` term is left, with no round trip (`:1197-1198`). The other is `df` + `vocab_table_absent` (`:1219-1224`); the owner sets that reason only when `sqlite_master` has no row for the vocab table (`engramd_owner.py:5417-5427`), a fact about the store, not a denial. A denial of that `sqlite_master` read would be `read_failed` and would raise. | SURFACES |
| `src/engram/tools/_owner_read.py` `_OwnerReader.execute` `:256-260`, `failure` `:181-217` | Turns every `OwnerReadError` into the tool's own typed error, with reason, detail and remedy. `ping` (`:224-242`) and `snapshot` (`:355-430`) are not on this path. | SURFACES |
| `src/engram/engram_idf.py` `extract_keywords` `:231-315`, `idf` `:187-228` | No `try` around its statements; the error passes up. | SURFACES |
| `src/engram/tools/anchoring_report.py` `_fetch_population` `:188` (handlers `:205-209`), `main` `:343-371` | The typed error is a `RuntimeError`, so the `except sqlite3.Error` at `:205` does not take it. `main` prints `ERROR: ...` to stderr and returns `OWNER_READ_FAILED_EXIT`. Nothing is printed before every page is in. | SURFACES |
| `src/engram/tools/quote_quality_survey.py` `main` `:775-791` (uses the reader above) | Prints one stderr line, exits 3. | SURFACES |
| `src/engram/tools/cohort_dispatch.py` `_fetch_nodes` `:200-227`; `cmd_prepare` `:610`; `main` `:1843-1848`; `cmd_verify_in` `:875-893` | `prepare`: `CohortOwnerReadError` is not caught in `cmd_prepare`; `main` prints `error: ...` and returns 1. `verify-in`: recorded as a `db_access` error with `db_nodes = None`; the run ends `passed: False` and returns 1 (`:966-969`). | SURFACES |
| `src/engram/tools/dream_master_batch.py` `_with_reader` `:726-741`, `main` `:760-777` | `except Exception`: one `<Class>: <message>` line on stderr, nothing on stdout, returns 1. | SURFACES |
| `src/engram/tools/engram-regenerate-embeddings.py` readers `:335-377`, `_owner_run` `:474`, `_backfill_via_owner` `:462-471` | `RegenOwnerError` goes to `_owner_fail`, exit 3. | SURFACES |
| `src/engram/tools/inspect_raw.py` `read_row` `:148-210`, `_get_row` `:213-236` | With the owner on, a `sqlite3.Error` is re-raised, not wrapped; the typed error reaches `_get_row`, which prints it and exits `OWNER_READ_FAILED_EXIT`. | SURFACES |
| `src/engram/tools/lesson-pattern-audit.py` `read_lesson_rows` `:206-228`; callers `run_audit` `:316`, `run_view_comparison` `:397`, `run_write_ranks` `:516`; `main` `:608-613` | One stderr line, returns 1. Nothing is written: every store read comes before the rank file is touched. | SURFACES |
| `src/engram/tools/reconcile_lesson_demotions.py` `find_candidates` `:257-336`, `main` `:338-352` | `ReconcileError` becomes `{"error": ..., "candidates": null}` on stdout and `sys.exit(1)`. | SURFACES |
| `src/engram/tools/study_queue.py` `read_store_candidates` `:397-423`, `_weakest_candidates` `:426`, `build_queue` `:444`, `main` `:534-554` | `except Exception`: stderr, returns 1. | SURFACES |
| `src/engram/tools/weakest_load_bearing.py` `read_ranking` `:408-443`, `main` `:469-478` | Same shape: stderr, returns 1. | SURFACES |
| `src/engram/hooks/claude/engram-stop-hook.py` `_read_count_via_owner` `:542-662` (Rejected branch `:609-635`), `_maturity_muted` `:673-753`, `main` `:1034-1037`, `_report_owner_failure` `:979-990` | Raises `_OwnerReadFailure`; `main` emits a `block` decision with reason, remedy and steps. If `stop_hook_active` is true it writes one stderr line instead ("already reported in this stop chain"), by the #3664 ruling. It never returns 0 or "not muted" for a failure. | SURFACES (rate-limited: once per stop chain, by the #3664 ruling) |
| `src/engram/hooks/claude/engram-user-identity-hook.py` `read_people_via_owner` `:198-279` (Rejected branch `:268-279`), `main` `:429-432` | Returns `(None, response)`; `main` emits the response. Never an empty list for a failure (`:199-202`). Emitted on every prompt. | SURFACES |
| `src/engram/hooks/claude/engram-lesson-tripwire-hook.py` six sites: `load_tripwires` `:610-649`; `render_triggered_principles` `:982` (handler `:1151-1154`); `load_prevention_lessons` `:1434-1455`; `check_in_turn_recall` `:2049` (handler `:2242-2247`); focused-ids reads via `_focused_read_conn` `:268-280` and `_hooklib._focused_ids` `:1750-1795` (wiring `:2303-2316`, `:2347-2351`) | Each site returns its own empty result (`[]` or `""`) and records the first failure. `main` adds one notice at the head of the block (`_pending_owner_notice` `:556-573`), at most once per 1800 s (`probe_notice_due`, `_hooklib.py:2154`; `PROBE_NOTICE_WINDOW_SECONDS`, `:2085`). Every run writes a stderr line (`_write_owner_stderr_lines` `:576-591`). The hook's header says the empty result is "never presented as 'no tripwires matched'" (`:169-174`). An empty `load_prevention_lessons` leaves the deny gate fail-open (`:1438-1439`). | SURFACES, rate-limited (3.7.5) |
| `src/engram/hooks/claude/engram-surface-hook.py` `_check_db_liveness` `:655`, `_check_embedding_restore_flag` `:866`, `_dormant_focus_set_candidates` `:1971`, `_render_triggered_principles_section` `:2081`, `_should_prepend` `:2460` (call `:4346-4366`), focused-ids wiring `:4767-4774`, `:4914-4916` | Same pattern: degraded result, failure recorded, one notice per window (`_owner_failure_text` `:537`, `_pending_owner_notice` `:561-584`), stderr line every run (`_write_owner_stderr_lines` `:609-625`). The restore flag reads as unknown (`None`), not healthy (`:944-962`). `_check_db_liveness` treats a `read_failed` as healthy only when its detail contains `no such table: embedding_restore_state` (`:701-702`); a denial's detail is `DatabaseError: not authorized` (3.7.2), so it does not match. | SURFACES, rate-limited (3.7.5) |
| `src/engram/hooks/claude/engram-session-start-hook.py` `_check_backup_staleness` `:783` (read `:808-824`), `_check_embedding_restore_flag` `:858`, `focus_block` `:1684`, `saved_focus_sets_block` `:1930`, `session_checkpoint_block` `:2116`, `sleep_status_block` `:3068`, calibration in `main` `:4351-4391` | Same pattern; one notice per window (`_pending_owner_health` `:431-451`), stderr line every run (`_write_owner_stderr_lines` `:453-468`). `_check_backup_staleness` turns a failed count into `_count = 0` and returns `(True, None)`, i.e. no backup warning, with the failure recorded (`:823-824`). The cold-start retry (`_owner_read` `:357-372`) applies to `Unavailable` only. | SURFACES, rate-limited (3.7.5) |

Not on the client-read path, so not rows: `tools/backup_knowledge_db.py` (`ping` `:621`, `snapshot` `:638`), `src/engram/tools/engram-pkg/engram-pkg` (`snapshot` `:1173`), `src/engram/tools/engram-fix-git-backup.sh` (`snapshot` through inline Python, `:150-176`); the `ping` probes (`hooks/claude/_engramd_owner_hook_io.py:237`, `engram-session-start-hook.py:335`, `engram-surface-hook.py:637`, `tools/engramd_a6_latency.py:628`, `tools/engramd_stage_profile.py:358`); the owner writes (`engram-utility-credit-mention-stop.py:290`, `engram-surface-hook.py:381`, `tools/_owner_read.py` `write` `:433`). `tools/engramd_a6_latency.py` and `tools/engramd_stage_profile.py` drive the hooks above and add no caller. `tools/engramd_lockwait_report.py` reads files, not the socket. No verdict is FALLS-BACK-BY-DESIGN: no caller falls back to a direct read on a refusal; the flag-off path is a separate configuration, chosen by the operator.

**3.7.5 Swallowing callers found.** None that hides a denial without a trace: every caller either raises, prints, exits non-zero, emits a visible block, or records the failure and shows it at least once. Four callers are quiet on stdout for a repeat: the three hooks below, inside their 1800 s window, and the stop hook, after its first block in a stop chain. The commands in 3.7.3 are the evidence for the enumeration.

One class needs a plain statement, because the test is "turns the denial into an empty or default answer". Three hooks (tripwire, surface, session-start) do that for the failing site, by design: the site returns its empty or fallback result, the first failure is recorded, and the fire shows a notice. That notice is limited to one per 1800 s window on stdout. **What the window means:** inside 1800 s a repeated denial is silent on stdout, and present on stderr (every run) and in the owner log's warning (`_read_failed`, 3.7.2); the hook's own failure record lives in memory for that one fire only. So inside the window stdout alone looks like "nothing matched" or "nothing to report", and a hook's stderr is not what the agent reads. The notice itself names the result as missing, not empty. The surface hook's wording (`_owner_failure_text`, `engram-surface-hook.py:537`): "This prompt's <blocks> could NOT be read (the read failed; it is NOT empty). Do not open knowledge.db directly to work around this." The hook headers state the degrade-and-notify behaviour as intended (tripwire `:169-174`; surface `:214-219`; session-start `:113-116`). **Ruled SURFACES (rate-limited)** by the design seat (Borges, 2026-10-31, in a Workshop world crossing, not on GitHub): every denial is recorded and printed to stderr on every run; the agent notice says missing, not empty; repeating it on every fire would break the #2863 injection budget; and the tripwire deny gate's fail-open on an empty prevention-lesson read is a separate, deliberate choice that the notice states.

A latent form: `_hooklib._focused_ids` (`:1750-1795`) returns `(set(), False)` and says nothing when called with an owner `read_conn` and no `on_failure`. Every present caller passes one (`engram-lesson-tripwire-hook.py:2313`, `:2351`; `engram-surface-hook.py:4774`, `:4916`), so nothing swallows today. A second latent form: `engram_core._sanitize_fts_query` (`engram_core.py:7058-7062`) wraps `extract_keywords(conn, ...)` in `except Exception` and falls back to the legacy sanitizer with no record. Its callers (`engram_core.py:11021`, `engram_query.py:1757`) pass server-side direct connections, never an owner connection, so it does not swallow an owner denial today.

**Not checked.** A live end-to-end run of each caller against a refusing owner. Other Python and SQLite versions. Callers outside `src/` and `tools/`. The flag-off paths (they do not reach the owner). Whether the 1800 s window is the right figure.

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
   ⚠ **Built twice (§6).** The real-socket cell
   `tests/test_engramd_owner.py::test_peer_uid_mismatch_is_rejected` uses a uid-0 peer (passwordless
   `sudo`: Linux CI runners only), and a uid-0 peer cannot tell the mismatch rule from the uid-0
   clause, so **that cell alone** does not kill the stated mutation. **The suite does**, through the
   fake-credential cells on each arm that feed a non-root foreign uid (`own + 1`) — named in §6.
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
    template registered for `public_notes`. (P14.) *(As-built, 2026-10-31: settled as deny; the registry refuses the template outright, `tests/test_engramd_read_registry.py::test_16_fts5_match_template_is_refused_registration`.)*
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

## 6. The cross-uid control — what now runs, and what it cannot show

**Status, 2026-10-16 (this section was stale; corrected here, #3842).** The text below, from
revision 5, said the cross-uid rejection test "has not been run". That stopped being true when the
build landed. What runs, and what it can and cannot show:

**Updated 2026-10-31 (#3097):** the Darwin real-socket control has now run too (last bullet below). No control in this section is still unrun.

- **The CI cell.** `tests/test_engramd_owner.py::test_peer_uid_mismatch_is_rejected` (added by
  PR #3201 as an unconditional skip; made to run where `sudo -n` works by PR #3421, closing #3197)
  connects a real foreign peer, **uid 0 via passwordless `sudo`**, after a same-uid control that is
  served. It PASSED in the main full suite (`tests.yml` run 38829935115 on `main`, 2026-10-16), and
  it FAILED on the scratch branch `scratch/3197-crossuid-mutant` on 2026-10-05 (run 37316689182: the
  uid-0 client was served `Ok`), so it has been seen to fail. It runs on Linux CI runners only;
  agent seats skip it (no passwordless `sudo`).
- **A live run.** Lei ran a uid-0 control against a live owner on 2026-09-29 (`Rejected
  peer_identity`, recorded on #3201).
- **The bound, documented at the cell.** `_check_peer_credentials` has two rejection branches: the
  uid **mismatch**, and **uid 0 with a non-root owner**, with identical `peer_identity` reason and
  `detail`. So a uid-0 peer is refused even if the mismatch rule is deleted, and the uid-0 cell
  **cannot show that the mismatch rule works**. This is already stated in the comment above
  `test_peer_uid_mismatch_is_rejected` (written 2026-10-05). The Darwin arm folds both conditions
  into one `if`, so the same holds there.
- **The mismatch rule IS tested, at unit level, on both arms**, by cells that hand the check a
  manufactured credential carrying a non-root foreign uid (`own + 1`) and require `peer_identity`:
  Linux — `tests/test_engramd_owner_displacement_3240.py::test_snapshot_dest_rights_premise_peer_uid_equals_owner`
  (Linux-only file); Darwin — `tests/test_engramd_owner_darwin_3666.py::test_peercred_uid_mismatch_is_rejected_peer_identity`.
  The Darwin file is skipped unless `sys.platform == "darwin"`, and every CI workflow runs on
  `ubuntu-latest`, so the Darwin cell runs on Mac seats only, never in CI. These exercise the parse
  and the decision; they do **not** exercise the kernel supplying a
  foreign peer over a real socket.
- **Real socket, non-root foreign peer, Linux: REJECTED (2026-10-16, recorded on #3097).** Borges ran
  an owner as uid 1002 on a throwaway home at dev `3191ab3c` (Linux 6.6.87.2, WSL2), with `run/` at
  0711 and the socket at 0777, so file permissions are not what refuses. Control: the owner's own uid
  was served (`contract_version 1.1`). Arm: Zephyr connected as uid 1008 and got
  `{"status": "rejected", "reason": "peer_identity", "detail": "connecting uid 1008 is not this
  owner's uid 1002", "remedy": "connect to the socket under your own $ENGRAM_HOME"}`. Nothing was
  served, and the listener stayed alive (`ss -xl`). Two runs each; Kepler accepted the result on
  #3097. This is the discriminating control: a non-root peer is refused only by the mismatch rule.
  Two things the run surfaced, both tracked on #3845 and neither a security weakness: the owner closes
  with the request unread, so a client that sends first and then reads to EOF receives the typed
  refusal and then a `ConnectionResetError` on its next read (the shipped client stops at the newline
  and is unaffected); and the owner logs no line on a refusal.
- **Real socket, non-root foreign peer, Darwin: REJECTED, twice (2026-10-17, recorded on #3097).** Peer
  side: Clio, on a second account on the same Mac, with the shipped `engramd_client` from dev `c3222d97`
  (peer uid 504, macOS 26.6.2 arm64, Python 3.12.10; #3097 comment 6227179714). Owner side and control:
  Aleph (owner uid 501, plugin v0.5.3-rc1 `engramd_owner.py`, a throwaway home under `/tmp` holding a
  private copy of the store; comment 6227186043). The home and `run/` were set to 0711 and the socket to
  0777, so file permissions are not what refused. The peer got `Rejected(reason='peer_identity',
  detail="connecting uid 504 is not this owner's uid 501")`, on the shipped client and on a raw
  send-first probe (20 of 20 clean in both runs: the typed line, then EOF, no reset). Nothing was served.
  Control: the owner's own uid was served (`Ok`, `contract_version 1.1`) before and after the peer
  runs, and the owner pid did not change. A non-root peer is refused only by the mismatch rule, so
  this is the discriminating control for the Darwin arm. The owner wrote no refusal line to its stderr,
  as on Linux (#3845). **Limits:** one pair of accounts, one macOS version, one machine, loopback, a
  throwaway owner. The run does not speak to the ACL cross-user connect effect of §2.1: the permissions were opened by hand.

The rest of this section is revision 5's reasoning, kept because it is still the reason the parse
form (§2.2) and test 2 matter. Read its present tense as history: the second-uid control it asks for
has run on both arms, Linux on 2026-10-16 and Darwin on 2026-10-17 (above), so "until one runs"
below no longer describes either arm.

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

**As-built pass, 2026-10-31 (#3097).** Each row below was re-read against dev `b7f963f3`, in code and tests, not against merged-PR titles. A row says what is true there, with a pointer. "Not checked" means not checked. The earlier text of this section (the revision 5 and 6 state) is in git history; §9 records this pass. Where a row still needs someone's call, the row says whose.

| question | state |
|---|---|
| **How an FTS5 `MATCH` parameter is authorised** (§3.5) | **DECIDED in code: deny. No longer open, no longer a blocker.** The registry refuses any template that reads a virtual table or a shadow table of one (R5). Its docstring calls this "this unit's answer to design SS7's open FTS5 policy" (`src/engram/engramd_read_registry.py:38-40`). Cells: `tests/test_engramd_read_registry.py::test_16_fts5_match_template_is_refused_registration` (`:306`), `::test_21_fts5_equality_template_is_refused_registration` (`:298`), `::test_21_fts5_shadow_table_read_is_refused` (`:313`). No owner-side FTS expression policy exists, because no FTS read is admitted through the owner. Run in this pass: `tests/test_engramd_read_registry.py` and `tests/test_engramd_read_scan.py`, 257 passed (macOS arm64, Python 3.12.10, SQLite 3.49.1, one seat). **Residual, stated and not solved here:** the viz search still runs an FTS5 `MATCH` on a live store directly: `src/engram/viz/data/graph.py:53-61` (`MATCH ?` at `:56`), through `src/engram/viz/data/sqlite_ro.py:21` `connect_ro`. Its sanitizer (`graph.py:41-49`) strips non-word characters and appends `*`; it does not quote. That is the migration ledger's row 76, state `direct`, "no owner path planned yet" (`docs/internal/2652-migration-ledger.md:121`). It is the ledger's business, not this slice's. The old row named two more sanitizers; by function name they now sit at `src/engram/engram_core.py:7041` (`_sanitize_fts_query`) and `:7089` (`_sanitize_fts_query_legacy`), not re-audited. The hook's FTS site reads attached pack DBs (`src/engram/hooks/claude/engram-surface-hook.py:2286-2319`, opened `immutable=1`), out of scope, unchanged. |
| **Which connection serves `snapshot()`** (§3.1) | **Built.** `_handle_snapshot` (`src/engram/engramd_owner.py:5587`) copies with `self._owner_conn.backup(...)` (`:5873`) under `_owner_lock`. Client SQL never reaches `_owner_conn`. The module docstring records the measured fact that `Connection.backup()` never consults an authorizer, on either side (`:197-214`). So `_owner_internal_authorizer` (`:1196`, permissive) has no test that can fail (`:176-184`). |
| **One connection with a swapped authorizer, or two in the one owner process** (§3.1) | **Built as (b): two connections.** `start()` opens `_owner_conn` (`engramd_owner.py:3296-3301`, permissive authorizer) and `_client_read_conn` (`:3308-3310`). The second one's authorizer `_client_read_authorizer` and its `SQLITE_LIMIT_LENGTH` are set there, once; the only other `set_authorizer` calls in the file are on `_owner_conn` (`:3301`, `:4737`, `:4757`). `_ClientReadGate` (`:1559`) holds only `_client_read_conn` (`:3319-3321`). The four client read methods run on it through `_run_client_read` (`:5351`); `snapshot` runs on `_owner_conn`. |
| **The parent's "only open handle" wording** (§3.1) | **Resolved in the parent.** Commit `4bf1f164` (#3198) replaced "holding the only open handle on `knowledge.db`" with "in the one process that holds `knowledge.db` open; that process may hold more than one connection, and does" (now `docs/internal/2652-engrammanager-design.md:681`). Checked with `git show 4bf1f164` and a read of that line. §3.1 below still quotes the old wording, as history. The same phrase survives in the owner module docstring (`engramd_owner.py:3`, "holds the only open handle on `knowledge.db`"); it reads as process-scoped and was not edited here (doc-only pass). |
| **Budgets** (§4, §8) | **Numbered in code.** `CLIENT_READ_LIMIT_LENGTH = 2_000_000` (`engramd_owner.py:1242`; its comment: about 94 times the largest value measured on a real store, 21,272 bytes of `metadata`). `DEFAULT_REQUEST_DEADLINE = 5.0` s (`:368`). `SNAPSHOT_REQUEST_DEADLINE = 30.0` s (`:379`; its comment: a 48.62 MB store backed up in 0.0758 s). `PROGRESS_HANDLER_INSTRUCTION_COUNT = 1000` (`:1231`; its comment: candidates 100, 500 and 1000 aborted within microseconds of the deadline alike, and 1000 has the lowest check overhead). The progress handler has no deadline of its own: it stops against the request deadline passed in (`_arm_read_deadline`, `:1245`), which for all four client-read methods is the request deadline minus `READ_RESPONSE_RESERVE` (applied in the shared `_run_client_read`), 0.25 s (`:474`, `:5359`). **The 5.0 s default is CHOSEN, not measured.** Its comment says only "generous enough" (`:363-367`). Known headroom, from Borges's first-hand run: a whole SessionStart hook fire through the owner, as a fresh process with imports, sequential p99 109.710 ms (A6 S-syn run 6r, 2026-10-31: synthetic 5,010-node store, dev `558eb46f`, installed layout, SQLite 3.53.0, Python 3.14.4, Linux/WSL2, N=300; raw table in #3735 comment 6544788213). One request is part of that fire, so the figure **bounds a request from above. It is not a per-request distribution, and no per-request distribution was measured.** Against 5.0 s that is about 45 times (5000 / 109.7). The p99 was first cited from #3735 comment 6543471311, which prints ratios only; its public source is the raw table in 6544788213, posted for this citation, and the ruling's pointer was corrected in place. Client side: every hook bounds one read below 5.0 s (tripwire, session-start and surface 1.0 s; user-identity 1.5 s; stop 2.5 s: `OWNER_READ_TIMEOUT_S` in each hook), so a hook gives up before the owner's deadline. Tools allow 10 s per call and 60 s per run (`src/engram/tools/_owner_read.py:56-57`), so they can receive the owner's own deadline answer. No new issue unless someone needs to tighten the default (ruling item 3). |
| **vec0 detection at registration** (§3.5) | **The rule is decided and shipped.** Placement is checked against `sqlite_master`, not against a module name; a virtual table or a shadow table is refused (`engramd_read_registry.py:30-40`, R5 and R6b). Measured: FTS5 (the author, Darwin; and the registry cells above, this pass). vec0 only on one runtime (Mira, Linux, SQLite 3.45.1, sqlite-vec 0.1.9: direct, alias and view all surface; §3.5). **Not re-measured in this pass:** no cell in `tests/test_engramd_read_registry.py` or `tests/test_engramd_read_scan.py` mentions vec0 (a grep for `vec0` finds 0 hits; the same grep finds 7 in `src/engram/engram_core.py`), and this seat has no `sqlite_vec` module. Not established for every module or platform. The token-match backstop can miss quoting or view indirection, so it makes no never-under-deny claim. Incomplete classification fails closed (next row). |
| **How incomplete classification is DETECTED** (§3.5) | **Built at two layers, with cells that go red.** Registry: an attached-database read, a temp-table read, an unknown table and an empty table name are unplaceable and refused (`tests/test_engramd_read_registry.py::test_r6b_attached_database_read_is_unplaceable` `:344`, `::test_r6b_temp_table_read_is_unplaceable` `:351`, `::test_r6b_unknown_table_cannot_be_placed` `:357`, `::test_r6b_empty_table_name_read_is_unplaceable` `:389`); a parameter-count disagreement fails closed (`::test_r6a_parameter_count_disagreement_fails_closed` `:333`). Scanner: any reached site it cannot classify is a failure, the CLI exits 1 and writes nothing, and a failed run does not overwrite the committed file (`tools/engramd_read_scan.py:23-37`, R10; `tests/test_engramd_read_scan.py::test_r10_cli_exit_nonzero_and_no_file_written_on_failure` `:224`, `::test_r10_cli_failure_does_not_clobber_an_existing_committed_file` `:234`). Re-run in this pass: both test files, 257 passed. **Limit, from the scanner's own docstring (`:39-49`):** the root set is prescribed, not checked; a read site that no declared root reaches is not scanned at all. |
| **Tier 2 is DEFERRED, not designed** (§3.2) | **Still deferred.** No tier-2 mechanism exists: a case-insensitive grep for `tier.?2` in `engramd_read_registry.py`, `engramd_owner.py` and `tools/engramd_read_scan.py` finds 0 hits (the same grep finds it in this document). The two variable-arity `IN` f-strings the old row named are now the fixed `json_each` form (#3324; `src/engram/engram_core.py:2701-2715`), and the scanner treats an f-string or a variable as a failure (R10). The real-tree drift gate `tests/test_engramd_read_scan.py::test_r12_committed_file_equals_a_fresh_scan_of_the_real_tree` (`:345`) passes in this pass. So no site reached from the declared roots has needed tier 2. Same limit as above: the roots are prescribed. |
| **The authorizer's action allowlist** (§3.1) | **Derived, and swept: see §3.7.** The allowlist is `CLIENT_READ_ALWAYS_ALLOWED_ACTIONS` = `SELECT`, `READ`, `FUNCTION`, plus read-form `PRAGMA user_version` and `PRAGMA data_version`; every other action, `TRANSACTION` included, is DENY (`engramd_owner.py:1132-1193`). It was measured on a representative `SELECT` against the real schema (`:1114-1131`) and widened once, for `data_version`, by a measured ruling (#3665; `:1140-1155`). The authorizer never returns IGNORE. §3.7 traces a denial to the client and lists every caller of the client-read path. Result: no caller turns a denial into an empty or default answer without a trace; four are quiet on stdout for a repeat (§3.7.5). Three hooks degrade the failing site and show a rate-limited notice; the design seat ruled them SURFACES (rate-limited) (§3.7.5). |
| **The Linux peer-credential arm** (§2.2) | **Measured.** A same-uid `SO_PEERCRED` read, 12 bytes, correct decode (revision 5, Mira; §6). A uid-0 foreign peer is rejected in CI (`tests/test_engramd_owner.py::test_peer_uid_mismatch_is_rejected` `:723`) and was rejected live on 2026-09-29, but uid 0 cannot tell the mismatch rule from the uid-0 clause (§6). A real-socket **non-root** foreign peer was rejected on Linux on 2026-10-16 (uid 1008 against a uid-1002 owner; #3097; §6). |
| **Cross-uid rejection control** (§6) | **Measured on both platforms.** Linux: real socket, non-root foreign peer, rejected, 2026-10-16. Darwin: real socket, non-root foreign peer, rejected twice, 2026-10-17 (peer side #3097 comment 6227179714; owner side and control comment 6227186043). The mismatch rule is also unit-tested on both arms with a manufactured non-root credential (`tests/test_engramd_owner_displacement_3240.py::test_snapshot_dest_rights_premise_peer_uid_equals_owner`; `tests/test_engramd_owner_darwin_3666.py::test_peercred_uid_mismatch_is_rejected_peer_identity` `:427`). Limits on the Darwin result: one pair of accounts, one macOS version, one machine, loopback, a throwaway owner (§6). |
| **The 37 unclassifiable census sites** (§3.2) | **No longer the gate.** The scanner walks the declared roots and fails the build on any reached site it cannot classify. `tools/engramd_read_scan.config.json` holds 612 roots and 2 exclusions (counted with `json.load` in this pass). A site that no root reaches is not classified, by design (see the Known holes above). The 37 is the historical P17 count at dev `5001800654bc` (§1). The census was not re-run, and the 37 sites were not classified one by one. |
| Row-level / per-caller result scoping | Out of scope, named in §3.5 so it is not read as covered. |
| Supervision per platform | Already Kepler's, already open in the parent §7. Untouched here. |

## 8. What is NOT done

*(As-built note, 2026-10-31.)* This list was written at design time. Only the budgets bullet was
re-audited in the as-built pass. The others are design-time text: code now exists (U4a to U4d and the
units after them), and the scanner of the third bullet is built (`tools/engramd_read_scan.py`). The
A6 session-start cells (#3735) measure a whole hook fire, not the registry lookup of the second bullet.

- No code. This is the design; U4 is the build.
- No measurement of the registry's cost on the hook path. §6.1's A6 criterion applies to the first
  unit that routes a real hook, and a hash lookup's contribution is unmeasured.
- The build-time scanner is specified as an obligation (§3.3) and not designed. It has the same shape
  as #3095's call-path walk and should reuse it rather than grow a second instrument with its own
  blind spots — and #3095's walk is also what could classify §3.2's 37 unclassifiable sites.
- **Budgets: numbered in code; one is chosen, not measured.** *(As-built, 2026-10-31; this bullet said
  "three budgets have no number".)* `SQLITE_LIMIT_LENGTH` is 2,000,000 (measured against a real store),
  the `progress_handler` stops against the request deadline (its tick is 1000 VM instructions, measured),
  and the end-to-end request deadline is 5.0 s, with 30.0 s for `snapshot`. The 30 s figure carries a
  measured backup time. The 5.0 s default is **chosen**, with a known upper bound on one request
  (whole-fire p99 109.7 ms) and **no per-request distribution measured**. Numbers and pointers: §7,
  "Budgets".

## 9. What changed, revision by revision, and who found it

Recorded because a reader should be able to see which parts survived attack.

### Follow-up — §7 to as-built, and the authorizer swallow sweep (Aleph, #3097)

| § | change | found by |
|---|---|---|
| §7, §8 | **Every §7 row re-read against dev `b7f963f3` and rewritten to what is true there, with a pointer.** The FTS5 blocker is DECIDED (deny, by the registry's R5). The connection and `snapshot()` rows are built, the parent's "only open handle" wording is gone (`4bf1f164`), the four budgets are numbered (the 5.0 s default is **chosen, not measured**; whole-fire p99 109.7 ms bounds one request from above, no per-request distribution measured), the 37 census sites are no longer the gate (the scanner fails closed over 612 declared roots), and tier 2 is still deferred. §8's "three budgets have no number" bullet now says so. | Aleph (audit, #3097 comment 6544409645) found the five stale rows; Borges (ruling, comment 6544421809) set the 5.0 s wording and kept the sweep in this pass |
| §6, §7 | **The Darwin real-socket cross-uid control is recorded as run.** Rejected twice on 2026-10-17 (#3097 comments 6227179714 and 6227186043). Every "not run" / "unrun" for the Darwin arm in §6 and §7 is fixed. | Aleph (audit); Clio and Aleph (the run) |
| §3.7 (new), §7 | **The authorizer swallow sweep.** What `_client_read_authorizer` denies; how a denial travels to the client (`DatabaseError` "not authorized" to `Rejected(read_failed)`, never an empty row set); every caller of the client-read path, enumerated with the grep commands and counts, one row each. No caller silently turns a denial into an empty or default answer. One residual is named: three hooks degrade the failing site and show a notice at most once per 1800 s (§3.7.5). A gap is named too: no committed test feeds an authorizer DENY through `_run_client_read`. | Aleph (audit: no evidence of the sweep was found); Borges (ruling: the sweep stays in this issue) |
| §3.1, §3.5 | One-line as-built notes after the parent-wording paragraph, the allowlist paragraph and the FTS5 "still open" paragraph, so those passages are not read as current. Their text is unchanged. | Aleph |

### Follow-up — Darwin arm build notes (Aleph, #3666)

| § | change | found by |
|---|---|---|
| §2.5 (new), §2.1 | **Darwin build notes, measured on one Mac.** The fd-relative strip is `chmod -N /dev/fd/<fd>` (swap control passed); the strip must be read back, not trusted from its exit code; short `LOCAL_PEERCRED` buffers truncate silently and groups cap at 16; the refusal gains a Darwin branch and the test marker moves with it; a launchd agent reads the store with no TCC barrier. Also found a **socket-path length gap on both arms**: no check, and `run/` is hardened before the failing bind. §2.1 and §2.2 were NOT changed: they already specified the Darwin mechanisms on 2026-09-28, which #3666 at first failed to notice. | Aleph (measurements; the #3666 scope miss is also mine). Round 1 review (reviewer fairy) corrected two over-claims (`ENGRAM_RUNTIME_DIR` never feeds the socket path; 104 is the header value, not measured), placed the length check before `engram_home.mkdir`, added `cr_version`, and made cells 2/4/5 testable as written. |

### Follow-up — U4c re-measure (Sol, #3196)

| § | change | found by |
|---|---|---|
| §3.2 | **The U4 scope finding corrected.** "21 of 21 byte-stable, 0 f-string" counted statements inside the hook files only, and included a WAL-mode PRAGMA write and a package-DB read. By call path the hooks reach 30 SELECTs, 2 of them f-string variable-arity `IN` reads in `engram_core`; that count is an instrument's finding, not a bound. Tier 2 stays off U4's path because both f-strings are tier-1 `json_each` cases; their rewrite becomes precursor unit U4c-0. | Sol (re-measure); Aleph (design owner, correction) |

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
