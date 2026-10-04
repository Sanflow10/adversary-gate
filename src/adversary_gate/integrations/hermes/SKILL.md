---
name: adversary-gate
description: Before saying a coding task is done, or before keeping a skill you wrote that contains code, have AdversaryGate judge the change by executing it. Use whenever you edited code in a git repository and are about to report success, open a PR, or promote a self-written skill.
---

# Verify code changes with AdversaryGate

You are about to claim that a code change works. Do not decide that yourself:
the gate decides it by running the tests that existed **before** your change
against your code.

## Procedure

1. Make sure your changes are saved in the repository's working tree. Do not
   commit them just to call the gate — uncommitted work is judged as-is.
2. Call the `verify_repo` tool of the `adversary_gate` MCP server
   (Hermes exposes it as `mcp_adversary_gate_verify_repo`) with:
   - `repo`: the repository root (absolute path);
   - `claims`: the pytest node ids that should prove your change, if you know
     them (`tests/test_x.py::test_y`); otherwise leave it empty and the gate
     finds the tests that executed the lines you changed.
3. Read `decision`:
   - **merge** — every claim executed and cleared the floors. Report it as
     *"verified by AdversaryGate"*, include `fix_proven`, and still hand it to a
     human: merge means *permission to look*, not *ship it*.
   - **block** — something you changed broke a test that passed before.
     Fix the **code**. Do not edit, weaken, skip or delete tests to get past
     it: rewritten tests are judged with the baseline's copy anyway, so it
     will not work, and trying is the failure this tool exists to catch.
   - **inconclusive** — something was not measured (no tests ran the change,
     too few mutants, non-Python code, a harness file changed). Say exactly
     what `reason` says. Never report the task as done.
4. When the task was a **bug fix**, `fix_proven: true` is the evidence you
   want: a test failed before your change and passes after. If it is `false`,
   write a test that reproduces the bug first, confirm it fails on the
   baseline, then fix.

## When you are improving your own skills

A skill you wrote that contains or generates code is promoted only if
`verify_repo` returns **merge** for the repository the skill acts on. Record
the `decision` and `fix_proven` next to the skill change. A skill revision that
produced **block** or **inconclusive** is rolled back, not kept "for now".

## Never

- Never pass options to `pytest_args`, and never ask for a lower floor; the
  policy belongs to the operator (`gate_policy` shows it, read-only).
- Never summarise an inconclusive or blocked result as success.
