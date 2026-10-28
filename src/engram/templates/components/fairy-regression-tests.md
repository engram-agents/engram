---
id: fairy-regression-tests
kind: rule
harnesses: all
surfaces: [CLAUDE.md]
anchor: "Parallelization and Delegation"
order: 30
merge: replace
version: 2
---
### Regression tests

- **Pattern-following:** about to do the same mechanical operation a 3rd time? Delegate the 4th onward.
- **Spec-while-fairy-runs:** waiting on a fairy while other specs are undrafted? STOP — draft them NOW. Specs are text; only implementation can conflict.

