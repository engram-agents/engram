# name-leak gate: trusted-context run, explicit fork status, coverage attestation

Issue: #4429. This note covers the **CI gate** surface only; it does not close
#2159 (`release-cutover`'s roster skip) or #2163 (`scan-leaks`' roster-less
root resolution).

## The defect

`pr-checks.yml`'s `name-leak-warn` step is advisory by design: it
`continue-on-error`s, always exits success, and sets `skip=true` when
`NAME_LEAK_ROSTER` is unset. It runs on `pull_request` only. On a public repo,
a PR from a fork therefore runs the gate with **no roster**, prints a benign
skip line, and passes — the gate is absent exactly where the risk is, and its
absence is reported as a green tick.

## The design

`.github/workflows/name-leak-gate.yml` is the blocking surface. The decision is
a pure function, `tools/name_leak_gate.py::decide`, and the workflow performs
all I/O and hands it five facts.

- **Same-repo pull request**: the gate runs with the secret. Success requires
  the roster AND a present, matching coverage attestation.
- **Fork pull request**: a **FAILURE** whose status name contains the literal
  token `NOT SCANNED` (message: "not scanned, roster unavailable"). A fork
  never receives the roster secret, so the gate cannot run there. The
  `NOT SCANNED` status name must **never** be configured as a required check.
- **Push to the default branch (`dev`)**: post-merge backstop. Fails loud when
  the roster is missing or the attestation does not match, and files (or
  comments on) an issue labelled `name-leak-gate`, closing it on the next
  green run — the same notify shape as nightly.yml's `ci-nightly` issue, under
  its own label so the two surfaces cannot close each other's issue.

`pull_request_target` is forbidden throughout: it would hand the roster secret
to contributor-controlled code.

### The scan on a pull request

On a same-repo pull request the gate **also scans the PR diff against the
roster** and reports the match count in the job's step summary. The count is
advisory: a match is reported and **does not fail the check**; the decision
(`conclusion`) alone decides the verdict. A scan that cannot run (a missing
base commit, a scanner error) fails loudly instead, so a green tick always
means the diff was examined. The per-match inline PR comment remains the job of
the advisory `name-leak-warn` step in `pr-checks.yml`; the gate reports counts
only, so the two surfaces cannot post duplicate comments.

The push backstop scans the pushed range the same way.

### Outside contributions

Fork PRs are **not scanned**. The supported path is to re-push the fork branch
as a branch in this repository, so the gate runs with the secret before merge.
The fork PR's red `NOT SCANNED` check is the signal that this has not happened
yet, not a thing to silence.

**A re-push makes the branch trusted. It does not make the branch's code
trusted**, and that distinction is the whole of this section.

The job holding the secret executes code. If that code came from the branch,
then re-pushing an outside branch hands the roster to whoever wrote it — the
exposure `pull_request_target` is banned to prevent, reachable through another
door. So the gate reads its own code from the pull request's **base commit**,
which a PR author cannot write:

```
git show <base-sha>:tools/name_leak_gate.py    # executed
git show <base-sha>:tools/name_leak_warn.py    # executed
```

The branch contributes **data only** — its diff and its attestation. The diff is
the thing being scanned; the attestation is a claim, safe to read from the
branch because a mismatch can only fail the gate.

Every `python3` call in the workflow also runs with `-P`. That flag keeps the
checkout off `sys.path`. Measured, both directions: without it `sys.path[0]` is
`''` for a `-` script, so a `json.py` committed at the repository root **is
imported instead of the standard library** in the step that holds the roster;
with `-P` the import resolves to the standard library. This is why the check a
human has to perform is one path and not a list of files — the code that runs
is base-controlled by construction, so nothing the branch adds can be imported,
whatever it is named.

**The one file the branch still controls at run time is the workflow itself.**
GitHub runs the workflow at the pull request's merge ref — that is why the gate
can run at all on a branch that adds it. So the re-push check is:

```
git diff --stat origin/dev...<branch>
```

If anything under `.github/workflows/` appears, **do not re-push it**. A
maintainer reads that change and pushes it. Nothing else on the branch needs
this treatment, because nothing else on the branch is executed.

For a pull request from **inside** the repository, the author already has write
access, and on GitHub Actions write access to a repository implies access to its
secrets (any collaborator can push a workflow that prints one). That case is
trusted by membership, not by this gate, and it is written down here so nobody
reads the section above as a guarantee it does not make.

