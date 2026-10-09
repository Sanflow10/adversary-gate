# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versionado: [SemVer 2.0.0](https://semver.org/lang/pt-BR/).

Duas regras que este arquivo obedece, e que valem mais que o formato:

1. **Uma entrada por mudança que um usuário poderia notar**, escrita quando a
   mudança acontece — não quando ela fica "pronta".
2. **A seção diz a verdade sobre o artefato.** Uma versão sem tag não é uma
   release, e este projeto já sofreu com README dizendo uma coisa e pacote
   dizendo outra (ver [`docs/AUDITORIA_CONFRONTO_v2.0.2.md`](docs/AUDITORIA_CONFRONTO_v2.0.2.md)).

Estado das tags hoje: **`v2.0.0`, `v2.0.1`, `v2.1.0`, `v2.2.0`, `v2.3.0`, `v2.4.0`, `v2.5.0`, `v2.6.0`, `v2.7.0`, `v2.8.0`, `v2.9.0`, `v2.10.0`, `v2.11.0`, `v2.12.0`, `v2.13.0` e `v2.14.0`**, mais a
flutuante `v2`, que o Release move para a 2.x mais nova. A `2.1.1` chegou a ter
seção aqui e nunca virou tag nem Release (AG-026): o trabalho dela entrou na
`2.2.0`, do mesmo jeito que o da `2.0.2` entrou na `2.1.0`. A `2.14.1` é
publicada pelo workflow **Release** (*Actions → Release → Run workflow*) a
partir do commit que a contém — se `v2.14.1` não aparece em *Releases*, o
workflow ainda não rodou, e esta seção ainda é uma promessa.

O AG-008 está **fechado** desde 2026-10-04: o Trusted Publisher foi registrado
no pypi.org (`Sanflow10` / `adversary-gate` / `pypi-publish.yml`, sem
environment) e a `2.4.0` subiu por OIDC, com atestados de proveniência. A
`2.2.0` e a `2.3.0` nunca chegaram ao PyPI: lá a sequência é `2.1.0` → `2.4.0`.

Rotas de publicação até aqui: `2.0.0`, `2.0.1` e `2.1.0` subiram ao PyPI **por
upload com API token**, não por OIDC. A partir da `2.2.0` o workflow Release
envia ao PyPI sozinho quando o segredo `PYPI_API_TOKEN` existe, e avisa na run
quando não existe. Hoje o PyPI tem a mesma versão que o Release; uma versão
antiga lá continua com os achados corrigidos depois dela (a `2.14.0` tem o
AG-043; todas antes da `2.14.1` têm o AG-044) — confira com
`pip index versions adversary-gate`.

---

## [Unreleased]

A seção fica sem número de propósito: uma versão no `pyproject.toml` sem tag
correspondente é uma promessa de artefato que não existe, e o bump só acontece
quando esta seção vira uma com versão e data.

### Corrigido

- **AG-045 — o baseline do MCP é o que o id do commit contém.** O baseline passa
  a ser lido objeto a objeto (`ls-tree` + `cat-file`), com a substituição de
  objetos desligada, e cada blob é conferido contra o id que a árvore cita; o
  `ref` é resolvido uma vez por execução e o diff roda contra esse id, sempre
  como texto. `verify_repo` devolve `baseline_sha`, e `gate_policy` diz se o pin
  do operador é um id de commit (que não se move) ou um nome de ref. Limite
  mantido: um agente que executa código com o mesmo usuário ainda alcança a
  pasta temporária do gate (a fronteira já documentada).

### Alterado

- **Licença: AGPL-3.0-only a partir da próxima versão.** Qualquer cópia
  modificada, inclusive oferecida como serviço, tem que publicar o código. As
  versões até a `2.14.1` foram publicadas sob MIT e continuam sob MIT (cada tag
  carrega o seu `LICENSE`). Licença comercial, sem as obrigações da AGPL, sob
  consulta.

---

## [2.14.1] — 2026-10-09

Corrige dois fail-open achados na revisão da `2.14.0` (ledger, seção 18). A tag
flutuante `v2` passa para a `2.14.1` sozinha quando o Release roda; quem fixou
uma versão anterior (`@v2.14.0`, `==2.14.0` ou mais antiga) deve atualizar. O
padrão da `2.14.0` podia dar `MERGE` para um teste que não confere nada.

### Corrigido

- **AG-043 — uma execução que outra execução fez falhar contava como mutante
  morto (só na `2.14.0`).** Os mutantes rodavam em paralelo e a suíte completa
  junto com eles, e só a morte por limite de tempo era refeita sozinha. Uma
  execução que falha porque outra mexeu na mesma coisa fora do repositório (um
  caminho fixo em `/tmp`, uma porta, um banco de teste) sai com exit 1, igual a
  uma detecção. Medido: um teste que não confere nenhum valor foi de 0/8 mortos
  (`INCONCLUSIVE`) para 8/8 e `MERGE`; só a suíte ao lado, com os mutantes um
  por vez, já levava 0/8 a 4/8. Agora a mutação espera a suíte terminar e roda
  um mutante por vez. Custo no caso real que mais ganhava com o paralelo
  (more-itertools d992be0, 12 mutantes): a parte do gate foi de 126,5 s para
  153,6 s, com a mesma decisão e os mesmos 12/12 mortos. As claims continuam
  rodando com os lados juntos e com a suíte ao lado: ali a interferência pode inflar o rótulo
  `fixed` (`fix_proven`), nunca a decisão; `--serial-sides` evita.
