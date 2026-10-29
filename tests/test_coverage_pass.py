# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

"""The Action's coverage pass runs inside the gate's execution boundary.

Until 3.0.7 the Action's ``base-sha`` mode ran ``coverage-command`` -- the
patch's own tests -- as a bare ``bash -c`` in the job: the job's whole
environment, no ``sandbox: bwrap``, no limit, every path the runner can write.
The pass now goes through the gate's runner, as the MCP's did in item 1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

def _bwrap_runs_here() -> bool:
    """Raw bwrap with a network namespace, none of the gate's arguments: GitHub's
    runner refuses it (``loopback: Failed RTM_NEWADDR``), which says nothing
    about the gate. Same probe as ``test_runner._bwrap_usable_here``."""
    if not shutil.which("bwrap"):
        return False
    try:
        return subprocess.run(["bwrap", "--unshare-net", "--ro-bind", "/", "/", "--", "true"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


WRITE_REPORT = 'printf \'{"files": {}}\' > "$COVERAGE_JSON"; '


def _pass(repo: Path, out: Path, command: str, *extra: str,
          env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "adversary_gate.integrations.coverage_pass",
         "--repo", str(repo), "--out-dir", str(out), "--command", command, *extra],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(SRC), **(env or {})},
    )


class TestTheCoveragePass(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.out = self.base / "out" / "coverage-pass"

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_job_environment_does_not_reach_the_patch(self):
        command = WRITE_REPORT + 'env > "$(dirname "$COVERAGE_JSON")/env.txt"'
        result = _pass(self.repo, self.out, command, "--pass-env", "AG_KEPT",
                       "--env", "AG_SET=1",
                       env={"AG_JOB_SECRET": "s3cret", "AG_KEPT": "k"})
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = (self.out / "env.txt").read_text()
        self.assertNotIn("s3cret", seen)
        self.assertIn("AG_KEPT=k", seen)
        self.assertIn("AG_SET=1", seen)
        self.assertTrue((self.out / "coverage.json").is_file())

    def test_a_command_that_writes_no_report_fails(self):
        result = _pass(self.repo, self.out, "true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-empty", result.stderr)

    def test_the_tools_directory_is_first_on_path(self):
        tools = self.base / "tools" / "bin"
        tools.mkdir(parents=True)
        (tools / "coverage").write_text('#!/bin/sh\nprintf \'{"files": {}}\' > "$COVERAGE_JSON"\n')
        (tools / "coverage").chmod(0o755)
        result = _pass(self.repo, self.out, "coverage", "--tools-bin", str(tools))
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(_bwrap_runs_here(), "bwrap cannot run in this environment")
    def test_under_bwrap_only_the_report_directory_is_writable_outside_the_repo(self):
        tools = self.base / "out" / "tools" / "bin"
        tools.mkdir(parents=True)
        (tools / "coverage").write_text('#!/bin/sh\nprintf \'{"files": {}}\' > "$COVERAGE_JSON"\n')
        (tools / "coverage").chmod(0o755)
        sibling = self.base / "out" / "baseline"
        sibling.mkdir(parents=True)
        command = f"coverage; echo planted > {sibling}/calc.py; echo x > {tools}/coverage; true"
        result = _pass(self.repo, self.out, command, "--tools-bin", str(tools),
                       "--sandbox", "bwrap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((sibling / "calc.py").exists())
        self.assertIn("COVERAGE_JSON", (tools / "coverage").read_text())


class TestTheActionUsesThePass(unittest.TestCase):
    """action.yml, read as data."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "action.yml").read_text()
        start = text.index("- name: Prepare evidence from base-sha")
        cls.step = text[start:text.index("- name:", start + 10)]

    def test_the_script_no_longer_runs_the_command(self):
        self.assertIn("--skip-coverage", self.step)
        self.assertNotIn("--coverage-command", self.step)

    def test_the_pass_has_no_wall_clock_of_its_own(self):
        """A 900 s default killed the whole suite of a real project in CI."""
        self.assertNotIn("--timeout", self.step)

    def test_the_pass_gets_the_gate_boundary(self):
        self.assertIn("adversary_gate.integrations.coverage_pass", self.step)
        for name in ("inputs.sandbox", "inputs.pass-env", "inputs.env",
                     "inputs.require-network-isolation"):
            self.assertIn(name, self.step)


