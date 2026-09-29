# Confronto da auditoria v2.0.1 com o código executado — e correções em v2.0.2 → v2.1.0

**Documento:** confronto entre [`AUDITORIA_SENIOR_v2.0.1.md`](AUDITORIA_SENIOR_v2.0.1.md) e o repositório auditado, executado por reprodução e não por leitura.
**Commit auditado:** `de67c5582004527d6fa6aa5e1fc9e4197d7a5862` (`v2.0.1`)
**Versões cobertas:** `v2.0.2` (correções AG-001..AG-011) → `v2.1.0` (AG-012 e o recurso `base-sha`)
**Data do confronto:** 2026-09-28 · **AG-012 e `base-sha`:** 2026-09-29

---

## Resumo executivo

**Dos oito achados da auditoria, sete se confirmaram por execução e um se confirmou parcialmente. Nenhum foi refutado.** A auditoria errou apenas num número: o total de cobertura declarado como 89% não se reproduz — mede-se 83% com os mesmos cinco módulos que ela lista.

Além de confirmar tudo, o confronto encontrou **três defeitos novos (AG-009, AG-010, AG-011)** que a auditoria não tinha visto e que têm a mesma natureza dos achados originais: código que documenta um comportamento e não o entrega. Um quarto (**AG-012**) apareceu depois, ao responder se C++ e Rust estariam "faltando de robustez".

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

### AG-012 — Patch só em C++/Rust chegava a `MERGE` — **Alta (fail-open)**

A pergunta que motivou o achado foi "C++ e Rust podem ser o que falta em robustez". A resposta, obtida por execução, é mais grave: **não faltava um recurso, faltava um defeito.**

`changed_source_files()` fazia `root.rglob("*.py")`. Uma mudança só em `.cpp` produzia `changed_files: []`, o guarda adicionado em AG-001 (`elif mutation_detail.get("changed_files")`) nunca disparava, `suite_strength_unverified` ficava `false` — e o patch seguia adiante.

**Reprodução, antes da correção:**

```
$ adversary-gate --baseline b --patch p --test-path test_ok.py --test-id test_ok \
      --diff change.diff --coverage-json cov.json
decision: merge                                                exit 0
suite_strength: null | suite_strength_unverified: false
mutation.changed_files: []        # o patch só mexeu em calculator.cpp
mutation.reason: "no non-test source file changed between baseline and patch"
```

O patch reescrevia `int add(...)` de `a - b` para `a * b`. O único código que executou em qualquer lugar foi `assert True`. E o artefato de evidência gravava uma **afirmação falsa** — "nenhum arquivo-fonte mudou", enquanto `calculator.cpp` tinha mudado.

**A metade que passou despercebida:** a primeira correção só cobria patches onde *nada* de Python mudava. Com Python mudando junto, `is_measured` voltava `true`, `changed_files` não estava vazio e o `elif` não chegava na lista de estrangeiros — os arquivos eram registrados e ignorados:

```
$ adversary-gate ... (Python alterado em 4 linhas + vec.cpp de `a * b` para `a / b`,
                      cobertura exatamente no piso 0.80, força medida 1.0)
decision: merge                                                exit 0
coverage: 0.8 | suite_strength: 1.0 | suite_strength_unverified: false
mutation.foreign_changed_files: ["vec.cpp"]   # gravado, nunca agido
```

A cobertura *chegava* a derrubar a razão no caso puro (linhas `.cpp` entram no denominador e não estão no relatório), mas isso é coincidência de aritmética, não garantia — e no caso misto a razão limpa o piso. **O caso estava aberto.**

**Corrigido, nas duas metades:**
- `changed_foreign_source_files()` separa as duas perguntas: `changed_files` (o que *dá* para medir) e `foreign_changed_files` (o que mudou e *não* dá). Lista em `FOREIGN_SOURCE_SUFFIXES`; `.md`/`.yml`/`.json` e arquivos de teste ficam de fora, então patch só de documentação não é afetado.
- `suite_strength_unverified` dispara com **qualquer** fonte não-Python no patch, mesmo com a metade Python medida — uma nota parcial no lugar da nota inteira é o mesmo exagero que nota nenhuma.
- `mutation.reason` não fica mais em branco quando mede com estrangeiros presentes: diz para que aquele número é número.
- O caso misto é fixado com cobertura **declarada boa de propósito**, para que seja a força que bloqueia e não o piso.

**Testes:** 6 em `TestForeignSourceCannotMergeUnmeasured` — puro, misto, `README.md` e `tests/*.cpp` não contam, e o texto honesto sobrevive no caso em que foi escrito.

---

## Recursos novos em v2.1.0

### `base-sha` — uma referência dentro, três artefatos fora

**Problema:** v2.0.2 estava correto em fechar e hostil de instalar. Antes de qualquer coisa acontecer, quem instalava precisava construir `baseline/`, `changes.diff` e `coverage.json` à mão — e a primeira coisa que via era o estado de falha. Rigidez na *decisão* é o produto; rigidez no *setup* é atrito.