- **AG-044 — o baseline perdia o que o `.gitattributes` do projeto tira do
  pacote.** O baseline era montado com `git archive`, que aplica os atributos
  de exportação: `export-ignore` tira arquivos e `export-subst` os reescreve.
  Num projeto com `tests/ export-ignore` (prática comum para deixar os testes
  fora do sdist), o baseline saía sem os testes, um teste reescrito pelo patch
  era julgado como novo e o oráculo do baseline (AG-021) não se aplicava. Na
  Action e no servidor MCP, o baseline agora é a árvore do commit escrita por
  um índice temporário (`read-tree` + `checkout-index`): todo arquivo como foi
  commitado, como o checkout do workspace o escreveu, sem tocar no índice nem
  na árvore de trabalho.

### Adicionado

- **`--mutation-workers N`** (padrão 1): mutantes ao mesmo tempo, cada um na
  sua cópia da árvore. Só para testes que não dividem nada fora do
  repositório; recusado junto com `--census-min-mutants`, porque uma contagem
  completa exige todos mortos e lê a razão exata. `--serial-sides` força 1.

### Alterado

- **A documentação do servidor MCP diz o limite do baseline fixado:** fixar a
  ref nomeia o baseline; não é defesa contra um agente que pode escrever no
  `.git` do repositório (refs, objetos, configuração) — esse agente tem que
  rodar onde não pode, como qualquer código que roda com os seus privilégios.
- **`TestWholeFileRunsHaveSuiteLimits` deixou de depender da máquina:** usava
  `--timeout 1` fixo e falhava onde o pytest leva mais que isso só para subir
  (1,8 s com alguns plugins). O limite sai de uma execução medida na hora.

---

## [2.14.0] — 2026-10-08

**Errata (2.14.1).** Duas afirmações desta seção não valem para os mutantes.
"Cada um desses derruba um lado, o que termina em BLOCK ou INCONCLUSIVE, nunca
em MERGE" vale para os lados de uma checagem; uma execução de mutante que
outra execução fez falhar contava como **morta**, a direção do `MERGE`
(AG-043). E "a decisão ... saíram idênticos" foi medido numa matriz cujos
testes não dividem nada fora do repositório, onde isso não aparece. Use a
`2.14.1`.

O gate ficou mais rápido sem mudar o que decide. Os lados baseline e patch, a
suíte completa e os mutantes passam a rodar ao mesmo tempo. Na matriz de
cinco casos reais do more-itertools, a parte do gate em relação à `2.13.0`
caiu de 319→127 s, 177→101 s, 285→109 s e 285→108 s (a reversão 6b1907d, que
não tem nada para sobrepor, ficou em ~90 s). A decisão, `fix_proven`, as
claims e o morto/sobrevivente de cada mutante saíram idênticos. `--serial-sides`
volta tudo ao modo um-depois-do-outro.

### Alterado

- **Os dois lados de cada verificação rodam ao mesmo tempo.** A claim no
  baseline e no patch, o arquivo de testes do baseline nos dois lados (AG-021)
  e, quando o patch reescreveu testes, a suíte completa no patch e no
  transplante. As execuções são as mesmas, na mesma quantidade e em diretórios
  diferentes. Em cinco casos reais do more-itertools (reversão, correção e
  correção com asserções esvaziadas), a decisão, `fix_proven` e a
  classificação de cada claim saíram idênticas, e a parte do gate caiu
  de 285→156 s, 319→199 s, 177→119 s e 286→171 s (a reversão 6b1907d, sem par
  a sobrepor, ficou em ~90 s). O que a sobreposição pode mudar é o que a
  disputa por máquina causa: um limite de tempo de relógio estourado, memória
  para dois processos, estado que os lados dividem fora do repositório (um
  caminho fixo em `/tmp`, uma porta, um `HOME` passado com `--pass-env`).
  Cada um desses derruba um lado, o que termina em BLOCK ou INCONCLUSIVE,
  nunca em MERGE; um lado que estourou o tempo não é repetido, porque uma
  segunda chance também seria uma segunda chance para um teste que só trava
  às vezes. `--serial-sides` volta ao modo um-depois-do-outro, e o artefato
  registra o modo em `execution.serial_sides`. Quando a suíte completa passa
  no baseline e estoura o tempo só no patch, `next_steps` sugere subir o
  limite ou `--serial-sides` em vez de "corrigir o código".