**Bootstrap, and the branch that had to be added to close it.** The gate tool
is new in the pull request that introduces the gate, so before the first merge
it exists on no protected branch. The step therefore reads its code in this
order:

1. the pull request's **base** commit;
2. **`origin/<default branch>`**, fetched explicitly;
3. the pull request's own copy — only if neither of the first two has the tool,
   and it prints a `BOOTSTRAP` warning when it does.

The second link is not redundancy. An earlier version of this document claimed
the bootstrap branch "is unreachable for every later pull request" because a
base commit can never lose a merged file. That was true of pull requests into
`dev` and **false of every other base**: the workflow has no branch filter, so
a pull request into `main` runs the gate too, and `main` does not carry the tool
until the next release cut. Measured 2026-11-07: `tools/name_leak_gate.py` is
absent on `main` (`69894967`) while `tools/name_leak_warn.py` is present there.
Such a pull request would have sat on the bootstrap path indefinitely, running
its own gate code with the roster. The claim was a clause true of the branch I
checked and extended, in my head, to branches I had not.

With the default-branch link in place the property holds: `dev` is protected, so
a pull request cannot remove the tool from it, and step 3 becomes unreachable
once the gate has merged there.

**Step 3 is entered only on positive knowledge, and that distinction is the last
one in this section.** Before reading anything, both protected refs must
**resolve**. If either does not — a fetch failed, the ref is missing, the remote
is unusual — the step **fails closed** with an error naming the ref. It never
falls back to the pull request's copy because a ref *could not be read*.

The difference matters because the two failures look alike from inside the
script and are not alike at all. "The branch resolves and does not contain the
file" is a fact about the tree, and the only legitimate way into the bootstrap
branch. "The branch does not resolve" is a fact about this clone, and reading it
as the first one is the fail-open shape the whole gate exists to remove — it is
reachable by a network blip alone, with no attacker at all.

**The seat's positive check**, for any re-pushed branch. The run prints the tree
it executed, on stdout and in the step summary, so a seat can read it without
opening the web UI:

```
gh run view <run-id> --log | grep "code read from"
```

The line must name the base commit or `origin/dev`. If it says
`pull request checkout (BOOTSTRAP)`, stop and read the branch by hand. Absence
of a `BOOTSTRAP` warning is **not** the check — silence and a run that never
reached that paragraph look identical, which is the whole reason a positive line
is printed.

**Not this workflow's job, but owed at cutover:** code-owner review on
`.github/workflows/` and the two `tools/` files, so a forged green cannot land;
and GitHub's approval requirement for fork PR workflows, which covers forks but
**not** a re-pushed same-repo branch — a same-repo push is by definition by
someone with write access, so the platform has no reason to gate it. Both belong
on the cutover checklist, and the seat rule above remains the floor either way.

## The contract

```
decide(event, is_fork, roster_present, attestation_present, attestation_matches)
    -> Outcome{conclusion, status_name, reason}
```

| event | is_fork | roster | attest present | attest matches | conclusion | status contains |
|---|---|---|---|---|---|---|
| `push` | either | True | True | True | success | — |
| `push` | either | False | * | * | failure | — |
| `push` | either | True | True | False | failure | — |
| `push` | either | True | False | * | failure | — |
| `pull_request` | False | True | True | True | success | — |
| `pull_request` | False | False | * | * | failure | — |
| `pull_request` | False | True | True | False | failure | — |
| `pull_request` | False | True | False | * | failure | — |
| `pull_request` | True | * | * | * | failure | `NOT SCANNED` |
| any unknown event | — | — | — | — | failure | — |

The named invariant: **`conclusion == "success"` if and only if the event is
trusted AND the roster was present AND the attestation matched. There is no
success case in which the gate did not run.**

**What this bound does not cover, stated next to it because a reader will
otherwise assume it covers everything.** The invariant is a property of the
contract as committed. It cannot hold against a pull request that rewrites the
gate: a branch that edits the job-name expression and `decide()` can produce a
green `name-leak-gate` — the required name — with no roster and no scan. A
workflow cannot defend itself from inside itself. The mitigations are of a
different kind: the base-commit rule below, code-owner review on the gate's own
paths, and a maintainer reading a workflow diff before any re-push. Code-owner
review stops a forged green from *landing*; it does not stop the workflow at a
pull request's merge ref from *running*, which is a separate failure and the
reason the re-push rule is a seat rule rather than a setting.

