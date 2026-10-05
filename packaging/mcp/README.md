# adversary-gate-mcp

<!-- mcp-name: io.github.Sanflow10/adversary-gate -->

The MCP server of [AdversaryGate](https://github.com/Sanflow10/adversary-gate),
packaged so it runs with one command:

```bash
uvx adversary-gate-mcp
```

It is the same server as `pip install "adversary-gate[mcp]"` followed by
`adversary-gate-mcp`; this package ships no code of its own and pins
`adversary-gate[mcp]` at its own version.

AdversaryGate runs your tests on the baseline and on the patch and answers
MERGE, BLOCK or INCONCLUSIVE. The server gives a coding agent two tools,
`verify_repo` and `gate_policy`, so it can check its change before saying it
is done. The agent only names evidence; the operator sets the rest through the
environment:

| Variable | What it sets |
|---|---|
| `ADVERSARY_GATE_BASE_REF` | the baseline (e.g. `origin/main`). Unset, the agent may pick it, and whoever picks the baseline picks the oracle |
| `ADVERSARY_GATE_PYTHON` | the interpreter that runs the tests; one the agent cannot write to |
| `ADVERSARY_GATE_POLICY` | extra gate flags; evidence flags are refused |

Measurement covers Python/pytest today; other languages get INCONCLUSIVE, not
a false MERGE. Documentation, limits and the bug ledger:
<https://github.com/Sanflow10/adversary-gate>.
