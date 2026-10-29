"""The sandbox is on by default where it can run (3.0.18)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import adversary_gate.sandbox.runner as runner


class TestTheSandboxIsOnByDefault(unittest.TestCase):
    """``auto`` is the default (3.0.18): bwrap wherever it can run the
    interpreter, no sandbox elsewhere -- and the answer says which and why.
    A gate built to run an agent's code ran it unsandboxed unless asked."""

    def setUp(self):
        runner._AUTO.clear()
        self.addCleanup(runner._AUTO.clear)

    def test_no_bwrap_binary_falls_back_and_says_so(self):
        with mock.patch.object(runner, "bwrap_available", return_value=False):
            self.assertEqual(runner.resolve_sandbox("auto", sys.executable),
                             ("none", "bwrap is not on PATH"))

    def test_a_bwrap_that_cannot_run_the_interpreter_falls_back(self):
        failed = subprocess.CompletedProcess([], 1, "", "bwrap: No permissions to creating new namespace")
        with mock.patch.object(runner, "bwrap_available", return_value=True), \
                mock.patch.object(runner, "bwrap_prefix", return_value=["bwrap"]), \
                mock.patch.object(runner.subprocess, "run", return_value=failed):
            sandbox, why = runner.resolve_sandbox("auto", sys.executable)
        self.assertEqual(sandbox, "none")
        self.assertIn("No permissions", why)

    def test_a_working_bwrap_is_used_and_probed_once(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(runner, "bwrap_available", return_value=True), \
                mock.patch.object(runner, "bwrap_prefix", return_value=["bwrap"]), \
                mock.patch.object(runner.subprocess, "run", return_value=ok) as probe:
            first = runner.resolve_sandbox("auto", sys.executable)
            again = runner.resolve_sandbox("auto", sys.executable)
        self.assertEqual(first, ("bwrap", ""))
        self.assertEqual(again, first)
        self.assertEqual(probe.call_count, 1)

    def test_a_bwrap_that_does_not_start_falls_back(self):
        with mock.patch.object(runner, "bwrap_available", return_value=True), \
                mock.patch.object(runner, "bwrap_prefix", return_value=["bwrap"]), \
                mock.patch.object(runner.subprocess, "run", side_effect=OSError("exec format error")):
            sandbox, why = runner.resolve_sandbox("auto", sys.executable)
        self.assertEqual(sandbox, "none")
        self.assertIn("did not start", why)

    def test_an_unknown_sandbox_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, \
                self.assertRaisesRegex(ValueError, "expected 'none', 'bwrap' or 'auto'"):
            runner.run_test(Path(directory), "test_x.py", sandbox="docker")

    def test_an_explicit_choice_is_kept(self):
        self.assertEqual(runner.resolve_sandbox("none"), ("none", ""))
        self.assertEqual(runner.resolve_sandbox("bwrap"), ("bwrap", ""))
        self.assertEqual(runner.resolve_sandbox(None), ("none", ""))

    def test_the_cli_and_the_mcp_default_to_auto(self):
        from adversary_gate.cli import build_parser
        from adversary_gate.integrations.mcp_server import _boundary
        args = build_parser().parse_args(["--baseline", "b", "--patch", "p"])
        self.assertEqual(args.sandbox, "auto")
        self.assertEqual(_boundary([])["sandbox"], "auto")
        self.assertEqual(_boundary(["--sandbox", "none"])["sandbox"], "none")


if __name__ == "__main__":
    unittest.main()
