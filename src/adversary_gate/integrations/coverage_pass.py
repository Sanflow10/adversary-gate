# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

"""The Action's coverage pass, run inside the gate's execution boundary.

``base-sha`` mode measures coverage by running the patch's tests before the
gate starts. Until 3.0.7 the Action ran ``coverage-command`` as a bare
``bash -c`` in the job: the job's whole environment, outside ``sandbox:
bwrap``, able to write anywhere the runner can -- the baseline the oracle
reads and the gate's own install among it. It now goes through
:func:`adversary_gate.sandbox.runner.run_test`, as the MCP's pass does: the
same sandbox, the same minimal environment plus ``pass-env``/``env``, and one
writable directory outside the repository, the one the report goes into.

This bounds the run; it does not make the report unforgeable. The patch's
code runs inside coverage.py's process, as it must for coverage to be
measured at all.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from adversary_gate.cli import _run_environment
from adversary_gate.sandbox.runner import run_test

#: As in the MCP's pass: a whole suite's data file outgrows the per-test 16 MiB.
_FILE_BYTES = 4 * 1024 * 1024 * 1024

#: What a test run leaves behind on its own; never what the gate judges.
_SCRATCH_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
#: A new file with one of these names can change what the gate's runs do.
_JUDGED_SUFFIXES = (".py", ".pth", ".cfg", ".ini", ".toml")


def _snapshot(repo: Path) -> Dict[str, str]:
    # The pass runs the patch's tests in the tree the gate then judges as the
    # patch (3.0.18): a test that rewrote a file there made the gate judge
    # bytes no commit holds. Every file and its hash, before and after.
    files: Dict[str, str] = {}
    for top, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SCRATCH_DIRS]
        for name in names:
            path = Path(top, name)
            rel = path.relative_to(repo).as_posix()
            if path.is_symlink():
                files[rel] = "link:" + os.readlink(path)
            elif path.is_file():
                files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _rewritten(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    changed = [rel for rel, digest in before.items() if after.get(rel) != digest]
    added = [rel for rel in after if rel not in before and rel.endswith(_JUDGED_SUFFIXES)]
    return sorted(changed + added)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="adversary_gate.integrations.coverage_pass")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="created empty; the report is written here as coverage.json")
    parser.add_argument("--command", required=True)
    parser.add_argument("--tools-bin", type=Path, default=None)
    parser.add_argument("--readonly", action="append", type=Path, default=[])
    parser.add_argument("--python", default=None)
    parser.add_argument("--sandbox", default=None)
    parser.add_argument("--pass-env", action="append", default=[])
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="a variable the pass itself needs (not the project's)")
    # No wall clock by default: the whole suite runs here, as it did before
    # 3.0.7, and the job's own timeout bounds it. 900 s killed a real suite.
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--require-network-isolation", action="store_true")
    args = parser.parse_args(argv)

    try:
        env, _ = _run_environment(args)
    except ValueError as exc:
        print(f"adversary-gate: {exc}", file=sys.stderr)
        return 3
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = out / "coverage.json"
    for raw in args.set:
        name, _, value = raw.partition("=")
        env[name] = value
    env["COVERAGE_JSON"] = str(report)
    readonly = list(args.readonly)
    if args.tools_bin is not None:
        env["PATH"] = f"{args.tools_bin}:{env.get('PATH', '/usr/bin:/bin')}"
        readonly.append(args.tools_bin.parent)

    before = _snapshot(args.repo)
    try:
        run = run_test(args.repo, "", command=args.command, sandbox=args.sandbox,
                       python=args.python, env=env, timeout_seconds=args.timeout,
                       cpu_seconds=None, mem_bytes=None, file_bytes=_FILE_BYTES,
                       open_files=4096, writable=[out], readonly=readonly,
                       require_network_isolation=args.require_network_isolation)
    except (RuntimeError, ValueError) as exc:
        print(f"adversary-gate: the coverage pass could not run under the policy: {exc}",
              file=sys.stderr)
        return 4
    sys.stdout.write(run.stdout)
    sys.stderr.write(run.stderr)
    rewritten = _rewritten(before, _snapshot(args.repo))
    if rewritten:
        shown = ", ".join(rewritten[:3]) + (f" (+{len(rewritten) - 3} more)" if len(rewritten) > 3 else "")
        print(f"adversary-gate: coverage-command changed the tree the gate judges ({shown}); "
              "the diff no longer describes it", file=sys.stderr)
        return 4
    if run.timed_out:
        print(f"adversary-gate: coverage-command ran past {args.timeout} s", file=sys.stderr)
        return 4
    if run.exit_code != 0:
        print(f"adversary-gate: coverage-command failed (exit {run.exit_code})", file=sys.stderr)
        return 4
    if not report.is_file() or report.stat().st_size == 0:
        print(f"adversary-gate: coverage-command did not produce a non-empty file at {report}",
              file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
