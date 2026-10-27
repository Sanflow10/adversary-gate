# Language support

What the gate does on a repository that is not Python/pytest. Back to the [README](../README.md).

## 🌐 Language support — read this if your repo is not Python

**Today the gate measures Python.** Three layers are Python-specific, and each
one is a different kind of "no":

| Layer | What it does | Non-Python |
|---|---|---|
| Test execution | `python -m pytest <path::id>` | never runs; pytest reports *no tests collected* (exit 5) |
| Coverage | parses `coverage json` (`files → executed_lines`) | no report in that shape to parse |
| Mutation | tokenizes Python and swaps operators | cannot break a `.cpp` or a `.rs` |

Everything *above* those layers — decision precedence, the evidence questions,
the SHA-256'd artefact, exit codes — is language-agnostic. The brain is
portable; the senses are not.

### What you get today on a non-Python patch

**`INCONCLUSIVE`, never `MERGE`** — and that is deliberate, not a limitation
that happens to work out.

Until AG-012 it was worse than useless, it was wrong. The mutation scanner only
walked `*.py`, so a patch whose entire effect was in `calculator.cpp` reported
`changed_files: []` and the reason **`"no non-test source file changed between
baseline and patch"`** — a statement that was simply false. With a passing
Python test and a coverage artefact, that patch reached **`MERGE`** while the
only thing that had executed anywhere was `assert True`:

```console
$ adversary-gate --baseline b --patch p --test-path test_ok.py --test-id test_ok \
      --diff change.diff --coverage-json cov.json
decision: merge        # changed calculator.cpp from `a - b` to `a * b`
suite_strength: null   # reason: "no non-test source file changed"
```

It now records what it actually observed and refuses to judge what it cannot:

```console
decision: inconclusive                                        exit 2
suite_strength: null | suite_strength_unverified: true
mutation.foreign_changed_files: ["calculator.cpp"]
mutation.reason: "1 non-Python source file(s) changed that suite strength
                   cannot judge: calculator.cpp"
```

`INCONCLUSIVE` here is the product working: *the patch changed code we did not
execute, so we are not saying it is clean.* The alternative — a green checkmark
over a test suite that never looked at the change — is the exact failure this
project exists to prevent.

#### The mixed patch, which is the one that hides

The pure-C++ case is easy to spot; the dangerous one is **Python + C++ in the
same patch**, because there the Python half *does* measure:

```console
$ adversary-gate ... --diff change.diff --coverage-json cov.json
decision: merge                                                 # before
coverage: 0.80   suite_strength: 1.0   suite_strength_unverified: false
mutation.foreign_changed_files: ["vec.cpp"]      # recorded, never acted on
```

A `1.0` next to an untouched bug reads as a clean patch. Coverage often masks
this by accident — an unexecuted `.cpp` line drags the ratio down — but that is
a coincidence of arithmetic, not a guarantee, so the case is now pinned
directly: **any** non-Python source in the patch makes
`suite_strength_unverified` fire, and the artefact says what the score is a
score *for*:

```console
decision: inconclusive                                         exit 2
suite_strength: 1.0 | suite_strength_unverified: true
mutation.reason: "score covers the 1 Python file(s) mutated only; 1 non-Python
                  source file(s) in this patch were never judged"
```

### Which files count as "source we cannot judge"

`NON_SOURCE_SUFFIXES` in `src/adversary_gate/verifiers/strength.py` — a **denylist**, not an
allowlist. Anything that is not Python, not a test, and not on the list of
things that are plainly not code (`.md`, `.yml`, `.json`, images, archives,
object files) counts as source we cannot judge. That direction is
deliberate: the previous shape was an allowlist of languages somebody had
remembered to type, and every suffix outside it was invisible. Compiled code a
runtime loads (`.so`, `.pyd`, `.dll`, `.dylib`, `.pyc`, `.jar`, `.class`,
`.wasm`, `.exe`) is code: Python imports `rules.so` before `rules.py`. Until
3.0.7 it was on the not-code list. A project that builds extensions in place
now gets INCONCLUSIVE when the built files differ from the baseline; build them
outside the tree, or into `build/`. The tests also never read bytecode from the
tree's `__pycache__`.

