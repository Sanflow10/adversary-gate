# AdversaryGate on Android / Termux

**Platform:** Android 14 (aarch64), Termux, Bionic libc
**Python:** 3.14 · **pytest:** 9.1.1 · **AdversaryGate:** 3.0.0 (PyPI)
**Date:** 2026-10-11

What happened when AdversaryGate ran on a platform its CI does not test, what does not work there, and the workaround. CI runs CPython 3.10–3.12 on Linux x86_64; the package accepts Python ≥ 3.10 and pytest ≥ 8.0. These are observations from a single device, not a support commitment.

## Summary

- **No scenario produced `MERGE` (exit 0).** The gate:
  - refused to run without the sandbox it was asked for;
  - stopped on usage errors;
  - answered `INCONCLUSIVE` when a test could not run or evidence was missing;
  - answered `BLOCK` on a regression.
- **One platform limit:** the address-space limit (`--memory`, `--full-suite-memory`) aborts every test process on Android. Workaround: `--memory none --full-suite-memory none` ([#4](https://github.com/Sanflow10/adversary-gate/issues/4)).
- **Not shown here:** a `MERGE` on a real fix.

## Installation

```bash
pkg install python git
python -m venv ~/gate-venv && source ~/gate-venv/bin/activate
pip install adversary-gate
```

Nothing was compiled. pip installed adversary-gate 3.0.0 with pytest 9.1.1, pluggy, packaging, iniconfig and pygments. `adversary-gate --version` printed `adversary-gate 3.0.0`.

## Scenarios (raw output)

### 1. Sandbox requested but missing → refused, exit 3

The run was in `/tmp`, outside any repository: this check comes first.

```
$ adversary-gate --sandbox bwrap --baseline HEAD --patch . --test-path . --test-command "true"
error: --sandbox bwrap was requested but 'bwrap' is not on PATH; install bubblewrap (apt install bubblewrap) or drop the flag
$ echo "exit: $?"
exit: 3
```

### 2. Usage errors → exit 3

```
error: the following arguments are required: --baseline, --patch      (exit 3)
error: repo_dir is not a directory: HEAD                               (exit 3)
error: --diff is not a readable file: /tmp/patch.diff
```

`--baseline` and `--patch` take directories, not git refs.

### 3. Invalid claim → `INCONCLUSIVE`, exit 2

| `--test-path` | `reason` |
|---|---|
| an absolute path | `test_path must be a relative path` |
| `.` | `test_path escapes repository` |
| `more_itertools` (a directory) | `test file does not exist: more_itertools` |

`--test-path` names a test **file**, relative to `--patch`. The message for `.` is misleading: the repository root is not outside the repository; what is missing is a file. For a command that is not pytest, use `--claim PATH::(test-command)`.

### 4. A run that never happens → `INCONCLUSIVE`, exit 2

With the default limits, every run aborted (`-6`). The gate counted that as neither a failure nor a pass:

```
"classification": "unrunnable",
"reason": "... baseline did not execute; a run that never happened proves nothing [baseline: execution did not happen (unexpected exit code -6); patch: execution did not happen (unexpected exit code -6)]",
"baseline_exit_codes": [-6, -6, -6],
"patch_exit_codes": [-6, -6, -6]
exit: 2
```

### 5. Comment-only change → strength not measured

```
"mutation": {"measured": false, "reason": "lines changed by this patch contain no mutable operator", "changed_files": ["more_itertools/__init__.py"]}
```

### 6. Regression in a small project → `BLOCK`

The setup:
- `mathlib.py` had `return a + b` changed to `return a - b`;
- `test_add` and `test_multiply` were identical on both sides.

The result:
- `test_add` was classified `regression`;
- the decision was `block`.

The run used `--timeout none`. That also set `source_sensitive_claims: true`, a wrong label; the decision is right ([#5](https://github.com/Sanflow10/adversary-gate/issues/5)).

### 7. Mismatched directories → no approval

Two runs compared directories that did not belong together.

**The `mathlib` project against a tabulate checkout.** The gate:
- saw `mathlib.py` deleted and the tabulate files added;
- saw `pyproject.toml` and `tox.ini` (test-harness files) added by the patch;
- invalidated the claims, because a patch may not configure the runner that judges it (AG-032);
- answered `INCONCLUSIVE`.

**Two tabulate checkouts of different versions.** Besides the intended one-line change, the patch changed `pyproject.toml` and `tox.ini`, rewrote four test files, and deleted `tabulate/cli.py` and `tabulate/__main__.py`. The gate:
- invalidated the claim because of the harness change;
- ran the full suite with the baseline's tests against the patched code, and it could not even be collected (exit 2);
- answered `BLOCK`.

## Known issue: `RLIMIT_AS` ([#4](https://github.com/Sanflow10/adversary-gate/issues/4))

On Android (Bionic libc), a child process started under an address-space limit exits with SIGABRT (signal 6). AdversaryGate applies `RLIMIT_AS` to the claims, the mutants and the full suite by default, so with the defaults every run on Termux exits `-6` and is reported as unrunnable (scenario 4).

```python
import subprocess, resource

# RLIMIT_CPU alone: works
r = subprocess.run(['true'], preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CPU, (10, 10)))
print(r.returncode)  # 0

# RLIMIT_AS alone: SIGABRT
r = subprocess.run(['true'], preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2))
print(r.returncode)  # -6
```

The other limits the gate sets in the same `preexec_fn` (`RLIMIT_CORE`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, `RLIMIT_NPROC`) are applied even with `--memory none`, and those runs worked.

Even the full suite's default cap of 4 GiB aborted a full suite that only runs `true`. The most likely reason is the address space Android reserves when a process starts, not memory use.

### Workaround

```
--memory none --full-suite-memory none
```

Pass both flags. `--full-suite-memory` has its own default (4 GiB) and does not follow `--memory`.

Keep the timeouts:
- the wall-clock timeout is not an rlimit;
- `RLIMIT_CPU` alone did not abort in the isolation above;
- `--timeout none` triggers the wrong label in [#5](https://github.com/Sanflow10/adversary-gate/issues/5).

## UX notes

- **`reason` and `next_steps` disagree.** When the full suite dies with signal 6, the `reason` names `--full-suite-timeout` and `--full-suite-cpu-seconds` and says "Raise the limit", even when the CPU limit is off. `next_steps` does include `--full-suite-memory`. The `reason` should name the signal and the memory flag.
- **`/tmp` is not writable in Termux.** Use `$HOME` or `$PREFIX/tmp`.
- **Coverage evidence comes from coverage.py.** `--diff` / `--coverage-json` (and `--discover-claims`, which needs per-test contexts) read coverage.py's JSON report. The README recipe uses the `coverage` package (`coverage run`, then `coverage json --show-contexts`); pytest-cov is not needed. The CLI does not install `coverage`; only the `[mcp]` extra does.

## Recommendations for the project

1. **Detect Android** and default to no address-space limit, recording the decision in the artefact as `--memory none` does today. `sys.getandroidapilevel` exists there, and `sys.platform == "android"` on CPython 3.13+.
2. **Or use `RLIMIT_DATA` instead of `RLIMIT_AS`.** Since Linux 4.7 it counts private writable memory, not reserved address space.
3. **Fix the `reason`** to name the signal and include `--full-suite-memory` when the full suite dies from a resource limit.
4. **Fix the message for `--test-path .`** to ask for a test file instead of saying it escapes the repository.

## Not shown here

- **A `MERGE` on a real fix.** On a real tabulate fix (`3aa568c`, measured on Linux x86_64), the gate proved the fix (`fix_proven: true`) and answered `INCONCLUSIVE`, for two reasons:
  - the commit's own test does not detect 1 of 4 mutations (line 287: `False` → `True`);
  - diff coverage came out at 0.64. All the changed code lines are covered; the 4 lines counted as uncovered are `README.md` lines, which the measure currently includes (to be fixed).
- **Two runs without raw output.** They were reported on the device, but the raw output was not attached: a fix proven with a single mutant, and a one-line regression in tabulate.
- The MCP server on Termux.
- `--sandbox bwrap` in use, since Android has no bubblewrap.
