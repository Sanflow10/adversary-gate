# Auditoria técnica sênior — AdversaryGate v2.0.1

**Repositório auditado:** `https://github.com/Sanflow10/adversary-gate`

**Commit auditado:** `de67c5582004527d6fa6aa5e1fc9e4197d7a5862` (`v2.0.1`)

**Data da auditoria:** 2026-09-28

## Parecer executivo

**Conclusão: NÃO CERTIFICO a proposta como um gate de merge confiável na versão auditada.**

A proposta conceitual é válida e bem direcionada: transformar falhas de harness em `INCONCLUSIVE`, comparar baseline/patch, preservar evidência, usar mutation score e bloquear regressões observadas. A implementação também demonstra evolução real em relação ao desenho anterior e a suíte própria passa.

Porém, há falhas que contradizem invariantes centrais anunciadas no README:

1. **Arquivos-fonte deletados não são reconhecidos como alteração para mutation score.** Isso permite que uma deleção passe sem medição de força e alcance `MERGE` se os demais sinais forem favoráveis.
2. **Diff coverage não é calculada pela CLI.** `--coverage-ratio` é um valor fornecido pelo chamador, com default `1.0`; portanto, a cobertura pode ser declarada como perfeita sem evidência correspondente.
3. **A execução chamada de sandbox não é isolamento de segurança.** Há apenas limites POSIX de recursos; isolamento de rede é opt-in e depende de uma variável de ambiente declarativa. O processo continua podendo executar código arbitrário do repositório com as permissões do runner.
4. **A checagem de symlink para `changed_paths` não é aplicada pelo gate e, mesmo quando `repo_dir` é informado diretamente à policy, o próprio arquivo symlink não é checado — apenas pais.**
5. **Não há workflow de CI para rodar os testes do projeto.** Os workflows públicos disponíveis são publicação PyPI e Dependabot; as duas execuções de publicação observadas falharam.
6. **A GitHub Action não instala as dependências do projeto antes de executar `cli.py`.** Em um runner limpo, `pytest` não é garantido, apesar de ser dependência do pacote PyPI.

Assim, o status adequado é: **protótipo avançado / release candidata, com testes internos verdes, mas não pronto para certificação de segurança ou autorização automática de merge**.

## O que foi verificado

### Resultados positivos

- Clone limpo da branch `main`, sem alterações locais.
- Tag e versão do pacote: `v2.0.1` / `2.0.1`.
- Compilação Python: **OK** (`compileall`).
- Suíte própria: **74 passed, 3 subtests passed em 10,63 s**.
- Cobertura executada localmente: **89% total**.
- Wheel e sdist construídos: **OK**.
- Instalação limpa do wheel em virtualenv: **OK**.
- Comando `adversary-gate --help`: **OK**.
- O modelo de decisão de três estados e a precedência `BLOCK > INCONCLUSIVE > MERGE` estão implementados e cobertos por testes.
- Há testes úteis para exit codes, granularidade `path::test_id`, flaky runs, timeout, symlink no `test_path`, mutation score e comparação baseline/patch da full suite.

### Resultados externos observados

Metadados públicos do GitHub indicam apenas estes workflows ativos no repositório:

- `Publish to PyPI`
- `Dependency Graph`

Não há workflow de testes/CI da suíte. As duas execuções recentes de `Publish to PyPI` consultadas estavam com conclusão **failure**. O próprio arquivo `ERRORS_AND_INCONSISTENCIES.md` atribui a falha a `invalid-publisher` no Trusted Publishing OIDC do PyPI.

## Achados detalhados

### AG-001 — Deleção de código não entra em `changed_source_files` — **Alta / fail-open relativo à proposta**

**Local:** `src/verifiers/strength.py:143-164`

A função cria um snapshot do baseline e do patch, mas retorna apenas caminhos presentes no snapshot do patch:

```python
return sorted(rel for rel, blob in patch.items() if base.get(rel) != blob)
```

Logo, um `.py` existente no baseline e ausente no patch não é listado, apesar de a documentação dizer que deleções contam como mudança.

**Reprodução executada:**

```text
changed_source_files(delete)= []
mutation= SuiteStrength(..., is_measured=False)
decision= Decision.MERGE
```

