# Confronto da auditoria v2.0.1 com o código executado — e correções em v2.0.2

**Documento:** confronto entre [`AUDITORIA_SENIOR_v2.0.1.md`](AUDITORIA_SENIOR_v2.0.1.md) e o repositório auditado, executado por reprodução e não por leitura.
**Commit auditado:** `de67c5582004527d6fa6aa5e1fc9e4197d7a5862` (`v2.0.1`)
**Versão corrigida:** `v2.0.2`
**Data do confronto:** 2026-09-28

---

## Resumo executivo

**Dos oito achados da auditoria, sete se confirmaram por execução e um se confirmou parcialmente. Nenhum foi refutado.** A auditoria errou apenas num número: o total de cobertura declarado como 89% não se reproduz — mede-se 83% com os mesmos cinco módulos que ela lista.

Além de confirmar tudo, o confronto encontrou **três defeitos novos (AG-009, AG-010, AG-011)** que a auditoria não tinha visto e que têm a mesma natureza dos achados originais: código que documenta um comportamento e não o entrega.

Todos os oito achados e os três novos foram corrigidos, com teste de regressão para cada um. A suíte passou de **74 para 92 testes** e continua verde em Python 3.10, 3.11 e 3.12.

---

## Confronto achado a achado

### AG-001 — Deleção fora de `changed_source_files` — **CONFIRMADO**

O docstring de `changed_source_files` dizia *"Added, modified and deleted files all count as changed"*. O código na linha 163 iterava só o lado do patch:

```python
return sorted(rel for rel, blob in patch.items() if base.get(rel) != blob)
```

**Reprodução executada** (fixture com um arquivo apagado e outro adicionado):

```text
base .py files     : ['gone.py', 'kept.py', 'new_mod.py']
patch .py files    : ['added.py', 'kept.py']
changed_source_files = ['pkg/added.py']
deleted but NOT reported: ['pkg/gone.py']
AG-001 REPRODUCED: True
```

A deleção não aparece. Como `changed_files` fica vazio, `cli.py:202` não aciona `suite_strength_unverified`, e o gate chega a `MERGE` sem nenhuma medição de força — exatamente o impacto descrito na auditoria.

**Corrigido:** a função agora itera `set(base) | set(patch)`; `deleted_source_files()` reporta as deleções em campo próprio do artefato; uma deleção isolada agora produz `INCONCLUSIVE`.
**Teste:** `TestDeletionsAreChanges` (4 testes).

---

### AG-002 — Cobertura era input confiado — **CONFIRMADO**

A auditoria afirmou que, com o mesmo verdict verificado, `ratio=1.0 → MERGE` e `ratio=0.0 → INCONCLUSIVE`.

**Reprodução executada** em `v2.0.1` — mesma fixture, mesmo verdict, mesma força medida, variando **só** o número passado pelo chamador:

```text
--coverage-ratio 1.0   -> exit=0 decision=merge         outcome=verified strength=1.0 measured=True killed=1/1
--coverage-ratio 0.85  -> exit=0 decision=merge         outcome=verified strength=1.0 measured=True killed=1/1
--coverage-ratio 0.80  -> exit=0 decision=merge         outcome=verified strength=1.0 measured=True killed=1/1
--coverage-ratio 0.79  -> exit=2 decision=inconclusive  outcome=verified strength=1.0 measured=True killed=1/1
--coverage-ratio 0.0   -> exit=2 decision=inconclusive  outcome=verified strength=1.0 measured=True killed=1/1
```

`grep` confirma que `covered_diff_ratio` **não aparece em `cli.py`** — o cálculo existe em `src/verifiers/coverage.py` e ninguém o chama.

**Agravante encontrado durante o confronto:** a `action.yml` original não continha nenhuma entrada de cobertura, logo nenhum `--coverage-ratio` era passado. O default `1.0` satisfazia o floor **em toda execução da Action**, sem que ninguém tivesse medido nada. A auditoria identificou o defeito; a consequência operacional na Action ficou por nossa conta.

**Corrigido:** `--diff` + `--coverage-json` são agora a fonte medida (com SHA-256 dos dois artefatos no registro); `--coverage-ratio` é rejeitado com exit 3 a menos que `--coverage-source untrusted` seja declarado; sem evidência, `diff_coverage_ratio` é `null` e a decisão é `INCONCLUSIVE`.
**Testes:** `test_no_coverage_evidence_is_inconclusive_not_merge`, `test_caller_supplied_ratio_must_be_declared_untrusted`, `test_computed_diff_coverage_drives_the_decision`, `test_coverage_artefacts_are_hashed_into_the_evidence`, `test_missing_coverage_evidence_is_inconclusive`.

