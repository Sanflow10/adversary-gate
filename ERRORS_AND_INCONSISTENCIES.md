# Registro de Erros e Inconsistências (AdversaryGate)

Este documento registra erros, divergências de ambiente e inconsistências identificadas e tratadas durante o desenvolvimento, revisão e correção cirúrgica do **AdversaryGate v2.0.1**.

---

## 1. Conflito de Diretórios do Repositório (`adversary-gate` vs `adversary_gate_v2`)

* **Inconsistência**: Existência de dois diretórios locais: `/home/sa/adversary-gate` e `/home/sa/adversary_gate_v2`.
* **Causa Raiz**: O diretório `/home/sa/adversary-gate` era um clone legado preso na branch `master` no commit `90d18ab` (v0.8.0), sem o remote do GitHub configurado (`git remote -v` vazio). O repositório real ativo conectado ao GitHub (`git@github.com:Sanflow10/adversary-gate.git`) estava em `/home/sa/adversary_gate_v2` na branch `main`.
* **Impacto**: Ao tentar executar `cd adversary-gate && git apply /tmp/opencode/adversary-gate-2.0.1-fix.patch`, o comando falhava com erros de patch rejeitado (`patch does not apply`), pois os arquivos esperados pelo patch (ex.: `src/verifiers/strength.py` e linhas de `README.md`) inexistiam naquela versão legada.
* **Solução**:
  1. No repositório de trabalho ativo (`adversary_gate_v2`), o patch foi aplicado e validado.
  2. No diretório legado (`/home/sa/adversary-gate`), configuramos o remote `origin` para `git@github.com:Sanflow10/adversary-gate.git` e realizamos o tracking da branch `main`, sincronizando o ambiente.

---

## 2. Bloqueio de Aplicação de Patch por Alterações Unstaged/Locais

* **Inconsistência**: `git apply --check` inicialmente falhava em `src/core/gate.py` e `src/core/types.py`.
* **Causa Raiz**: Modificações manuais prévias não commitadas na árvore de trabalho colidiam com os blocos de diff do arquivo `.patch`.
* **Solução**: Execução de `git stash` para salvar e isolar o estado sujo antes da aplicação limpa do patch, seguida de revisão detalhada das mudanças.

---

## 3. Detecção Incompleta de Arquivos de Teste em `_is_test_path`

* **Inconsistência**: A função `_is_test_path` checava apenas `path.name.startswith("test_")`, `path.name == "conftest.py"` ou `"tests" in path.parts[:-1]`.
* **Causa Raiz**: Convenções comuns de descoberta do pytest reconhecem tanto arquivos no formato `test_*.py` quanto `*_test.py` (ex.: `calc_test.py`).
* **Impacto**: Um arquivo de teste nomeado no padrão `*_test.py` fora de uma pasta `tests/` seria erroneamente classificado como código-fonte e incluído no pipeline de mutação.
* **Solução (TDD)**:
  1. Criação do teste unitário `test_is_test_path_recognizes_all_test_patterns` (fase RED comprovada).
  2. Implementação da cláusula `or path.name.endswith("_test.py")` na função `_is_test_path` (fase GREEN).

---

## 4. Medição de Mutation Score e Mutantes "Stillborn"

* **Inconsistência**: Em versões anteriores, `suite_strength` recebia valores fixos (`1.0`) ou decorativos, criando uma falsa sensação de segurança onde o floor nunca disparava.
* **Correção Cirúrgica**:
  * Execução real dos testes contra mutantes gerados cirurgicamente apenas nas linhas alteradas pelo patch.
  * Mutantes com erros de sintaxe/importação/timeout que não executam testes são classificados como **stillborn** e descartados de ambos os lados do ratio, evitando inflação do score.
  * Se o código foi alterado, o claim passou (`VERIFIED`), mas não foi possível extrair medição real de mutação, a decisão é estritamente **fail-closed** (`INCONCLUSIVE`).

---

## 5. Falha de Atribuição de Regressão em Suíte Completa (`full_suite_exit_codes`)

* **Inconsistência**: Checar apenas `patch_exit_code != 0` causava o erro de culpar o patch por testes que já estavam quebrados no baseline.
* **Correção Cirúrgica**:
  * Avaliação em par `(baseline_exit_code, patch_exit_code)`.
  * `(0, 1)`: Regressão colateral direta -> `Decision.BLOCK`.
  * `(1, 1)`: Falhas preexistentes em ambos os lados -> `Decision.INCONCLUSIVE` (não atribuível ao patch).
  * `(0, 0)` ou `(1, 0)`: Suíte íntegra no patch -> autoriza prosseguimento para `Decision.MERGE`.

---

## 6. Falha no Trusted Publishing (OIDC) do PyPI (`invalid-publisher`)