**Impacto:** uma deleção de fonte não testada pode deixar a força da suíte sem medição. Como `suite_strength_unverified` não é acionado quando `changed_files` está vazio, o gate pode chegar a `MERGE`.

**Correção recomendada:** usar a união `set(base) | set(patch)`; tratar adições e deleções explicitamente. Para arquivos sem conteúdo mutável no patch, a decisão deve ser `INCONCLUSIVE` ou a alteração deve ser coberta por uma verificação estrutural/full-suite obrigatória.

### AG-002 — Diff coverage é input confiado, não evidência produzida — **Alta**

**Locais:** `src/cli.py:108-110`, `src/cli.py:220-226`, `src/verifiers/coverage.py`

Existe um verificador `covered_diff_ratio(diff_text, coverage_json)`, mas a CLI não o chama. Em vez disso, aceita `--coverage-ratio`, cujo default é `1.0`.

**Reprodução executada:** com um verdict verificado:

```text
ratio=1.0 -> Decision.MERGE
ratio=0.0 -> Decision.INCONCLUSIVE
```

Isso prova que a decisão depende diretamente do número fornecido ao processo, sem vinculação a diff e artefato de coverage. A Action também não fornece diff/coverage JSON; apenas repassa `coverage-floor`.

**Impacto:** o gate pode aparentar atender `diff_coverage >= 80%` sem ter executado ou validado cobertura. Isso contradiz a tese de “executed evidence”.

**Correção recomendada:** receber diff e coverage JSON como entradas obrigatórias quando a cobertura estiver habilitada; calcular internamente; rejeitar valor manual fora de um modo explicitamente declarado como `untrusted-input`; registrar no artefato o hash dos inputs e o conjunto de linhas calculadas.

### AG-003 — “Sandbox” não fornece isolamento de segurança — **Alta / risco de execução arbitrária**

**Local:** `src/sandbox/runner.py:66-168`

O runner usa `subprocess.Popen` com `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE` e `RLIMIT_NPROC`. Isso é controle de recursos, não uma sandbox de segurança completa.

O isolamento de rede só é exigido quando `require_network_isolation=True`, e a condição é satisfeita por:

```python
os.environ.get("ADVERSARY_NETWORK_ISOLATED") == "1"
```

Essa variável não prova que a rede está isolada. Não há namespace, seccomp, container, chroot, usuário não privilegiado, montagem read-only, bloqueio de syscalls, controle de processos descendentes fora do grupo, ou política de filesystem. O código de teste é arbitrário e pode tentar exfiltrar dados, alterar arquivos acessíveis ou abusar de serviços locais.

**Impacto:** adequado apenas para executar código já confiável em runner dedicado/efêmero. Inadequado para tratar patches de agentes como não confiáveis sem uma camada externa real.

**Correção recomendada:** declarar claramente “resource-limited runner”, ou integrar um sandbox externo real (container rootless/VM, network namespace, filesystem efêmero e mínimo privilégio). O modo seguro deve ser obrigatório para entradas não confiáveis; a variável de ambiente não deve ser a única prova.

### AG-004 — Proteção de symlink incompleta para `changed_paths` — **Média/Alta**

**Locais:** `src/core/gate.py:351-352`, `src/core/path_policy.py:64-83`

`Gate.verify_claim()` chama `self.validate_changed_paths(changed_paths)` sem passar `repo_dir`, portanto a checagem de symlink da policy não é usada neste caminho.

Além disso, quando `repo_dir` é fornecido diretamente, a policy verifica `root` e `unresolved.parents`, mas não o próprio `unresolved`. Um arquivo alterado que seja um symlink pode passar essa verificação.

**Reprodução executada:**

```text
without repo_dir: []
with repo_dir: []
```

**Correção recomendada:** passar a raiz real ao validar os caminhos e verificar `(root, unresolved, *unresolved.parents)`, além de rejeitar symlinks em qualquer componente relevante.

### AG-005 — Action executa do source sem instalar `pytest` — **Média**

**Local:** `action.yml:61-103`

A Action faz `setup-python` e chama diretamente `src/cli.py`, mas não executa `pip install -e .`, não instala `pytest` e não instala dependências do projeto.

