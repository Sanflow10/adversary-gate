"""Regression suite for the 2026-10-03 product audit (AG-021, AG-022, AG-028).

Each test pins a scenario that was *executed* against ``3cc103c`` and gave the
wrong answer:

* AG-021 -- an agent introduces a bug and rewrites the claim's own test to agree
  with it. The patch reached ``MERGE``, exit 0, ``suite_strength: 1.0``.
* AG-022 -- added test lines counted as covered diff lines, so 0 of 10 source
  lines executed still cleared the 0.80 floor.
* AG-028 -- ``SECURITY.md`` sent reporters to an address ``pyproject.toml`` does
  not contain.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, EXIT_MERGE, EXIT_USAGE, main
from adversary_gate.core.gate import Gate
from adversary_gate.core.types import CriticClaim, Decision, FailureClass, Outcome
from adversary_gate.verifiers.coverage import covered_diff_ratio

BASE_CALC = "def sub(a, b):\n    return a - b\n"
BUGGY_CALC = "def sub(a, b):\n    return a + b\n"
TEST_OLD = "from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
TEST_REWRITTEN = "from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 8\n"

#: Both hunks of the AG-021 patch, as ``git diff`` would write them.
AG021_DIFF = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
    " def sub(a, b):\n-    return a - b\n+    return a + b\n"
    "--- a/test_calc.py\n+++ b/test_calc.py\n@@ -2,4 +2,4 @@\n"
    " \n \n def test_sub():\n-    assert sub(5, 3) == 2\n+    assert sub(5, 3) == 8\n"
)
#: What ``coverage json`` reports once the rewritten test has run: every line.
AG021_COVERAGE = {
    "files": {
        "calc.py": {"executed_lines": [1, 2]},
        "test_calc.py": {"executed_lines": [1, 4, 5]},
    }
}


def _write(root: Path, side: str, name: str, text: str) -> None:
    (root / side).mkdir(parents=True, exist_ok=True)
    (root / side / name).write_text(text)


def _cli(root: Path, extra, *, test_path: str = "test_calc.py", test_id: str = "test_sub") -> int:
    return main(
        [
            "--baseline", str(root / "baseline"),
            "--patch", str(root / "patch"),
            "--test-path", test_path,
            "--test-id", test_id,
            "--max-rounds", "1",
            "--rounds-used", "1",
            *extra,
        ]
    )


def _last_decision(log: Path) -> dict:
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("kind") == "decision"][-1]


class TestClaimTestIsNotTheBuilders(unittest.TestCase):
    """AG-021: the oracle cannot be rewritten by the patch it is judging."""

    def _verify(self, root: Path):
        gate = Gate([])
        return gate.verify_claim(
            CriticClaim("test_calc.py", "test_sub"), root / "baseline", root / "patch"
        )

    def test_a_rewritten_claim_test_is_not_a_verification(self):
        """The exact AG-021 shape, at the library boundary: passes, still not VERIFIED."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "baseline", "calc.py", BASE_CALC)
            _write(root, "baseline", "test_calc.py", TEST_OLD)
            _write(root, "patch", "calc.py", BUGGY_CALC)
            _write(root, "patch", "test_calc.py", TEST_REWRITTEN)

            verdict = self._verify(root)

            self.assertIs(verdict.outcome, Outcome.UNVERIFIED, verdict.reason)
            self.assertIs(verdict.classification, FailureClass.INVALID)
            self.assertIn("test_calc.py", verdict.reason)
            self.assertIn("rewrote", verdict.reason)
            self.assertIs(Gate([]).decide([verdict], 4, 1.0), Decision.INCONCLUSIVE)

    def test_the_reason_still_says_how_each_side_behaved(self):
        """An UNVERIFIED verdict that hides what ran is how self-deception survives."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "baseline", "calc.py", BASE_CALC)
            _write(root, "baseline", "test_calc.py", TEST_OLD)
            _write(root, "patch", "calc.py", BUGGY_CALC)
            _write(root, "patch", "test_calc.py", TEST_REWRITTEN)

            reason = self._verify(root).reason
            self.assertIn("baseline: passed", reason)
            self.assertIn("patch: passed", reason)

    def test_the_cli_no_longer_merges_the_ag021_patch(self):
        """End to end, with a real diff and a real coverage artefact: exit 2, not 0."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "baseline", "calc.py", BASE_CALC)
            _write(root, "baseline", "test_calc.py", TEST_OLD)
            _write(root, "patch", "calc.py", BUGGY_CALC)
            _write(root, "patch", "test_calc.py", TEST_REWRITTEN)
            (root / "change.diff").write_text(AG021_DIFF)
            (root / "cov.json").write_text(json.dumps(AG021_COVERAGE))
            log = root / "ev.jsonl"

            code = _cli(
                root,
                [
                    "--diff", str(root / "change.diff"),
                    "--coverage-json", str(root / "cov.json"),
                    "--evidence-log", str(log),
                ],
            )

            self.assertEqual(code, EXIT_INCONCLUSIVE)
            self.assertEqual(_last_decision(log)["decision"], "inconclusive")

    def test_it_does_not_depend_on_the_caller_supplying_a_diff(self):
        """No --diff, a declared-untrusted coverage claim: the bytes still differ."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "baseline", "calc.py", BASE_CALC)
            _write(root, "baseline", "test_calc.py", TEST_OLD)
            _write(root, "patch", "calc.py", BUGGY_CALC)
            _write(root, "patch", "test_calc.py", TEST_REWRITTEN)

            code = _cli(
                root, ["--coverage-ratio", "1.0", "--coverage-source", "untrusted"]
            )

            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_a_rewritten_test_that_fails_is_still_a_block(self):
        """Direct evidence outranks the missing kind: we watched it fail."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "baseline", "calc.py", BASE_CALC)
            _write(root, "baseline", "test_calc.py", TEST_OLD)
            _write(root, "patch", "calc.py", BUGGY_CALC)
            # rewritten, but to a value the buggy code still does not produce
            _write(root, "patch", "test_calc.py", TEST_REWRITTEN.replace("== 8", "== 99"))

            verdict = self._verify(root)

            self.assertIs(verdict.outcome, Outcome.REFUTED, verdict.reason)
            self.assertIs(Gate([]).decide([verdict], 4, 1.0), Decision.BLOCK)

    def test_an_untouched_claim_test_still_merges(self):
        """The control: the rule must not turn every patch into INCONCLUSIVE."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "baseline", "calc.py", BASE_CALC)
            _write(root, "baseline", "test_calc.py", TEST_OLD)
            _write(root, "patch", "calc.py", "def sub(a, b):\n    return (a - b)\n")
            _write(root, "patch", "test_calc.py", TEST_OLD)
            (root / "change.diff").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
                " def sub(a, b):\n-    return a - b\n+    return (a - b)\n"
            )
            (root / "cov.json").write_text(
                json.dumps({"files": {"calc.py": {"executed_lines": [1, 2]}}})
            )

            code = _cli(
                root,
                ["--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json")],
            )

            self.assertEqual(code, EXIT_MERGE)

    def test_a_test_the_patch_adds_is_not_a_rewrite(self):
        """The rule is about an oracle that existed. A new test has no baseline."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, "baseline", "calc.py", BASE_CALC)
            _write(root, "patch", "calc.py", BASE_CALC)
            _write(root, "patch", "test_calc.py", TEST_OLD)

            verdict = self._verify(root)

            self.assertIs(verdict.outcome, Outcome.VERIFIED, verdict.reason)


