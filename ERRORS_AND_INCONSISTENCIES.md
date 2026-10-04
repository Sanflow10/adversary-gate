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

## 8. Achados da re-auditoria de v2.1.0 (AG-013..AG-017)

A re-auditoria de `315b40e` não repetiu AG-001..AG-012 — verificou-os por
execução (todos confirmados) e procurou *caminhos residuais* ao redor deles.
Os cinco abaixo foram reproduzidos por exit code real antes de corrigidos
(todos no mesmo commit, `dcfb894`).

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

* **AG-008 — Trusted Publishing do PyPI.** **Fechado em 2026-10-04** — publisher registrado no pypi.org e `2.4.0` publicada por OIDC. Texto original: fora do repositório; exige cadastro
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

---

## 9. Achados da auditoria externa de 2026-10-01 (AG-018..AG-020)

Auditoria comparativa feita por terceiro sobre `Sanflow10/adversary-gate` e
`Sanflow10/SuperAgent`, executada contra o commit `5159f5c`. Os dois achados
deste repositório foram **reproduzidos por execução aqui, antes de corrigidos** —
mesmo cenário, mesmo exit code, sem confiar no relatório de ninguém.

| ID | Achado | Reprodução (`5159f5c`) | Status |
|---|---|---|---|
| **AG-018** (Alta) | Patch misto modificado+deletado chegava a `MERGE` | `calc.py` modificado e medido (mutação 1/1 → `suite_strength: 1.0`), `gone.py` deletado e nunca julgado, diff real e coverage fornecidos → **`exit 0` / `merge`**, com `deleted_files: ["gone.py"]` gravado no próprio artefato. Os sete campos alegados batiam com os obtidos. | **Corrigido** — `deleted_files` não-vazio passa a levantar `suite_strength_unverified` nas duas ramas (inclusive `--mutation-max 0`), pela mesma regra de AG-001 e AG-012: código que mudou e este motor não pôde julgar não pode autorizar a decisão. Mesmo cenário → **`exit 2` / `inconclusive`**, mantendo `suite_strength: 1.0` — o número continua verdadeiro sobre o que mediu — e gravando `suite_strength_unverified: true`. |
| **AG-019** (Média) | `test_prepare_evidence.sh` dependia da configuração de cor global do git | `GIT_CONFIG_GLOBAL` com `color.ui = always` → **`exit 1`** / `FAIL: diff does not contain the added line`; controle com `color.ui = false` → `exit 0`. Causa: `prepare_evidence.sh` chamava `git diff --no-ext-diff` sem `--no-color`, e o artefato é *parseado* por `grep`, não lido. A CI verde não desmentia: o runner não impõe `color.ui`, então o teste passava por acidente de ambiente. | **Corrigido** — `--no-color` na chamada + regressão no próprio script, que roda o mesmo caminho com `GIT_CONFIG_COUNT=1 color.ui=always` e falha se o diff trouxer `ESC` ou perder a linha adicionada. |
| **AG-020** | Trusted Publishing do PyPI continua aberto | Verdadeiro — é o mesmo item que aqui se chama **AG-008**, já descrito acima. | **Não é achado novo.** Já estava registrado em `SECURITY.md`, `CHANGELOG.md` e `README.md` como aberto; o cadastro é no `pypi.org`, fora do alcance do repositório. |



---

## 10. Achados da auditoria de produto de 2026-10-03 (AG-021..AG-031)

Auditoria completa, com reprodução de cada item, em
[`docs/AUDITORIA_PRODUTO_v2.1.1.md`](docs/AUDITORIA_PRODUTO_v2.1.1.md),
executada contra `3cc103c`. Os quatro itens de maior retorno e menor esforço
saíram na `2.2.0`, com teste de regressão em `tests/test_oracle_integrity.py`;
AG-024, AG-025 e AG-031 saíram na `2.3.0`, com teste em
`tests/test_execution_and_claims.py` e o job de CI *Composite action discovers
claims on the project's Python*. **O que continua aberto está dito na coluna
Status, não omitido.**

