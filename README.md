# AdversaryGate (v2.0.2)

> **Uso alto de IA ≠ confiança alta.**
> O custo de um pipeline com agentes de código não está na inteligência do modelo, está no autoengano do pipeline.

An evidence-based fail-closed verification gate for AI coding agents where **uncertainty is a first-class result (`INCONCLUSIVE`)** instead of a silent approval.

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

Install via PyPI:

```bash
pip install adversary-gate
```

Or run directly from source:

```bash
python3 -m cli --help
```

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

Add AdversaryGate to your GitHub Workflow:

```yaml
name: Verification Gate

on: [pull_request]

jobs:
  verify-agent-patch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Collect coverage evidence
        run: |
          git diff --no-ext-diff ${{ github.event.pull_request.base.sha }}...HEAD > changes.diff
          python -m pip install pytest coverage
          coverage run -m pytest
          coverage json -o coverage.json

      - name: Run AdversaryGate
        uses: Sanflow10/adversary-gate@v2
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

Without `diff` + `coverage-json` the Action reports `INCONCLUSIVE`, because there is no coverage evidence to clear the floor with. If you deliberately want to assert the number instead, set `coverage-ratio` **and** `coverage-source: 'untrusted'`.

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
