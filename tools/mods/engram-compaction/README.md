# engram-compaction (part of #3640)

Agent-decided compaction at the nap boundary. Default OFF.

The last step of the nap skill runs `tools/compact-request.py`, which leaves three files under
`$ENGRAM_HOME/compact/` (`context.md`, `context.sha256`, and `request.json` last). With this mod
switched on, the end of the next answered turn consumes the request once and compacts, and the
compaction carries the assembled context (focus list, history arc, warm-briefing arc, loop marker fields).

- `userConfig.enabled` (default false): false registers nothing.
- The mods API has no file delete, so a consumed request (and the in-flight token) is overwritten with
  `{"consumed": true, "request_id": ...}`; a tombstone reads as absent.
- The context message goes BESIDE the engine's own messages by default; `COMPACT_SHAPE` in
  `hooks/compaction.ts` is the one constant that flips it to REPLACE.
- Only a `plugin`-triggered compaction with a fresh (60 s) matching token is answered; `manual` and `auto`
  pass through untouched. Any failed guard is `next(e)`: the engine's own summary applies.

## Bounds

- The no-`loop-wake` guarantee covers OUR message only (the tool scrubs the context it writes). Under BESIDE the
  engine's own messages are passed through exactly as the engine made them; this mod never edits the engine's summary.
- A request that was written but not consumed (its turn was aborted, or the mod was off) stays live for up to 30 minutes in the
  same session. A declined ask-first step runs `compact-request.py --clear`, which removes it; only a nap that never reaches Step 9 leaves it live.
- Consume-once rests on parse-failure-as-absent plus the write-before-compact ordering, NOT on write atomicity
  (`$.fs.write` gives none): the tombstone is a full overwrite, and no prefix of it is valid JSON.

## Tests

```
claude plugin validate tools/mods/engram-compaction
claude plugin test tools/mods/engram-compaction
python3 -m pytest tests/test_compaction_mod_3640.py tests/test_compact_request_3640.py
```

Save the runner output to a file first and read the `(fail)` lines, not only the counts.

Plugin-test limits: the harness does not stamp `trigger` on a compaction a plugin starts itself, so the
cells pass `trigger` through the test's own call (the live value is covered only by the probe nap); a
`next(e)` needs a non-empty transcript, so cells supply one; test files take static imports only and
share no module state with the loaded plugin.
