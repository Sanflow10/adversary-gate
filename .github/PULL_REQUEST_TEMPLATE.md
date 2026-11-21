<!--
  Pull requests with code from outside the project are closed unread: the code
  is written by the maintainer alone (see CONTRIBUTING.md). To report a bug,
  open an issue; to report a fail-open, use private vulnerability reporting
  (SECURITY.md).
-->

## What does this PR change, and why?

<!-- One logical change; reference its AG-### entry in
     ERRORS_AND_INCONSISTENCIES.md. -->

## Evidence

- [ ] `pytest` passes locally
- [ ] A test that fails before this change and passes after it is included
      (behavior fixes)
- [ ] No existing test was deleted or weakened to make CI green

## Fail-closed check

- [ ] When the new/changed code fails, it falls towards `INCONCLUSIVE`,
      never towards `MERGE`