---

### AG-003 — "Sandbox" não isola — **CONFIRMADO**

Leitura de `src/sandbox/runner.py` (linhas 66–168): `subprocess.Popen` + `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, `RLIMIT_NPROC`, `RLIMIT_CORE`. `grep` por `namespace|seccomp|chroot|unshare|bwrap|landlock|no_new_privs` em `src/` devolve **uma única ocorrência**, e é um padrão de denylist em `path_policy.py`.

O isolamento de rede continua dependendo de `os.environ.get("ADVERSARY_NETWORK_ISOLATED") == "1"` — uma declaração, não um mecanismo.

**Corrigido dentro do que o repositório pode corrigir:** nova seção *"Execution boundary"* no README dizendo explicitamente *"resource-limited runner, not a security sandbox"* e listando o que **não** existe; `--require-network-isolation` agora é exposto pela CLI (existia em `run_test` e em `verify_claim`, mas a CLI nunca o passava — era inalcançável pelo usuário final).
**Não corrigido, por impossibilidade:** isolamento real exige container/VM/namespace, decisão de infraestrutura do executor, não do repositório.

---

### AG-004 — Symlink de `changed_paths` não checado — **CONFIRMADO**

**Reprodução executada** com um symlink *no próprio arquivo* alterado:

```text
violations WITHOUT repo_dir = []
violations WITH    repo_dir = []
is_symlink(unresolved)      = True
checked by policy (parents) = [..., '/repo/src', '/repo', '/tmp/...', '/']
```

`unresolved` (o arquivo) **não está** no conjunto checado — a política olhava `root` e `unresolved.parents`. E `gate.py:351` chamava `validate_changed_paths(changed_paths)` sem `repo_dir`, então metade da proteção nem era alcançável. A auditoria acertou os dois pontos.

Note-se que `validate_claim` (linha 111) **já** usava `(root, unresolved, *unresolved.parents)` — o padrão correto existia no mesmo arquivo, faltava aplicá-lo.

**Corrigido:** a política checa `(root, unresolved, *unresolved.parents)`; `verify_claim` passa `patch_dir`.
**Testes:** `test_file_level_symlink_in_changed_paths_is_caught`, `test_gate_validates_changed_paths_against_the_patch_dir`.

---

### AG-005 — Action sem dependências — **CONFIRMADO**

`action.yml` fazia `setup-python@v5` e chamava `python3 "${{ github.action_path }}/src/cli.py"` direto. Nenhum `pip install`. O pacote declara `pytest>=8.0.0`, mas isso só ajuda quem instala do PyPI.

**Corrigido:** passo `Install AdversaryGate and its dependencies` (`python -m pip install "${{ github.action_path }}"`) antes da execução, e `action-smoke` no novo CI roda a Action inteira em runner limpo.

---

### AG-006 — Sem CI de testes — **CONFIRMADO**

API do GitHub no momento do confronto:

```text
368703207 .github/workflows/pypi-publish.yml      active
368776377 dynamic/dependabot/update-graph         active
```

Nenhum workflow de teste.

**Corrigido:** `.github/workflows/ci.yml` com três jobs — suíte em matriz **3.10/3.11/3.12**, build + instalação do wheel + smoke da CLI, e smoke da Action em runner limpo esperando `decision=merge`.

---

### AG-007 — Cobertura desigual — **CONFIRMADO PARCIALMENTE (e a auditoria errou no total)**

Os cinco números por módulo da auditoria batem exatamente com a medição local:

| Módulo | Auditoria | Confronto |
|---|---:|---:|
| `src/cli.py` | 80% | **80%** |
| `src/core/gate.py` | 90% | **90%** |
| `src/sandbox/runner.py` | 63% | **63%** |
| `src/verifiers/coverage.py` | 33% | **33%** |
| `src/verifiers/strength.py` | 87% | **87%** |
| **Total** | **89%** | **83%** |

**O total de 89% não se reproduz.** Com `coverage run --source=src -m pytest` o total é **83%** (1021 instruções, 171 perdidas). Os cinco subtotais idênticos descartam a hipótese de ter sido medido num código diferente; a diferença de 6 pontos sugere que a medição da auditoria incluiu `tests/` no escopo, o que inflaria o total.

**Correção aplicada e re-medição** — a cobertura subiu de **83% → 87%** com os novos testes de regressão:

| Módulo | Auditoria | Antes | Depois |
|---|---:|---:|---:|
| `src/cli.py` | 80% | 80% | **80%** |
| `src/core/gate.py` | 90% | 90% | **91%** |
| `src/core/path_policy.py` | — | 84% | **93%** |
| `src/sandbox/runner.py` | 63% | 63% | **63%** ← o mais fraco |
| `src/verifiers/coverage.py` | 33% | 33% | **100%** |
| `src/verifiers/strength.py` | 87% | 87% | **88%** |
| **Total** | **89%** | **83%** | **87%** |

`src/verifiers/coverage.py` ia de 33% a 100% porque o módulo passou a ser efetivamente *chamado* — ele já existia desde a auditoria, só ninguém o invocava (era literalmente o defeito AG-002).

A auditoria tinha razão no diagnóstico de "cobertura desigual": `sandbox/runner.py` segue em 63% e é o único módulo que não melhorou, porque boa parte dele são caminhos de erro de `subprocess`/`RLIMIT` que só se exercitam num runner com limites distintos.

---

### AG-008 — Metadados de release inconsistentes — **CONFIRMADO**

Verificado pela API do GitHub e pelo clone:

| Item da auditoria | Verificado |
|---|---|
| README diz `v2.0.0`, pacote é `2.0.1` | **Sim** — `README.md:1` vs `pyproject.toml` |
| `uses: adversary-gate/action@v2` não existe | **Sim** — repositório é `Sanflow10/adversary-gate` |
| `LICENSE` ausente | **Sim** — `GET /repos/.../license` → **404**; `license` no GitHub = `null` |
| Execuções de publicação falhando | **Sim** — as duas últimas `Publish to PyPI` com `conclusion: failure` |

Detalhe que a auditoria registrou corretamente e que confirmamos pelo log do workflow: o passo **`Build binary wheel and source distribution` passa**; só o passo **`Publish package distributions to PyPI` falha**. Isso confirma que é cadastro do Trusted Publisher no PyPI, não um problema do código.

**Corrigido no repositório:** `LICENSE` adicionado (MIT — já declarado em `pyproject.toml` e no README; o arquivo só faltava), README em `v2.0.2`, referência da Action corrigida para `Sanflow10/adversary-gate@v2`.
**Não corrigível no repositório:** o cadastro no PyPI. Ver "Pendências" abaixo.

---

## Achados novos, não presentes na auditoria

### AG-009 — `classify()` sombreava `NEW_BUG` e o ramo de timeout do baseline — **Alta**

`gate.classify()` abria com:

```python
if run_outcome.run_count > 1:
    baseline_consistent = base in (ExecState.PASS, ExecState.FAIL)
    if not baseline_consistent:
        return ... UNVERIFIED, "baseline result was not consistent across runs"
