# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versionado: [SemVer 2.0.0](https://semver.org/lang/pt-BR/).

Duas regras que este arquivo obedece, e que valem mais que o formato:

1. **Uma entrada por mudança que um usuário poderia notar**, escrita quando a
   mudança acontece — não quando ela fica "pronta".
2. **A seção diz a verdade sobre o artefato.** Uma versão sem tag não é uma
   release, e este projeto já sofreu com README dizendo uma coisa e pacote
   dizendo outra (ver [`docs/AUDITORIA_CONFRONTO_v2.0.2.md`](docs/AUDITORIA_CONFRONTO_v2.0.2.md)).

Estado das tags hoje: **`v2.0.0` e `v2.0.1` existem**. `2.0.2`, `2.1.0` e o
trabalho abaixo estão em `main` e **não foram taggeados** — porque empurrar uma
tag dispara o `pypi-publish.yml`, que está quebrado desde o AG-008.

---

## [Unreleased] — em `main`, sem tag

Nada aqui pode ser instalado por tag. Até a próxima release, use
`pip install git+https://github.com/Sanflow10/adversary-gate`.

### Adicionado

- **`--test-command CMD`** — executa um comando arbitrário no lugar do pytest,
  nos dois lados (baseline e patch). Convenção: `0` passou, `1` falhou,
  `2`/`3`/`4` o próprio harness quebrou → `UNVERIFIED`, timeout → `UNVERIFIED`.
  Antes disso, um repositório não-pytest saía com `exit 5 / no tests collected`
  e **nunca** chegava a `MERGE`; agora é possível `BLOCK` real.
- **`--full-suite-command CMD`** — comando da checagem colateral de regressão.
  Padrão: herda de `--test-command`, então declarar uma suíte não-pytest não
  custa silenciosamente a checagem colateral.
- **`--sandbox {none,bwrap}`** — envolve toda execução em bubblewrap: sem rede,
  PID e `/tmp` próprios, sistema somente-leitura, e só o repositório gravável.
  **Opcional e explicitamente limitado**: não há seccomp nem drop de privilégio,
  e não substitui container/VM. Se `bwrap` não existir a porta **recusa a
  partida** (exit 3) em vez de rodar sem isolamento.
- **Inputs novos no `action.yml`**: `test-command`, `full-suite-command`,
  `sandbox`. `test-id` deixou de ser obrigatório (ele nunca precisou existir
  para uma suíte que é um comando).
- **CI com análise estática**: jobs `static-analysis` (bandit em severidade
  MEDIUM+, semgrep `p/security-audit`) — era o único item da checklist de
  maturidade que faltava de verdade.
- **CI com piso de cobertura**: job `coverage` com `--fail-under=87`, para que
  o número da suíte própria não caia em silêncio.
- **`tests/test_runner.py`** — 52 testes para o módulo que mais executa código
  alheio e era o menos coberto do projeto.
- **`SECURITY.md`** — política de divulgação responsável, escopo, limitações
  conhecidas e tempos de resposta.
- **`CHANGELOG.md`** — este arquivo.
- **`scripts/benchmark.py`** — mede a latência de decisão (uma invocação
  completa) e imprime junto o CPU, o Python, o modo de sandbox **e o custo de
  carregamento dos plugins do pytest**. Fica fora do CI de propósito: tempo em
  runner compartilhado é barulhento demais para ser um gate, e um threshold
  frouxo não testa nada enquanto um apertado fica vermelho com a carga de
  outra pessoa.
  - **Medido:** 20,7 s de mediana numa máquina com **9 plugins do pytest**
    auto-carregados. A decisão são **8 execuções** de pytest (baseline, patch,
    4 rodadas de estabilidade, ≥1 mutante, suíte colateral) × 2,29 s de
    startup cada = 18,3 s de ambiente; ≈2,4 s é o gate. O teste em si roda em
    0,01 s.
  - **Limitação operacional registrada:** o runner entrega ao filho um
    ambiente mínimo por design, então `PYTEST_DISABLE_PLUGIN_AUTOLOAD` **não
    chega a ele** — numa máquina com plugins pesados o gate herda o custo oito
    vezes sem como optar out. É a consequência honesta de "isolamento é o filho
    ver só o que eu passo", não um defeito, mas é por que o ambiente precisa
    ser impresso ao lado do número.
- **README**: seção *"What this decision is, and what it is not"* — o
  `MERGE` é permissão para *olhar*, não instrução para mesclar; a decisão
  pertence a quem assina o PR. Antes o disclaimer de revisão humana não
  existia.

### Corrigido

Cada um abaixo foi **reproduzido por exit code real** antes da correção, e
todos eram *fail-open*: produziam `MERGE` que a evidência não sustentava.

- **AG-013 — allowlist de sufixos de origem.** `FOREIGN_SOURCE_SUFFIXES` era
  uma lista de linguagens que alguém se lembrou de digitar; qualquer sufixo
  fora dela era invisível. Um patch cujo único efeito era `schema.sql` saía
  com `decision: merge` e `mutation.reason: "no source file changed between
  baseline and patch"` — a frase falsa que o AG-012 existia para eliminar.
  Virou **denylist** (`NON_SOURCE_SUFFIXES`), e `changed_foreign_source_files`
  agora **une** o scan de diretório com os caminhos que o `--diff` do chamador
  nomeia: nenhuma das duas fontes sozinha fecha o buraco. `.proto`, `.pyi`,
  `.vue`, `.sol`, `.tf`, `.r` e `.sql` passaram a contar.
