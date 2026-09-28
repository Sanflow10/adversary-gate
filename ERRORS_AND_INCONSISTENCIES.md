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