| ID | Severidade | Achado | Status |
|---|---|---|---|
| **AG-021** | Alta — fail-open | Agente introduz bug e reescreve o teste da claim → `exit 0` / `merge`, `suite_strength: 1.0`. A proteção `critic_test_paths` existia na biblioteca, mas a CLI e a Action não a ligavam. | **Corrigido** — mínima na 2.2.0 (teste reescrito deixa de ser `VERIFIED`); **oráculo do baseline na 2.5.0** (o teste *original* roda contra o código do patch → o ataque vira `BLOCK`). Ver §12. |
| **AG-022** | Média — fail-open parcial | Linhas de teste entram no denominador da cobertura do diff: 0/10 linhas de fonte cobertas + 40 de teste → `0.8`, passa no piso. | **Corrigido** — ratio só sobre fonte; exclusão gravada no artefato. |
| **AG-023** | Média — rigor | Mutation score com n = 1..6, sítios pegos na ordem do arquivo, sem intervalo de confiança; poucos operadores. | **Corrigido (2.7.0)** — operadores ampliados (2.4.0); o piso lê o **limite inferior de um intervalo de Wilson a 80 %** (`--strength-confidence`, `0` volta à razão); sítios espalhados, um por linha alterada antes de repetir; orçamento padrão 12. Consequência: menos de 5 mutantes mortos não chega a `MERGE`. Ver §13. |
| **AG-024** | Alta — adoção | Timeout 30 s, CPU 10 s e 512 MB fixos, sem flag; `--test-command` recebe `PATH=/usr/bin:/bin` sem `HOME`; JVM sai com exit 1 (lido como falha de teste). | **Corrigido (2.3.0)** — `--timeout`, `--cpu-seconds`, `--memory` (aceitam `none`), `--pass-env`, `--env`; `HOME` privado por execução; morte por limite de memória (exit 1 + `MemoryError`, `Could not reserve enough space`, `Fatal process out of memory`…) vira exit 3 → `INCONCLUSIVE`; tudo registrado em `execution`. **Aberto:** a detecção é por texto na saída — uma ferramenta que morra sem imprimir nenhum desses marcadores continua lida como falha de teste. |
| **AG-025** | Alta — adoção (por leitura) | A Action força Python 3.12 e roda pytest com `sys.executable`, sem as dependências do projeto. | **Corrigido (2.3.0)** — `--python`; a Action instala o gate num venv privado (`update-environment: false`), roda os testes no Python do projeto e o coverage.py nesse mesmo interpretador; os mutantes passaram a receber o mesmo interpretador, ambiente e sandbox. Verificado por job de CI com Python 3.11 e uma dependência que só existe nele. |
| **AG-026** | Alta — distribuição | Tag e Release `v2.1.1` não existem (wheel → `404`); o PyPI entrega `2.1.0`, que contém o AG-018. | **Parcial** — o workflow de Release agora move a tag `vN`, envia ao PyPI por token quando o segredo existe e avisa quando não existe. A `2.1.1` não será publicada: o conteúdo dela foi para a `2.2.0`, já com versão e CHANGELOG cortados. A GitHub Release `v2.2.0` (e a `v2`) foi publicada pelo workflow. **Aberto:** o PyPI segue em `2.1.0` até o token ou o Trusted Publisher funcionar (AG-008). |
| **AG-027** | Média — empacotamento | O wheel instala `cli`, `core`, `sandbox` e `verifiers` como pacotes top-level; sem `--version`. | **Corrigido** — tudo sob `adversary_gate/` (o wheel instala só esse pacote), `python -m adversary_gate`, `--version`; testes de consistência travam os dois. |
| **AG-028** | Baixa | `SECURITY.md` manda usar o e-mail do `pyproject.toml`, que não tem e-mail. | **Corrigido** — link direto para o advisory + fallback por issue sem detalhe técnico. **Aberto:** não há e-mail monitorado; e *Private vulnerability reporting* precisa estar habilitado em Settings → Code security (não verificável daqui). |
| **AG-029** | Conceitual | `self_deception_index` é 0 por construção quando calculado só sobre decisões do gate. | **Mitigado (documentação)** — saiu do pitch do README; README, `SECURITY.md` e a docstring dizem que é 0 por construção. **Aberto:** métricas que cruzam a decisão com o que aconteceu depois (override, revert) exigem dados de fora do gate. |
| **AG-030** | Conceitual | `FAIL→PASS` (correção provada) e `PASS→PASS` (não regressão) recebem o mesmo rótulo. | **Corrigido (2.7.0)** — `fixed` × `no_regression` (`discarded` fica para teste novo); saída ganha `claims_fixed` e `fix_proven`; métricas contam os dois. Ver §13. |
| **AG-031** | Alta — adoção | Uma claim por execução e `test-path` obrigatório e fixo no YAML da Action. | **Corrigido (2.3.0)** — várias claims (`--test-id` e `--claim` repetíveis, `--claim-json` com N) e `--discover-claims`: os testes que executaram uma linha de fonte alterada, lidos dos contextos por teste do coverage.py; arquivos de teste reescritos pelo patch ficam de fora e nomeados. Na Action, sem teste nomeado, a descoberta liga sozinha. **Aberto:** só pytest — com `--test-command` não há node id para descobrir. |