```

Todo `StabilityPolicy` roda **mais de uma vez** — `deterministic=3`, `concurrency=100`, `performance=5`. Logo `run_count > 1` é sempre verdadeiro e a guarda dispara em **todo** claim cujo baseline não é PASS/FAIL.

**Reprodução executada:**

```text
policy.runs (deterministic) = 3
baseline N/A + patch PASS (novo teste ok)   -> unverified  flaky  | baseline result was not consistent across runs
baseline N/A + patch FAIL (novo bug)        -> unverified  flaky  | baseline result was not consistent across runs
runs values: {'deterministic': 3, 'concurrency': 100, 'performance': 5}
=> run_count > 1 for every bug kind: True
```

Consequências:

1. O branch `NEW_BUG` (`gate.py`, comentário *"test is absent on baseline and fails on patch"*) era **código morto** — um teste novo que falha nunca era `REFUTED`.
2. O branch *"baseline timed out; no clean reference"* também era morto, pelo mesmo motivo.
3. Um patch que **adiciona** um arquivo de teste nunca podia ser `VERIFIED`, logo nunca chegava a `MERGE`.
4. O motivo registrado no artefato era **falso**: dizia "flaky" quando o baseline simplesmente não tinha o teste.

`git blame` mostra que a guarda e os dois branches vieram do **mesmo commit** (`84b4888`, release inicial) — é descuido de origem, não uma decisão posterior que precise ser respeitada.

**Corrigido:** a guarda redundante foi removida; cada estado passa a chegar ao branch que o nomeia.
**Testes:** `TestBaselineNotApplicable` (4 testes).

---

### AG-010 — Artefato não distingue "suíte passou" de "suíte não rodou" — **Média**

`cli.py` só gravava `full_suite_exit_codes` quando precisava rodar o lado do baseline, ou seja, só quando o patch **falhou**:

```python
if full_codes is not None:
    payload["full_suite_exit_codes"] = list(full_codes)