- **AG-014 — o guard do AG-012 vivia dentro do ramo de mutação.**
  `--mutation-max 0` pulava a checagem inteira: patch só-C++ saía com
  `exit 0 MERGE` e um artefato **sem** `changed_files` / `foreign_changed_files`
  / `deleted_files`, com `unverified: false`. Zero testes cobriam
  `mutation_max=0`. Agora `classify_changes()` calcula o que mudou **antes**
  do teste de orçamento e alimenta os dois ramos, mantendo a *fixed shape* do
  artefato em todo caminho de retorno.
- **AG-015 — o parser de diff só reconhecia o prefixo `+++ b/`.** Um `diff -u`
  padrão parseava para **zero** arquivos → `DiffCoverage(0, 0).ratio == 1.0` →
  piso de cobertura limpo com **zero** linhas medidas. Aceita `+++ ` com e sem
  `b/`, e um diff com hunks sem cabeçalho atribuível agora levanta
  `UnparseableDiff` → `exit 3`, em vez de medir perfeição a partir do nada.
- **AG-016 — `--test-id` obrigatório para suíte que não tem node id.** Impossível
  declarar `--test-command` sem inventar um identificador que não existe
  naquela stack. A claim recebe o rótulo `(test-command)` no lugar.
- **AG-017 — deleção de arquivo inteiro era recusada como "unparseable".**
  `+++ /dev/null` é a grafia de um arquivo removido, e o lado `--- ` era
  descartado, deixando o hunk sem arquivo a que pertencer — o gate acusando o
  *seu input* de malformado quando ele estava perfeitamente válido. O caminho
  antigo virou o fallback de atribuição. Um diff misto (um deletado + um
  modificado) agora atribui os dois.

### Documentado

- `--mutation-max 0` desliga a **medição** (como `--coverage-floor 0` desliga o
  piso), **não** desliga a guarda de origem estrangeira — que é propriedade do
  patch, não do seu orçamento.
- `README.md` passou a descrever a **denylist** em vez da allowlist antiga.
- Seção *"Execution boundary"* reescrita em torno do `--sandbox bwrap`, com a
  tabela do que ele contém e a lista explícita do que ainda não contém.
- **Correção de um item da auditoria anterior**: ela listava *"Ausência de CI
  verificado"* como o bloqueador mais grave. Não era — `.github/workflows/ci.yml`
  existia desde AG-006 com `push` + `pull_request` e matriz 3.10/3.11/3.12. O
  que faltava era análise estática, e agora existe.

### Desempenho da suíte

| | antes | depois |
| --- | --- | --- |
| testes | 122 | 153 + 1 skip |
| cobertura total de `src` | 86,9 % | ver job `coverage` do CI |
| `src/sandbox/runner.py` | 63,4 % | ~82 % |

---

## [2.1.0] — `main`, **sem tag**

### Adicionado

- **`base-sha`** — um ref entra, três artefacts saem: o baseline é materializado
  daquele commit, o checkout vira o patch, e o diff são `base-sha...HEAD`.
  Sem ele, cada usuário montava os três artefatos à mão e errava algum.
- **Demo de aceitação** (`demo/demo.py`) exercitando as três decisões.

### Corrigido

- **AG-012** — um patch alterando **apenas** código não-Python não podia chegar
  a `MERGE`. Caso puro e caso misto (`calc.py` + `calculator.cpp`) fechados.

---

## [2.0.2] — `main`, **sem tag**

### Corrigido

- **AG-001..AG-011** — a auditoria sênior de `v2.0.1` foi confrontada com o
  código *executado*, não lido. Cada achado foi reproduzido por exit code e
  corrigido, com teste de regressão explícito.
- A cobertura da suíte própria passou a ser **evidência** medida, não afirmação.

Ver [`docs/AUDITORIA_CONFRONTO_v2.0.2.md`](docs/AUDITORIA_CONFRONTO_v2.0.2.md)
para a reprodução de cada um.

---

## [2.0.1] — tag `v2.0.1`

### Corrigido

- Mutation score passou a ser **verificado**, não apenas coletado; `suite_strength`
  usa regra estrita.
- Atribuição da suíte completa passou a ser estrita — `full_suite_ran` separa
  "passou" de "nunca rodou", que em `2.0.1` eram indistinguíveis no artefato.
- `exit 3` de uso, outputs da Action, detecção de harness e inputs do piso de
  força passaram a se comportar como documentam.
- Publish deixou de publicar artefatos `dist/` velhos; URLs de projeto
  corrigidas.

---

## [2.0.0] — tag `v2.0.0`

### Adicionado

- Primeiro release: gate de verificação baseado em evidência com rigor de
  filtro duplo — `diff_coverage`, `suite_strength`, estabilidade e a cadeia de
  saída `MERGE` / `BLOCK` / `INCONCLUSIVE`.

---

## Como cortar uma release

Porque a disciplina semver só vale se o procedimento for explícito:

1. Mover as entradas de `[Unreleased]` para uma seção com a versão e a data.
2. `pyproject.toml` → a mesma versão. Se mudou `Added`, bump de **minor**; se
   mudou apenas `Fixed`, bump de **patch**; se quebrou um contrato documentado,
   bump de **major**.
3. `README.md:1` e o banner do `demo/demo.py` → a mesma versão. Um destes
   divergindo é o defeito que a auditoria já apontou.
4. Abrir PR, CI verde (suíte, análise estática, cobertura, build, demo).
5. Só então tag `vX.Y.Z`.
6. Verificar se o `pypi-publish.yml` está saudável (AG-008) **antes** de taggar —
   uma tag com publish quebrado deixa uma run vermelha a cada release, que é
   exatamente por que `2.0.2` e `2.1.0` nunca foram taggeadas.

**Não** bumpar a versão com mudanças soltas em `main`: uma versão no
`pyproject.toml` sem tag correspondente é uma promessa de artefato que não
existe. É por isso que esta seção `[Unreleased]` não tem número.
