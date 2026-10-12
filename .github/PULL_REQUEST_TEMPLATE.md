<!--
  Before opening this PR, please read CONTRIBUTING.md.
  The project is a merge gate: its own standard is fail-closed, and its own
  tests are part of the evidence. PRs are reviewed accordingly.
-->

## What does this PR change, and why?

<!-- One logical change. If you found a bug, reference its AG-### entry in
     ERRORS_AND_INCONSISTENCIES.md, or say "new entry proposed". -->

## Evidence

- [ ] `pytest` passes locally
- [ ] `scripts/verify_fixes.sh` passes locally
- [ ] A test that fails before this change and passes after it is included
      (behavior fixes)
- [ ] No existing test was deleted or weakened to make CI green

## Fail-closed check

- [ ] When the new/changed code fails, it falls towards `INCONCLUSIVE`,
      never towards `MERGE`

## CLA

- [ ] I have signed the CLA by commenting `I have read the CLA at CLA.md and I hereby sign it.` below
