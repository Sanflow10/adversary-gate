# Demo — AdversaryGate ao vivo

Um teste de aceitação que, ao mesmo tempo, é a demonstração visual da tese.

```bash
python3 demo/demo.py            # ~60 s. Sai com 0 se os três cenários passarem.
python3 demo/demo.py --no-color # transcript puro, para log de CI
python3 demo/render_gif.py      # regenera demo/demo.gif a partir dos frames capturados
```

## O que ele prova

Três cenários sobre o **mesmo repositório** de fixture (`baseline` / `patch_*`):

| | Patch | Evidência de cobertura | Decisão | Exit |
|---|---|---|---|---|
| **A** | quebra o teste | `--coverage-source untrusted` | `BLOCK` | 1 |
| **B** | limpo | nenhuma | `INCONCLUSIVE` | 2 |
| **C** | limpo | `--diff` + `--coverage-json` | `MERGE` | 0 |

**B e C são idênticos.** Mesmo `calc.py`, mesmo `test_sum.py::test_add`, mesma
execução, mesma força de suíte (`1.0`). A única variável é se a evidência foi
trazida. É por isso que `diff_coverage_source` é gravado ao lado do número no
artefato: um ratio sem proveniência era exatamente o achado AG-002.

## Por que é um teste e não um vídeo

O script:

1. monta os fixtures em um diretório temporário;
2. invoca a CLI real **como subprocesso**, do jeito que a CI faz;
3. lê o *exit code* real e o JSON real da stdout;
4. confere contra o esperado e retorna `1` se qualquer decisão mudar;
5. persiste os artefatos em `demo/out/`.

Se alguém alterar a precedência do `decide()`, mascarar uma deleção ou voltar a
aceitar `--coverage-ratio` sem declaração, **este script falha na CI** — não é
possível "consertar" o demo sem consertar o produto.

## Saídas

| Arquivo | Conteúdo |
|---|---|
| `demo/out/evidence_a.json` | Registo de decisão do cenário A (refutado → BLOCK) |
| `demo/out/evidence_b.json` | Registo do cenário B (`diff_coverage_source: none`) |
| `demo/out/evidence_c.json` | Registo do cenário C, com os dois SHA-256 |
| `demo/out/transcript.txt` | Transcript sem ANSI, para diff/grep |
| `demo/out/frames.json` | Linhas estruturadas que o renderizador consome |
| `demo/demo.gif` | O vídeo de 60 s no README |

## Regenerar o GIF

`render_gif.py` não grava código — ele apenas anima o transcript **real**.
A implementação parseia SGR inline (`\033[1m`, `\033[32m`, …) e renderiza cada
run com a cor e o peso corretos, então nenhum escape vaza como texto.

```bash
python3 demo/demo.py --json demo/out/frames.json
python3 demo/render_gif.py demo/out/frames.json demo/demo.gif
AG_KEEP_FRAMES=1 python3 demo/render_gif.py ...   # mantém os PNGs para inspeção
```

Os PNGs de um frame isolado são a referência de verdade; extrair frames de um
GIF pode dar errado porque os frames são deltas com `disposal`.
