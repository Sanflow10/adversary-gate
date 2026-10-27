# How AdversaryGate decides

The decision model in full: the fail-closed rules, how each measurement gets its value, the pytest exit-code taxonomy, and what a decision does and does not mean. Back to the [README](../README.md).

## 🔒 The Fail-Closed Model & Double-Filter Rigor

A single invariant governs the entire system:
> *The gate only reports what a healthy test harness actually executed. Unproduced proof is never proof of clean code.*

| State | Outcome | Meaning |
|---|---|---|
| `Executed & Passed` | `Outcome.VERIFIED` | Test ran to completion and evidence confirms clean execution. |
| `Executed & Failed` | `Outcome.REFUTED` | Test ran to completion and evidence condemns the patch. |
| `Harness / Error` | `Outcome.UNVERIFIED` | Collection error, syntax error, missing file, timeout or flaky signal. **Never mergeable.** |

### Patch Decision Matrix

- **`Decision.MERGE`**: Requires **every** verdict to be `VERIFIED`, a **measured** `diff_coverage >= 80%`, a suite strength whose **80 % lower confidence bound** is `>= 0.75` when strength was measurable (`suite_strength_lower`), and the full repository test suite to pass on the patch side.
- **`Decision.BLOCK`**: Triggered if any claim is `REFUTED`, or if the patch fails the full test suite **that the baseline passed** (*collateral regression*). Checked **before** the coverage floor: direct evidence of breakage outranks missing evidence.
- **`Decision.INCONCLUSIVE`**: Triggered on `UNVERIFIED` outcomes, open circuit breakers, **coverage that was never measured**, a weak test suite (`suite_strength < 0.75`), a strength that could not be measured even though source changed, or a full suite that was already red before the patch. **Never merges.**

Precedence is `BLOCK` > `INCONCLUSIVE` > `MERGE`, and direct evidence always outranks missing evidence: a patch whose claim could not be executed but whose collateral run broke the suite is `BLOCK`, not a shrug.