* **Inconsistência**: O workflow do GitHub Actions disparado pela tag `v2.0.1` falhou no step `Publish package distributions to PyPI`.
* **Causa Raiz**: O PyPI recusou a troca de token OIDC com o erro `invalid-publisher: valid token, but no corresponding publisher (Publisher with matching claims was not found)`.
  * Claims recebidas pelo PyPI:
    * `repository`: `Sanflow10/adversary-gate`
    * `workflow_ref`: `Sanflow10/adversary-gate/.github/workflows/pypi-publish.yml@refs/tags/v2.0.1`
    * `environment`: `MISSING`
  * O PyPI exige que o projeto em `https://pypi.org/manage/project/adversary-gate/settings/publishing/` tenha o GitHub Actions cadastrado como Trusted Publisher. Além disso, se um `environment` (ex.: `pypi`) for especificado no PyPI, o workflow deve declarar `environment: { name: pypi, url: https://pypi.org/p/adversary-gate }` para casar com a claim.
* **Soluções Possíveis**:
  1. **Configuração OIDC no PyPI**: Adicionar o Trusted Publisher nas configurações do projeto no PyPI com `workflow: pypi-publish.yml`.
  2. **Publicação via API Token (`twine upload`)**: Alternativa direta e imediata usando um token de API gerado no PyPI.

---

## 7. Auditoria técnica externa v2.0.1 → correções na v2.1.0

Auditoria completa em [`docs/AUDITORIA_SENIOR_v2.0.1.md`](docs/AUDITORIA_SENIOR_v2.0.1.md) e confronto com as correções em [`docs/AUDITORIA_CONFRONTO_v2.0.2.md`](docs/AUDITORIA_CONFRONTO_v2.0.2.md). Cada achado foi **reproduzido por execução** antes de ser corrigido; a lista abaixo registra o que mudou e o que continua aberto.

| ID | Achado | Status na v2.1.0 |
|---|---|---|
| AG-001 | Deleção de fonte não entrava em `changed_source_files` | **Corrigido** — união `set(base) \| set(patch)`, `deleted_files` no artefato, teste de regressão. Deleção isolada agora é `INCONCLUSIVE`. |
| AG-002 | `--coverage-ratio` era input confiado, default `1.0` | **Corrigido** — `--diff` + `--coverage-json` são a fonte medida; `--coverage-ratio` exige `--coverage-source untrusted`; sem evidência a decisão é `INCONCLUSIVE`. |
| AG-003 | "Sandbox" é controle de recursos, não isolamento | **Documentado com honestidade** — nova seção *"Execution boundary"* no README, runner renomeado em docstring, `--require-network-isolation` agora é exposto pela CLI. Isolamento real continua sendo responsabilidade de um sandbox externo. |
| AG-004 | Symlink de `changed_paths` não era checado | **Corrigido** — `verify_claim` passa `patch_dir` à policy e a policy checa o próprio `unresolved`, não só os pais. |
| AG-005 | Action não instalava dependências | **Corrigido** — `pip install` do pacote antes de executar. |
| AG-006 | Sem CI de testes | **Corrigido** — `.github/workflows/ci.yml`: matriz 3.10–3.12, build, instalação do wheel, smoke da CLI e smoke da Action em runner limpo. |
| AG-007 | Cobertura própria desigual | **Corrigido** — total 83% → **87%**; `verifiers/coverage.py` de 33% → **100%** (passou a ser invocado), `path_policy.py` 84% → 93%. `sandbox/runner.py` segue em **63%**, o único módulo que não melhorou. A auditoria declarou 89% no total — não se reproduz; ver o confronto. |
| AG-008 | README/versão/licença/Trusted Publishing inconsistentes | **Parcial** — README corrigido (a `2.0.2` nunca saiu do papel; saiu na `2.1.0`), referência da Action corrigida, `LICENSE` adicionado. **Trusted Publishing continua quebrado**: é configuração no PyPI, não no repositório — por isso a `2.1.0` subiu por token e as três runs de publish ficaram vermelhas. |
| AG-009 | `classify()` sombreava `NEW_BUG` e o ramo de timeout do baseline | **Corrigido** (novo) — guarda "baseline deve ser PASS ou FAIL" removida; todo `StabilityPolicy` roda mais de uma vez, então ela disparava sempre. |
| AG-010 | Artefato não distinguia "suíte passou" de "suíte não rodou" | **Corrigido** (novo) — campo `full_suite_ran` no JSON de saída e no registro de decisão. |
| AG-011 | Floor de coverage era checado antes de `REFUTED` | **Corrigido** (novo) — `BLOCK` volta a ter precedência, como o próprio docstring de `decide()` já dizia. |
| AG-012 | Patch só em C++/Rust chegava a `MERGE` | **Corrigido** (novo) — `changed_source_files` só varria `*.py`, então `changed_files` ficava vazio, o guarda de AG-001 não disparava e o artefato gravava `"no non-test source file changed"` enquanto `calculator.cpp` mudava. Agora `foreign_changed_files` separa "o que dá para medir" de "o que mudou e não dá", e `suite_strength_unverified` dispara com **qualquer** fonte não-Python — inclusive no patch misto (Python medido + C++ ignorado), que era a metade que passou despercebida. |

---

## 8. Achados da re-auditoria de v2.1.0 (AG-013..AG-016)

