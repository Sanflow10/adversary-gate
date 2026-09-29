# AdversaryGate (v2.1.0)

> **Uso alto de IA ≠ confiança alta.**
> O custo de um pipeline com agentes de código não está na inteligência do modelo, está no autoengano do pipeline.

An evidence-based fail-closed verification gate for AI coding agents where **uncertainty is a first-class result (`INCONCLUSIVE`)** instead of a silent approval.

---

## 🎬 Watch it decide (60 seconds)

![AdversaryGate — demonstração ao vivo](demo/demo.gif)

**B and C are the same patch.** Same code, same tests, same execution. The only difference is that C brought evidence (`--diff` + `--coverage-json`); B brought none.

| | Patch | Coverage evidence | Decision | Exit |
|---|---|---|---|---|
| **A** | breaks the test | declared `untrusted` | `BLOCK` | 1 |
| **B** | clean | none — never measured | `INCONCLUSIVE` | 2 |
| **C** | clean | measured, both artefacts SHA-256'd | `MERGE` | 0 |

That middle row is the whole product. An unproduced measurement is not a measurement, so it cannot clear a floor — and a number nobody can re-derive is an opinion with a false precision label.

```bash
python3 demo/demo.py          # runs the three scenarios; exits non-zero if one regresses
python3 demo/render_gif.py    # rebuilds demo/demo.gif from the captured frames
```

`demo/demo.py` is an **acceptance test**, not a screenshot: it builds the fixtures, runs the real CLI as a subprocess, reads the real exit codes, and fails if any of the three decisions changes. Its evidence artefacts are checked in under `demo/out/`.

