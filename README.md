# AdversaryGate (v2.0.0)

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

- **`Decision.MERGE`**: Requires **every** verdict to be `VERIFIED`, `diff_coverage >= 80%`, `suite_strength >= 75%` when strength was measurable, and the full repository test suite to pass on the patch side.
- **`Decision.BLOCK`**: Triggered if any claim is `REFUTED`, or if the patch fails the full test suite **that the baseline passed** (*collateral regression*).
- **`Decision.INCONCLUSIVE`**: Triggered on `UNVERIFIED` outcomes, open circuit breakers, a weak test suite (`suite_strength < 0.75`), a strength that could not be measured even though source changed, or a full suite that was already red before the patch. **Never merges.**

Precedence is `BLOCK` > `INCONCLUSIVE` > `MERGE`, and direct evidence always outranks missing evidence: a patch whose claim could not be executed but whose collateral run broke the suite is `BLOCK`, not a shrug.

### How `suite_strength` is measured

It is a **mutation score** — `mutants killed / mutants executable` — produced by breaking the lines the patch changed and re-running the claim's tests against them. It is not derived from coverage or from parsing pytest output.

- Mutants are limited to lines the patch actually wrote; mutating untouched lines would let tests covering unrelated code inflate the score.
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
  --coverage-ratio 0.85 \
  --model "${AGENT_MODEL_NAME}" \
  --evidence-log evidence.jsonl \
  --report
```

Exit Codes for CI Integration:
- `0` — **`MERGE`**: Every claim executed cleanly and cleared coverage & suite strength floors.
- `1` — **`BLOCK`**: Regressions or collateral suite failures detected.
- `2` — **`INCONCLUSIVE`**: Infrastructure error, missing test, or weak suite (blocks merge).
- `3` — **`USAGE_ERROR`**: Bad arguments or malformed JSON.

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

      - name: Run AdversaryGate
        uses: adversary-gate/action@v2
        with:
          baseline: './baseline'
          patch: './patch'
          test-path: 'tests/test_token_expiry.py'
          test-id: 'test_token_expiry'
          coverage-floor: '0.80'
          suite-strength-floor: '0.75'
          model: '${{ matrix.model }}'
          evidence-log: 'evidence.jsonl'
```

---

## 📈 Provenance & `self_deception_index`

Every execution logs `ctx_model` and `ctx_commit` into the audit trail. Running patches through the same harness allows `compare_models()` to report verification rates side by side:

$$\text{self\_deception\_index} = \frac{\text{unverified\_merges}}{\text{merge\_count}}$$

If your product pitch is *"menos autoengano no pipeline"*, this is the dashboard tile that proves it and the metric to watch drop to zero.

---

## 📄 License

Distributed under the [MIT License](LICENSE).
