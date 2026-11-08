"""File-backed :class:`CoordinationStore` — the concrete fork-1 impl.

The store roots everything at a single ``root`` directory (the forum home), with
two subtrees::

    <root>/projects/<project_id>.md   baton/board items (frontmatter + body)
    <root>/dm/<a>+<b>.md              per-pair DM threads (length-prefixed)

Resolution + portability (spec §"Resolution + portability"): the root is passed
in explicitly (the forum app resolves it once at startup via
:func:`default_store_root`, which honours ``$FORUM_HOME`` and falls back to
``~/.forum``) — never a hardcoded ``/home/agents-shared/`` or username, so a
second install works out of the box and tests point it at a ``tmp_path``.

Every mutation is atomic (write a sibling tempfile, ``os.replace`` over the
target) and embeds the module-assigned ``seq`` so :meth:`FileStore.recover_max_seq`
can rebuild the high-water-mark after a restart. The store NEVER allocates a seq
itself — the module passes it in under the :class:`~forum.coordination.seq.SeqAllocator`
lock (fork-4 co-atomicity); the store's only seq job is to embed it durably and
expose it on reads.

Frontmatter + participant parsing here mirrors ``tools/baton.py`` deliberately.
The two converge when the writer-fn relocation slice moves the ``baton`` write
bodies server-side; until then this is the read/parse half of the same on-disk
contract, kept self-contained so Slice A ships without a cross-package import.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Optional

from .names import InvalidDmPath, InvalidProjectId
from .store import (
    CoordinationStore,
    DmMessage,
    ProjectRecord,
    dm_thread_key,
)

# Statuses a closed project carries — excluded from ``read_projects(active_only=True)``.
# Mirrors baton.py CLOSED_STATUSES (the on-disk contract is shared).
CLOSED_STATUSES = frozenset({"merged", "cancelled"})

# Frontmatter grammar — identical to tools/baton.py so both read the same files.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):[ \t]*(.*)$", re.MULTILINE)
_PARTICIPANTS_LIST_RE = re.compile(r"^\[?(.*?)\]?$")

# DM header line: --- seq=42 from=ariadne to=sol at=<iso> lines=2 ---
_DM_HEADER_RE = re.compile(
    r"^---\s+seq=(?P<seq>\d+)\s+from=(?P<from>\S+)\s+to=(?P<to>\S+)\s+"
    r"at=(?P<at>\S+)\s+lines=(?P<lines>\d+)\s+---\s*$"
)


def default_store_root() -> Path:
    """Resolve the forum home dir the store roots at.

    ``$FORUM_HOME`` wins (the deploy convention — the systemd unit sets it to the
    shared service dir); otherwise ``~/.forum`` mirrors the forum DB default
    (``server.py`` ``--db ~/.forum/forum.db``). The forum app calls this once at
    startup and hands the result to :class:`FileStore`; tests pass a ``tmp_path``
    directly and never call this function.
    """
    env = os.environ.get("FORUM_HOME", "").strip()
    return Path(env) if env else (Path.home() / ".forum")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(fields, body)`` from a frontmatter markdown file.

    Fields are lowercased keys → stripped string values. On no-match / missing
    frontmatter returns ``({}, text)``. Mirrors ``baton._parse_frontmatter``.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict = {}
    for field_m in _FRONTMATTER_FIELD_RE.finditer(m.group(1)):
        fields[field_m.group(1).strip().lower()] = field_m.group(2).strip()
    return fields, text[m.end():]


def _parse_participants(participants_str: str) -> tuple[str, ...]:
    """Parse ``[borges, ariadne]`` / ``borges,ariadne`` → lowercase name tuple."""
    m = _PARTICIPANTS_LIST_RE.match(participants_str.strip())
    inner = m.group(1) if m else participants_str
    return tuple(p.strip().lower() for p in inner.split(",") if p.strip())


def _coerce_seq(raw: str) -> int:
    """Parse a frontmatter ``seq:`` value to int; 0 when absent/garbage.

    A missing or unparseable seq means "pre-migration / never written by the
    module" — seq 0, which sorts below every real mutation and is the documented
    ProjectRecord default.
    """
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return 0


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    """Write ``content`` to ``path`` atomically (tempfile + ``os.replace``).

    The tempfile is created in the SAME directory so ``os.replace`` is a true
    rename (atomic on POSIX), never a cross-device copy. On any failure the
    tempfile is removed so a crash mid-write never leaves a partial sibling.

    The final file mode is explicit per callsite (``mode``, default 0644):
    baton files stay 0644 because every seat reads them off disk; DM threads
    are written 0600 (#4501) because only the forum server reads them.
    mkstemp already creates the temp 0600; the explicit os.chmod below sets
    the final mode on the TEMP file before the rename, so there is no window
    in which the target carries a wider mode than intended.

    Deliberately NOT converted to the shared hooks/claude/_hooklib.py
    atomic_write_text()/atomic_write_json() helpers (gh#2035, #4013): they keep
    an existing file's mode and give a new file the umask / default-ACL mode,
    and take no mode argument, so they cannot pin 0644 for batons or 0600 for
    DM threads -- see the os.chmod call below.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)  # final mode set before rename; mkstemp defaults to 0600
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _embed_seq(content: str, seq: int) -> str:
    """Return ``content`` with its frontmatter ``seq:`` field set to ``seq``.

    Updates the field in place if present (preserving field order), else inserts
    it just before the closing ``---``.

    Raises ``ValueError`` if ``content`` has no frontmatter. The module only ever
    writes well-formed baton/board markdown, so no-frontmatter content is a caller
    bug — and silently returning it unchanged would persist a project the seq was
    NEVER embedded in, which ``recover_max_seq`` then can't see (its seq vanishes
    from the high-water-mark). A loud failure here beats a silent cursor-corruption
    later (the honesty axiom — loud failures over silent ones).
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        raise ValueError(
            "write_project content has no frontmatter block; cannot embed seq. "
            "The module only writes well-formed baton/board markdown."
        )
    block = m.group(1)
    new_field = f"seq: {seq}"
    if re.search(r"^seq:\s*.*$", block, re.MULTILINE):
        new_block = re.sub(r"^seq:\s*.*$", new_field, block, count=1, flags=re.MULTILINE)
    else:
        new_block = block + ("\n" if not block.endswith("\n") else "") + new_field
    return f"---\n{new_block}\n---\n" + content[m.end():]


def _read_store_file_text(path: Path, *, errors: str = "strict") -> Optional[str]:
    """Read a store file as UTF-8, or ``None`` when it is not a plain regular file.

    Returns ``None`` for exactly two refusals: the final component is a symlink
    (``O_NOFOLLOW`` makes the open itself fail with ``ELOOP``) or the opened fd
    is not a regular file (a FIFO, a device, a directory). Every other
    ``OSError`` propagates unchanged, so ``FileNotFoundError`` keeps its "not
    there" meaning at the call sites that already rely on it.

    ``errors`` selects the decode policy, split by what the read is FOR:

    - **``"replace"`` for display and integrity reads** -- the board
      (``read_projects``), a single project (``read_project``), and the seq
      high-water-mark (``recover_max_seq``). For a high-water mark,
      UNDER-counting is dangerous (a fresh allocator reissues sequence numbers
      over a live baton) while over-counting is safe (gaps are harmless); for a
      display read, lossy rendering beats the entry vanishing. The seq lives in
      ASCII frontmatter, so a bad byte in the body no longer hides it.
    - **``"strict"`` (the default) for DM content reads** -- ``read_dm_thread``
      and ``append_dm``. A corrupted thread body must not be silently mangled
      into a clean-looking parse; those callers can react (the explicit read
      raises, the wake feed skips the thread).

    ``O_NONBLOCK`` is what makes a FIFO (or other non-regular file) open
    INSTANTLY instead of blocking the reader, so the kind refusal has to be the
    ``fstat`` + ``S_ISREG`` check on the already-opened fd, never a
    stat-then-open (that would reintroduce the check-then-use window this helper
    exists to close). ``O_NONBLOCK`` is a no-op on a regular file on Linux, so
    the normal read is unchanged.

    ``getattr(os, "O_NOFOLLOW", 0)`` is the portability floor: on a platform
    without the flag this helper cannot detect a link on its own, and the
    structural ``os.path.islink`` check in ``FileStore._dm_path`` is what
    remains. The ``fstat`` regular-file check still runs there.
    """
    try:
        fd = os.open(
            str(path),
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise
    try:
        # Checked on the fd, after the open: a path stat before opening is the
        # very check-then-use window this helper closes.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors)


def _scan_text(path: Path) -> Optional[str]:
    """Read one directory-scan entry; ``None`` means "skip this entry".

    A scan over many files must never raise (and must never block) because one
    entry is unusable: a planted FIFO opens instantly under ``O_NONBLOCK`` and
    is refused by the fd kind check, a symlink is refused, and any ``OSError``
    (a file deleted mid-scan, a permission denial) folds into the same skip. The
    caller continues the scan.

    Decoding is ``errors="replace"`` (see :func:`_read_store_file_text`), so a
    malformed byte cannot raise here at all. That is deliberate: the old
    "also catch ``UnicodeDecodeError``" would have been a silent seq
    UNDER-count -- dropping the entry from a high-water mark reissues sequence
    numbers over a live baton -- so the display/integrity policy replaces the
    byte instead, and "a scan never raises because one entry is bad" is true by
    construction rather than by a catch that only looked like coverage.
    """
    try:
        return _read_store_file_text(path, errors="replace")
    except OSError:
        return None


def _read_text_nofollow(path: Path) -> str:
    """Read ``path`` as UTF-8, refusing a symlink and any non-regular file.

    The open/fstat/read work is :func:`_read_store_file_text` -- one shared core,
    so this DM-facing surface and the directory scans cannot drift apart. This
    wrapper supplies the DM surface's two named refusals; it never reads a byte
    of a refused path (the core refuses on the opened fd, before any read).

    Every other ``OSError`` is re-raised unchanged, so ``FileNotFoundError``
    keeps its current "no thread yet" meaning at the DM call sites.

    Decoding stays STRICT by design: a corrupted thread body must not be
    silently mangled into a clean-looking parse. This is the deliberate
    asymmetry with the display/integrity reads, which use ``errors="replace"``
    -- the explicit read can react (it raises), and the wake feed wraps it.
    """
    text = _read_store_file_text(path)
    if text is not None:
        return text
    # ``None``: a link under O_NOFOLLOW, or a non-regular kind. Distinguish only
    # to preserve this surface's two named messages; the refusal itself already
    # happened before any byte was read, so this probe selects a string and
    # never gates content access.
    if os.path.islink(path):
        raise InvalidDmPath(f"dm thread path is not a plain file: {path.name!r}")
    raise InvalidDmPath(f"dm thread path is not a regular file: {path.name!r}")


class FileStore(CoordinationStore):
    """Concrete file-backed coordination store (fork-1 default impl)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.projects_dir = self.root / "projects"
        self.dm_dir = self.root / "dm"

    # --- path containment (#3286) ------------------------------------------
    def _contained_dir(self, sub: str) -> Path:
        """``<root>/<sub>`` iff it really is that directory once symlinks resolve.

        The root itself may be a symlink (an admin's layout): both sides are
        compared RESOLVED. But ``projects/`` / ``archive/`` must not lead
        anywhere other than ``<resolved root>/<sub>``. Returns the resolved path.
        """
        want = self.root.resolve() / sub
        if (self.root / sub).resolve() != want:
            raise InvalidProjectId(f"store directory {sub!r} resolves outside the store root")
        return want

    def _project_file(self, project_id, *, sub: str = "projects", suffix: str = "") -> Path:
        """The ONLY way a project id becomes a path (read/write/archive/exists).

        Refuses (``InvalidProjectId``) an id that is not a non-empty str, contains
        a slash, a backslash or NUL, or is ``.``/``..``; then requires the target
        to sit directly in the contained ``sub`` directory and NOT to be a symlink
        (a link is refused even when it points inside the store; so is a dangling
        one). Returns a path built from the resolved directory, so later I/O cannot
        be redirected through a symlinked parent. (A check-then-use window remains
        against a concurrent local attacker who can already write in ``projects/``;
        ``os.replace`` replaces a final-component link rather than following it.)
        """
        if (
            not isinstance(project_id, str)
            or not project_id
            or project_id in (".", "..")
            or any(c in project_id for c in ("/", "\\", "\x00"))
        ):
            raise InvalidProjectId(f"invalid project id: {project_id!r}")
        base = self._contained_dir(sub)
        path = base / f"{project_id}{suffix}.md"
        if os.path.islink(path) or path.resolve().parent != base:
            raise InvalidProjectId(f"project path is not a plain file in the store: {project_id!r}")
        return path

    # --- baton / board (projects) ------------------------------------------
    def read_projects(self, *, active_only: bool = True) -> list[ProjectRecord]:
        # Resolve the scan ROOT first: a directory that is not the store's is
        # not a per-file anomaly to skip -- nothing in it is trustworthy.
        base = self._contained_dir("projects")
        if not base.is_dir():
            return []
        records: list[ProjectRecord] = []
        for md in sorted(base.glob("*.md")):
            # A scan over many files must not turn one planted link into a 500
            # for the whole board, so the scan SKIPS it: non-disclosure is what
            # this buys, and detection is the operator's filesystem audit, not
            # this read path.
            if os.path.islink(md):
                continue
            raw = _scan_text(md)
            if raw is None:
                continue
            fields, _body = _parse_frontmatter(raw)
            status = fields.get("status", "").strip().lower()
            if active_only and status in CLOSED_STATUSES:
                continue
            turn_reason = fields.get("turn_reason", "").strip()
            if len(turn_reason) >= 2 and turn_reason[0] == '"' and turn_reason[-1] == '"':
                turn_reason = turn_reason[1:-1]
            # turn_by is written quoted (like turn_reason — see projects.flip's
            # comment for why: an unquoted empty value corrupts the frontmatter
            # parser). Unquote the same way; a MISSING field (legacy baton,
            # pre-#1812) is bare "" and the quote-check is a no-op on it. An
            # unquoted-but-present turn_by (e.g. a hand-authored file) also
            # passes through unchanged, since the strip only fires on an
            # actual leading+trailing quote pair.
            turn_by = fields.get("turn_by", "").strip()
            if len(turn_by) >= 2 and turn_by[0] == '"' and turn_by[-1] == '"':
                turn_by = turn_by[1:-1]
            records.append(
                ProjectRecord(
                    project_id=fields.get("project_id", md.stem).strip() or md.stem,
                    title=fields.get("title", "").strip(),
                    status=status,
                    turn=fields.get("turn", "").strip().lower(),
                    turn_since=fields.get("turn_since", "").strip(),
                    turn_reason=turn_reason,
                    participants=_parse_participants(fields.get("participants", "")),
                    seq=_coerce_seq(fields.get("seq", "")),
                    raw=raw,
                    github=fields.get("github", "").strip(),
                    # Missing turn_by (pre-this-field batons) parses to "" — the
                    # documented safe default, never a crash, never a migration.
                    turn_by=turn_by.strip().lower(),
                )
            )
        # ISO-8601 turn_since sorts lexicographically == chronologically; empty
        # (unstamped) sorts first. Mirrors baton list's oldest-first ordering.
        records.sort(key=lambda r: r.turn_since)
        return records

    def read_project(self, project_id: str) -> Optional[str]:
        path = self._project_file(project_id)
        try:
            # A single-project read blocks on a FIFO exactly like a scan read
            # does. The core returns None for a non-regular kind (and for a
            # link, which _project_file already refused above) and re-raises a
            # real OSError for the handler below. Decoding is lossy on purpose
            # (display/integrity policy): a bad body byte must not make a live
            # project vanish, and its ASCII frontmatter stays readable.
            return _read_store_file_text(path, errors="replace")
        except OSError:
            return None

    def write_project(self, project_id: str, content: str, *, seq: int) -> None:
        path = self._project_file(project_id)
        _atomic_write(path, _embed_seq(content, seq))

    def archive_project(self, project_id: str) -> None:
        src = self._project_file(project_id)
        dst = self._project_file(project_id, sub="archive")  # also validates archive/
        if not src.exists():
            return  # idempotent: already archived or never written
        dst.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(dst):
            # Guard against silent clobber on pid reuse (#1512): suffix with seq.
            seq = 1
            while os.path.lexists(self._project_file(project_id, sub="archive", suffix=f".{seq}")):
                seq += 1
            dst = self._project_file(project_id, sub="archive", suffix=f".{seq}")
        src.rename(dst)  # atomic POSIX rename (same fs)

    # --- DM ----------------------------------------------------------------
    def _dm_dir(self) -> Path:
        """The store's ``dm/`` directory, guaranteed to really be ``<root>/dm``.

        Mirrors :meth:`_contained_dir`: the root itself may be a symlink (an
        admin's layout), so both sides are compared RESOLVED. Differs only in
        raising :class:`InvalidDmPath` (not ``InvalidProjectId``), since this is
        the DM surface. The containment check here is deliberately redundant
        with ``_dm_path``'s ``resolve().parent`` comparison -- the directory's
        identity vs the file's -- and the redundancy is defence in depth, not
        dead code.
        """
        want = self.root.resolve() / "dm"
        if (self.root / "dm").resolve() != want:
            raise InvalidDmPath("store directory 'dm' resolves outside the store root")
        return want

    def _dm_path(self, a: str, b: str) -> Path:
        """The ONLY way a DM pair becomes a path (read/write).

        ``dm_thread_key`` validates both names at this chokepoint, so the
        filename cannot carry a separator. Then the target must sit directly in
        the contained ``dm`` directory and NOT be a symlink (a link is refused
        even when it points inside the store; so is a dangling one). Returns a
        path built from the resolved directory, so later I/O cannot be
        redirected through a symlinked parent. (A check-then-use window remains
        against a concurrent local attacker who can already write in ``dm/``;
        the ``O_NOFOLLOW`` open in :func:`_read_text_nofollow` is what closes it
        for READS.)
        """
        dm_dir = self._dm_dir()
        path = dm_dir / f"{dm_thread_key(a, b)}.md"
        if os.path.islink(path) or path.resolve().parent != dm_dir:
            raise InvalidDmPath(f"dm thread path is not a plain file in the store: {path.name!r}")
        return path

    def read_dm_thread(
        self, a: str, b: str, *, since_seq: int = 0
    ) -> list[DmMessage]:
        path = self._dm_path(a, b)
        try:
            raw = _read_text_nofollow(path)
        except OSError:
            # "No thread yet". InvalidDmPath is a ValueError, NOT an OSError, so
            # a planted-link refusal propagates past this handler on purpose --
            # do not widen it to Exception.
            return []
        messages = _parse_dm_thread(raw)
        out = [m for m in messages if m.seq > since_seq]
        out.sort(key=lambda m: m.seq)
        return out

    def append_dm(
        self, sender: str, recipient: str, body: str, *, seq: int, ts: str
    ) -> DmMessage:
        # Normalize to the same lowercase-stripped form the thread key uses, so a
        # re-read's from=/to= fields match the key and ACL checks are stable.
        sender = sender.strip().lower()
        recipient = recipient.strip().lower()
        path = self._dm_path(sender, recipient)
        # Read-modify-write is serialization-safe ONLY because the module holds
        # the SeqAllocator lock across this whole call (fork-4 co-atomicity);
        # _atomic_write makes the write POSIX-atomic but does not guard the read
        # that precedes it. Do not "simplify" this to drop that assumption.
        body_lines = body.split("\n")
        header = (
            f"--- seq={seq} from={sender} to={recipient} "
            f"at={ts} lines={len(body_lines)} ---"
        )
        block = header + "\n" + "\n".join(body_lines) + "\n"

        try:
            existing = _read_text_nofollow(path)
        except OSError:
            existing = f"<!-- dm thread: {dm_thread_key(sender, recipient)} -->\n"
        # DM threads are read only by the forum server: owner-only (#4501).
        _atomic_write(path, existing + block, mode=0o600)
        return DmMessage(
            seq=seq, sender=sender, recipient=recipient, body=body, ts=ts
        )

    def list_dm_threads(self, agent: str) -> list[str]:
        if not self.dm_dir.is_dir():
            return []
        agent = agent.strip().lower()
        others: list[str] = []
        for md in sorted(self.dm_dir.glob("*.md")):
            # Key is "<x>+<y>" with x,y sorted; "+" is reserved in agent names so
            # a two-element split is exact for registered names.
            parts = md.stem.split("+")
            if len(parts) != 2 or agent not in parts:
                continue
            # Self-thread (x == y == agent) → the other party is the agent itself.
            other = parts[1] if parts[0] == agent else parts[0]
            others.append(other)
        return others

    def list_all_dm_threads(self) -> list[tuple[str, str]]:
        """Return every DM pair as sorted ``(a, b)`` tuples — operator-view, all pairs.

        Scans the ``dm/`` directory for ``<a>+<b>.md`` files and returns each pair
        as a ``(a, b)`` tuple (already sorted lexicographically because the filename
        key is produced by :func:`dm_thread_key`). Files whose stem does not split
        into exactly two parts are skipped (should not exist under normal operation,
        but ``+`` is reserved in agent names so a two-element split is exact for
        any well-formed thread file). Returns ``[]`` when the directory is absent.
        """
        if not self.dm_dir.is_dir():
            return []
        pairs: list[tuple[str, str]] = []
        for md in sorted(self.dm_dir.glob("*.md")):
            parts = md.stem.split("+")
            if len(parts) != 2:
                continue
            pairs.append((parts[0], parts[1]))
        return pairs

    # --- cursor recovery ---------------------------------------------------
    def recover_max_seq(self) -> int:
        hi = 0
        # Each scan ROOT is resolved through the contained-dir helpers: a
        # directory that resolves outside the store is refused, not scanned.
        projects_dir = self._contained_dir("projects")
        if projects_dir.is_dir():
            for md in projects_dir.glob("*.md"):
                # Same rule as read_projects: a scan skips a planted link
                # (non-disclosure; detection is the operator's filesystem audit).
                if os.path.islink(md):
                    continue
                raw = _scan_text(md)
                if raw is None:
                    continue
                fields, _ = _parse_frontmatter(raw)
                hi = max(hi, _coerce_seq(fields.get("seq", "")))
        archive_dir = self._contained_dir("archive")
        if archive_dir.is_dir():
            for md in archive_dir.glob("*.md"):
                # Same rule: a scan skips a planted link (non-disclosure).
                if os.path.islink(md):
                    continue
                raw = _scan_text(md)
                if raw is None:
                    continue
                fields, _ = _parse_frontmatter(raw)
                hi = max(hi, _coerce_seq(fields.get("seq", "")))
        dm_dir = self._dm_dir()
        if dm_dir.is_dir():
            for md in dm_dir.glob("*.md"):
                # Same rule: a scan skips a planted link (non-disclosure).
                if os.path.islink(md):
                    continue
                raw = _scan_text(md)
                if raw is None:
                    continue
                # Parse STRUCTURALLY (length-prefixed), not by line-scanning for
                # headers — a body line shaped like a header must not inflate the
                # high-water-mark, exactly as read_dm_thread won't mis-read it as
                # a message. Over-counting from body text would jump the global
                # seq on attacker-/typo-controlled content.
                for msg in _parse_dm_thread(raw):
                    hi = max(hi, msg.seq)
        return hi


def _parse_dm_thread(raw: str) -> list[DmMessage]:
    """Parse a per-pair DM thread file into ordered :class:`DmMessage` records.

    Walks physical lines: on a header match, consume EXACTLY ``lines`` body lines
    after it (never split on ``---``), so a body line that itself looks like a
    header is one of the counted lines, not a new message — collision-proof by
    construction (the contract in ``store.py``'s module docstring). Between
    messages, non-header lines (the ``<!-- dm thread -->`` comment, stray blanks)
    are skipped.
    """
    physical = raw.split("\n")
    messages: list[DmMessage] = []
    i = 0
    n = len(physical)
    while i < n:
        hm = _DM_HEADER_RE.match(physical[i])
        if not hm:
            i += 1
            continue
        count = int(hm.group("lines"))
        body_lines = physical[i + 1 : i + 1 + count]
        messages.append(
            DmMessage(
                seq=int(hm.group("seq")),
                sender=hm.group("from"),
                recipient=hm.group("to"),
                body="\n".join(body_lines),
                ts=hm.group("at"),
            )
        )
        i += 1 + count
    return messages