`merge_group` is deliberately not a trusted event: this repository has no merge
queue (measured: zero `merge_queue` rules, no `merge_group` run has ever
fired), so a merge_group decision would never run. Unknown events fail closed.

**`is_fork` is ignored for `push`**, which is why those rows read `either`. A
push has no head repository, so the flag cannot mean anything there and
`decide()` does not consult it — `decide("push", True, ...)` can succeed. The
table writes `either` rather than `False` so it does not state a constraint the
code does not impose; a reader who implemented this from the old spelling would
have written a check that the gate does not have.

## Where the roster lives at run time

The secret has to reach a file on disk to be scanned. That file is a secret, so
it gets a private location rather than a convenient one:

- the step creates a fresh directory under `RUNNER_TEMP` with `mkdtemp`, so its
  mode is `0700`;
- the roster is created at `0600` **by `open(2)`**, not by a `chmod` afterwards,
  so there is no window in which it is readable by anyone else;
- **nothing in the workflow uses a fixed `/tmp` path.** The scan steps take the
  directory and the roster path from the decide step's outputs.

A fixed path is wrong twice, and both halves were found the same way — by
running the workflow's own script on a host where the assumption did not hold.
On a **shared** machine the name belongs to whoever wrote it first, so the next
user's run cannot write the file at all and fails before reaching any logic. That
is exactly how all five harness cells failed for a colleague on a shared host
while passing on a private one: the cells were right and the path was not. And a
predictable world-readable path would hand the roster to every account on the
box, which is the disclosure the whole attestation design exists to avoid.

I had considered this earlier in the round and dismissed it, reasoning that a
GitHub-hosted runner is ephemeral and single-tenant. That reasoning was about
CI and the file was code: the same script runs on a self-hosted runner, in a
container with a shared `/tmp`, and on a seat. A justification that covers one
of a script's three homes is not a justification for the script.

## Coverage attestation

`.github/name-leak-roster.attestation.json` records `roster_format`,
`pattern_count`, `roster_sha256` and `updated_at`, so a seat that cannot read
the secret can still verify the gate ran against the roster the admin set.

**Hash the WHOLE file, never per pattern.** A per-pattern hash is a membership
oracle: anyone can hash a candidate name and learn whether it is in the roster.
A whole-file hash only confirms the file is the one that was attested.

The canonical roster form is the secret's value with **all** trailing newlines
stripped and **exactly one** appended. A plain `sha256sum` agrees only when the
file happens to end in exactly one newline: with no trailing newline, or with
extra blank lines at the end, it silently produces a different digest. Use the
canonicalising one-liner:

```bash
python3 -c 'import sys,hashlib;d=open(sys.argv[1],"rb").read().rstrip(b"\n")+b"\n";print(hashlib.sha256(d).hexdigest())' roster.txt
# -> roster_sha256 (strips ALL trailing newlines, appends exactly one)

grep -cve '^[[:space:]]*$' -e '^[[:space:]]*#' roster.txt   # -> pattern_count
```

`sha256sum` is GNU (Linux); macOS seats have `shasum -a 256`. The Python
one-liner is the authoritative form on every seat.

`updated_at` is an ISO-8601 UTC timestamp. A malformed or unset attestation is
treated as absent and fails the gate; it never passes.

**Bootstrap note.** The attestation committed with this change carries the
placeholder `roster_sha256` `"UNSET - ..."` because the roster is a repository
secret and cannot be read from a checkout. It must be replaced with the real
hash by a roster holder before the gate can go green; until then the gate fails
loud (which is the intended failure direction).

### Filling or rotating the attestation

Set or change the `NAME_LEAK_ROSTER` secret and let a same-repo run fail on the
resulting mismatch: the `decide the gate verdict` step prints
`roster_sha256=…` and `pattern_count=…` to the job log (`gh run view --log`).
Commit those two values to the attestation (with `roster_format: 1` and a fresh
`updated_at`) and the next run goes green. The hash is public by design — it is
what the committed attestation exists to publish — while the roster never is:
the gate prints no pattern, no roster line, and no substring of either.

## Tests

`tests/test_name_leak_gate.py` is table-driven over every row above with every
wildcard expanded, plus the invariant property over all input combinations and
an AST check that the module performs no I/O and reads no environment.