**A passing claim says *how* it passed (AG-030).** `VERIFIED` comes with one of three classifications: `fixed` — the test **failed on the baseline and passes on the patch**, the strongest evidence there is that the patch fixes what the test checks (SWE-bench's *FAIL_TO_PASS*); `no_regression` — it passed on both sides, which only says nothing it checks broke; `discarded` — a test the patch added, with no "before". The output carries `claims_fixed` and `fix_proven`, so *"this patch fixes what it says it fixes"* is a field, not an inference — and `MERGE` with `fix_proven: false` is a patch that broke nothing and proved nothing about the bug it was for.

**The patch does not get to write its own answer — the baseline does.** If the claim's test file already existed on the baseline and its bytes differ on the patch, the patch's copy is never run for the verdict. The gate copies the patch tree, puts the **baseline's** test file back, and runs the claim there: the patch's code answers to the test that existed before it (the *baseline oracle*; the artefact records `"oracle": "baseline"`). So a bug hidden behind a rewritten assertion is `REFUTED` → `BLOCK`, and an honest refactor of the test file is `VERIFIED` instead of stuck. A test the patch *added to an existing file* has no baseline copy: it is judged like any added test, but only after **every test the baseline shipped in that file** has passed on the patch's code (`"oracle": "baseline-file"`) — so the new test cannot be the cover for an old one the patch bent. The oracle covers three more places a test's answer can hide:

- **Helpers.** Every baseline file that is a test file is put back, not only the claim's — `test_*`, `*_test.py`, `conftest.py`, anything under `tests/` or `__tests__/`, and `*.test.*`, `*.spec.*`, `*_test.go`, `*Test.java`, `*_spec.rb`. A helper whose path says nothing (`testing_utils.py` at the root) cannot be told apart from code under test — `numpy.testing` is public API — so it is **declared**: `--test-support testing_utils.py` (repeatable, a glob; Action input `test-support`). Declared paths are test files everywhere: out of diff coverage and mutation, and restored from the baseline. Any change to a baseline test file engages the oracle, even with the claim's own file untouched.
- **The collateral suite.** When the patch changed tests the baseline had, the full-suite run also executes the **baseline's** tests against the patch's code (`full_suite_oracle` in the artefact). A test that is no claim's, bent to agree with a bug, is a collateral regression → `BLOCK`.
- **`--test-command` claims.** The `test-id` of a command suite is a label, so the transplant always applies: the command runs from the baseline's copy of the files.

When the baseline's copy of a Python test cannot be parsed, the old rule stands: a passing rewritten test is `UNVERIFIED`. A test file the patch *adds* is judged as before. A legitimate behaviour change that rewrites its tests therefore lands on `BLOCK` — the old tests fail on the new code — which is the gate saying *"the contract changed; a person signs that"*. Nor does it get to configure the runner that judges it (AG-032): every test-harness file in the tree — the five config names pytest 9 reads (`pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `tox.ini`), plus `pyproject.toml`, `setup.cfg`, any `conftest.py`, `sitecustomize.py`, `usercustomize.py` and `*.pth`, at any depth — must be byte-for-byte the baseline's, or the claim is `UNVERIFIED`. The trees are compared directly, so a harness change the `--diff` leaves out is still caught. The paths named by `--diff` are also checked against the protected-path policy (`conftest.py`, `pytest.ini`, `pyproject.toml`, …), not only the ones passed with `--changed-path`. A directory holding `pyvenv.cfg` is a virtualenv and is skipped by the tree comparison, so from 3.0.7 a `pyvenv.cfg` in the diff is a protected path: a patch cannot turn a directory of its own into one and hide harness files there. Nor can it put a module where it would be imported in place of the real one (AG-048): `python -m pytest` searches the tree's root first, pytest adds the directory of every test module and `conftest.py` that is not a package, and a `pythonpath` setting or a test's `sys.path.insert` can add more. So in every directory that is not a package, an `inspect.py`, a `json.py` or a `pytest/` package the patch adds or changes — any standard-library name, or a module pytest itself loads — is treated as a harness change. A directory without `__init__.py` does not count (a namespace never wins over a real module), virtualenvs are skipped, and a module on pytest's path is also known to the AG-047 check by its bare name.

### How `diff_coverage` gets its value

Coverage is an **evidence** question, not a parameter. There are exactly three ways it can be supplied, and the artefact records which one was used (`diff_coverage_source`):

| `--coverage-source` | Inputs | Meaning |
|---|---|---|
| `computed` (via `auto`) | `--diff` + `--coverage-json` | The gate parses the unified diff and the coverage.py JSON report itself, and records the SHA-256 of both. **This is the only measured option.** |
| `untrusted` | `--coverage-ratio` | A number the caller asserts. Accepted only when you say so out loud; the artefact marks it `untrusted`. |
| `none` (via `auto`) | neither | No evidence. `diff_coverage_ratio` is `null` and the floor is not cleared → `INCONCLUSIVE`. |

Test files are not part of the ratio: `diff_coverage` is covered added lines over added lines **of source files**. A test executes by construction, so counting its lines let ten unexecuted source lines plus forty test lines read as `0.8`. The artefact records `test_lines_excluded` and `test_files_excluded` next to `changed_lines`.

Documents (README, LICENSE, CHANGELOG and the like) are not code and are left out. A patch that changes documents or configuration and no source line has nothing to cover: its ratio is **not measured**, so the decision cannot be `MERGE` on coverage. A patch of tests alone keeps the vacuous `1.0` it always had. That closes a gap in the earlier 3.0.0 code, where deleting one line of a README measured `1.0`, and a claim that read the README then passed. Every changed source line counts. coverage.py reports a statement on its first line only, so the second line of a statement is covered when its statement ran and counts against the patch when it did not (E); the gate reads the patch's file to tell which statement a line belongs to. A docstring line counts and is never covered. A Python file that `git diff` shows only as `Binary files ... differ` (its `.gitattributes` says `-diff` or `binary`) is **refused with exit 3**, because its changed lines are not in the diff at all (C). The evidence script diffs with `--text --no-textconv`, so the GitHub Action does not reach that case.

A bare `--coverage-ratio` with no `--coverage-source` is **exit 3 (usage error)**. Before v2.1.0 the flag defaulted to `1.0`, the gate never read a diff or a coverage report, and the GitHub Action passed neither — so `diff_coverage >= 80%` was satisfied by a default value on every run.

`--coverage-floor 0` disables the requirement explicitly; it is not a way to satisfy it.

### How `suite_strength` is measured

It is a **mutation score** — `mutants killed / mutants executable` — produced by breaking the lines the patch changed and re-running the claims — their node ids, not their whole test files (AG-039) — against them. Fewer tests per mutant can only lower the score; a whole file let a failing test outside the claims count every mutant as killed. Each claim meets the mutants with the copy of its test file that judged it: when the patch rewrote a test the baseline had, that is the baseline's copy (until 3.0.3 the mutants ran the patch's, and asserts the patch added to a rewritten test killed mutants the verdict never ran). It is not derived from coverage or from parsing pytest output.