---

## 11. AG-032 — o patch configurava o runner que o julgava

Achado em revisão externa de 2026-10-03 contra `02aa0cd` (2.3.0), reproduzido
por exit code antes da correção e travado em `tests/test_harness_integrity.py`.

| ID | Severidade | Achado | Status |
|---|---|---|---|
| **AG-032** | Alta — fail-open | O patch quebra `add` (`a + b` → `a + b + 1`), adiciona `.pytest.ini` com `addopts = -p agplug` e um `agplug.py` que reescreve o relatório para "passed" **só enquanto** `calc.py` tem exatamente os bytes com bug. Os mutantes mudam esses bytes, o plugin sai do caminho e todo mutante morre de verdade → `MERGE`, exit 0, `diff_coverage: 0.8`, `suite_strength: 1.0`, com `add(2, 3) == 6`. Duas causas: o denylist conhecia `pytest.ini` mas não `.pytest.ini`, `pytest.toml` nem `.pytest.toml` (o pytest 9 lê os três), e a policy só olhava os caminhos *declarados* (`--diff`, `--changed-path`). | **Corrigido** — `harness_drift()` compara as **árvores** baseline × patch: todo arquivo de harness (os cinco nomes de config do pytest 9, `pyproject.toml`, `setup.cfg`, `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`, em qualquer profundidade; `.git` e virtualenvs fora) tem que ser idêntico ao do baseline, senão a claim é `UNVERIFIED` → `INCONCLUSIVE`. O denylist ganhou os nomes que faltavam. Teste negativo: com a correção revertida, os cenários voltam a dar exit 0. **Aberto (fora do alcance de um gate por testes, documentado no README):** código-fonte importado pelo teste roda no mesmo processo do pytest e pode adulterá-lo, ou detectar que está sob teste. |

**AG-008 / AG-020 / AG-026 — fechados em 2026-10-04.** Trusted Publisher
registrado no pypi.org (`Sanflow10` / `adversary-gate` / `pypi-publish.yml`,
sem environment); *Publish to PyPI* com a tag `v2.4.0` subiu wheel e sdist por
OIDC, com atestados de proveniência. `pip install adversary-gate==2.4.0`
verificado num ambiente limpo. A `2.2.0` e a `2.3.0` não foram enviadas.

## 12. Oráculo do baseline — o aberto do AG-021

O AG-021 parou o patch de se autoaprovar, mas só até `INCONCLUSIVE`: o ataque
(bug + teste reescrito) não era bloqueado, e todo patch honesto que mexia no
arquivo de teste — refatoração, assert novo, teste novo num arquivo existente,
o formato mais comum de um agente — também caía em `INCONCLUSIVE`.

