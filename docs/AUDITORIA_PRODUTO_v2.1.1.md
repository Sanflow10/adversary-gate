# Auditoria de produto e engenharia — AdversaryGate v2.1.1

**Repositório auditado:** `https://github.com/Sanflow10/adversary-gate`
**Commit auditado:** `3cc103c` (`main`, `pyproject.toml` em `2.1.1`)
**Data:** 2026-10-03
**Pergunta:** o que falta para o projeto subir de conceito, como ele está
desenvolvido hoje, e o que mudar para ele ter relevância real entre
desenvolvedores e empresas.

**Método.** Mesmo critério das auditorias anteriores: nada aqui é afirmado por
leitura quando dava para executar. Cada achado novo traz o comando que o
reproduz. Os dois pontos que só puderam ser verificados por leitura (porque
exigem um runner do GitHub) estão marcados como tal.

---

## Parecer executivo

**Conceito atual: protótipo avançado com núcleo de nível profissional — ainda
não é uma ferramenta adotável fora de repositórios Python sem dependências.**

O que existe é raro e bom: um modelo de decisão de três estados
(`MERGE`/`BLOCK`/`INCONCLUSIVE`) correto, com precedência documentada e testada,
evidência com SHA-256, mutation score restrito às linhas do patch, uma suíte de
153 testes verdes, CI com 8 jobs, análise estática e uma cultura de "reproduzir
antes de corrigir" que pouquíssimos projetos têm. O núcleo **merece** ir longe.

O que segura o projeto não é o núcleo, são três coisas:

1. **A tese não resiste ao ataque mais comum de um agente de código.** Um agente
   que introduz um bug *e reescreve o teste para concordar com ele* recebe
   `MERGE`, exit 0, `suite_strength: 1.0`, `diff_coverage: 1.0` (**AG-021**). A
   proteção existe na biblioteca (`PathPolicy.critic_test_paths`), mas a CLI e a
   Action nunca a ligam.
2. **Em projeto real, quase tudo vira `INCONCLUSIVE`.** Limites fixos
   (30 s, 10 s de CPU, 512 MB) sem flag para mudar, `PATH=/usr/bin:/bin` sem
   `HOME` no `--test-command`, a Action forçando Python 3.12 e rodando os testes
   num interpretador sem as dependências do projeto, e um `test-path` estático
   no YAML (**AG-024, AG-025, AG-031**). Um gate que diz `INCONCLUSIVE` para
   tudo é desligado na segunda semana — é o maior risco de adoção.
3. **A distribuição contradiz a documentação.** O README manda usar a `v2.1.1`,
   mas essa tag e essa Release **não existem** (a URL do wheel responde `404`), e
   o `pip install adversary-gate` padrão entrega a `2.1.0`, que contém um
   fail-open conhecido (AG-018) (**AG-026**).

### Avaliação por dimensão

Escala qualitativa de 0 a 10. Serve para orientar a prioridade, não é uma medição.

| Dimensão | Nota | Por quê |
|---|---|---|
| Núcleo de decisão (rigor, fail-closed) | **8** | Precedência correta, `None` ≠ `0.0` ≠ `1.0`, todos os AG-001..AG-019 com regressão |
| Testes e CI | **8** | 153 testes, 88 % de cobertura, matriz 3.10–3.12, bandit + semgrep, demo como teste de aceitação; faltam lint e checagem de tipos |
| Resistência adversarial (a tese) | **4** | AG-021 e AG-022 são fail-open dentro do próprio modelo de ameaça |
| Experiência do desenvolvedor | **3** | Uma claim por execução, `test-path` estático, saída só em JSON no log, sem resumo no PR |
| Suporte a outras linguagens | **2** | `--test-command` existe, mas o ambiente o inviabiliza; sem adaptador de cobertura nem de mutação |
| Distribuição e releases | **3** | Tag e Release prometidas inexistentes, PyPI com fail-open, namespace top-level genérico |
| Prontidão para empresas | **2** | Sem atestação assinada, policy-as-code, override auditado nem métrica ligada a merges reais |
| Documentação | **6** | Honesta e precisa, mas longa, histórica e misturando dois idiomas |

---

## 1. Como ele está desenvolvido

### 1.1 O que está bom (e deve ser preservado)