A re-auditoria de `315b40e` não repetiu AG-001..AG-012 — verificou-os por
execução (todos confirmados) e procurou *caminhos residuais* ao redor deles.
Os quatro abaixo foram reproduzidos por exit code real antes de corrigidos.

| ID | Achado | Reprodução (v2.1.0) | Status |
|---|---|---|---|
| **AG-013** | `FOREIGN_SOURCE_SUFFIXES` era uma **allowlist** de linguagens lembradas | patch só em `schema.sql` → `exit 0 MERGE` com `mutation.reason: "no source file changed between baseline and patch"` (afirmação falsa) e `foreign_changed_files: []`; patch misto `calc.py`+`schema.sql` → `exit 0 MERGE`, `suite_strength: 1.0`, `unverified: false` | **Corrigido** — virou *denylist* (`NON_SOURCE_SUFFIXES`), e `changed_foreign_source_files` passa a unir o scan de diretório com os caminhos que o `--diff` do chamador nomeia. Sufixos antes invisíveis (`.sql`, `.proto`, `.pyi`, `.vue`, `.sol`, `.tf`, `.r`, `.tmpl`…) agora contam. |
| **AG-014** | O guard do AG-012 vivia **dentro** do ramo de mutação | `--mutation-max 0` no patch só-C++ → `exit 0 MERGE`, artefato **sem** as chaves `changed_files`/`foreign_changed_files`/`deleted_files`, `unverified: false`. Controle com o default → `exit 2`. Zero testes cobriam `mutation_max=0`. | **Corrigido** — o que mudou passou a ser calculado por `classify_changes()` *antes* do teste de orçamento, usado nos dois ramos, de modo que a "fixed shape on every return path" de `strength.py` vale também com a medição desligada. |
| **AG-015** | O parser de diff só reconhecia o prefixo `+++ b/` (git) | um `diff -u` padrão parseava para **zero** arquivos → `DiffCoverage(0,0).ratio == 1.0` → floor de coverage limpo com **zero** linhas medidas. Falha **aberta** num produto cuja tese é fail-closed. | **Corrigido** — aceita `+++ ` com e sem `b/` (o par `--- `/`+++ ` é o que identifica o cabeçalho), e um diff com hunks sem cabeçalho atribuível agora levanta `UnparseableDiff` → `exit 3`, em vez de medir `1.0`. |
| **AG-016** | `--test-id` era obrigatório mesmo para suíte não-pytest | impossível declarar `--test-command` sem inventar um node id que não existe naquela stack. | **Corrigido** — `--test-id` deixa de ser exigido quando `--test-command` é dado; a claim recebe o rótulo `(test-command)`. |
| **AG-017** | O parser descartava o lado `--- ` de um cabeçalho | uma deleção de arquivo inteiro (`--- a/x` / `+++ /dev/null`) parseava para **zero** arquivos e era rejeitada como `UnparseableDiff` → `exit 3` com a mensagem "re-generate it with git diff" — acusando o *input* de malformado quando ele era perfeitamente válido. Um diff misto (um deletado + um modificado) falhava do mesmo jeito, escondendo a metade que funcionava. | **Corrigido** — `+++ /dev/null` é a grafia de um arquivo removido, e o caminho antigo virou o *fallback* de atribuição. Direção: **fail-closed, mas falso**; era o oposto do AG-015, que era fail-open. |

### Correção de um item da auditoria anterior

A auditoria anterior listou *"Ausência de CI verificado"* como **o bloqueador
mais grave**. Não era: `.github/workflows/ci.yml` já existia desde AG-006, com
`on: push` + `pull_request`, matriz `3.10/3.11/3.12`, build de wheel, instalação
limpa, smoke da CLI e smoke da Action. O que **faltava** era análise estática
(bandit/semgrep) — hoje existe, em dois jobs novos, junto com um piso de
cobertura da suíte própria que impede o número de cair em silêncio.

### Continua aberto

* **AG-008 — Trusted Publishing do PyPI.** Fora do repositório; exige cadastro
  em `pypi.org/manage/project/adversary-gate/settings/publishing/`.
* **AG-003 — isolamento.** `--sandbox bwrap` cobre o caso "estou rodando isto
  agora e não tenho infraestrutura": sem rede, PID e `/tmp` próprios, sistema
  somente-leitura. **Não é um sustituto de container/VM** — não há seccomp,
  nem drop de privilégio, nem leitura-only do repositório. A fronteira de
  execução no README continua valendo para código hostil de verdade.
* **`DiffCoverage.ratio` é `1.0` quando não há linhas adicionadas.** Um patch
  puramente deletante continua reportando cobertura perfeita por vacuidade.
  Comportamento documentado; registrado como dívida. (AG-015 fecha o caso
  *parecido* em que o parser não acha nenhum arquivo, que era o pior dos dois.)
* **Cobertura de patches com alteração mista modificado+deletado.** O artefato
  grava `deleted_files`, mas só a deleção isolada força `INCONCLUSIVE`.