> **`INCONCLUSIVE` is not an error.** It is the gate saying *"I did not measure that"*, and it is exit 2 so CI treats it as "do not merge yet", not as "pass". See [How `diff_coverage` gets its value](#how-diff_coverage-gets-its-value) for the one-step path from `INCONCLUSIVE` to `MERGE`.

---

## 🎯 The Thesis

1. **Uso alto de IA ≠ Confiança alta**: Quanto mais um time depende de agentes no fluxo real de engenharia, mais aparece o custo do *"parece certo"*. Agentes geram código fluente e aparentemente correto, mas pipelines ingênuos que colapsam erros de infraestrutura aprovam patches com testes quebrados ou pulados.
2. **Troca de Modelo como Sintoma**: Times trocam de modelo (Claude → GPT → Gemini) buscando credibilidade nos Pull Requests. Isso é **falta de verificação determinística, não falta de modelo**. O AdversaryGate executa exatamente o mesmo harness de teste sem invocar LLMs no verificador, tornando os modelos comparáveis empiricamente.
3. **Perda e Reprocessamento**: O prejuízo financeiro das empresas é concreto: *merged regressions*, tarefas "concluídas" que não estão, rollbacks e horas de code review humano repassando o mesmo PR.
4. **Menos Autoengano do Pipeline**: O produto não vende "IA mais inteligente". Vende **menos autoengano no pipeline**, medido numericamente pelo **`self_deception_index`**.

---

## 🔒 The Fail-Closed Model & Double-Filter Rigor

A single invariant governs the entire system:
> *The gate only reports what a healthy test harness actually executed. Unproduced proof is never proof of clean code.*

| State | Outcome | Meaning |
|---|---|---|
| `Executed & Passed` | `Outcome.VERIFIED` | Test ran to completion and evidence confirms clean execution. |
| `Executed & Failed` | `Outcome.REFUTED` | Test ran to completion and evidence condemns the patch. |
| `Harness / Error` | `Outcome.UNVERIFIED` | Collection error, syntax error, missing file, timeout or flaky signal. **Never mergeable.** |

### Patch Decision Matrix

- **`Decision.MERGE`**: Requires **every** verdict to be `VERIFIED`, a **measured** `diff_coverage >= 80%`, `suite_strength >= 75%` when strength was measurable, and the full repository test suite to pass on the patch side.
- **`Decision.BLOCK`**: Triggered if any claim is `REFUTED`, or if the patch fails the full test suite **that the baseline passed** (*collateral regression*). Checked **before** the coverage floor: direct evidence of breakage outranks missing evidence.
- **`Decision.INCONCLUSIVE`**: Triggered on `UNVERIFIED` outcomes, open circuit breakers, **coverage that was never measured**, a weak test suite (`suite_strength < 0.75`), a strength that could not be measured even though source changed, or a full suite that was already red before the patch. **Never merges.**

Precedence is `BLOCK` > `INCONCLUSIVE` > `MERGE`, and direct evidence always outranks missing evidence: a patch whose claim could not be executed but whose collateral run broke the suite is `BLOCK`, not a shrug.

### How `diff_coverage` gets its value

Coverage is an **evidence** question, not a parameter. There are exactly three ways it can be supplied, and the artefact records which one was used (`diff_coverage_source`):

| `--coverage-source` | Inputs | Meaning |
|---|---|---|
| `computed` (via `auto`) | `--diff` + `--coverage-json` | The gate parses the unified diff and the coverage.py JSON report itself, and records the SHA-256 of both. **This is the only measured option.** |
| `untrusted` | `--coverage-ratio` | A number the caller asserts. Accepted only when you say so out loud; the artefact marks it `untrusted`. |
| `none` (via `auto`) | neither | No evidence. `diff_coverage_ratio` is `null` and the floor is not cleared → `INCONCLUSIVE`. |

A bare `--coverage-ratio` with no `--coverage-source` is **exit 3 (usage error)**. Before v2.0.2 the flag defaulted to `1.0`, the gate never read a diff or a coverage report, and the GitHub Action passed neither — so `diff_coverage >= 80%` was satisfied by a default value on every run.

`--coverage-floor 0` disables the requirement explicitly; it is not a way to satisfy it.

### How `suite_strength` is measured

It is a **mutation score** — `mutants killed / mutants executable` — produced by breaking the lines the patch changed and re-running the claim's tests against them. It is not derived from coverage or from parsing pytest output.

- Mutants are limited to lines the patch actually wrote; mutating untouched lines would let tests covering unrelated code inflate the score.
- **Added, modified and deleted** source files all count as changes. A deleted file has nothing left to mutate, so it is reported separately as `mutation.deleted_files` and cannot contribute a score: a deletion-only patch measures as *not measured* and therefore lands on `INCONCLUSIVE`, never `MERGE`.
- A mutant that no longer runs at all (syntax/collection error) is *stillborn* and excluded from both sides of the ratio rather than counted as a kill.
- `suite_strength: null` means **not measured** — there was no source change to judge. It is never reported as `1.0`, and it does not block.
- If source **did** change and no score could be produced, that is *unknown, not strong*: the decision is `INCONCLUSIVE`.

`--mutation-max N` bounds the cost (`0` disables the measurement entirely, which is recorded as such in the evidence artefact).

---

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

`FOREIGN_SOURCE_SUFFIXES` in `src/verifiers/strength.py`: C/C++, Rust, Go, JVM,
.NET, Swift, Ruby, PHP, JS/TS, shell and the rest of the common compiled and
interpreted set. Files that are not code (`.md`, `.yml`, `.json`, images) and
files that are tests (`test_*`, anything under `tests/`) are excluded, so a
documentation-only patch is unaffected.

### What full support actually requires

Three adapters, in this order of value:

1. **`--test-command`** — run an arbitrary command instead of pytest, with a
   documented pass/fail/harness-error convention. Unlocks execution first, and
   is worth having even for Python repos with a non-pytest suite.
2. **A coverage adapter** — `llvm-cov export`, `grcov`, `cargo-llvm-cov` and
   `cargo tarpaulin` all emit different shapes; normalise them to
   `{files: {path: {executed_lines: [...]}}}` and `covered_diff_ratio` needs no
   change at all.
3. **A mutation adapter** — `cargo-mutants` is mature for Rust; for C++ the
   options (`mull`, LLVM pass-based) are much thinner. Without this layer
   `suite_strength` stays `null` and every patch is `INCONCLUSIVE`, so **this
   one is a hard requirement for MERGE, not an optimisation.**

Until all three exist, the honest answer for a non-Python repo stays
`INCONCLUSIVE`.

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

## 📦 Installation

**Install from GitHub — that is where the fixes are:**

```bash
pip install git+https://github.com/Sanflow10/adversary-gate.git
```

| Route | Resolves to | Note |
|---|---|---|
| `pip install git+https://github.com/Sanflow10/adversary-gate.git` | 2.1.0 | current |
| `git clone … && pip install .` | `main` | current |
| `pip install adversary-gate` (PyPI) | **2.0.1** | ⚠️ stale — see below |
| `uses: Sanflow10/adversary-gate@main` | `main` | current |

Run from source with no install at all:

```bash
python3 src/cli.py --help
```

### Why PyPI is behind

PyPI holds `2.0.0` and `2.0.1` only, and **`2.0.1` is the version the audit was
run against** — it predates every fix in this document:

```console
$ pip install adversary-gate
$ adversary-gate --baseline b --patch p --diff changes.diff
error: unrecognized arguments: --diff changes.diff
```

`--diff` and `--coverage-json` did not exist yet (AG-002), and on that version
the gate returns `decision: merge` with `diff_coverage_ratio: null` — coverage
simply was not a question. Running it against a patch that changed only
`calculator.cpp` (rewriting `a - b` to `a * b`) merges on the strength of a
Python test whose only assertion is `assert True`.

Publishing a fixed version is blocked by **AG-008**: the Trusted Publisher is
registered in the PyPI project settings (`pypi.org/manage/project/
adversary-gate/settings/publishing/`), which cannot be changed from inside this
repository. Until someone with PyPI access registers it, **PyPI will keep
serving the buggy version and GitHub is the only correct route.**

---

## 🚀 Quick Start (CLI & GitHub Action)

### Command-Line Usage

```bash
adversary-gate \
  --baseline /path/to/before \
  --patch /path/to/after \
  --test-path tests/test_auth.py \
  --test-id test_token_expiry \
  --diff changes.diff \
  --coverage-json coverage.json \
  --model "${AGENT_MODEL_NAME}" \
  --evidence-log evidence.jsonl \
  --report
```

Produce the two artefacts in your own pipeline, for example:

```bash
git diff --no-ext-diff baseline...patch > changes.diff
coverage json -o coverage.json          # after: coverage run -m pytest
```

Exit Codes for CI Integration:
- `0` — **`MERGE`**: Every claim executed cleanly and cleared coverage & suite strength floors.
- `1` — **`BLOCK`**: Regressions or collateral suite failures detected.
- `2` — **`INCONCLUSIVE`**: Infrastructure error, missing test, missing coverage evidence, or weak suite (blocks merge).
- `3` — **`USAGE_ERROR`**: Bad arguments, malformed JSON, or a coverage claim that was not declared as untrusted.

---

### GitHub Action Integration (`action.yml`)

**One ref in.** `base-sha` derives the baseline, the diff and the coverage
report itself, so the first thing you install is not a `INCONCLUSIVE`:

```yaml
name: Verification Gate

on: [pull_request]

jobs:
  verify-agent-patch:
    runs-on: ubuntu-latest
    steps:
      # base-sha is a commit in history: this is not optional.
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Run AdversaryGate
        uses: Sanflow10/adversary-gate@main
        with:
          base-sha: ${{ github.event.pull_request.base.sha }}
          test-path: 'tests/test_token_expiry.py'
          test-id: 'test_token_expiry'
          evidence-log: 'evidence.jsonl'
```

> **Which ref?** There is no `v2` tag — only `v2.0.0` and `v2.0.1`, and both
> point at the audited version with the bugs. `@main` is the only ref that
> resolves to a fixed build. For anything that matters, pin the full commit SHA
> instead (`uses: Sanflow10/adversary-gate@<full-sha>`): a tag would be ideal,
> but pushing one triggers the PyPI publish workflow, which fails — AG-008 — and
> would leave a red run on every release until that is registered.

What that does, and where each piece comes from:

| Artefact | Derived from |
|---|---|
| `baseline` | `git archive <base-sha>` — the committed tree, no worktree leftovers |
| `diff` | `git diff --no-ext-diff <base-sha>...HEAD` |
| `coverage-json` | `coverage-command`, run in the checkout |
| `patch` | the checkout itself |

If the ref is not in your local history the Action **stops with exit 4 and
tells you `fetch-depth: 0`** rather than measuring against a baseline it
invented. `coverage-command` defaults to `coverage run -m pytest` + `coverage json`;
override it to point at your own suite, or pass `coverage-json` to supply the
artefact yourself.

**Everything it derived is one `git diff` you could have run by hand.** When
you would rather assemble it yourself — a monorepo, a generated diff, coverage
from a non-coverage.py tool — pass the paths explicitly instead:

```yaml
      - name: Collect coverage evidence
        run: |
          git diff --no-ext-diff ${{ github.event.pull_request.base.sha }}...HEAD > changes.diff
          python -m pip install pytest coverage
          coverage run -m pytest
          coverage json -o coverage.json

      - name: Run AdversaryGate
        uses: Sanflow10/adversary-gate@main
        with:
          baseline: './baseline'
          patch: './patch'
          test-path: 'tests/test_token_expiry.py'
          test-id: 'test_token_expiry'
          diff: 'changes.diff'
          coverage-json: 'coverage.json'
          coverage-floor: '0.80'
          suite-strength-floor: '0.75'
          model: '${{ matrix.model }}'
          evidence-log: 'evidence.jsonl'
```

Without `diff` + `coverage-json` (and without `base-sha` to derive them) the Action reports `INCONCLUSIVE`, because there is no coverage evidence to clear the floor with. If you deliberately want to assert the number instead, set `coverage-ratio` **and** `coverage-source: 'untrusted'`.

---

## 🛡️ Execution boundary (read this before trusting it with untrusted code)

`src/sandbox/runner.py` applies POSIX resource limits only: `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, `RLIMIT_NPROC`, plus a timeout that kills the process group. That is a **resource-limited runner, not a security sandbox**.

What it does *not* provide: network namespaces, seccomp, chroot, containers, an unprivileged user, a read-only filesystem, or any restriction on what the test code may do. The test code runs arbitrary code from the repository with the runner's own privileges.

The network flag is a **declaration, not a mechanism**: `--require-network-isolation` refuses to start unless `ADVERSARY_NETWORK_ISOLATED=1`, which is an assertion made by *you* about *your* environment. It cannot verify itself.

**Run untrusted patches inside an outer sandbox you control** — rootless container, VM, or an ephemeral isolated runner. AdversaryGate verifies test outcomes; it does not contain hostile code.

---

## 📈 Provenance & `self_deception_index`

Every execution logs `ctx_model` and `ctx_commit` into the audit trail. Running patches through the same harness allows `compare_models()` to report verification rates side by side:

$$\text{self\_deception\_index} = \frac{\text{unverified\_merges}}{\text{merge\_count}}$$

If your product pitch is *"menos autoengano no pipeline"*, this is the dashboard tile that proves it and the metric to watch drop to zero.

---

## 📄 License

Distributed under the [MIT License](LICENSE).