That is not a hypothetical. Until AG-013, a patch whose entire effect was in
`schema.sql` reported:

~~~
decision: merge                                   # changed schema.sql
suite_strength: null
mutation.reason: "no source file changed between baseline and patch"
~~~

— the exact false statement AG-012 was opened to remove, still reachable
through a suffix nobody had listed. `.proto`, `.pyi`, `.vue`, `.sol`, `.tf`
and `.r` were all in the same hole, and the **mixed** patch (a measured
`calc.py` plus an unmeasured `schema.sql`) merged with `suite_strength: 1.0`
and `suite_strength_unverified: false`.

A list of remembered languages has an end; a list of what is *not* code does
not. Unrecognised now fails closed.

Two sources are unioned, so either alone being wrong cannot open a hole:

| Source | Catches |
| --- | --- |
| directory scan (denylist) | changes a hand-written or partial diff left out |
| `--diff` paths (authoritative) | anything the scan cannot see — generated files, paths absent from both trees |

A documentation-only patch is still unaffected, because `.md` is on the list.

**A test's name is confirmed against who imports it (from 3.0.6).** Tests are
left out of coverage and mutation, and the oracle puts them back from the
baseline, so a product module under a test path — a `test_*`/`*_test.py` name,
or a `tests` directory — used to be invisible: a change to it was never judged.
Now a test-pathed Python module that a **non-test** module imports is judged as
source. A helper imported only by other tests stays a test; `conftest.py` and
`--test-support` are never reclassified. The detection reads imports from the
AST, so a module reached only through an import path it cannot match, or a
dynamic import, is not caught (SECURITY.md); declare a real helper with
`--test-support`, and never put product code under a test path.

### What shipped, and what still requires

**1. Execution on any stack — `--test-command` (shipped).**

```bash
adversary-gate --baseline before --patch after \
      --test-path run_tests.sh --test-command "./run_tests.sh"
```

Replaces pytest with a command you supply, on the baseline side and the patch
side. The convention is the table below with the pytest names stripped out:

| Exit | Meaning | ExecState |
| --- | --- | --- |
| `0` | passed | `PASS` |
| `1` | failed | `FAIL` — the only code that counts as evidence |
| `2`/`3`/`4` | the harness itself broke | `UNRUNNABLE` → `UNVERIFIED` |
| timeout | gave up | `TIMED_OUT` → `UNVERIFIED` |

`--test-id` is no longer required when `--test-command` is given: a suite that
is one command has no node IDs to name, so the claim is labelled
`(test-command)`. The collateral full-suite run uses the same command unless
`--full-suite-command` says otherwise — declaring a non-pytest suite does not
silently cost you the collateral-regression check.

**2. A coverage adapter — not yet.** `llvm-cov export`, `grcov`,
`cargo-llvm-cov` and `cargo tarpaulin` all emit different shapes; normalise
them to `{files: {path: {executed_lines: [...]}}}` and `covered_diff_ratio`
needs no change at all.

**3. A mutation adapter — not yet, and this one is the hard one.**
`cargo-mutants` is mature for Rust; for C++ the options (`mull`, LLVM
pass-based) are much thinner. Without it `suite_strength` stays `null` and
every non-Python patch is `INCONCLUSIVE`, so **this is a requirement for
MERGE, not an optimisation.**

The distinction between (1) and (3) matters. `--test-command` turns *"the
gate cannot run my suite"* into *"the gate ran it"*, which converts
`UNVERIFIED` into a genuine `PASS` or `FAIL` — a real `BLOCK` becomes
possible. Only a mutation adapter converts `INCONCLUSIVE` into `MERGE`.
Until it exists the honest answer for a non-Python repo stays
`INCONCLUSIVE`, and that is the product working rather than failing.
