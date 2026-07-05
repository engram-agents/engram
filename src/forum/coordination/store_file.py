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

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

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
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)
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


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (tempfile + ``os.replace``).

    The tempfile is created in the SAME directory so ``os.replace`` is a true
    rename (atomic on POSIX), never a cross-device copy. On any failure the
    tempfile is removed so a crash mid-write never leaves a partial sibling.
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
        os.chmod(tmp, 0o644)  # world-readable before rename; mkstemp defaults to 0600
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


class FileStore(CoordinationStore):
    """Concrete file-backed coordination store (fork-1 default impl)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.projects_dir = self.root / "projects"
        self.dm_dir = self.root / "dm"

    # --- baton / board (projects) ------------------------------------------
    def read_projects(self, *, active_only: bool = True) -> list[ProjectRecord]:
        if not self.projects_dir.is_dir():
            return []
        records: list[ProjectRecord] = []
        for md in sorted(self.projects_dir.glob("*.md")):
            try:
                raw = md.read_text(encoding="utf-8")
            except OSError:
                continue
            fields, _body = _parse_frontmatter(raw)
            status = fields.get("status", "").strip().lower()
            if active_only and status in CLOSED_STATUSES:
                continue
            turn_reason = fields.get("turn_reason", "").strip()
            if len(turn_reason) >= 2 and turn_reason[0] == '"' and turn_reason[-1] == '"':
                turn_reason = turn_reason[1:-1]
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
                )
            )
        # ISO-8601 turn_since sorts lexicographically == chronologically; empty
        # (unstamped) sorts first. Mirrors baton list's oldest-first ordering.
        records.sort(key=lambda r: r.turn_since)
        return records

    def read_project(self, project_id: str) -> Optional[str]:
        path = self.projects_dir / f"{project_id}.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def write_project(self, project_id: str, content: str, *, seq: int) -> None:
        path = self.projects_dir / f"{project_id}.md"
        _atomic_write(path, _embed_seq(content, seq))

    def archive_project(self, project_id: str) -> None:
        src = self.projects_dir / f"{project_id}.md"
        if not src.exists():
            return  # idempotent: already archived or never written
        dst_dir = self.root / "archive"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{project_id}.md"
        if dst.exists():
            # Guard against silent clobber on pid reuse (#1512): suffix with seq.
            seq = 1
            while (dst_dir / f"{project_id}.{seq}.md").exists():
                seq += 1
            dst = dst_dir / f"{project_id}.{seq}.md"
        src.rename(dst)  # atomic POSIX rename (same fs)

    # --- DM ----------------------------------------------------------------
    def _dm_path(self, a: str, b: str) -> Path:
        return self.dm_dir / f"{dm_thread_key(a, b)}.md"

    def read_dm_thread(
        self, a: str, b: str, *, since_seq: int = 0
    ) -> list[DmMessage]:
        path = self._dm_path(a, b)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
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
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = f"<!-- dm thread: {dm_thread_key(sender, recipient)} -->\n"
        _atomic_write(path, existing + block)
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
        if self.projects_dir.is_dir():
            for md in self.projects_dir.glob("*.md"):
                try:
                    fields, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
                except OSError:
                    continue
                hi = max(hi, _coerce_seq(fields.get("seq", "")))
        archive_dir = self.root / "archive"
        if archive_dir.is_dir():
            for md in archive_dir.glob("*.md"):
                try:
                    fields, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
                except OSError:
                    continue
                hi = max(hi, _coerce_seq(fields.get("seq", "")))
        if self.dm_dir.is_dir():
            for md in self.dm_dir.glob("*.md"):
                try:
                    raw = md.read_text(encoding="utf-8")
                except OSError:
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
