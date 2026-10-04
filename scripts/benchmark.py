#!/usr/bin/env python3
"""Measure how long one AdversaryGate decision actually takes.

    python3 scripts/benchmark.py [--runs N] [--sandbox none|bwrap]

What it measures is **decision latency**: one complete `adversary-gate`
invocation on a fixture that mirrors the CI Action smoke test -- baseline
pytest run, patch pytest run, collateral full-suite run, diff parsed,
coverage floored, decision written. Three interpreter startups dominate it,
which is the honest shape of the thing: the gate is not the bottleneck,
pytest is.

It deliberately does *not* run in CI as a performance gate. Timing on a shared
runner is noisy enough that a threshold would either be so loose it tests
nothing or so tight it goes red on somebody else's load. Run it by hand, read
the numbers, and keep them next to the machine they came from.

Printed with every run: CPU, Python version and the sandbox mode, because a
benchmark without its environment is a rumour.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "src" / "adversary_gate" / "cli.py"

CALC_BEFORE = "def add(a, b):\n    return a + b\n"
CALC_AFTER = "def add(a, b):\n    return (a + b)\n"
TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
DIFF = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n-    return a + b\n+    return (a + b)\n"
)
COVERAGE = {"files": {"calc.py": {"executed_lines": [1, 2]}}}


def build_fixture(root: Path) -> Path:
    """The smallest tree the gate will accept as a real, measured patch."""
    baseline = root / "baseline"
    patch = root / "patch"
    for side in (baseline, patch):
        side.mkdir(parents=True, exist_ok=True)
        (side / "calc.py").write_text(CALC_BEFORE if side == baseline else CALC_AFTER)
        (side / "test_sum.py").write_text(TEST)
    (root / "change.diff").write_text(DIFF)
    (root / "cov.json").write_text(json.dumps(COVERAGE))
    return root


def once(root: Path, sandbox: str) -> tuple[float, int]:
    argv = [
        sys.executable,
        str(CLI),
        "--baseline", str(root / "baseline"),
        "--patch", str(root / "patch"),
        "--test-path", "test_sum.py",
        "--test-id", "test_add",
        "--diff", str(root / "change.diff"),
        "--coverage-json", str(root / "cov.json"),
        "--evidence-log", str(root / "evidence.jsonl"),
    ]
    if sandbox != "none":
        argv += ["--sandbox", sandbox]
    started = time.perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    return time.perf_counter() - started, proc.returncode


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[index]


def pytest_profile() -> tuple[int, float]:
    """How many plugins each run pays for, and what one bare start costs.

    The gate invokes pytest eight times (baseline, patch, four stability
    rounds, at least one mutant, and the collateral full-suite run), and the
    runner hands the child a minimal environment -- which means plugins that
    autoload on this machine autoload on *every* one of those eight, with no
    way to opt out short of uninstalling them. On a machine carrying
    seleniumbase that alone is over a second per run, i.e. most of the total.
    Worth printing: without it a slow box looks like a slow gate.
    """
    probe = [sys.executable, "-m", "pytest", "--version", "--version"]
    started = time.perf_counter()
    proc = subprocess.run(probe, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    listed = (proc.stdout or "") + (proc.stderr or "")
    plugins = sum(1 for line in listed.splitlines() if " at " in line)
    return plugins, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="repetitions (default 5)")
    parser.add_argument(
        "--sandbox", default="none", choices=["none", "bwrap"],
        help="run every repetition inside bubblewrap",
    )
    args = parser.parse_args()

    if not CLI.is_file():
        print(f"error: {CLI} not found; run from a checkout", file=sys.stderr)
        return 3

    root = build_fixture(Path(tempfile.mkdtemp(prefix="ag-bench-")))
    try:
        # One warm-up: the first run pays for .pyc compilation and page cache,
        # and reporting that as the steady-state number would be dishonest.
        _, code = once(root, args.sandbox)
        if code != 0:
            print(f"error: warm-up run exited {code}; fix the fixture first",
                  file=sys.stderr)
            return code

        samples: list[float] = []
        codes: list[int] = []
        for _ in range(args.runs):
            elapsed, code = once(root, args.sandbox)
            samples.append(elapsed)
            codes.append(code)

        if len(set(codes)) != 1:
            print(f"error: inconsistent decisions {sorted(set(codes))}", file=sys.stderr)
            return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)

    plugins, pytest_startup = pytest_profile()

    print("AdversaryGate — decision latency")
    print("=" * 56)
    print(f"machine     : {platform.platform()}")
    print(f"processor   : {platform.processor() or 'n/d'}")
    print(f"python      : {platform.python_version()}  ({sys.executable})")
    print(f"sandbox     : {args.sandbox}")
    print(f"repetitions : {args.runs} (1 warm-up discarded)")
    print("-" * 56)
    print(f"min         : {min(samples):.2f} s")
    print(f"median      : {statistics.median(samples):.2f} s")
    print(f"p95         : {percentile(samples, 95):.2f} s")
    print(f"max         : {max(samples):.2f} s")
    print(f"mean        : {statistics.mean(samples):.2f} s")
    print("-" * 56)
    print(f"decision    : exit {codes[0]}")
    print()
    print("What is inside that number:")
    print("  8 pytest invocations, not 3:")
    print("    1 baseline, 1 patch, 4 stability rounds (--rounds-used)")
    print("    >= 1 mutant (-m per killed/surviving mutant)")
    print("    1 collateral full-suite run")
    print(f"  each one starts Python + pytest: {pytest_startup:.2f} s here")
    print("  diff parse, coverage floor, evidence artefact")
    print()
    print("What is NOT inside it:")
    print("  generating the diff or the coverage JSON (your pipeline does that)")
    print("  checkout / cache restore")
    print("  adversarial repetition of this run in *your* CI")
    print()
    if plugins:
        print(f"Environment caveat — {plugins} pytest plugins autoload on this")
        print(f"machine, costing {pytest_startup:.2f} s of every one of the 8 runs.")
        print("The runner gives the child a minimal environment, so those plugins")
        print("load anyway and PYTEST_DISABLE_PLUGIN_AUTOLOAD cannot reach it.")
        print("A single test here executes in 0.01 s; the rest is plugin import.")
        print(f"Rough floor on this box: {8 * pytest_startup:.1f} s of the")
        print("median above is environment, not gate. Compare across machines")
        print("only with the plugin set attached.")
        print()
    print("Publish this number with the machine attached. A latency figure with")
    print("no environment is not a measurement, it is an advertisement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