**Implementado (2.5.0).** Quando o arquivo de teste da claim existia no
baseline e mudou no patch, o gate copia a árvore do patch, devolve a cópia do
**baseline** do arquivo de teste e roda a claim ali (`"oracle": "baseline"`):
o ataque do AG-021 passa a `REFUTED` → `BLOCK` (exit 1), e a refatoração
honesta passa a `VERIFIED`. Teste novo num arquivo existente: julgado como
teste adicionado, mas só depois de **todos** os testes que o baseline tinha
naquele arquivo passarem no código do patch (`"oracle": "baseline-file"`); se
o patch entortou um deles, `REFUTED`. A descoberta por coverage não exclui
mais arquivos reescritos (`rewritten_test_files_judged_by_baseline`). Testes:
`tests/test_baseline_oracle.py`, `tests/test_oracle_integrity.py`.

Helpers contam como resposta: o transplante devolve **todo** arquivo de teste
do baseline (`test_*`, `*_test.py`, qualquer coisa sob `tests/`), e o oráculo
é acionado se o patch mudou qualquer um deles — mesmo com o arquivo da claim
intacto. Um `tests/helpers.py` entortado para concordar com o bug dá `REFUTED`.

**Fechado na 2.6.0** — os três itens que tinham ficado abertos:

* **Helpers fora de caminhos de teste.** Não há como distinguir pelo caminho um
  helper de código sob teste (`numpy.testing` é API pública; chutar errado
  em qualquer direção é furo). Viraram **declarados**: `--test-support GLOB`
  (Action: `test-support`). Declarados contam como teste em todo lugar —
  fora da cobertura e da mutação, e restaurados do baseline pelo oráculo. O
  heurístico ganhou só convenções que nunca são código de produto
  (`__tests__/`, `*.test.*`, `*.spec.*`, `*_test.go`, `*Test.java`, `*_spec.rb`).
* **Suíte colateral.** Quando o patch mudou testes do baseline, a suíte inteira
  também roda com os testes do baseline contra o código do patch
  (`full_suite_oracle`); um teste que não é de nenhuma claim, entortado para
  concordar com o bug, vira regressão colateral → `BLOCK`.
* **Claims de `--test-command` com `test-id`.** O id é rótulo, não nó a
  coletar: o transplante sempre se aplica.

Testes: `tests/test_baseline_oracle.py`.

## 13. AG-023 e AG-030 — fechados na 2.7.0

Antes de corrigir, conferido que não estavam corrigidos em lugar nenhum: `main`
local e remota, as branches `claude/keen-hamilton-bemwz8`, `master`,
`backup/local-pre-sync`, `docs/auditoria-v2` e a cópia antiga em
`~/adversary_gate_v2`. `classify` dava `discarded` para `FAIL→PASS` e
`PASS→PASS`, e não havia intervalo de confiança no código.

* **AG-023.** O piso de força da suíte é aplicado ao limite inferior de um
  intervalo de Wilson bilateral a 80 % (escolha do mantenedor entre 80 %,
  95 % e só-reportar). 5/5 → 0,753 passa; 4/4 → 0,709 e 1/1 → 0,378 não.
  Sítios de mutação em round-robin pelas linhas alteradas; orçamento padrão
  6 → 12. Fixtures de um operador em testes de outras coisas, no CI da Action,
  no benchmark e no `test_prepare_evidence.sh` passam `--strength-confidence 0`
  com comentário; o demo passou a ter 5 operações e chega a `MERGE` no padrão.
* **AG-030.** `FailureClass.FIXED` (`fixed`) para `FAIL→PASS`,
  `FailureClass.NO_REGRESSION` (`no_regression`) para `PASS→PASS`;
  `discarded` fica só para teste que não existe no baseline. Saída:
  `claims_fixed`, `fix_proven`; `GateMetrics.fixed` / `no_regression`.

Testes: `tests/test_rigor_and_labels.py`.
