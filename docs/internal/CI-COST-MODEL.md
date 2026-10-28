# CI cost model — measurements

Evidence behind the CI cost rules in CLAUDE.md § Branching convention.

## Moved from CLAUDE.md § Branching convention — the macOS billing measurement (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

- **No macOS runners in CI — ever (Lei, 2026-09-28).** A `runs-on: macos-*` job
  bills at **ten times** the Linux minute rate, and `pr-checks` fires on every
  PR event (99 times on 2026-09-27 alone). The one macOS job this repo ever had,
  `macos-bash32-syntax` (added 2026-09-18 by #3000 to run a single bash-3.2
  parse test), produced $5.85 of a $8.84 day — two-thirds of the bill for one
  test that takes seconds. Removed 2026-09-28. **macOS coverage comes from the
  Mac seats**: the test stays in the suite and skips on Linux; a seat on a real
  Mac (Aleph, Clio) runs it locally whenever a shipped shell script changes and
  pastes the result in the review, exactly like affected-tests above. If a
  future change genuinely needs a hosted macOS runner, it is Lei's call, opened
  as a question with the per-run cost stated — never added inside a fix PR.
