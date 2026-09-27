"""WS4 adjudication kit (engram-alpha#2753) — offline, read-only tools that
cut "heeding-window" spans out of Claude Code transcripts for blinded human
adjudication, and analyze the resulting verdicts as an arm-to-arm difference.

See tools/ws4/extract_spans.py (Piece 1), the verdicts.jsonl format documented
in tools/ws4/analyze.py's module docstring (Piece 2), and tools/ws4/analyze.py
(Piece 3) for the pre-registered analysis (O1 / length control / O2).

Everything here is stdlib-only and read-only: it never writes to
knowledge.db or to the fire ledger, and it never touches the lesson-tripwire
hook (src/engram/hooks/claude/engram-lesson-tripwire-hook.py) beyond
importing two of its existing symbols by path (see tools/ws4/hook_bridge.py).
"""