Several things must hold before a score exists, and each one that fails leaves the strength **not measured**. The claims must pass on the unmutated patch, the reference: if they do not, a mutant that fails the same way is not a kill (D). A Python file the patch adds that cannot be paired with a baseline file, because the baseline is over 400 files or 5000 lines, is never mutated (B). Every changed Python file must be UTF-8: one that is not cannot be mutated, and it used to be skipped in silence while the other files' score stood for the patch. And every claim runs on a neutral copy of the patch: one comment per changed line, a space after each token a mutant would swap, and one no-op assignment — deleted on the same line, so it never shows in `locals()` or a class's `vars()` — in the block of each changed statement (in front of it, or after a docstring or the `from __future__` imports, which must stay first). A claim that fails on that copy reads the code's text or structure instead of what it does (A). The copy catches a claim that reads the file, a piece of a changed line, its whole tree or the bytecode of the changed function; one that reads a smaller piece of the tree, such as a single expression, or that normalizes the text before comparing, can still pass. If that copy cannot be built for a changed file, nothing is measured either.

#### Small changes: why a correct one-line fix is INCONCLUSIVE

The strength floor reads the lower bound of a confidence interval, and a small
change has few mutation sites: 1 killed out of 1 bounds the strength at 0.38,
and the default floor (0.75 at 0.80 confidence) takes **at least 5** killed
mutants. When every mutant was killed and there were simply too few, the
`reason` says *"the change is too small to measure at this floor"* and gives
the count — weak tests look different: a survivor, named in `next_steps`.
More tests cannot add mutants; only a bigger change can. What remains is the
operator's choice, never the patch's — which is why `next_steps`, which an
agent reads, names no flag:

- accept the answer: the fix is proven (`fix_proven: true`), only the strength is unmeasured, and a human reviews it;
- `--census-min-mutants N`, below: judge a complete census of the change by its exact ratio;
- `--mutate-constants`: also break integer literals, which adds sites on some lines.

**`--census-min-mutants N` (experimental, off by default).** Below the mutant budget the gate breaks *every* mutable site of the change — a census, not a sample. With this flag, when the census covers the whole change (every changed executable line has a site; statement-level sites are added for lines with no operator), nothing was stillborn, **every mutant died**, and there are at least N, the exact ratio is judged instead of the Wilson lower bound. Off by default: "1 killed out of 1 is not strong" stays the rule unless you opt in. A first version let a real bug through (one surviving operator mutant diluted by easy statement-level kills); the ledger has the case.

**`next_steps`.** Every answer that is not `MERGE` lists what would make it one, as actions a coding agent can take: `cover` (file, lines), `kill_mutant` (file, line, mutation), `add_test`, `fix_code`, `human_review_test_edit`, `raise_limit`, `see_reason`. It never suggests editing a baseline test, a harness file, a floor or the policy — those are the oracle.

- Mutants are limited to lines the patch actually wrote; mutating untouched lines would let tests covering unrelated code inflate the score.
- **A branch that shares its line with its guard gets a mutant of its own.** Coverage is per line, so `return 999 if x == 0 else x + 1` reads as covered when only the `else` ran — and the guard's operator mutant (`==` → `!=`) sends the tested input *into* the hidden arm, so the test kills it and the kill used to count as strength: on 3.0.3 that patch merged at 5/5 with `value(0)` changed from 1 to 999. Now a conditional expression is also replaced by each of its arms, an `and`/`or` loses its last operand, and an `if` or `case` whose body sits on the header's line is switched off. These mutants (`kind: "branch"`) come first on their line. One that **survives** means a branch on a changed line that no claim takes. It is listed in `mutation.blind_spots`, `suite_strength_unverified` turns `true` and the decision is `INCONCLUSIVE`, whatever the other mutants scored — the ratio itself stays what the operator mutants measured — and `next_steps` names the line. Killed, they do not enter the ratio, so they cannot pad it.
- **Added, modified and deleted** source files all count as changes. A deleted file has nothing left to mutate, so it is reported separately as `mutation.deleted_files` and cannot contribute a score: whenever `deleted_files` is non-empty the patch lands on `INCONCLUSIVE`, never `MERGE`. That includes a *mixed* patch where the surviving file measured `1.0` — that score covered only what was left after the deletion, so `suite_strength_unverified` goes `true` rather than let one file's number authorise the change as a whole.
- A mutant that no longer runs at all (syntax/collection error) is *stillborn* and excluded from both sides of the ratio rather than counted as a kill.
- `suite_strength: null` means **not measured** — there was no source change to judge. It is never reported as `1.0`, and it does not block.
- If source **did** change and no score could be produced, that is *unknown, not strong*: the decision is `INCONCLUSIVE`.
- **The floor reads a confidence bound, not the ratio (AG-023).** A ratio from one mutant is `1.0` and evidence of almost nothing. The gate computes a two-sided **Wilson interval** around `killed / counted` and applies `--suite-strength-floor` to its **lower bound** (`--strength-confidence`, default `0.80`; `0` restores the raw ratio). At the default, **5 of 5** killed mutants clear 0.75 (lower bound 0.753) and 4 of 4 do not (0.709). The artefact records `suite_strength` (the ratio), `suite_strength_lower`, `mutation.interval` and `mutation.confidence`. Consequence, said plainly: a patch whose changed lines hold fewer than five mutable operators cannot reach `MERGE` at the default — it is `INCONCLUSIVE`, because the gate cannot tell a strong suite from a lucky one on that sample.
- **Every changed line is broken at least once — with listed exceptions.** With its first operator site, or — on a line no operator reaches — with a statement-level mutant: a `return` gives `None`, a condition is negated, a loop iterates nothing, any other statement is deleted. A changed decorator is replaced by `lambda f: f` (not `cache`, `lru_cache` or `wraps`, whose removal no test can see), a changed default value by `None`, a changed base class by `object` — these lines run on import, so coverage counts them as run with no test looking; until 3.0.7 they had no mutant. Not broken, and listed in `mutation.unmutated_lines`: a call on its own line (a log call, say), an `assert`, an `import`, and a header with nothing of that kind to break. **A listed line does not stop a `MERGE`**: it is judged only by the claims and the full suite, so read the list. A statement-level mutant that survives is a blind spot too, like a branch mutant; killed, it stays out of the ratio. `mutation.mutants_needed` records how many mutants breaking every line once took. The remaining sites fill the sample, one per line before any line gets a second, files and lines in order, up to **12** mutants. Until 3.0.3 the 12 were simply the first 12 changed lines: three untested boundary changes after twelve tested lines merged without ever being broken, and `return a` → `return b` merged beside a tested line. `--mutation-max` (default **60**) is the ceiling: a change that needs more mutants than that to break every line once is **not measured**, and the reason says how far to raise it.

