# AdversaryGate 3.0.4 on Android / Termux

**Platform:** Android 16 (aarch64), Termux, unrooted
**Python:** 3.14.6 · **pytest:** 9.1.1 · **coverage:** 7.16.2 (without its C extension) · **AdversaryGate:** 3.0.4 (PyPI)
**Date:** 2026-10-13
**Operator:** Sanflow10, the project's maintainer

A follow-up to [TERMUX_VALIDATION.md](TERMUX_VALIDATION.md), which tested 3.0.0. CI runs CPython 3.10–3.14 on GitHub's `ubuntu-latest` (Linux x86_64); Android is not part of it. These are observations from a single device, not a support commitment. The versions above were read in the same virtualenv after the runs.

## Summary

- **3.0.4 runs end to end on Termux** with `--memory none --full-suite-memory none` ([#4](https://github.com/Sanflow10/adversary-gate/issues/4)). The runs also used `--serial-sides`.
- **At the default settings, a one-line honest fix is `INCONCLUSIVE`, not `MERGE`.** That is the documented rule, not a defect: one killed mutant out of one gives a lower bound of 0.3784 at 80 % confidence, below the 0.75 floor. It becomes `MERGE` only with the experimental `--census-min-mutants 1`, which is off by default.
- **A patch that rewrites its own tests is judged by the baseline's copy of them.** Changing only the tests gave `INCONCLUSIVE`; breaking the code and bending the tests to match gave `BLOCK`.

## Setup

One module, `calculator.py`, and one test file, `tests/test_addition.py`, in a git repository under `$HOME/agretest` (`/tmp` is not writable in Termux). The baseline is the first commit, the diff is `git diff --no-ext-diff` against it, and coverage comes from `coverage run -m pytest` on the patch side. The claims are `tests/test_addition.py::test_add` and `tests/test_addition.py::test_add_negative`.

| Scenario | Baseline | Patch |
|---|---|---|
| honest fix | `a - b`; the tests expect `5` and `-2` | `a + b`; same tests |
| test-only rewrite | `a - b`; the tests expect `5` and `-2` | `a - b`; tests rewritten to expect `-1` and `0` |
| test-rewriting fraud | `a + b`; the tests expect `5` and `-2` | `a - b`; tests rewritten to expect `-1` and `0` |

In the last two scenarios the patch's own tests pass on the patch. That is the lie the gate has to see through.

Every run used `--memory none --full-suite-memory none --serial-sides`. The other limits were the defaults (`--timeout 30`, `--cpu-seconds 10`, no sandbox), as each artefact's `execution` block records. Run 1b adds `--census-min-mutants 1`.

`--serial-sides` runs the baseline side and the patch side one after the other instead of at the same time. Its help recommends it on a machine too small for several test processes. Whether this phone needs it was not tested.

## Results

| Run | Extra flag | Exit | Decision | Detail |
|---|---|---|---|---|
| 1a honest fix | none | 2 | `inconclusive` | `fix_proven: true`; 1/1 mutants killed; lower bound 0.3784; census off |
| 1b honest fix | `--census-min-mutants 1` | 0 | `merge` | `fix_proven: true`; 1/1 mutants killed; census exact, lower bound 1.0 |
| 2 test-only rewrite | none | 2 | `inconclusive` | both claims `invalid`, judged by the baseline's tests; strength not measured |
| 3 test-rewriting fraud | none | 1 | `block` | both claims `regression`, judged by the baseline's tests |

The exit codes are the ones the operator's terminal printed. They are the codes the CLI assigns to each decision.

- Diff coverage was 1.0 and the full suite ran in all four runs.
- In runs 2 and 3, the full suite also ran with the baseline's copy of the rewritten test file (`full_suite_oracle.source: "baseline"`) and failed (exit 1).
- The gate's own clock (`duration_seconds`) read 17.2 to 17.7 s per run.

### Reasons, as recorded

**1a:**

```
every claim held and the fix is proven, but suite strength's lower bound is 0.38, below the 0.75 floor (1/1 mutants killed)
```

**1b:**

```
test fails on baseline and passes on patch: the patch fixes what this test checks
```

**2:**

```
judged by the baseline's version of tests/test_addition.py and its test files (the patch rewrote tests/test_addition.py): incomplete-fix claims require a valid cited acceptance criterion [baseline: failed 3/3; patch: failed 3/3]
```

Mutation was not measured: `no source file changed between baseline and patch`.

**3:**

```
judged by the baseline's version of tests/test_addition.py and its test files (the patch rewrote tests/test_addition.py): test passes on baseline and fails on patch. If this behaviour change is intended, the edit to the baseline's test is the thing to review: a human approves it, the gate cannot tell it from a test bent to hide a bug
```

`next_steps` asks for `human_review_test_edit` on `tests/test_addition.py`. Mutation was not measured, because the claims did not pass on the unmutated patch.

### Artefacts

The four JSON reports are in [termux-3.0.4/](termux-3.0.4/), byte for byte as the device wrote them. Only the file names were translated.

| File | Name on the device | SHA-256 |
|---|---|---|
| `1a_honest_fix_default.json` | `1a_honesto_padrao.json` | `34636f6f183cac469585120f4a0728bd0f8763fa9abda8b7a116a7cf2a748081` |
| `1b_honest_fix_census.json` | `1b_honesto_census.json` | `86da86de1b4098bbd235b385671d1e4eab37a3b974d19b14472a7b71f5b13aab` |
| `2_test_only_rewrite.json` | `2_degenerado.json` | `5f4326a5e38212876dd426fdcca5bb6780a3f10de385f55bbb33f7de1c8df431` |
| `3_test_rewriting_fraud.json` | `3_fraude.json` | `bc84931c602b44dcfdc15ce068b79e138d1633b504a2912b54dc0a107b56f9dc` |

## What this shows, and what it does not

**Shows:**
- The CLI, pytest, the coverage evidence, the mutation engine, the census rule and the baseline oracle all work on Termux at 3.0.4.
- The gate reads 1/1 as too little evidence unless the operator opts in.
- The 3.0.0 report listed, without raw output, a fix proven with a single mutant. Run 1a is that case, with its artefact.

**Does not show:**
- **A `MERGE` on a real project's fix.** The `MERGE` here is a one-line fixture with an opt-in flag.
- **The six fail-open fixes of 3.0.4.** These fixtures do not exercise them:
  - hidden branches;
  - every changed line broken once;
  - lines no operator reaches;
  - a rewritten claim test meeting the mutants;
  - non-UTF-8 files;
  - the canary.
- **The project's own test suite on Termux.**
- **The MCP server, or `--sandbox bwrap`.** Android has no bubblewrap.
- **Independence.** This is one operator, the maintainer, on one device.

## Reproduce

```bash
pkg install python git
python -m venv ~/agtest-venv && source ~/agtest-venv/bin/activate
pip install adversary-gate==3.0.4 coverage
```

Build the scenarios of the Setup table. Each needs two directories (baseline and patch), a diff and a coverage report; the README's [Command-Line Usage](../README.md#command-line-usage) shows how to produce both. Then run, for each scenario:

```bash
adversary-gate --baseline BASELINE_DIR --patch PATCH_DIR \
  --diff change.diff --coverage-json coverage.json \
  --claim tests/test_addition.py::test_add \
  --claim tests/test_addition.py::test_add_negative \
  --memory none --full-suite-memory none --serial-sides
```

Run 1b adds `--census-min-mutants 1`.

The script that built the fixtures and ran the four scenarios is not attached. The runs and the artefacts are the operator's.