class TestDiffPathsReachThePolicy(unittest.TestCase):
    """AG-021: the policy existed; nothing fed it the paths the patch changed."""

    def _tree(self, root: Path) -> None:
        for side in ("baseline", "patch"):
            _write(root, side, "test_calc.py", TEST_OLD)
            _write(root, side, "calc.py", BASE_CALC)
        _write(root, "patch", "calc.py", "def sub(a, b):\n    return (a - b)\n")

    def _diff(self, *extra_hunks: str) -> str:
        return (
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
            " def sub(a, b):\n-    return a - b\n+    return (a - b)\n"
        ) + "".join(extra_hunks)

    def test_a_diff_that_touches_harness_config_cannot_merge(self):
        """No --changed-path anywhere: the diff alone names conftest.py."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            _write(root, "patch", "conftest.py", "collect_ignore = ['x']\n")
            (root / "change.diff").write_text(
                self._diff("--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+collect_ignore = ['x']\n")
            )
            (root / "cov.json").write_text(
                json.dumps({"files": {"calc.py": {"executed_lines": [1, 2]}}})
            )
            log = root / "ev.jsonl"

            code = _cli(
                root,
                [
                    "--diff", str(root / "change.diff"),
                    "--coverage-json", str(root / "cov.json"),
                    "--evidence-log", str(log),
                ],
            )

            self.assertEqual(code, EXIT_INCONCLUSIVE)
            verdicts = [
                json.loads(line)
                for line in log.read_text().splitlines()
                if '"kind":"decision"' not in line
            ]
            self.assertIn("protected path violation", verdicts[-1]["reason"])
            self.assertIn("conftest.py", verdicts[-1]["reason"])

    def test_a_diff_that_touches_only_source_is_unaffected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            (root / "change.diff").write_text(self._diff())
            (root / "cov.json").write_text(
                json.dumps({"files": {"calc.py": {"executed_lines": [1, 2]}}})
            )

            code = _cli(
                root,
                ["--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json")],
            )

            self.assertEqual(code, EXIT_MERGE)


class TestTestLinesDoNotInflateDiffCoverage(unittest.TestCase):
    """AG-022: the cheapest way to raise a ratio is to write more test."""

    @staticmethod
    def _diff(source_lines: int, test_lines: int) -> str:
        return (
            f"--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,{source_lines} @@\n"
            + "".join(f"+x{i} = 1\n" for i in range(source_lines))
            + f"--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -0,0 +1,{test_lines} @@\n"
            + "".join(f"+y{i} = 1\n" for i in range(test_lines))
        )

    def test_unexecuted_source_is_not_rescued_by_executed_tests(self):
        coverage = {
            "files": {
                "app.py": {"executed_lines": []},
                "tests/test_app.py": {"executed_lines": list(range(1, 41))},
            }
        }

        measured = covered_diff_ratio(self._diff(10, 40), coverage)

        # 0.8 before the fix: (0 + 40) / (10 + 40), which clears the 0.80 floor.
        self.assertEqual(measured.changed_lines, 10)
        self.assertEqual(measured.covered_lines, 0)
        self.assertEqual(measured.ratio, 0.0)

    def test_the_exclusion_is_recorded_not_silent(self):
        coverage = {"files": {"app.py": {"executed_lines": [1, 2]}}}

        measured = covered_diff_ratio(self._diff(2, 40), coverage)

        self.assertEqual(measured.excluded_test_lines, 40)
        self.assertEqual(measured.excluded_test_files, ("tests/test_app.py",))

    def test_conftest_and_suffix_test_modules_are_excluded_too(self):
        diff = (
            "--- a/conftest.py\n+++ b/conftest.py\n@@ -0,0 +1,3 @@\n+a = 1\n+b = 2\n+c = 3\n"
            "--- a/calc_test.py\n+++ b/calc_test.py\n@@ -0,0 +1,2 @@\n+d = 4\n+e = 5\n"
        )

        measured = covered_diff_ratio(diff, {"files": {}})

        self.assertEqual(measured.changed_lines, 0)
        self.assertEqual(measured.excluded_test_lines, 5)

    def test_source_lines_are_still_measured_exactly(self):
        coverage = {"files": {"app.py": {"executed_lines": [1, 2, 3]}}}

        measured = covered_diff_ratio(self._diff(4, 0), coverage)

        self.assertEqual((measured.changed_lines, measured.covered_lines), (4, 3))
        self.assertEqual(measured.ratio, 0.75)

    def test_the_cli_rejects_the_patch_the_old_arithmetic_let_through(self):
        """Ten new source lines, one executed, forty test lines: 0.82 before, 0.10 now."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                _write(root, side, "test_sum.py", "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n")
                _write(root, side, "calc.py", "def add(a, b):\n    return a + b\n")
            unused = "def unused():\n" + "".join(f"    v{i} = {i}\n" for i in range(9))
            _write(root, "patch", "calc.py", "def add(a, b):\n    return a + b\n" + unused)
            extra = "".join(f"VALUE_{i} = {i}\n" for i in range(40))
            _write(root, "patch", "test_extra.py", extra)
            (root / "change.diff").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -2,0 +3,10 @@\n"
                + "".join(f"+{line}\n" for line in unused.splitlines())
                + "--- /dev/null\n+++ b/test_extra.py\n@@ -0,0 +1,40 @@\n"
                + "".join(f"+{line}\n" for line in extra.splitlines())
            )
            (root / "cov.json").write_text(
                json.dumps(
                    {
                        "files": {
                            "calc.py": {"executed_lines": [1, 2, 3]},
                            "test_extra.py": {"executed_lines": list(range(1, 41))},
                        }
                    }
                )
            )
            log = root / "ev.jsonl"

            code = _cli(
                root,
                [
                    "--diff", str(root / "change.diff"),
                    "--coverage-json", str(root / "cov.json"),
                    "--evidence-log", str(log),
                    # Mutation is switched off so the only thing that can
                    # decide this run is the coverage floor.
                    "--mutation-max", "0",
                ],
                test_path="test_sum.py",
                test_id="test_add",
            )

            decision = _last_decision(log)
            self.assertEqual(decision["diff_coverage"]["changed_lines"], 10)
            self.assertEqual(decision["diff_coverage"]["test_lines_excluded"], 40)
            self.assertAlmostEqual(decision["diff_coverage_ratio"], 0.1)
            self.assertEqual(code, EXIT_INCONCLUSIVE)


