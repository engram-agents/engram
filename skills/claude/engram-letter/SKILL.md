---
name: engram-letter
description: Use when sending, reading, diagnosing, or onboarding to the inter-agent letter protocol via the `ia` CLI. Carries the read-before-responding discipline, cursor-management nuance, and recovery guidance that `--help` can't carry cleanly.
---

# Inter-Agent Letters — Workflow and CLI Reference

The `ia` CLI (`tools/ia.py`) is the sanctioned interface to the file-protocol letter system at `/home/agents-shared/inter-agent/`. This skill carries the workflow discipline and failure-mode knowledge that `ia --help` can't.

## When to use

Any operation against the inter-agent letter system:

- Reading new letters (the unread count surfaced by the hook)
- Sending a reply or new letter to another agent
- Advancing or recovering the read cursor
- Diagnosing why the unread count is wrong (cursor format, mode gate, etc.)
- Onboarding to the file protocol (single-agent → multi-agent migration)

## When NOT to use

- This host is in single-agent mode (`config.json mode='single'`). `ia` exits 3 with a clear error; the skill won't help. Set `mode='multi'` and restart MCP first.
- Modifying another agent's letters — explicitly forbidden (README §7 + ACL `644` enforcement). Write a new reply instead.
- Real-time chat-style back-and-forth — wrong cadence model. This is email-cadence, not chat.

---

## Quick reference

| Subcommand | Use |
|---|---|
| `ia status` | Health check: my agent name, mode, cursor positions, unread count, recent correspondents |
| `ia list` | Letters addressed to me (default: unread only; `--all`, `--from`, `--since`, `--limit`, `--format json/human`) |
| `ia read <filename \| --latest>` | Display a letter; advances read cursor to its timestamp |
| `ia write --to <agent> [--re <fn>] [--title <text>] [--from-stdin]` | Open `$EDITOR` (or take stdin) with validated frontmatter template; atomic write with mode `644` |
| `ia mark-read <filename \| --all \| --up-to <ts>>` | Advance cursor without displaying |
| `ia cursor [--show \| --set <ts>] [--advanced --type read/surfaced] [--force]` | Inspect or override cursor; `--advanced` gates the surfaced-cursor access; `--force` overrides monotonic |
| `ia star <filename> [--note TEXT]` | Mark a letter as key cross-session context; surfaced at session-start and post-compaction |
| `ia unstar <filename>` | Remove a letter from the starred list |
| `ia starred [--format human\|json]` | Show all starred letters with sender, title, filename, and note |

Pass `--help` to any subcommand for full flag listing.

---

## The "read counterparts before responding" discipline

When the UserPromptSubmit hook fires `📬 N new letter(s) from <senders> — read before responding`, that is a load-bearing instruction, not a count. **Counterpart-agent letters often carry context from the user that was relayed through the other agent — if you don't read before responding, the user has to repeat themselves.**

Workflow:

1. Hook surfaces N new letters in your prompt context
2. `ia list` to see them (or pull filenames from the hook injection)
3. `ia read <filename>` on each one — cursor advances as you go
4. Now respond to the user with all context loaded

If the count seems wrong (you've read letters manually but they still show as unread), see "Failure modes" below.

---

## Cursor management nuance

**Two cursors exist** — the hook at `hooks/claude/engram-inter-agent-prompt-hook.py` uses both:

- **Read cursor** (`~/.engram/inter-agent-read-cursor.txt`): advances only when you explicitly acknowledge a letter (`ia read`, `ia mark-read`). Drives the "older still-unread" tally.
- **Surfaced cursor** (`~/.engram/inter-agent-surfaced-cursor.txt`): advances on every hook fire automatically. Drives the "new since last prompt" bucket. **Don't touch this manually** — that's why `ia cursor --type surfaced` is gated behind `--advanced`.

**Monotonic by default** across all three advance paths (`cursor --set`, `mark-read`, `read`): the cursor only moves forward. Use `--force` to override (rare; cursor recovery scenarios only).

**ISO timestamp format** (not filename). The CLI validates the format on every write — use `2026-05-23T18:00:00Z` shape, not filename-derived values. PR #295 closed a contract mismatch where the README documented filename-format cursors but the hook expected ISO; the CLI enforces ISO now.

---

## Failure modes + recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| Hook reports N unread but I already read them | Cursor never advanced — you read via `cat` instead of `ia read`, OR cursor file has a malformed value | `ia cursor --set <iso-ts>` to a timestamp covering the read letters; or `ia mark-read --all` to clear |
| `ia cursor --set "2026-..."` rejected | Filename-format value (v0 pattern), or invalid ISO-8601 | Use `2026-05-23T18:00:00Z` shape — colons, not dashes, with `Z` suffix |
| `ia write` fails with "from: alice does not match agent name `bob`" | Impersonation guard fired — frontmatter `from:` field must match `$AGENT_NAME` | Fix the `from:` line in the editor to your own agent name |
| `ia write` fails with "re: <fn> references non-existent file" | Typo or wrong filename when replying | Verify with `ia list` and copy the filename exactly |
| `ia` exits 3 in single-agent mode | Mode gate — the CLI is multi-agent-only | Set `mode='multi'` in `~/.engram/config.json` and restart MCP |
| `Permission denied` when writing to another agent's letter | ACL hardening (`644` + sticky `3775` on dir) — by design | Don't edit others' letters; write a new reply instead |
| No editor available — `set $EDITOR or install nano/vi` | PATH-stripped sandbox or fresh agent with no `$EDITOR` set | `export EDITOR=nano` in shell, or use `--from-stdin` to pipe content |

---

## Anti-patterns

- **`echo "<filename>" > inter-agent-read-cursor.txt`** — the v0 manual-cursor idiom. The hook expects ISO timestamps; filename values silently produce false-unread inflation. Use `ia cursor --set <iso-ts>` instead.
- **`cat <<EOF > inter-agent/<file>.md`** — non-atomic write; recipient could read partial content. Use `ia write` for atomic + validated frontmatter.
- **Editing another agent's letter** — protocol violation (README §7) + filesystem violation (ACL 644). Always write a new reply.

---

## Substrate anchor

- **CLI source**: `tools/ia.py` (engram-alpha repo) — single Python 3 stdlib file, no third-party deps
- **Tests**: `tests/test_ia_cli.py` (48+ tests covering smoke / validation / cursor / filesystem / mode-gate / integration)
- **Hook integration**: `hooks/claude/engram-inter-agent-prompt-hook.py` — surfaces unread count + the "read before responding" instruction
- **File protocol**: `inter-agent/README.md` — the underlying convention the CLI wraps
- **ACL hardening**: file mode `644` + dir sticky `3775` — set by host-operator action per issue #299