- **A suíte completa roda junto com as claims e os mutantes**, não depois
  deles. Ela não depende de nada que eles encontram, então começa antes da
  primeira claim, numa cópia do patch feita nesse momento (com `.git`, para
  uma suíte que consulta o git não falhar só na cópia e virar "regressão
  colateral"), e o baseline continua rodando só se o patch falhar. A disputa
  por máquina abria um caminho para fail-open: um mutante que estoura o tempo
  conta como morto (AG-042), e um estouro causado pela suíte ao lado inflaria
  o score. Por isso um mutante que estoura o tempo enquanto a suíte ainda roda
  é repetido depois que ela termina, sozinho, e a repetição decide
  (`rerun_alone` no artefato traz o código da primeira); a repetição só pode
  trocar morto por sobrevivente ou natimorto. A execução de referência é
  repetida do mesmo jeito quando saiu lenta demais sob carga. Na matriz dos
  cinco casos do more-itertools, a decisão, `fix_proven`, as claims e o
  morto/sobrevivente de cada mutante saíram idênticos, e a parte do gate caiu
  de 192→145 s, 120→100 s, 158→106 s e 153→103 s (a reversão 6b1907d ficou
  em ~93 s). `--serial-sides` também desliga isto (suíte depois, no próprio
  diretório do patch), e o artefato registra o modo em
  `execution.full_suite_alongside`.
- **Mutantes rodam em paralelo**, metade das CPUs e no máximo 4 ao mesmo
  tempo, cada worker na sua própria cópia da árvore; o artefato traz a
  quantidade em `execution.mutation_workers` e as entradas saem na ordem do
  plano, seja qual for o worker que terminar primeiro. Toda execução teve
  vizinhas, então a mesma regra de cima vale: um mutante morto pelo relógio é
  repetido sozinho, depois que todos terminam, e a repetição decide. Um morto
  pelo limite de **CPU** não é repetido, porque tempo de CPU é do próprio
  processo e não cresce com a disputa, a não ser que a referência sem
  mutação tenha usado um terço desse limite (aí ele também é repetido). Na
  matriz do more-itertools, o morto/sobrevivente de cada mutante, as claims e
  a decisão saíram idênticos ao modo um-por-vez, sem nenhuma repetição; o
  ganho aparece onde há mutantes em número (d992be0, 12 mutantes: 145→127 s),
  e com 3 mutantes é nulo. `--serial-sides` volta a um por vez.
- **O transplante da suíte completa é copiado antes de a suíte do patch
  rodar** (antes, depois). Arquivos que a suíte do patch gerava dentro do
  repositório não entram mais na cópia que o oráculo do baseline julga.
- **A criação de cada processo de teste é serializada** (só o `fork`; a espera
  pelo processo continua em paralelo): `preexec_fn`, que aplica os limites, é
  documentado como inseguro com outras threads rodando.

---

## [2.13.0] — 2026-10-07

A Action expõe o código de saída do gate, para que um workflow possa separar
`BLOCK`, `INCONCLUSIVE` e erro sem `continue-on-error` às cegas.

### Adicionado

- **Saída `exit-code` na Action** (0 merge, 1 block, 2 inconclusive, 3/4 erro
  de uso ou do harness; vazia se a Action parou antes do gate). Um workflow que
  quer deixar `INCONCLUSIVE` passar onde a medição é impossível (C++, SQL)
  precisava de `continue-on-error`, que deixa passar `BLOCK` junto; e
  `decision` não serve para separar, porque uma execução sem veredito (exit
  3/4) também lê `inconclusive`. Receita no README. Apontado por um leitor do
  artigo no dev.to.

---

## [2.12.0] — 2026-10-07

O gate operado como agente até o `MERGE`. Mutantes rodam só as claims (AG-039);
`next_steps` diz o que transformaria cada resultado em `MERGE`; mutante que trava
os testes conta como morto (AG-042); comentário deixou de contar como linha sem
teste (AG-040); contagem completa experimental, desligada por padrão. Validação
de 06/out, 40 execuções reais do `more-itertools` (padrão e
`--census-min-mutants 1`): nenhum bug reintroduzido chegou a `MERGE`, e 7 de 8
correções reais foram provadas (`fix_proven`). Detalhes no ledger, seções 16 e 17.

### Desempenho

- A checagem de que os testes antigos de um arquivo ainda passam roda **uma vez
  por arquivo**, não uma vez por teste novo nele: ela depende do baseline, do
  patch e do arquivo, não de qual teste perguntou. `def2dab` real (três testes
  novos num arquivo): 782 → 477 s, mesma decisão.

### Corrigido

- **AG-039** — mutantes rodam os node ids das claims, não seus arquivos de
  teste inteiros: a força não pode mais ser inflada por um teste fora das
  claims que falha de qualquer jeito, e arquivos grandes não deixam todo
  mutante "natimorto". O `reason` separa mutante morto por limite.
- **AG-040** — linhas adicionadas em branco ou só de comentário (em `.py`) não
  contam mais como linhas alteradas sem teste na cobertura de diff.
- **AG-042** — mutante que faz os testes nunca terminarem conta como morto,
  desde que as claims sem mutação terminem em menos de 1/3 do limite
  (`reference_run` no artefato); senão continua natimorto.

### Adicionado

- **`next_steps`** no artefato e no `verify_repo` (MCP): o que transformaria
  um resultado em `MERGE`, em ações que um agente executa — cobrir linhas,
  matar um mutante nomeado, adicionar um teste novo, corrigir o código, pedir
  revisão humana de um teste do baseline alterado, subir um limite. Nunca
  sugere editar teste do baseline, harness, piso ou política.
- **`--census-min-mutants N` (experimental, padrão 0 = desligado):** quando a
  mutação cobriu todos os pontos da mudança e todos morreram, julga a razão
  exata em vez do limite de Wilson. Detalhes e a validação em 24 execuções
  reais no ledger, seção 16.

---

## [2.11.0] — 2026-10-05

O gate usado como agente: o servidor MCP ligado a um agente de código e chamado sobre
oito correções reais do `more-itertools`, nos dois sentidos. Antes: 4 de 8 bugs
reintroduzidos bloqueados, 0 de 8 correções provadas. Agora: 7 de 8 e 6 de 8,
nenhum `MERGE` errado em 16 execuções. Detalhes no ledger, seções 14 e 15.

```bash
uvx adversary-gate-mcp
```

### Corrigido

- **AG-033** — a suíte colateral rodava sob os limites de um teste
  (`--timeout 30`, `--cpu-seconds 10`) e morria dos dois lados em qualquer
  suíte de tamanho real; patches que só removem código voltavam `INCONCLUSIVE`
  em vez de `BLOCK`; e sob 512M de memória o pytest de uma suíte real morre
  com `MemoryError`. Novos `--full-suite-timeout` (900),
  `--full-suite-cpu-seconds` (nenhum) e `--full-suite-memory` (4G).
- **AG-034** — o `reason` explica a decisão quando ela veio da suíte colateral
  (regressão fora das claims, ou suíte morta pelo limite).
- **AG-035** — o arquivo `.git` de uma worktree ou submódulo não conta mais como
  código estrangeiro alterado.

### Adicionado

- **Teste adicionado pode provar a correção (FAIL_TO_PASS).** Um teste que o
  patch acrescenta roda contra o código do baseline; falhando lá (exit 1, em
  todas as execuções) e passando no patch, vira `fixed` e `fix_proven: true`.
  Medido: 6 de 8 correções reais do `more-itertools` (antes 0).

### Corrigido (cont.)

- **AG-036** — a rodada do arquivo de teste inteiro (teste novo num arquivo
  existente) usa os limites da suíte colateral.
- **AG-037** — teste definido numa mixin vira os nós das classes que o
  executam.
- **AG-038** — `BLOCK` pelo oráculo do baseline diz que mudança de
  comportamento intencional precisa de revisão humana do teste alterado.
- O `reason` de um `INCONCLUSIVE` com todas as claims verificadas nomeia o
  piso que faltou.

### Desempenho

- **`verify_repo` (MCP) não gera mais `coverage json --show-contexts`.** Ele
  escrevia toda linha de todo arquivo medido com todo teste que a executou —
  no `more-itertools`, 193 s e 312 MB — para um relatório do qual descoberta
  e cobertura de diff só leem as linhas alteradas. Agora lê o banco do
  coverage direto, só para os arquivos e linhas do diff (0,3 s). Medido em
  duas reversões reais: 411 → 278 s e 412 → 332 s, mesmas decisões.

---

## [2.10.0] — 2026-10-05

As ferramentas MCP dizem a quem chama quando chamar e o que acontece; o extra
`[mcp]` passa a exigir um SDK que de fato sobe o servidor.

```bash
uvx adversary-gate-mcp
```

### Adicionado

- **Título e dicas de comportamento nas ferramentas MCP** (`annotations`):
  `gate_policy` é somente leitura, idempotente e não sai da máquina;
  `verify_repo` não é somente leitura e é *open-world* — os testes do
  repositório rodam, e um `--triage` do operador manda o diff para fora.
- **Descrições das ferramentas reescritas** para quem chama: quando chamar,
  o que acontece, efeitos colaterais (os testes do repositório executam; a
  rodada de cobertura usa o repositório como diretório de trabalho), timeout
  vira inconclusive, e o que volta. `tests/test_integrations.py`
  (`TestToolMetadata`) trava as dicas.

### Corrigido

- **Piso do extra `[mcp]`: `mcp>=1.9` → `mcp>=1.14`.** Medido em 1.9.4, 1.12 e 1.13:
  o `tool()` do SDK falha ao registrar `verify_repo`
  (`issubclass() arg 1 must be a class`) e o servidor não sobe; 1.14, 1.30 e
  2.3 registram as duas ferramentas. Quem tinha um SDK antigo fixado recebia um
  servidor quebrado.
- O registro das ferramentas passa ao SDK só os argumentos que o `tool()`
  dele aceita, e nunca tenta de novo após um erro: um registro que falha
  estoura, em vez de deixar a ferramenta de fora em silêncio.

---

## [2.9.0] — 2026-10-05

O servidor MCP com um comando só, do jeito que os registros de MCP instalam
servidores Python.

```bash
uvx adversary-gate-mcp
```

### Adicionado

- **Pacote `adversary-gate-mcp`** (`packaging/mcp/`) — não traz código: fixa
  `adversary-gate[mcp]` na mesma versão e declara o executável
  `adversary-gate-mcp`. Registros de MCP iniciam servidores Python com
  `uvx <pacote>`, e `uvx adversary-gate` abre a CLI sem o SDK MCP (que é
  extra). O núcleo continua dependendo só do pytest.
- **`server.json`** — a entrada do servidor para o registro oficial de MCP
  (`io.github.Sanflow10/adversary-gate`); a linha `mcp-name` no README do
  pacote novo é o que prova a posse no PyPI.
- `pypi-publish.yml` constrói e envia os dois pacotes da mesma tag.
  `tests/test_release_consistency.py` trava a versão do pacote novo, o pino da
  dependência e as versões do `server.json` na versão do núcleo.

## [2.8.0] — 2026-10-04

Integrações com agentes: o gate como servidor MCP (Hermes, Claude Code) e
triagem de risco com o Jev (TypeSafe AI). Nenhuma das duas pode afrouxar uma
decisão.

```bash
pip install "adversary-gate[mcp]==2.8.0"
```

### Adicionado

- **`adversary-gate-mcp`** (extra `[mcp]`) — servidor MCP stdio com
  `verify_repo` e `gate_policy`. Julga a árvore de trabalho de um repositório
  git contra um baseline: `git archive` do baseline, diff com arquivos não
  rastreados, coverage.py com contextos por teste fora do repo, descoberta de
  claims. **O agente só nomeia evidência**; política
  (`ADVERSARY_GATE_POLICY`), baseline (`ADVERSARY_GATE_BASE_REF`) e
  interpretador (`ADVERSARY_GATE_PYTHON`) são do operador. Opções em
  `pytest_args` e flags de evidência na política são recusadas.
- **Skill do Hermes** e bloco de `config.yaml`, empacotados:
  `adversary-gate-mcp --print-hermes-skill` / `--print-hermes-config`.
- **`--triage jev`** (`--triage-endpoint`, `--triage-threshold`): pergunta ao
  Jev o risco do diff. `high` confiante sobe os pisos desta execução
  (confiança 0.95, cobertura 0.90); qualquer outra resposta, erro ou timeout
  não muda nada. Registrado em `execution.triage`. O diff sai da máquina
  (64 KiB, só https) — por isso é opt-in. Cliente testado contra servidor local
  no formato publicado pela TypeSafe, não contra a API real.

---

## [2.7.0] — 2026-10-04

Fecha o AG-023 e o AG-030.

```bash
pip install adversary-gate==2.7.0
```

### Alterado — **muda decisões**

- **AG-023 — o piso de força lê um intervalo de confiança.** O piso
  (`--suite-strength-floor`, 0.75) é aplicado ao **limite inferior de um
  intervalo de Wilson a 80 %** em volta de `mortos / contados`, não à razão.
  1 de 1 morto era `1.0` e passava; agora é `INCONCLUSIVE`. **Patches com
  menos de 5 operadores mutáveis nas linhas alteradas não chegam a `MERGE`**
  no padrão. `--strength-confidence 0` (Action: `strength-confidence: '0'`)
  restaura o comportamento anterior. Artefato: `suite_strength_lower`,
  `suite_strength_confidence`, `mutation.interval`, `mutation.confidence`.
- Sítios de mutação espalhados (um por linha alterada antes de repetir), não
  mais em ordem de arquivo; `--mutation-max` padrão 6 → 12.

### Adicionado

- **AG-030 — `fixed` × `no_regression`.** `FAIL→PASS` é `fixed`, `PASS→PASS` é
  `no_regression` (`discarded` fica para teste novo). Saída: `claims_fixed`,
  `fix_proven`. Relatório de métricas separa os dois.

---

## [2.6.0] — 2026-10-04

Fecha o que o oráculo do baseline tinha deixado aberto.

```bash
pip install adversary-gate==2.6.0
```

### Adicionado

- **`--test-support GLOB`** (Action: `test-support`) — declara helpers cujo
  caminho não diz que são de teste (`testing_utils.py`, `helpers/*`). Contam
  como teste em todo lugar: fora da cobertura e da mutação, e restaurados do
  baseline pelo oráculo. Registrado em `execution.test_support`.
- **A suíte colateral também roda os testes do baseline** contra o código do
  patch quando o patch mudou testes que o baseline tinha
  (`full_suite_oracle`). Teste fora de qualquer claim, entortado para
  concordar com o bug → `BLOCK`.

### Alterado

- Claims de `--test-command` com `test-id` passam pelo oráculo (antes:
  `UNVERIFIED`).
- `is_test_path` reconhece `__tests__/`, `*.test.*`, `*.spec.*`, `*_test.go`,
  `*Test.java`/`*Tests.java`, `*Test.kt`, `*_spec.rb`, `*_test.rb`. Nenhum é
  módulo Python, então cobertura e mutação de Python não mudam.

---

## [2.5.0] — 2026-10-04

O oráculo do baseline — o item aberto do AG-021.

```bash
pip install adversary-gate==2.5.0
```

### Alterado

- **Um teste reescrito é julgado pela cópia do baseline.** Se o patch mudou o
  arquivo de teste da claim (ou qualquer arquivo de teste que o baseline já
  tinha — helpers incluídos), o gate copia a árvore do patch, devolve os
  arquivos de teste do baseline e roda a claim ali. O ataque do AG-021 (bug +
  teste reescrito) passa de `INCONCLUSIVE` (exit 2) a **`BLOCK` (exit 1)**; a
  refatoração honesta do arquivo de teste passa de `INCONCLUSIVE` a `VERIFIED`.
  Um helper entortado para concordar com o bug também é pego.
- **Teste novo num arquivo existente** — o formato mais comum de um agente, e
  até aqui sempre `INCONCLUSIVE` — é julgado como teste adicionado, depois que
  todos os testes que o baseline tinha naquele arquivo passam no código do
  patch. Se o patch entortou um deles: `REFUTED`.
- O artefato ganha `"oracle"` por veredito: `patch`, `baseline` ou
  `baseline-file`.
- `--discover-claims` não exclui mais arquivos de teste reescritos; o detalhe
  passa de `excluded_rewritten_test_files` a
  `rewritten_test_files_judged_by_baseline`.

---

## [2.4.0] — 2026-10-04

Fecha o AG-032 (fail-open) e o AG-027; AG-023 parcial; AG-029 mitigado.

```bash
pip install https://github.com/Sanflow10/adversary-gate/releases/download/v2.4.0/adversary_gate-2.4.0-py3-none-any.whl
```

**Quebra para quem importa como biblioteca:** `core.*`, `verifiers.*`,
`sandbox.*` e `cli` passam a ser `adversary_gate.core.*` etc. A CLI e a Action
não mudam.

### Segurança

- **AG-032 — o patch não configura mais o runner que o julga.** Um patch com
  bug + `.pytest.ini` (`addopts = -p plugin`) + um plugin que só mente para os
  bytes exatos do bug chegava a `MERGE`, exit 0, `suite_strength: 1.0`. Agora
  os arquivos de harness são comparados **árvore contra árvore** com o
  baseline, não pela lista de caminhos declarados: qualquer diferença deixa a
  claim `UNVERIFIED`. O denylist passou a cobrir `.pytest.ini`, `pytest.toml`,
  `.pytest.toml`, `sitecustomize.py`, `usercustomize.py` e `*.pth`.

### Alterado

- **AG-027 — um só pacote.** O código foi para `src/adversary_gate/`; o wheel
  instala `adversary_gate` e mais nada no topo (antes: `cli`, `core`,
  `sandbox`, `verifiers`, que colidiam com pacotes do projeto testado). Quem
  importava `core.gate` passa a importar `adversary_gate.core.gate`. O console
  script `adversary-gate` não muda; sem instalar,
  `PYTHONPATH=src python3 -m adversary_gate`. Novo `--version`.
- **AG-023 (operadores)** — a mutação passa a quebrar `*` `/` `//` `%` `**`,
  `+=` `-=` `*=` `/=` e `True`/`False`. Um patch `a + b` → `a * b` deixava de
  ser medido ("no mutable operator"); agora tem mutante. `*` e `/` em posição
  de sintaxe (`*args`, `import *`, marcadores de assinatura) não são tocados.
  Tamanho de amostra e intervalo de confiança continuam abertos.

### Documentação

- README: a tese está em inglês; o `self_deception_index` saiu do pitch,
  porque é 0 por construção nos logs do próprio gate (AG-029); nova seção
  *What a test cannot see, by construction* (código no mesmo processo do
  pytest, código que detecta que está sob teste).

---

## [2.3.0] — 2026-10-04

Fecha AG-024, AG-025 e AG-031: **como** os testes rodam, **em qual** Python, e
**quais** testes. Até aqui um run verificava um único teste, escolhido à mão,
no interpretador do gate, sob limites gravados no código — e a Action trocava o
Python do job inteiro para conseguir um.

```bash
pip install https://github.com/Sanflow10/adversary-gate/releases/download/v2.3.0/adversary_gate-2.3.0-py3-none-any.whl
```

### Adicionado

- **AG-031 — várias claims por run, e descoberta automática.** `--test-id`
  repete, `--claim PATH::ID` repete, e `--claim-json` aceita qualquer número de
  claims (antes: exatamente uma). A decisão é sobre todas: um `REFUTED`
  bloqueia, um `UNVERIFIED` deixa `INCONCLUSIVE`. `--discover-claims` verifica
  os testes que **executaram uma linha de código-fonte alterada**, lidos dos
  contextos por teste do coverage.py (`dynamic_context = test_function` +
  `coverage json --show-contexts`) — não adivinhados. Só linhas de código
  contam (um teste sempre executa as próprias linhas); um arquivo de teste que
  o patch **reescreveu** nunca é escolhido, porque o veredito dele seria o
  patch se corrigindo (AG-021), e aparece nomeado em `discovery`. Relatório sem
  contextos é erro de uso (exit 3), não "nenhum teste". `--max-claims`
  (padrão 10) limita o custo e o artefato diz quantas foram cortadas.
- **AG-024 — limites e ambiente configuráveis.** `--timeout`, `--cpu-seconds`
  e `--memory` (`512M`, `4G`…, ou `none` — a JVM e o Node não sobem sob limite
  de espaço de endereçamento); `--pass-env NOME` e `--env NOME=VALOR`. Valem
  para todas as execuções — claims, mutantes e suíte completa — e o artefato
  registra tudo em `execution`, com os **nomes** das variáveis e nunca os
  valores.
- **AG-025 — `--python`.** O interpretador que roda o pytest: o do projeto,
  onde as dependências estão. Validado antes de qualquer execução (um caminho
  errado é exit 3) e registrado no artefato. Com `--sandbox bwrap`, o venv e a
  instalação dele são montados somente leitura.
- **Action:** entradas `python`, `claims`, `discover-claims` (`auto` por
  padrão: liga quando nenhum teste é nomeado), `max-claims`, `timeout`,
  `cpu-seconds`, `memory`, `pass-env`, `env`; saídas `claims-total` e
  `python`. `test-path` deixou de ser obrigatório.

### Corrigido

- **AG-024 — morte por limite deixou de ser evidência contra o patch.**
  Executado: sob 512 MiB, alocar 4 GiB num teste gera `MemoryError`, o pytest
  sai com 1, e o mapa de saída lia isso como teste falhando — `BLOCK` por algo
  que o patch não fez. Exit 1 com `MemoryError`, `Fatal process out of memory`,
  `Could not reserve enough space` e afins agora é morte do harness (exit 3 →
  `UNVERIFIED` → `INCONCLUSIVE`). O erro vai no sentido seguro: uma falha real
  que imprima um desses textos vira `INCONCLUSIVE`, nunca `MERGE`.
- **AG-025 — a Action não troca mais o Python do job.** Ela fazia
  `setup-python 3.12` no `PATH`: os testes rodavam num interpretador sem as
  dependências do projeto, e todo passo depois dela herdava esse Python. Agora
  o gate vive num venv privado (`update-environment: false`), os testes rodam
  no primeiro `python`/`python3` do `PATH` que importa pytest — o do
  `setup-python` do próprio usuário — ou no indicado em `python:`, e o
  coverage.py roda nesse mesmo interpretador (instalado ao lado dele, em
  diretório próprio, quando falta — nunca dentro do ambiente do projeto).
- **Os mutantes ignoravam `--sandbox bwrap`.** A medição de força rodava cada
  mutante sem o sandbox pedido, sempre no Python do gate e sem o ambiente do
  chamador. Agora recebe as mesmas opções das claims.
- **Ícone da Action.** `shield-check` não está no conjunto do Feather que o
  branding de Actions aceita; virou `shield`.

### Alterado

- Cada execução recebe um `HOME` privado, criado para ela e apagado depois
  (a menos que `--env HOME=…` diga outro). Ferramentas que insistem em gravar
  no home (npm, cargo, caches do pip) não escrevem no repositório nem no home
  real. Os pacotes do *user site* continuam visíveis via `PYTHONUSERBASE`.
- A saída JSON ganhou `claims_total`, `claims` (um registro por claim),
  `execution` e, quando há descoberta, `discovery`. Os campos de topo
  (`outcome`, `classification`, `reason`, `evidence`) descrevem o veredito que
  **explica** a decisão — o primeiro `REFUTED`, senão o primeiro
  `UNVERIFIED` — e com uma claim só são os mesmos de antes. O mesmo vale para a
  saída `outcome` da Action.
- A mesma claim nomeada duas vezes roda uma vez só.
- **Publicar no PyPI uma release que já existe.** O `pypi-publish.yml` ganhou
  *Run workflow* com a tag como entrada. A `2.2.0` saiu como GitHub Release, mas
  a tag foi criada pelo token do próprio workflow Release, e eventos desse
  token não disparam outros workflows. Sem este caminho, a única saída era
  refazer a Release, o que o Release recusa. O workflow constrói a partir da
  árvore da tag, recusa uma tag cujo `pyproject.toml` diga outra versão, e
  autentica com `PYPI_API_TOKEN` quando o segredo existe ou por Trusted
  Publishing quando não existe.
- O aviso do Release quando falta o token deixou de mandar "rodar este workflow
  de novo", o que falharia. Agora aponta para o *Publish to PyPI*.

### Comportamento que muda

- **Um teste morto pelo limite de memória era `BLOCK` e agora é
  `INCONCLUSIVE`.** Se o limite padrão não cabe na sua suíte, aumente-o com
  `--memory` (Action: `memory`) em vez de ler o bloqueio como regressão.
- **Passos depois da Action veem o Python que *você* configurou**, não mais o
  3.12 dela. Um workflow que, sem perceber, dependia desse 3.12 (ou do
  `coverage` que ela instalava) precisa do próprio `setup-python`.
- **Sem nenhum teste nomeado, a Action agora descobre os testes** em vez de
  falhar com exit 3. Se a descoberta não encontra nenhum teste que execute uma
  linha alterada, a decisão é `INCONCLUSIVE` — nunca `MERGE` por falta de
  claims.
- `--claim-json` com mais de uma claim deixou de ser erro de uso.

---

## [2.2.0] — 2026-10-03

Tudo o que saiu de `v2.1.0`: os achados da
[auditoria de produto de 2026-10-03](docs/AUDITORIA_PRODUTO_v2.1.1.md) e as
correções que estavam na seção `[2.1.1]`. Aquela seção foi fundida aqui porque
descrevia uma GitHub Release que nunca foi publicada — não há tag `v2.1.1` — e
este arquivo não pode descrever versões que nunca existiram.

**Minor, e não patch:** o artefato ganhou campos (`test_lines_excluded`,
`test_files_excluded`) e três situações mudam de decisão — ver *Comportamento
que muda*, abaixo, antes de atualizar.

```bash
pip install https://github.com/Sanflow10/adversary-gate/releases/download/v2.2.0/adversary_gate-2.2.0-py3-none-any.whl
```

### Corrigido

- **AG-021 (correção mínima) — um patch que reescreve o teste da claim já não
  chega a `MERGE`.** O teste era lido da árvore do *patch*: trocar `a - b` por
  `a + b` e `== 2` por `== 8` dava baseline verde, patch verde, mutante morto —
  `exit 0`, `suite_strength: 1.0`. Agora, se o arquivo de teste da claim já
  existia no baseline e seus bytes diferem no patch, um `VERIFIED` vira
  `UNVERIFIED` (`INCONCLUSIVE`) com o motivo escrito. `REFUTED` não é rebaixado:
  um teste reescrito que ainda falha continua sendo evidência contra o patch.
  Um teste que o patch *adiciona* não é reescrita e segue como antes.
  **Continua aberto** o oráculo do baseline (rodar a versão *original* do teste
  contra o código do patch) — ver §4.1 da auditoria.
- **AG-021 — os caminhos do `--diff` passam a alimentar a policy de caminhos
  protegidos.** `PathPolicy` já recusava `conftest.py`, `pytest.ini`,
  `pyproject.toml` etc., mas só enxergava `--changed-path`, que a Action nunca
  passa. O `--diff` é a declaração autoritativa do que mudou; é *unido* a
  `--changed-path`, não o substitui.
- **AG-022 — linhas de teste não entram mais na cobertura do diff.** Um teste
  executa por construção; contá-lo deixava 10 linhas de código novo sem executar
  passarem no piso de 0,80 com 40 linhas de teste (`0.8`). Agora o ratio é sobre
  arquivos-fonte, e o artefato grava `test_lines_excluded` e
  `test_files_excluded` ao lado de `changed_lines`. Um diff só com testes não tem
  linha de fonte a cobrir: ratio `1.0` por vacuidade, com `note` dizendo isso.
- **AG-028 — `SECURITY.md` já não manda o repórter a um e-mail inexistente.** A
  rota de fallback era "o endereço em `pyproject.toml`", que não tem endereço.
  Agora a rota primária é um link direto para o advisory e o fallback é uma issue
  pública *sem nenhum detalhe técnico*, que só pede um canal privado. Não há
  e-mail na política de propósito, e um teste impede que ela volte a apontar para
  um que não existe.
- **AG-018 — patch misto modificado+deletado já não chega a `MERGE`.** Um
  arquivo deletado não tem linhas para mutar, não gerava mutante e não baixava
  a força de teste — então passava em silêncio por baixo da nota do arquivo que
  sobrou. Reproduzido por terceiro e confirmado aqui (`exit 0` / `merge` com
  `deleted_files: ["gone.py"]` gravado no próprio artefato), agora
  `deleted_files` não-vazio levanta `suite_strength_unverified` e a decisão é
  `INCONCLUSIVE`. O `suite_strength` segue sendo reportado: o número continua
  verdadeiro sobre o que mediu, só deixa de autorizar o patch inteiro. É a
  terceira instância da mesma classe de AG-001 (deleção fora de `changed_files`)
  e AG-012/AG-013 (fonte fora do motor de mutação).
- **AG-019 — o artefato de diff não depende mais da cor do git de quem chama.**
  `prepare_evidence.sh` gravava `git diff` sem `--no-color`, e o teste o lê com
  `grep`: com `color.ui = always` no ambiente o diff continuava correto, mas o
  teste deixava de enxergar a linha adicionada e falhava (`exit 1`). A CI verde
  não desmentia, porque o runner não impõe a cor — o teste passava por acidente
  de ambiente. Corrigido, com regressão no próprio script que força a cor via
  `GIT_CONFIG_COUNT`.

### Adicionado

- **Site do produto** em `site/index.html`, uma página estática em inglês
  publicada no GitHub Pages pelo workflow `Site` (`.github/workflows/pages.yml`)
  a cada push em `main` que mude `site/`. O Pages precisa ser ligado uma vez em
  *Settings → Pages → Source: GitHub Actions*. O teste de consistência de versão
  passa a cobrir também a versão que o site manda instalar.

### Alterado

- **Release:** o workflow passa a mover a tag flutuante `vN` (só para frente: não
  arrasta `v2` para trás ao refazer uma versão antiga) e, havendo o segredo
  `PYPI_API_TOKEN`, também envia ao PyPI por token. Sem o segredo ele **avisa**
  que o PyPI não foi atualizado em vez de deixá-lo atrás em silêncio (AG-026).
  O `pypi-publish.yml` agora só dispara em `vX.Y.Z`, para a tag `v2` não iniciar
  uma publicação.

### Comportamento que muda — leia antes de atualizar

Três situações que antes davam `MERGE` (ou um ratio diferente) agora dão
`INCONCLUSIVE`, e é intencional:

1. o patch altera o arquivo de teste da claim que já existia no baseline;
2. o `--diff` nomeia um arquivo da policy (`conftest.py`, `pytest.ini`,
   `tox.ini`, `setup.cfg`, `pyproject.toml`, `.github/workflows/*`,
   `**/fixtures/**`, `**/Dockerfile*`, `**/docker-compose*.yml`, `**/*.env` —
   a lista é `DEFAULT_DENYLIST`, sem mudança nesta versão);
3. `diff_coverage.changed_lines` conta só linhas de fonte — o número é menor, e
   o ratio, que antes era inflado por testes, pode cair abaixo do piso.

`INCONCLUSIVE` não é erro: é o gate dizendo que a revisão humana decide.

---

## [2.1.0] — 2026-09-29

Tudo o que saiu de `v2.0.1`. As três seções que estavam empilhadas em `main`
(`[Unreleased]`, `[2.1.0]` e `[2.0.2]`) foram fundidas aqui, porque nenhuma
delas houve de virar artefato — e este arquivo não pode descrever versões que
nunca existiram.

```bash
pip install adversary-gate==2.1.0
```

### Adicionado

- **`base-sha`** — um ref entra, três artefatos saem: o baseline é materializado
  daquele commit, o checkout vira o patch, e o diff são `base-sha...HEAD`.
  Sem ele, cada usuário montava os três artefatos à mão e errava algum.
- **Demo de aceitação** (`demo/demo.py`) exercitando as três decisões.
- **Input `full-suite-path` no `action.yml`** — escopo da checagem colateral de
  regressão. Por padrão ela roda a árvore inteira, que num repositório que
  hospeda outro projeto como fixture é a suíte *do próprio repositório*, não a
  do que foi corrigido. Sem este input era impossível apontá-la: o CLI já tinha
  `--full-suite-path`, a Action não o repassava.
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

Cada um abaixo foi **reproduzido por exit code real** antes da correção.

- **AG-001..AG-011** — a auditoria sênior de `v2.0.1` foi confrontada com o
  código *executado*, não lido. Cada achado foi reproduzido por exit code e
  corrigido, com teste de regressão explícito.
  A cobertura da suíte própria passou a ser **evidência** medida, não afirmação.
  Ver [`docs/AUDITORIA_CONFRONTO_v2.0.2.md`](docs/AUDITORIA_CONFRONTO_v2.0.2.md)
  para a reprodução de cada um.
- **AG-012** — um patch alterando **apenas** código não-Python não podia chegar
  a `MERGE`. Caso puro e caso misto (`calc.py` + `calculator.cpp`) fechados.
- **A Action escrevia outputs vazios em toda saída não-zero.** O step roda sob
  `bash -e`, e `JSON_OUT=$(…cli.py)` é um comando que falha quando o gate
  termina com `1` (BLOCK) ou `2` (INCONCLUSIVE) — então `-e` abortava o step
  **antes** de escrever `decision` e `outcome`. O resultado: um workflow com
  `if: steps.gate.outputs.decision == 'block'` **nunca disparava**, e a única
  saída que algum dia funcionou foi `exit 0`. `cmd || var=$?` é falha tratada,
  então o `-e` não aborta. Presente na `v2.0.1`, reproduzido com
  `bash --noprofile --norc -eo pipefail`.

Os AG-013 a seguir eram *fail-open*: produziam `MERGE` que a evidência não
sustentava.

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
| testes | 122 | 170 + 1 skip |
| cobertura total de `src` | 86,9 % | 89,8 % — medida no CI por `coverage run --source=src`, piso `--fail-under=87` |
| `src/sandbox/runner.py` | 63,4 % | 96,7 % |

A queda de 90,0 % para 89,8 % no CI é o probe de `bubblewrap`: onde ele não
consegue criar um namespace de rede, os 7 testes e2e **pulam** em vez de falhar
e as linhas que cobriam saem da conta. O probe executa `bwrap` cru, sem nenhum
argumento nosso, para que "o ambiente não sabe rodar bwrap" e "nosso prefixo
está quebrado" não virem a mesma coisa: a primeira nomeia o erro do kernel e
vira skip, a segunda continua sendo um teste que falha.

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
6. Verificar se o `pypi-publish.yml` está saudável (AG-008) **antes** de taggar
   à mão — uma tag com publish quebrado deixa uma run vermelha a cada release.
   Foi por isso que `2.0.2` nunca foi taggeada. `2.1.0` ignorou a regra e o
   registro do publisher não foi feito: as três runs de publish falharam e ela
   subiu por token — é assim que fica registrada, e não como uma release
   publicada pelo workflow.

   **A rota do workflow Release não tem este custo.** O
   `release.yml` é `workflow_dispatch`: a tag é criada pelo `GITHUB_TOKEN` do
   próprio workflow, e eventos que esse token produz **não** disparam outros
   workflows, então `pypi-publish.yml` não roda e nenhuma run vermelha nasce.
   Por isso os passos 5 e 6 valem para tag empurrada à mão; no fluxo do Release
   a tag é *consequência* do Run, não pré-requisito dele.
   Com o segredo `PYPI_API_TOKEN` configurado, o mesmo Run também envia ao
   PyPI; sem ele, a run termina com um aviso dizendo que o PyPI não foi
   atualizado. Para mandar ao PyPI uma release que já existe: *Actions →
   Publish to PyPI → Run workflow*, com a tag.

**Não** bumpar a versão com mudanças soltas em `main`: uma versão no
`pyproject.toml` sem tag correspondente é uma promessa de artefato que não
existe. É por isso que esta seção `[Unreleased]` não tem número.
