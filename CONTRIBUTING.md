# Contributing to AdversaryGate

Thank you for wanting to help. A few rules keep this project trustworthy —
it is a merge gate, so it is held to a stricter standard than most tools.

## 1. Sign the CLA first

Before a pull request can be merged, sign the
[CLA](CLA.md) by commenting on the PR:

```
I have read the CLA at CLA.md and I hereby sign it.
```

This preserves the project's dual-license (AGPL-3.0 + commercial). One
comment covers all present and future contributions.

## 2. Every claim about behavior needs a test

The project's own history is documented in
[ERRORS_AND_INCONSISTENCIES.md](ERRORS_AND_INCONSISTENCIES.md) — bugs found
in this project carry their own ID (AG-###). Fixes land with:

- a regression test that fails before the fix and passes after;
- the AG-### entry updated or created;
- no weakening of existing tests (a test that was failing must stay
  meaningful, not be deleted or loosened to make CI green).

If you found a bug, you are welcome to open an issue using the AG-### ledger
format.

## 3. Fail-closed is the invariant

When adding or changing behavior, ask: *when this fails, which way does it
fall?* The answer must never be "towards MERGE". Missing evidence, crashed
harnesses, unreadable files, and unknown states resolve to `INCONCLUSIVE`,
never to a pass. PRs that introduce a path that can approve a patch without
evidence will be rejected regardless of test status.

## 4. Environment

- CPython 3.10–3.12 supported (3.13+ works but is untested).
- `pip install -e .[dev]` for development dependencies.
- `pytest` must pass; `scripts/verify_fixes.sh` must pass.
- Resource-limit behavior differs on non-glibc platforms (see
  [docs/TERMUX_VALIDATION.md](docs/TERMUX_VALIDATION.md)); do not add tests
  that assume `RLIMIT_AS` works everywhere.

## 5. Commit and PR conventions

- One logical change per PR.
- The artefact JSON is part of the interface: changes to its fields must be
  documented in `CHANGELOG.md` and backwards-compatible or versioned.
- CI must pass. If CI fails for a platform-specific reason, say so in the PR
  and why it cannot be reproduced in the supported matrix.