`--mutation-max N` is the ceiling on the cost (`0` disables the *measurement* entirely, which is recorded as such in the evidence artefact). It is the strength floor's escape hatch, the way `--coverage-floor 0` is the coverage floor's: the requirement is **disabled**, not satisfied. What it does *not* switch off is the check for source this engine cannot judge — that is a property of the patch, not of your budget, and it still forces `INCONCLUSIVE` with the budget at zero (AG-014).

---

## 📊 Measured Pytest Exit Code Taxonomy

| Exit Code | Pytest Meaning | ExecState | Gate Behavior |
|---|---|---|---|
| `0` | Tests passed | `PASS` | Evaluated against baseline comparison |
| `1` | Tests failed | `FAIL` | **The ONLY exit code counted as evidence** |
| `2` | Collection error (import crash) | `UNRUNNABLE` | `UNVERIFIED` $\rightarrow$ `INCONCLUSIVE` |
| `3` | Internal error (harness crash) | `UNRUNNABLE` | `UNVERIFIED` $\rightarrow$ `INCONCLUSIVE` |
| `4` | Usage error (bad path / node ID) | `UNRUNNABLE` | `UNVERIFIED` $\rightarrow$ `INCONCLUSIVE` |
| `5` | No tests collected | `UNRUNNABLE` | `UNVERIFIED` $\rightarrow$ `INCONCLUSIVE` |
| `-1` | Sandbox timeout | `TIMED_OUT` | `UNVERIFIED` $\rightarrow$ `INCONCLUSIVE` |

---

## 🤝 What this decision is, and what it is not

`MERGE` means *every claim the gate was asked to check executed, and the evidence it could produce cleared the floors you configured*. That is a statement about **measurements**, and nothing else.

It is not:

- a judgement about design, naming, security posture, or whether the change should exist;
- a substitute for human review — `MERGE` is permission for your team to look, not an instruction to merge;
- proof about code the gate could not execute. Where it could not measure, it said `INCONCLUSIVE` rather than guessing, and that silence is the product working.

**The decision to merge belongs to the person who signs the PR.** The gate's job is to make sure that person is not being lied to by their own pipeline. If a `MERGE` here becomes "the gate said yes, ship it", you have rebuilt the exact self-deception this project exists to remove — just with a better audit trail.

---

## 📈 Provenance & metrics

Every execution logs `ctx_model` and `ctx_commit` into the audit trail. Running patches through the same harness allows `compare_models()` to report verification rates side by side — which model's patches the gate could verify, and how often the evidence condemned them.

`self_deception_index` (`unverified_merges / merge_count`) is still computed, and is **always `0` on logs this gate wrote** (AG-029): `decide()` never returns `MERGE` with an unverified claim, so the numerator cannot grow. It only means something when the decision records come from a pipeline that *can* merge unverified work — another gate, or a hand-built log of what actually shipped. The metrics that would say whether `MERGE` means anything — overrides (PR merged while the gate said `BLOCK`/`INCONCLUSIVE`), reverts within N days of a `MERGE`, `INCONCLUSIVE` resolved to `MERGE` — need data from outside the gate and are not implemented. See [`docs/AUDITORIA_PRODUTO_v2.1.1.md` §4.4](AUDITORIA_PRODUTO_v2.1.1.md).
