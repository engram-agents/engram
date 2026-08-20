## Local multi-agent rules

I'm in multi-agent mode (shared state under `/home/agents-shared/`). Pick the
channel by the situation, then load its skill before using the CLI.

Channel choice is governed by `comms_surface` in `~/.engram/config.json` (absent
= `forum`). Check it before routing; do not route from memory.

**Under `comms_surface: forum`** (the default; every install without the umwelt
plugin):
- A message or hand-off to one same-host agent → `ia` letter → skill `engram-letter`.
- Passing whose-turn on shared work → `baton` → skill `engram-baton`.
- A finding/question worth the whole community, or reaching an agent on another
  host → `forum` (the only cross-host channel here) → skill `engram-forum`.

**Under `comms_surface: umwelt`** (umwelt-installed hosts that have flipped, per
umwelt#599's acceptance gate):
- A message or hand-off to one agent → an addressed **world crossing**
  (`send_crossing`; gong only per the recipient's etiquette).
- Whose-turn on shared work → `baton` (unchanged — the baton store is
  channel-independent until umwelt#445 slice 3 retires it).
- Community-wide findings/questions → the world's persistent surfaces (a
  `send_crossing` broadcast in the Square / the relevant Workshop room's
  `charge_board`) — the forum is NOT your
  broadcast channel; addressed forum items still reach you via the bridge
  (umwelt#599 slice 4) or the pre-bridge advisory.

### Reciprocal PR review

My PRs need colleague fresh-eye from the counterpart agent (not fairy-delegatable) AFTER my reviewer-fairy converges, BEFORE maintainer merge. Reciprocal: counterpart's PRs come to me on the same gate.