```

Resultado: `full_suite_exit_codes: null` significava ao mesmo tempo *"a suíte do patch passou"* e *"a suíte nunca rodou"* (`--full-suite-path ''`). Para um produto cujo pilar é evidência auditável, o registro não permitia provar que a verificação colateral aconteceu.

**Corrigido:** campo `full_suite_ran` no JSON de saída e no registro de decisão.
**Teste:** `test_coverage_artefacts_are_hashed_into_the_evidence` (assegura `full_suite_ran == true`).

---

### AG-011 — Floor de cobertura era checado **antes** de `REFUTED` — **Média**

O docstring de `decide()` declarava:

> *"Precedence is deliberate and is `BLOCK` > `INCONCLUSIVE` > `MERGE`, ordered so that direct evidence always outranks missing evidence: ... 1. malformed input, coverage below floor -> INCONCLUSIVE; 2. any verdict REFUTED -> BLOCK"*

Mas o código checava o floor **primeiro**. Um patch cuja execução **provou** uma regressão era reportado como `INCONCLUSIVE` sempre que a cobertura estivesse baixa — evidência faltante vencendo evidência observada, exatamente o inverso do que o próprio método documentava. Estava encoberto porque o default `1.0` fazia o check de cobertura passar sempre.

A ordenação também contradizia `test_missing_test_file_with_regressed_full_suite_exits_block`, que esperava `BLOCK` num cenário sem cobertura.

**Corrigido:** as duas rotas de `BLOCK` (`REFUTED` e regressão colateral da suíte completa) agora vêm antes da checagem de cobertura.
**Testes:** `test_refuted_outranks_missing_coverage_evidence`, `test_full_suite_regression_outranks_missing_coverage_evidence`.

---

## Verificação executada

| Verificação | Resultado |
|---|---|
| Suíte antes das correções | `74 passed, 3 subtests` |
| Suíte depois das correções — Python **3.10.19** | **`92 passed, 3 subtests`** |
| Suíte depois das correções — Python **3.11.14** | **`92 passed, 3 subtests`** |
| Suíte depois das correções — Python **3.12.3** | **`92 passed, 3 subtests`** |
| `python -m compileall src tests` | OK |
| Verificação das correções AG-001..AG-011 | **23/23 passaram** |
| Cobertura da suíte própria | 83% → **87%** |
| Wheel `adversary_gate-2.0.2-py3-none-any.whl` | construído |
| Instalação do wheel em venv limpa | OK |
| `adversary-gate --help` com as novas flags | OK |
| `LICENSE` dentro do wheel | `License-Expression: MIT`, `License-File: LICENSE` |
| YAML de `action.yml` e `ci.yml` | válidos |

Os três interpretes foram executados de verdade (`uv python install 3.10 3.11`), não presumidos — a matriz do CI novo só foi declarada depois de verificada.

Os 23 checks de verificação cobrem cada achado em dois níveis: o estado da artefato *e* a decisão observável. Por exemplo, AG-001 não é só "`changed_source_files` agora reporta a deleção" — é também "um patch cuja única mudança é uma deleção agora sai `exit=2 INCONCLUSIVE` onde antes saía `exit=0 MERGE`".

---

## Pendências

1. **Trusted Publishing do PyPI (AG-008).** As execuções de `Publish to PyPI` falham com `invalid-publisher`. O passo de build passa, o de publish falha — é cadastro em `pypi.org/manage/project/adversary-gate/settings/publishing/`, **fora do repositório**. Não foi alterado; exige decisão de quem administra o projeto.
2. **Isolamento real de execução (AG-003).** Container/VM/network-namespace são infraestrutura do executor. O README agora diz isso explicitamente, mas o runner continua sem isolamento de segurança.
3. **Cobertura de patches com alteração mista.** Um patch que modifica um arquivo e deleta outro tem o primeiro medido e o segundo não; o artefato registra `deleted_files`, mas a decisão só é forçada a `INCONCLUSIVE` quando a deleção é a única mudança. A salvaguarda restante é a suíte completa.
4. **`DiffCoverage.ratio` é `1.0` quando não há linhas adicionadas.** Um patch puramente deletante reporta cobertura perfeita por vacuidade — a mesma cegueira de AG-001 aplicada à cobertura. Mantido por ser o comportamento documentado da propriedade; registrado aqui como dívida.
5. **Tag `v2.0.2` não foi criada.** Publicaria no PyPI e reexporia a falha do publisher.
