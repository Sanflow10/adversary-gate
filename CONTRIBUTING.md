# Contributing to AdversaryGate

AdversaryGate is a merge gate: a wrong `MERGE` is its whole failure mode. To
keep every line of it accountable, **the code is written by the maintainer
alone, and pull requests with code are not accepted.**

There is no CLA: no outside code is accepted, so there is nothing to sign.

That does not mean help is not wanted. These are welcome, and credited:

## Report a fail-open privately

A patch that reaches `MERGE` when it should not is a security bug. Report it
through [private vulnerability reporting](SECURITY.md), not in a public issue.
The fix is written by the maintainer, and the reporter is credited in the
published advisory.

## Report a bug or a confusing result

Open an issue with:

- the version (`adversary-gate --version`) and the command you ran;
- the JSON answer, or the part of it that looks wrong;
- the smallest repository or diff that reproduces it, if you can share one.

A reproduction is the most useful thing you can send. Please describe the
behaviour; do not attach a patch — fixes are written from the report, not
copied from it.

## Ask for something

An issue describing what you need — a language, a CI system, an option — is
how features get prioritised. Say what you would use it for.

## What every change here is held to

For context on how fixes are made, whoever writes them:

- **Fail-closed is the invariant.** Missing evidence, crashed harnesses and
  unknown states resolve to `INCONCLUSIVE`, never to a pass.
- **Every fix lands with a regression test** that fails before it and passes
  after, and with its entry in
  [ERRORS_AND_INCONSISTENCIES.md](ERRORS_AND_INCONSISTENCIES.md).
- **No test is weakened** to make CI green.
