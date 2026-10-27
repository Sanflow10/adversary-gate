# Performance and compatibility

Back to the [README](../README.md).

## ⏱️ Performance

```bash
python3 scripts/benchmark.py --runs 5          # add --sandbox bwrap to include it
```

Measured on this machine (Linux 6.8, x86_64, CPython 3.12.3, no sandbox), on
the same fixture the CI Action smoke test uses — 3 repetitions, one warm-up
discarded:

| | |
| --- | --- |
| median | **20,7 s** |
| p95 | 21,1 s |
| range | 20,7 – 21,1 s |
| decision | `exit 0` / `merge` |

**Read the breakdown before you judge that number.** One decision is **8 pytest
invocations**, not 3:

```
1 baseline   1 patch   4 stability rounds (--rounds-used)   ≥1 mutant   1 collateral full-suite
```

Each one pays Python + pytest startup — **2,29 s here**, because this machine
auto-loads **9 pytest plugins** (seleniumbase, pytest-html, xdist, metadata,
rerunfailures, ordering…). The test itself executes in **0,01 s**. So:

```
8 runs × 2,29 s = 18,3 s  → environment, not gate
                    ≈2,4 s  → AdversaryGate
```

Disabling plugin autoload takes a single start from `2,47 s` to `0,53 s` on
this box — that step is measured; a ~5–6 s total on a clean machine is
**arithmetic, not a measurement**, and is labelled as such.

The runner hands the child a minimal environment by design, so a variable in
your shell does **not** reach it. Since 2.3.0 you pass it on purpose:
`--env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` (Action: `env:`) takes the plugin cost
out of all eight runs, and the artefact records that you did. Until then, on a
machine with a heavy plugin set, the gate paid the full cost eight times over
with no way to opt out — which is why the environment has to be printed next to
the number.

**This is not a CI performance gate.** Timing on a shared runner is noisy
enough that any threshold is either too loose to test anything or tight enough
to go red on somebody else's load. Run it by hand, and keep the machine next to
the figure. A latency number with no environment attached is not a
measurement — it is an advertisement.

---

## 🧭 Compatibility & deprecation

**What counts as breaking.** Anything a caller or a parser depends on:

| Surface | Why it is a contract |
| --- | --- |
| exit codes `0` / `1` / `2` / `3` | CI branches on them |
| `decision` and `outcome` values | dashboards and status checks match on them |
| field names in `evidence.jsonl` and the decision artefact | they are parsed, diffed and archived |
| CLI flags and `action.yml` inputs | pipelines pass them |
| the `0` / `1` / `2`/`3`/`4` test-command convention | every adapter is written against it |

**What is not.** Prose, argument help, comments, module layout inside `src/`,
and anything the docs never promised.

**How a deprecation happens.** At least one minor release with the old surface
still working **and a warning naming the replacement**, then removal in the
following minor release. A breaking change is a major bump.

**The rule that matters here, and it is the opposite of the usual one:**

> A deprecated input must **fail loudly** — exit 3, with the replacement named —
> and must never be quietly ignored.

That is not etiquette, it is the same thesis as everything else in this
repository. A flag that stops being read while the CLI keeps accepting it means
someone passes `--coverage-ratio 0.95`, gets no coverage floor, and reaches
`MERGE` on evidence they thought they had gated. Silent deprecation *is*
fail-open, wearing the costume of good manners. So an unknown or retired flag
is a usage error, always.