class TestDiffInputErrorsStayUsageErrors(unittest.TestCase):
    """Reading the diff earlier must not move a usage error to exit 2."""

    def test_a_missing_diff_file_is_exit_3_not_exit_2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                _write(root, side, "calc.py", BASE_CALC)
                _write(root, side, "test_calc.py", TEST_OLD)
            (root / "cov.json").write_text("{}")

            code = _cli(
                root,
                ["--diff", str(root / "nope.diff"), "--coverage-json", str(root / "cov.json")],
            )

            self.assertEqual(code, EXIT_USAGE)


class TestSecurityPolicyRoutesExist(unittest.TestCase):
    """AG-028: a policy that says "silence is off the table" cannot dead-end."""

    def test_no_instruction_points_at_an_address_that_is_not_there(self):
        text = (ROOT / "SECURITY.md").read_text()
        pyproject = (ROOT / "pyproject.toml").read_text()
        if re.search(r"address\s+in\s+`?pyproject\.toml", text):
            self.assertRegex(
                pyproject,
                r"(?m)^\s*(email\s*=|\{[^}]*email\s*=)",
                "SECURITY.md sends reporters to pyproject.toml for an address it does not contain",
            )

    def test_the_primary_route_is_a_link_not_a_menu_path(self):
        text = (ROOT / "SECURITY.md").read_text()
        self.assertIn(
            "https://github.com/Sanflow10/adversary-gate/security/advisories/new", text
        )

    def test_there_is_a_fallback_that_needs_no_private_channel_to_exist(self):
        text = (ROOT / "SECURITY.md").read_text()
        self.assertIn("[SECURITY]", text)
        self.assertRegex(text, r"(?i)no technical detail|without (any )?technical detail")


if __name__ == "__main__":
    unittest.main()