- **O modelo de dados é a tese.** `Outcome` de três valores, `CLASSIFICATION_OUTCOME`
  como tabela (`src/core/types.py:82`), `Decision` separado de `Outcome`. A
  incerteza é um resultado, não um default — o diferencial do projeto está no
  tipo, não no marketing.
- **Precedência explícita e testada** em `Gate.decide` (`src/core/gate.py:452`):
  `BLOCK` > `INCONCLUSIVE` > `MERGE`, com evidência direta acima de evidência
  ausente.
- **Execução diferencial baseline × patch** com política de estabilidade por tipo
  de bug (3/3 determinístico, 1/100 concorrência, 4/5 performance).
- **Mutation score sobre as linhas que o patch escreveu**, com mutantes
  *stillborn* fora das duas pontas da razão. É mais honesto que boa parte das
  ferramentas de mutação de mercado, que medem o arquivo inteiro.
- **Proveniência**: `diff_coverage_source`, SHA-256 do diff e da cobertura, e
  `full_suite_ran` separando "passou" de "não rodou".
- **Engenharia de qualidade**: regressão por achado, demo que falha a CI se uma
  decisão mudar, piso de cobertura da própria suíte (87 %), bandit/semgrep,
  `SECURITY.md` que trata fail-open como vulnerabilidade.

Reproduzido nesta auditoria:

```console
$ coverage run --source=src -m pytest -q
153 passed, 19 skipped, 31 subtests passed in 40.95s     # 19 skips = testes de bwrap (sem bubblewrap aqui)
$ coverage report
TOTAL    1205    145    88%
```

### 1.2 O que está fraco na engenharia

| Ponto | Evidência | Efeito |
|---|---|---|
| Orquestração dentro da CLI | `main()` em `src/cli.py:304` tem ~270 linhas: mutação, suíte completa, decisão e artefato | Não existe uma API `evaluate(patch) -> Report`; quem quiser usar como biblioteca precisa copiar a CLI |
| `Gate.decide` com 7 parâmetros soltos | `src/core/gate.py:452` | Fácil chamar errado; o tipo não carrega o significado de cada medida |
| Artefato sem versão de esquema | o JSON não tem `schema_version` | O README declara os campos como contrato, mas nada permite evoluí-los com segurança |
| Módulos não conectados | `contestation`, `quarantine`, `circuit_breaker`, `AggressionLevel.PARANOID` (sem nenhum uso) | Código que parece funcionalidade e não é alcançável pela CLI nem pela Action |
| Números declarados pelo chamador | `--rounds-used` e `--max-rounds` alimentam a regra `NUCLEAR` e a métrica de retrabalho | A regra do modo `NUCLEAR` é contornada passando `--rounds-used 4` (que já é o default) |
| Nome invertido | `GateVerdict.accepted` é `True` quando o resultado é `REFUTED` (`src/core/types.py:163`) | Armadilha para qualquer contribuidor novo |
| Comentários como histórico | docstrings narram versões antigas ("v2.0.1 fazia X") | Útil numa auditoria, ruim para ler o código; o lugar disso é o git e o CHANGELOG |
| Sem lint e sem checagem de tipos na CI | `ruff` acha 9 imports e variáveis não usados em `src/`; `mypy` nem roda (`core/types.py` sombreia o módulo `types` da stdlib) | Dívida silenciosa num código que tem anotações de tipo em todo lugar |

---

## 2. Achados novos (AG-021..AG-031)

IDs propostos, seguindo a numeração de `ERRORS_AND_INCONSISTENCIES.md`.

### AG-021 — Agente reescreve o teste da claim e recebe `MERGE` — **Alta / fail-open**

**Cenário:** o patch troca `a - b` por `a + b` e muda o teste de
`assert sub(5, 3) == 2` para `assert sub(5, 3) == 8`. É o comportamento mais
documentado de agentes de código sob pressão para "fazer os testes passarem".