**Solução:** `base-sha` deriva tudo que consegue:

| Artefato | De onde vem |
|---|---|
| `baseline` | `git archive <base-sha>` — a árvore committada, sem sobras de worktree nem não-versionados |
| `diff` | `git diff --no-ext-diff <base-sha>...HEAD` |
| `coverage-json` | `coverage-command`, rodado no checkout |
| `patch` | o próprio checkout |

A hidráulica vive em `scripts/prepare_evidence.sh` e não num bloco `run:`, **justamente para que possa ser executada fora de um runner** — que é o que a torna testável.

**O que não muda:** cobertura continua sendo pergunta de evidência. `base-sha` muda de onde o artefato vem, não se ele é exigido. Sem cobertura produzida continua `INCONCLUSIVE`.

**Falhas fecham com razão, não com stack trace:**
- referência fora da história → saída 4 citando `fetch-depth: 0` (a causa real em nove em cada dez casos) e recusa-se a medir contra um baseline que teria que inventar
- `coverage-command` sem artefato → saída 4 mostrando o formato que a gate parseia, em vez de deixar cobertura silenciosamente virar `none`
- sem `baseline` e sem `base-sha` → saída 3

**Testado:** `scripts/test_prepare_evidence.sh` monta um repositório, afirma cada artefato, **entrega eles à gate e exige exit 0** (produzir um diff não vale nada se a decisão que vem depois está errada), mais os dois caminhos de falha. E dois jobs de CI: o teste do script, e um smoke da Action real que committa um fixture, faz o patch e exige `merge` a partir daquela única referência.

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
2. **Isolamento real de execução (AG-003).** *Parcialmente endereçado:* existe agora `--sandbox bwrap`, que dá sem rede, PID e `/tmp` próprios, sistema somente-leitura e só o repositório gravável — e que **recusa a partida** (exit 3) se `bwrap` não existir, em vez de rodar sem isolamento. Continua **não sendo** um sandbox de segurança: não há seccomp, não há drop de privilégio, e o diretório pai do repositório continua visível. Para código hostil de verdade, container/VM seguem sendo infraestrutura do executor, e o README diz isso explicitamente. O que mudou é que o cenário "nada de infraestrutura, patch provavelmente limpo" deixou de ser coberto apenas por documentação.
3. **Cobertura de patches com alteração mista.** Um patch que modifica um arquivo e deleta outro tem o primeiro medido e o segundo não; o artefato registra `deleted_files`, mas a decisão só é forçada a `INCONCLUSIVE` quando a deleção é a única mudança. A salvaguarda restante é a suíte completa.
4. **`DiffCoverage.ratio` é `1.0` quando não há linhas adicionadas.** Um patch puramente deletante reporta cobertura perfeita por vacuidade — a mesma cegueira de AG-001 aplicada à cobertura. Mantido por ser o comportamento documentado da propriedade; registrado aqui como dívida. (O caso *parecido* em que o parser não achava nenhum arquivo — uma deleção de arquivo inteiro — **foi** corrigido como AG-017.)
5. **Nenhuma tag desde `v2.0.1`.** Empurrar uma `v*` dispara `pypi-publish.yml` e deixaria uma run vermelha a cada release enquanto o AG-008 estiver aberto — então nem `v2.0.2` nem `v2.1.0` foram criados. O `release.yml` novo contorna isso (cria a tag com o próprio token, que não dispara outros workflows), mas **ainda não rodou nenhuma vez**: `workflow_dispatch` exige um clique em *Actions → Release → Run workflow*. Enquanto isso `pip install git+https://…` resolve, pois segue `main`.
6. **Nenhuma verificação adversarial independente.** As correções AG-001..AG-017 foram verificadas por *execução* (exit code real, antes e depois) e por leitura, mas não por terceiro. Auditoria interna não substitui auditoria externa: as falhas AG-012..AG-015 existiam justamente por serem invisíveis de dentro. Resolvelível só com tempo e usuários reais.
7. **Release não assinada.** Não há tag GPG/Sigstore. Somado ao AG-008, a cadeia de suprimento não é reproduzível por terceiro hoje. Fora do escopo do repositório: exige chaves de identidade do mantenedor.

## Re-auditoria seguinte (AG-013..AG-017)

Uma re-auditoria posterior a este documento não repetiu AG-001..AG-012 —
verificou-os e procurou *caminhos residuais* ao redor deles. Achou cinco,
todos reproduzidos por exit code antes de corrigidos: a allowlist de sufixos
de origem (AG-013), o guard do AG-012 dentro do ramo de mutação (AG-014), o
parser de diff só reconhecer `+++ b/` (AG-015), o `--test-id` obrigatório para
suíte sem node id (AG-016) e a recusa de deleção de arquivo inteiro (AG-017).

O detalhamento está em
[`ERRORS_AND_INCONSISTENCIES.md`](../ERRORS_AND_INCONSISTENCIES.md), secção 8.
Os dois primeiros e o terceiro eram **fail-open**: produziam `MERGE` que a
evidência não sustentava, que é exatamente a classe de falha deste produto.