O pacote declara `pytest>=8.0.0`, mas isso só ajuda quem instala o pacote PyPI; não quem usa a Action diretamente do repositório.

**Impacto:** em runner limpo, `python -m pytest` pode falhar como erro de harness e produzir `INCONCLUSIVE`; em alguns ambientes o comportamento pode variar conforme pacotes pré-instalados. Isso torna o produto não reproduzível.

**Correção recomendada:** instalar dependências com `python -m pip install .` ou declarar e instalar um requirements dedicado antes da execução. Adicionar teste de smoke da Action em runner limpo.

### AG-006 — Ausência de CI de testes — **Média**

Não há workflow que execute `pytest`, build, instalação do wheel, lint/type-check ou smoke da Action em cada push/PR. A suíte verde foi observada apenas manualmente neste ambiente.

**Impacto:** regressões podem ser publicadas; a alegação “testada OK” não tem verificação contínua no repositório.

**Correção recomendada:** adicionar matriz Python 3.10–3.12, `pytest`, build/install do wheel, teste da CLI instalada, smoke da Action e um job de segurança estática.

### AG-007 — Cobertura própria desigual — **Média**

Cobertura local observada:

| Módulo | Cobertura |
|---|---:|
| `src/cli.py` | 80% |
| `src/core/gate.py` | 90% |
| `src/sandbox/runner.py` | 63% |
| `src/verifiers/coverage.py` | 33% |
| `src/verifiers/strength.py` | 87% |
| **Total** | **89%** |

A baixa cobertura em `coverage.py` é particularmente relevante porque o cálculo existe, mas não está integrado à CLI. A baixa cobertura do runner também deixa sem prova vários caminhos de timeout, harness error e limites.

### AG-008 — Metadados/documentação de release inconsistentes — **Baixa/Média**

- `README.md` começa com `AdversaryGate (v2.0.0)`, enquanto o pacote auditado é `2.0.1`.
- O README exemplifica `uses: adversary-gate/action@v2`, que não corresponde ao namespace/repositório público auditado (`Sanflow10/adversary-gate@...`).
- O README referencia `LICENSE`, mas o arquivo não está presente no clone auditado; o metadata do pacote também deixa o campo de licença vazio.
- O workflow de publicação está ativo, mas as execuções recentes falharam no Trusted Publishing.

## Veredito por dimensão

| Dimensão | Veredito |
|---|---|
| Tese conceitual fail-closed | **Válida** |
| Implementação de decisões básicas | **Parcialmente válida** |
| Evidência executada e auditável | **Parcial** |
| Mutation score | **Parcial, com bug em deleções** |
| Diff coverage | **Não certificada/integrada** |
| Isolamento de execução | **Não é sandbox de segurança** |
| Suíte própria | **Verde localmente** |
| CI reprodutível | **Não demonstrado** |
| Publicação PyPI | **Disponível, mas workflow recente falha** |
| Pronto para merge automático | **Não** |

## Ordem mínima de correção antes de certificar

1. Corrigir detecção de adições/deleções e criar testes de regressão para ambas.
2. Remover a cobertura manual como fonte de confiança; calcular diff coverage a partir de artefatos reais.
3. Passar `repo_dir` à policy e corrigir detecção do próprio arquivo symlink.
4. Documentar e reforçar o boundary de segurança; tornar sandbox externo obrigatório para patches não confiáveis.
5. Corrigir a Action para instalar dependências e testar em ambiente limpo.
6. Adicionar CI de testes, build, instalação e smoke test da Action.
7. Corrigir README, referência de Action, licença e Trusted Publishing.
8. Só depois executar uma avaliação independente com casos adversariais: deleção, arquivo adicionado, symlink, teste que altera filesystem, acesso de rede, subprocesso persistente, timeout, código de saída 2/3/4/5 e baseline já quebrado.

## Declaração final

**A proposta é tecnicamente promissora e a versão v2.0.1 está significativamente melhor do que um gate booleano fail-open. Entretanto, “74 testes passaram” certifica apenas a suíte fornecida pelo próprio projeto; não certifica a segurança, a completude da evidência ou a correção do gate em produção. Com os achados AG-001 e AG-002, eu não aprovaria este repositório para autorizar merge automático sem as correções acima.**