**Reprodução** (o script completo está no [Apêndice A](#apêndice-a--reprodução-do-ag-021)):

```console
$ bash repro_ag021.sh .
exit=0 decision=merge outcome=verified suite_strength=1.0 diff_coverage=1.0
```

**Causa:**

1. O teste da claim é lido da **árvore do patch** (`gate.py:407`). Baseline roda
   o teste antigo (passa), patch roda o teste reescrito (passa) → `VERIFIED`.
2. A mutação também usa o teste reescrito, que mata o único mutante (`+`→`-`)
   → `1.0`.
3. As linhas do teste entram na cobertura do diff (ver AG-022) → `1.0`.
4. A defesa existe — `PathPolicy(critic_test_paths=[...])` devolve
   `"Builder may not modify a Critic test file"`, verificado nesta auditoria —
   mas a CLI não expõe `critic_test_paths` e a Action nunca passa
   `--changed-path`, nem quando tem o diff em mãos (`base-sha`).

**Correção proposta**, da menor para a mais forte:

- (mínima) Derivar `changed_paths` do `--diff` (`_diff_paths` já faz isso, só
  não alimenta `verify_claim`) e tratar `test_path` da claim como arquivo do
  Critic por padrão. Alteração no arquivo da claim → `INCONCLUSIVE` com o motivo.
- (correta) **Oráculo confiável:** rodar o teste da claim *na versão do
  baseline* (ou de um conjunto congelado do Critic) contra o código do patch.
  O teste vem de quem não escreveu o patch. Testes novos do patch são avaliados
  à parte e nunca autorizam o merge sozinhos.
- Estender a mesma regra a `conftest.py`, `pytest.ini`, `pyproject.toml`
  (`[tool.pytest]`, `[tool.coverage]`) e `.coveragerc`: quem controla a
  configuração do harness controla o veredito.

### AG-022 — Linhas de teste inflam a cobertura do diff — **Média / fail-open parcial**

`covered_diff_ratio` (`src/verifiers/coverage.py:113`) soma **todos** os arquivos
do diff, inclusive testes, e testes quase sempre executam 100 %.

```console
$ python - <<'PY'
import sys; sys.path.insert(0, "src")
from verifiers.coverage import covered_diff_ratio
diff  = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,10 @@\n" + "".join(f"+x{i}=1\n" for i in range(10))
diff += "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -0,0 +1,40 @@\n" + "".join(f"+y{i}=1\n" for i in range(40))
cov = {"files": {"app.py": {"executed_lines": []},
                 "tests/test_app.py": {"executed_lines": list(range(1, 41))}}}
print(covered_diff_ratio(diff, cov).ratio)
PY
0.8
```

**0 de 10 linhas de código-fonte cobertas → cobertura do diff de 80 %, que
passa no piso padrão.** O incentivo é exatamente o errado: quanto mais teste
trivial o agente escreve, menos o código novo precisa ser executado.

**Correção:** excluir do denominador o que `_is_test_path` já reconhece como
teste (a função existe em `strength.py`), gravar no artefato
`source_changed_lines`/`test_changed_lines` separados, e regressão com o caso
acima.

### AG-023 — O mutation score usa uma amostra pequena e enviesada — **Média / rigor**

- `--mutation-max` padrão **6**, e os sítios são pegos **na ordem**: primeiro
  arquivo em ordem alfabética, primeiras linhas (`strength.py:531-551`). Num
  patch com 10 arquivos, os 6 mutantes podem cair todos no primeiro.
- Com **1** mutante morto o score é `1.0` (no AG-021 foi exatamente isso). O
  piso de 0,75 é comparado com uma proporção de n = 1..6 sem nenhum intervalo.
- O catálogo de operadores é pequeno (8 operadores, mais `and`/`or`/`not`). Não
  há mutação de `*`, `/`, `%`, constantes, `True`/`False`, `return`,
  `in`/`is`, nem remoção de instrução. Um patch que só troca `a - b` por `a * b`
  cai em "no mutable operator" → `INCONCLUSIVE`, quando poderia ser medido.

**Correção:** amostragem estratificada por arquivo/hunk com semente gravada no
artefato; mínimo de mutantes contados (ex.: 5) para o score autorizar
`MERGE`; comparar o **limite inferior de Wilson** com o piso, e não a proporção
crua; ampliar os operadores. Isso é a própria tese do projeto aplicada a ele:
*um número sem margem de erro é uma opinião com rótulo de precisão.*

### AG-024 — Limites fixos e ambiente mínimo inviabilizam projetos reais — **Alta / adoção**

Verificado por execução de `run_test`:

```console
PATH=/usr/bin:/bin   HOME=<não definido>     # o que o --test-command recebe
node, npm, cargo, go → não encontrados        # estão em /opt, ~/.cargo, /usr/local/go nesta máquina
alocar 700 MB         → MemoryError, exit 1   # RLIMIT_AS de 512 MB; exit 1 conta como FALHA de teste
java -version         → exit 1, "Could not reserve enough space for 262144KB object heap"
node -e 'console.log(1)' → exit 133, "Fatal process out of memory: Failed to reserve virtual memory"
suíte de 35 s         → exit -1, timed_out    # timeout fixo de 30 s
```

- `timeout_seconds=30`, `cpu_seconds=10`, `mem_bytes=512 MiB` estão fixos na
  assinatura de `verify_claim`/`run_test` e **não há flag na CLI nem input na
  Action** para mudá-los.
- Uma suíte completa de verdade passa de 30 s → a rodada colateral vira
  `(-1, -1)` → `INCONCLUSIVE` sempre. A própria CI do projeto precisou restringir
  `full-suite-path` a um fixture porque a suíte dele (41 s aqui; 70–100 s no runner, segundo o comentário em `ci.yml`) não cabe no limite.
- `RLIMIT_AS` de 512 MB impede a JVM e o Node de sequer iniciarem (medido
  acima). Pior: a JVM morre com **exit 1**, e em Python o estouro aparece como
  `MemoryError` → exit 1 — os dois são lidos como **falha de teste**, ou seja,
  *evidência*. Um patch que aumenta o uso de memória de forma legítima pode
  virar `BLOCK`, e uma suíte Java inteira nunca chega a rodar.
- Sem `HOME` e com `PATH` mínimo, `--test-command "npm test"` termina com 127 →
  `UNRUNNABLE`. A funcionalidade que deveria abrir outras linguagens não
  funciona sem um script intermediário que reconstrua o ambiente.

**Correção:** `--timeout`, `--cpu-seconds`, `--memory` (e `none` para cada um),
`--env KEY=VAL` / `--pass-env KEY` com lista explícita gravada no artefato,
`HOME` apontando para um diretório temporário por padrão. Classificar a morte
por limite de recurso (sinal, `MemoryError` no stderr) como `UNRUNNABLE`, não
como `FAIL`.

### AG-025 — A Action roda os testes num Python sem as dependências do projeto — **Alta / adoção** *(por leitura)*

`action.yml:138-151` executa `actions/setup-python@v5` com `3.12` fixo e
instala só o gate e o `coverage`. O runner chama `sys.executable -m pytest`
(`runner.py:166`), isto é, o Python 3.12 do toolcache.

Consequência, a partir do código (não executado num runner do GitHub):

- projeto em 3.10/3.11, ou que instala dependências em venv/Poetry/uv → os
  imports do projeto não existem nesse interpretador → erro de coleta (exit 2)
  → `UNVERIFIED` → `INCONCLUSIVE` em **todo** PR;
- o `setup-python` da Action troca o `python` do restante do job do usuário.

Só funciona sem fricção um repositório Python sem dependências, em 3.12 — que é
o que os fixtures da CI testam.

**Correção:** input `python` (caminho do interpretador do projeto, padrão
`python` do `PATH`), não chamar `setup-python` quando ele já foi configurado,
e documentar o padrão "instale suas dependências, depois chame a Action".

### AG-026 — A versão recomendada não existe; o PyPI entrega um fail-open conhecido — **Alta / distribuição**

```console
$ git ls-remote --tags origin
refs/tags/v2.0.0  refs/tags/v2.0.1  refs/tags/v2.1.0          # não há v2.1.1
$ curl -o /dev/null -w '%{http_code}' -L https://github.com/Sanflow10/adversary-gate/releases/download/v2.1.1/adversary_gate-2.1.1-py3-none-any.whl
404
$ pip index versions adversary-gate
adversary-gate (2.1.0)
```

- O README e o CHANGELOG dizem que a `2.1.1` "ships as a GitHub Release" e
  recomendam `uses: Sanflow10/adversary-gate@v2.1.1`. Não existe Release
  nenhuma no repositório e a tag não existe — esse `uses:` falha.
- O `pip install adversary-gate` padrão instala a `2.1.0`, que contém o AG-018
  (patch misto modificado + deletado chegando a `MERGE`). Para uma ferramenta
  cujo `SECURITY.md` diz que um `MERGE` indevido é vulnerabilidade, a
  instalação padrão ter um fail-open conhecido é o pior estado possível.

**Correção (minutos, não dias):** rodar o workflow *Release*; registrar o
Trusted Publisher no PyPI (AG-008) **ou** subir a `2.1.1` com token, como foi
feito com a `2.1.0`; criar uma tag móvel `v2` para a Action e recomendar pin
por SHA.

### AG-027 — O wheel instala pacotes top-level genéricos — **Média / empacotamento**

```console
$ unzip -l adversary_gate-2.1.1-py3-none-any.whl
cli.py  core/…  sandbox/…  verifiers/…
```

`cli`, `core`, `sandbox` e `verifiers` vão direto para o `site-packages`.
Qualquer outro pacote com um desses nomes sobrescreve ou é sobrescrito na
instalação — em empresa, onde o gate é instalado no mesmo ambiente do projeto
avaliado, isso é colisão garantida mais cedo ou mais tarde. O mesmo problema faz
o `mypy` recusar o código (`core/types.py` sombreia `types`). Também não há
`--version`.

**Correção:** um pacote só, `adversary_gate/` (`adversary_gate.core`,
`adversary_gate.verifiers`, …), `__version__` lido dos metadados e
`adversary-gate --version`. É uma quebra de layout interno; o README já declara
que "module layout inside `src/`" não é contrato.

### AG-028 — `SECURITY.md` aponta para um e-mail que não existe — **Baixa**

A rota alternativa de reporte é *"email the maintainers through the address in
`pyproject.toml`"*, mas `pyproject.toml` só tem `name = "AdversaryGate
Authors"`, sem e-mail. Para uma política que diz *"silence is the one response
that is off the table"*, a rota de fallback leva a lugar nenhum.

### AG-029 — `self_deception_index` é zero por construção — **Conceitual**

`unverified_merges` só conta registros com `decision == "merge"` e
`unverified > 0` (`metrics.py:127`), mas `decide()` nunca devolve `MERGE` com
claim `UNVERIFIED` (`gate.py:554`). O mesmo vale para `escaped_regressions`.
Com logs produzidos pelo próprio gate, **a métrica da tese é sempre 0 %**.

A métrica só passa a significar algo quando cruza a decisão do gate com o que
aconteceu de fato: PR mergeado apesar de `INCONCLUSIVE`/`BLOCK` (override),
revert em até N dias, incidente ligado ao commit. Ver §4.4.

### AG-030 — Prova de correção e não regressão têm o mesmo rótulo — **Conceitual**

Baseline `FAIL` → patch `PASS` e baseline `PASS` → patch `PASS` caem ambos em
`CLAIM_DISCARDED` / `"test passes on the patch"` (`gate.py:305`). O primeiro é
a evidência mais forte que existe de que o agente **corrigiu** o problema (é o
`FAIL_TO_PASS` do SWE-bench); o segundo só diz que nada quebrou. Separar
`FIXED` de `NO_REGRESSION` no artefato custa pouco e dá ao produto a frase que
falta: *"este patch resolve o que diz resolver"*.

### AG-031 — Uma claim fixa por execução torna a Action estática — **Alta / adoção**

`--claim-json` exige exatamente uma claim (`cli.py:63-67`) e `test-path` é
`required: true` na Action. Num PR qualquer, quem escolhe o teste? Do jeito que
está, o teste fica escrito no YAML — o exemplo do README usa
`tests/test_token_expiry.py` para todo PR do repositório.

**Correção:** várias claims por execução, e um modo de descoberta automática —
por exemplo, testes que cobrem as linhas alteradas (contextos dinâmicos do
coverage.py, `--context=test`) somados aos testes que o próprio PR adiciona.

---

## 3. O que falta para subir de conceito

Três degraus, cada um com um critério de saída verificável.

### Degrau 1 → "ferramenta confiável" (hoje é quase isso)

Critério: *nenhum fail-open conhecido no modelo de ameaça declarado, e a
versão instalada por padrão é a corrigida.*

- AG-021 (oráculo confiável), AG-022 (cobertura sem testes), AG-026 (release),
  AG-028.
- AG-023: mínimo de mutantes + limite inferior de Wilson.

### Degrau 2 → "adotável por um time de desenvolvimento"

Critério: *instalado num repositório Python real, com dependências e uma suíte
de minutos, em menos de 15 minutos, e menos de 20 % dos PRs saudáveis caindo em
`INCONCLUSIVE`.*

- AG-024, AG-025, AG-027, AG-031.
- **`INCONCLUSIVE` acionável:** cada motivo vem com o próximo passo
  ("passe `--diff` e `--coverage-json`", "a linha X de `calc.py` não foi
  executada por nenhum teste"). Hoje o motivo existe, mas o caminho para o
  `MERGE` não está no artefato.
- **Resumo no PR:** `$GITHUB_STEP_SUMMARY` + comentário opcional, com a tabela
  de decisão e o que falta. Ninguém lê JSON em log de CI.
- **Modo sombra** (`--report-only`, exit 0 sempre): é como um time liga o gate
  sem medo e mede a taxa de `INCONCLUSIVE` antes de torná-lo obrigatório.
- **Arquivo de configuração** (`.adversary-gate.toml`) para pisos, limites,
  comandos e caminhos protegidos, em vez de 30 flags.

### Degrau 3 → "relevante para empresas"

Critério: *um auditor de mudanças consegue responder "quem aprovou esta mudança
gerada por IA, com que evidência" a partir de um artefato assinado.*

- Evidência como **atestação assinada** (in-toto/SLSA via Sigstore ou as
  *artifact attestations* do GitHub), com o digest do commit avaliado.
- **Override auditado:** passar por cima de um `INCONCLUSIVE` exige
  justificativa, que vira registro no próprio log.
- **Métrica ligada à realidade** (AG-029): overrides, reverts e incidentes
  cruzados com as decisões, por modelo e por repositório.
- **Multi-linguagem de verdade** (§4.3).
- **Governança:** hoje o `CODEOWNERS` registra um único mantenedor. Empresas
  olham *bus factor*, `CONTRIBUTING.md`, roadmap público e tempo de resposta a
  issues antes de pôr uma ferramenta no caminho do merge.

---

## 4. Como melhorar o conceito

### 4.1 O oráculo vem de quem não escreveu o patch

É a mudança conceitual mais importante e corrige AG-021 pela raiz. A frase
"*o gate só reporta o que um harness saudável executou*" precisa de uma segunda
metade: **"…e o harness não pertence a quem está sendo julgado."**

Concretamente: testes, `conftest.py`, configuração do pytest e do coverage e o
workflow de CI vêm do baseline (ou de um conjunto congelado do Critic). O patch
pode *acrescentar* testes, que são avaliados à parte (e medidos por mutação),
mas nunca *substituir* o oráculo. Toda mudança num teste existente — ainda mais
quando enfraquece uma asserção — vira `INCONCLUSIVE` com o motivo "o patch
alterou o oráculo", e a revisão humana decide.

### 4.2 Fazer o "Adversary" existir

O nome promete um adversário; o repositório entrega o juiz. O Critic aparece
como conceito (`critic_test_paths`, contestação, quarentena) e não como
componente. Há dois caminhos, compatíveis com a tese 2 do README (nenhum LLM
*dentro* do verificador):

- **Critic determinístico:** testes de propriedade (Hypothesis) gerados a partir
  de assinaturas e contratos, mais os mutantes sobreviventes devolvidos ao
  agente como "escreva um teste que mate isto".
- **Critic por LLM, fora do verificador:** escreve as claims a partir da
  *issue/especificação*, **antes de ver o patch**, num diretório que o Builder
  não pode tocar. O gate continua determinístico; o adversário só propõe
  claims, e uma claim só conta se executar.

Isso transforma o produto de "gate de CI" em **protocolo Builder × Critic com
juiz determinístico** — que é o conceito original e o que ninguém mais oferece
pronto.

### 4.3 Multi-linguagem por formatos padrão, não por linguagem

- **Cobertura:** aceitar **LCOV** e **Cobertura XML** além do JSON do coverage.py.
  Um único adaptador LCOV cobre JS/TS (c8/istanbul/vitest), Rust
  (`cargo llvm-cov --lcov`), C/C++ (`llvm-cov export -format=lcov`), Go (via
  conversor) e outras. `covered_diff_ratio` não muda.
- **Mutação:** consumir relatórios externos no esquema JSON do Stryker
  (*mutation-testing-report-schema*) e de `cargo-mutants`, filtrando pelas
  linhas do diff. O gate não precisa saber mutar C++; precisa saber **ler e
  filtrar** o resultado de quem sabe, com o SHA-256 do relatório no artefato,
  como já faz com a cobertura.

Com isso o argumento "`INCONCLUSIVE` em toda stack não-Python é o produto
funcionando" deixa de ser defesa e passa a ser exceção.

### 4.4 Uma métrica de negócio que não é tautológica

Trocar o `self_deception_index` atual (sempre 0, AG-029) por indicadores que
cruzam a decisão do gate com o que aconteceu depois:

| Indicador | Fonte | O que prova |
|---|---|---|
| Taxa de override | PR mergeado com gate ≠ `MERGE` | quanto o time confia no gate |
| Reverts em 14 dias por decisão | git/GitHub | se `MERGE` significa algo |
| `BLOCK` confirmados | `BLOCK` cujo commit nunca foi mergeado ou foi corrigido | regressões evitadas |
| `INCONCLUSIVE` resolvidos | `INCONCLUSIVE` → `MERGE` no mesmo PR | custo de fricção |
| Tudo acima **por modelo/agente** | `ctx_model` | a comparação de modelos que a tese promete |

### 4.5 Um benchmark público — a prova que uma empresa pede

A filosofia do projeto exige isso dele mesmo: *um número que ninguém consegue
re-derivar é uma opinião.* Hoje não há número nenhum sobre a eficácia do gate.

Proposta: rodar o gate sobre patches gerados por agentes em tarefas do tipo
SWE-bench (há patches "plausíveis mas errados" e patches corretos
publicamente disponíveis para vários agentes) e publicar, com scripts
reproduzíveis:

- **taxa de captura:** patches incorretos que passam nos testes existentes e o
  gate não deixa em `MERGE`;
- **taxa de falso `INCONCLUSIVE`:** patches corretos que o gate não deixa passar;
- **custo:** tempo e CPU por decisão.

É o gráfico que abre conversa com empresa e o post que circula entre devs.

---

## 5. Relevância entre desenvolvedores

- **README de uma tela, em inglês.** O que é, por que, cinco linhas de
  instalação, o GIF. O histórico de AG-012/AG-013 vai para `docs/` — é ótimo
  como prova de rigor, mas ruim como primeira impressão. Manter a versão em
  português como tradução, não misturada no mesmo arquivo.
- **Funcionar no primeiro uso:** `uvx adversary-gate`/`pipx run`, uma Action
  que funcione com o Python e as dependências do projeto (AG-025), e um
  repositório de exemplo com o fluxo completo.
- **Entrar no ciclo do agente, não só da CI:** o README cita Claude Code, Cursor
  e Codex. Expor o gate como **servidor MCP** (`verify_patch`) e como hook de
  fim de tarefa do agente permite que o agente receba o `INCONCLUSIVE` e o
  corrija *antes* de abrir o PR. É aí que está a dor do dev hoje.
- **GitHub Marketplace**, tag móvel `v2`, badge de status.
- **Higiene de projeto aberto:** `CONTRIBUTING.md`, templates de issue/PR,
  `good first issue`, idioma consistente nos commits.

## 6. Relevância para empresas

- **Cadeia de suprimentos da própria ferramenta:** releases assinadas, SBOM,
  actions de terceiros fixadas por SHA, OpenSSF Scorecard. Uma ferramenta que
  decide merge é alvo; ela precisa passar no mesmo crivo que impõe.
- **Execução isolada pronta:** uma variante da Action em contêiner (ou imagem
  oficial) com isolamento já configurado, em vez de "rode dentro de um
  sandbox que você controla".
- **Policy-as-code** com padrões por organização e exceções por repositório.
- **Trilha de auditoria** compatível com controles de gestão de mudanças
  (SOC 2 / ISO 27001): quem pediu, qual agente/modelo gerou, que evidência havia,
  quem fez override e por quê.
- **Modelo de negócio possível:** núcleo aberto (CLI + Action) e um painel pago
  com métricas entre repositórios, comparação de modelos e gestão de políticas.
  O núcleo aberto é o que gera confiança; o painel é o que um gestor compra.

---

## 7. Roadmap priorizado

Esforço: P = horas, M = dias, G = semanas.

| # | Item | Achado | Esforço | Impacto |
|---|---|---|---|---|
| 1 | Publicar `2.1.1` (Release + PyPI), tag `v2` | AG-026, AG-008 | P | Alto — hoje a instalação padrão tem fail-open |
| 2 | Proteger o arquivo da claim e a config do harness, a partir do `--diff` | AG-021 (mínima) | P | Alto — fecha o fail-open principal |
| 3 | Cobertura do diff sem linhas de teste | AG-022 | P | Médio |
| 4 | E-mail ou rota real no `SECURITY.md` | AG-028 | P | Baixo |
| 5 | Limites e ambiente configuráveis; morte por recurso ≠ `FAIL` | AG-024 | M | Alto — destrava projetos reais |
| 6 | Action com o Python do projeto | AG-025 | M | Alto |
| 7 | Pacote `adversary_gate`, `--version`, ruff + mypy na CI | AG-027 | M | Médio |
| 8 | Múltiplas claims + descoberta automática | AG-031 | M | Alto |
| 9 | Resumo no PR, `INCONCLUSIVE` acionável, modo sombra | §3 | M | Alto — é o que faz alguém manter o gate ligado |
| 10 | Amostragem estratificada + Wilson + mais operadores | AG-023 | M | Médio |
| 11 | `FIXED` × `NO_REGRESSION` | AG-030 | P | Médio — dá a frase de venda |
| 12 | Oráculo do baseline (transplante de testes) | AG-021 (correta) | G | Alto |
| 13 | Adaptadores LCOV/Cobertura + relatórios de mutação externos | §4.3 | G | Alto — abre o mercado não-Python |
| 14 | Benchmark público com patches de agentes | §4.5 | G | Muito alto para relevância |
| 15 | Atestação assinada, override auditado, métricas ligadas a merges | §3, AG-029 | G | Alto para empresas |
| 16 | Critic (determinístico e/ou LLM) | §4.2 | G | Define o produto |

Ordem sugerida: **1–4 nesta semana** (todos pequenos, e dois são fail-open),
**5–11 no próximo mês** (adoção), **12–16 como o "v3"** que muda de conceito.

---

## Apêndice A — reprodução do AG-021

Requer `pytest` e `coverage` no `PATH`. Uso: `bash repro_ag021.sh <raiz do adversary-gate>`.

```bash
#!/usr/bin/env bash
# AG-021: o agente introduz um bug e reescreve o teste da claim para concordar com ele.
set -euo pipefail
GATE="$(cd "$1" && pwd)/src/cli.py"
W="$(mktemp -d)"; cd "$W"
git init -q repo && cd repo
git config user.email r@r && git config user.name r
printf 'def sub(a, b):\n    return a - b\n' > calc.py
printf 'from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n' > test_calc.py
git add . && git commit -qm baseline
mkdir ../baseline && git archive HEAD | tar -x -C ../baseline
# o "patch" do agente: bug em calc.py + teste reescrito para o valor errado
printf 'def sub(a, b):\n    return a + b\n' > calc.py
printf 'from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 8\n' > test_calc.py
git diff --no-ext-diff --no-color > ../change.diff
coverage run -m pytest -q -p no:cacheprovider >/dev/null && coverage json -q -o ../cov.json
set +e
python3 "$GATE" --baseline ../baseline --patch . --test-path test_calc.py --test-id test_sub \
  --diff ../change.diff --coverage-json ../cov.json > ../out.json
code=$?
set -e
python3 - "$code" <<'PY'
import json, sys
d = json.load(open("../out.json"))
print(f"exit={sys.argv[1]} decision={d['decision']} outcome={d['outcome']} "
      f"suite_strength={d['suite_strength']} diff_coverage={d['diff_coverage_ratio']}")
PY
```

Saída em `3cc103c`:

```console
exit=0 decision=merge outcome=verified suite_strength=1.0 diff_coverage=1.0
```

E a defesa que já existe na biblioteca, quando ligada:

```python
Gate([], path_policy=PathPolicy(critic_test_paths=["test_calc.py"])).verify_claim(
    CriticClaim("test_calc.py", "test_sub"), baseline, patch,
    changed_paths=["calc.py", "test_calc.py"],
)
# -> unverified: "protected path violation: test_calc.py: Builder may not modify a Critic test file"
```