class TestTheActionRunsItsOwnGate(unittest.TestCase):
    """The gate step runs in the PR's checkout. ``-m adversary_gate`` without
    ``-I`` put that checkout first on sys.path, so an ``adversary_gate/`` or a
    ``json.py`` at the root of the PR ran in place of the gate (3.0.11)."""

    def test_every_gate_python_call_is_isolated(self):
        import re
        text = (ROOT / "action.yml").read_text()
        calls = re.findall(r'"\$AG_GATE_PYTHON"((?: -[A-Za-z]+)+)', text)
        self.assertTrue(calls)
        for flags in calls:
            with self.subTest(flags=flags):
                self.assertIn(" -I", flags)
        self.assertNotIn('PYTHONPATH="${{ github.action_path }}/src"', text)

    def test_no_python_runs_from_the_checkout_before_the_sandbox(self):
        """The setup steps ran ``python -m venv``, ``-m pip`` and ``-c 'import
        pytest'`` in the checkout, without ``-I``: a ``venv/`` or ``pip/`` at
        the root of the PR ran in place of the module, before any sandbox and
        with the gate's own venv in reach (3.0.18). Every ``-m``/``-c`` call
        is isolated, or its step has left the checkout first."""
        import re
        text = (ROOT / "action.yml").read_text()
        steps = text.split("    - name: ")[1:]
        seen = 0
        for step in steps:
            left = step.find('cd "${RUNNER_TEMP}/adversary-gate/cwd"')
            for call in re.finditer(r'"\$(?:[A-Z_]*PYTHON|PROJECT|found|VENV/bin/python)"((?: -[A-Za-z]+)+)', step):
                flags = call.group(1)
                if not re.search(r" -[mc]\b", flags):
                    continue
                seen += 1
                with self.subTest(step=step.splitlines()[0], call=call.group(0)):
                    self.assertTrue(" -I" in flags or 0 <= left < call.start())
        self.assertGreaterEqual(seen, 6)


class TestASmallChangeCanBeRoutedToAPerson(unittest.TestCase):
    """small-change: review passes the step for one INCONCLUSIVE only -- a change
    too small to measure, where every other measurement held -- and still
    reports it; the default fails as before (3.0.18)."""

    SMALL = json.dumps({"decision": "inconclusive", "human_review": {"kind": "small_change"}})
    OTHER = json.dumps({"decision": "inconclusive", "human_review": None})

    def _run(self, answer, exit_code, mode):
        import contextlib
        import io
        from unittest import mock

        from adversary_gate.integrations import action_review
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            printed = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(answer)), contextlib.redirect_stdout(printed):
                code = action_review.main(["--exit-code", str(exit_code), "--mode", mode,
                                           "--output", str(output)])
            written = output.read_text() if output.exists() else ""
        return code, written, printed.getvalue()

    def test_review_passes_only_a_small_change(self):
        self.assertEqual(self._run(self.SMALL, 2, "review")[0], 0)
        code, written, printed = self._run(self.SMALL, 2, "review")
        self.assertEqual(written, "review-required=true\n")
        self.assertIn("::warning", printed)
        self.assertEqual(self._run(self.OTHER, 2, "review")[0], 2)  # any other INCONCLUSIVE fails
        self.assertEqual(self._run(self.SMALL, 1, "review")[0], 1)  # never over a BLOCK
        self.assertEqual(self._run(self.SMALL, 2, "fail")[0], 2)    # the default fails
        self.assertEqual(self._run("not json", 2, "review")[:2], (2, "review-required=false\n"))

    def test_an_unknown_mode_is_a_usage_error(self):
        code, written, printed = self._run(self.SMALL, 2, "maybe")
        self.assertEqual(code, 3)
        self.assertIn("::error", printed)

    def test_it_runs_as_a_module(self):
        import io
        import runpy
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            argv = ["action_review", "--exit-code", "0", "--mode", "fail",
                    "--output", str(Path(directory) / "out")]
            with mock.patch("sys.argv", argv), mock.patch("sys.stdin", io.StringIO("{}")), \
                    self.assertRaises(SystemExit) as done:
                runpy.run_module("adversary_gate.integrations.action_review", run_name="__main__")
        self.assertEqual(done.exception.code, 0)

    def test_the_action_uses_it(self):
        text = (ROOT / "action.yml").read_text()  # as text: PyYAML is not a dependency
        small = text[text.index("  small-change:\n"):text.index("  bug-kind:\n")]
        self.assertIn("default: 'fail'", small)
        self.assertIn("  review-required:\n", text[text.index("outputs:"):text.index("runs:")])
        self.assertIn('-I -m adversary_gate.integrations.action_review --exit-code "$EXIT_CODE"', text)
        self.assertTrue(text.rstrip().endswith("exit $FINAL") or "exit $FINAL" in text)


if __name__ == "__main__":
    unittest.main()
