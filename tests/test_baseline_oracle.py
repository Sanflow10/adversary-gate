"""The baseline oracle: a rewritten test is judged by the baseline's copy of it.

AG-021 closed the door on a patch grading itself: a claim whose test file the
patch rewrote stopped being ``VERIFIED``. That left two things undone, both
recorded as open in ``ERRORS_AND_INCONSISTENCIES.md``:

* the AG-021 attack (bug + rewritten test) only reached ``INCONCLUSIVE``,
  although the baseline's own test is right there and condemns the bug;
* every honest patch that touched its test file -- a refactor, a new
  assertion, a new test in an existing file, the commonest shape an agent
  produces -- was ``INCONCLUSIVE`` too, so the rule taxed the honest case as
  much as the attack.

The oracle copies the patch tree, puts the baseline's test file back, and runs
the claim there. A test the patch *added* to an existing file has no baseline
copy; it is judged like any added test, but only after every test the
baseline shipped in that file has passed on the patch's code.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_MERGE, main
from adversary_gate.cli import EXIT_BLOCK
from adversary_gate.core.gate import Gate, _defines_test
from adversary_gate.verifiers.testpaths import declared_test_support, is_test_path
from adversary_gate.core.types import CriticClaim, Decision, FailureClass, Outcome

CALC = "def sub(a, b):\n    return a - b\n\n\ndef add(a, b):\n    return a + b\n"
TESTS = (
    "from calc import add, sub\n\n\n"
    "def test_sub():\n    assert sub(5, 3) == 2\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n"
)


def _tree(root: Path, *, patch_calc: str = CALC, patch_tests: str = TESTS) -> None:
    for side, calc, tests in (("baseline", CALC, TESTS), ("patch", patch_calc, patch_tests)):
        (root / side).mkdir(parents=True, exist_ok=True)
        (root / side / "calc.py").write_text(calc)
        (root / side / "test_calc.py").write_text(tests)


def _verify(root: Path, test_id: str):
    return Gate([]).verify_claim(CriticClaim("test_calc.py", test_id), root / "baseline", root / "patch")


class TestRewrittenTestIsJudgedByTheBaseline(unittest.TestCase):
    def test_an_honest_refactor_of_the_test_file_verifies(self):
        """A rewrite that keeps the contract is no longer taxed with INCONCLUSIVE."""
        refactored = TESTS.replace("assert sub(5, 3) == 2", "assert sub(5, 3) == 2, 'sub'")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_calc=CALC.replace("return a - b", "return (a - b)"),
                  patch_tests=refactored)
            verdict = _verify(root, "test_sub")
            self.assertIs(verdict.outcome, Outcome.VERIFIED, verdict.reason)
            self.assertEqual(verdict.oracle, "baseline")

    def test_a_rewrite_cannot_hide_a_bug_behind_a_stronger_looking_test(self):
        """The rewritten test passes, kills mutants, looks thorough -- and is not the judge."""
        rewritten = TESTS.replace("assert sub(5, 3) == 2", "assert sub(5, 3) == 8\n    assert sub(0, 0) == 0")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_calc=CALC.replace("return a - b", "return a + b"), patch_tests=rewritten)
            verdict = _verify(root, "test_sub")
            self.assertIs(verdict.outcome, Outcome.REFUTED, verdict.reason)
            self.assertIs(Gate([]).decide([verdict], 4, 1.0), Decision.BLOCK)

    def test_an_untouched_test_file_is_not_transplanted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_calc=CALC.replace("return a - b", "return (a - b)"))
            verdict = _verify(root, "test_sub")
            self.assertIs(verdict.outcome, Outcome.VERIFIED, verdict.reason)
            self.assertEqual(verdict.oracle, "patch")


class TestHelpersComeFromTheBaselineToo(unittest.TestCase):
    def test_a_rewritten_helper_is_not_the_answer(self):
        """Bug + untouched test + a helper bent to agree with the bug. The helper is a test file."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, calc, expected in (("baseline", "return a - b", 2), ("patch", "return a + b", 8)):
                (root / side / "tests").mkdir(parents=True)
                (root / side / "calc.py").write_text(f"def sub(a, b):\n    {calc}\n")
                (root / side / "tests" / "__init__.py").write_text("")
                (root / side / "tests" / "helpers.py").write_text(f"EXPECTED = {expected}\n")
                (root / side / "tests" / "test_calc.py").write_text(
                    "from calc import sub\nfrom tests.helpers import EXPECTED\n\n\n"
                    "def test_sub():\n    assert sub(5, 3) == EXPECTED\n    assert sub(1, 1) is not None\n"
                )
            # The claim's own file is byte-for-byte the baseline's: only the
            # helper changed. That alone has to engage the oracle.
            verdict = Gate([]).verify_claim(
                CriticClaim("tests/test_calc.py", "test_sub"), root / "baseline", root / "patch"
            )
            self.assertIs(verdict.outcome, Outcome.REFUTED, verdict.reason)


class TestNewTestInAnExistingFile(unittest.TestCase):
    NEW = TESTS + "\n\ndef test_sub_negative():\n    assert sub(-1, -2) == 1\n"

    def test_the_new_test_verifies_when_the_old_ones_still_hold(self):
        """The commonest agent shape: change the code, add a test next to the old ones."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_calc=CALC.replace("return a - b", "return (a - b)"), patch_tests=self.NEW)
            verdict = _verify(root, "test_sub_negative")
            self.assertIs(verdict.outcome, Outcome.VERIFIED, verdict.reason)
            self.assertEqual(verdict.oracle, "baseline-file")
            self.assertIn("is new in test_calc.py", verdict.reason)

    def test_weakening_an_old_test_in_the_same_file_is_caught(self):
        """The new test is fine; the patch also bent an old one to fit a bug. REFUTED."""
        bent = self.NEW.replace("assert add(2, 3) == 5", "assert add(2, 3) == 6")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_calc=CALC.replace("return a + b", "return a + b + 1"), patch_tests=bent)
            verdict = _verify(root, "test_sub_negative")
            self.assertIs(verdict.outcome, Outcome.REFUTED, verdict.reason)
            self.assertIs(verdict.classification, FailureClass.REGRESSION)
            self.assertEqual(verdict.oracle, "baseline-file")
            self.assertIs(Gate([]).decide([verdict], 4, 1.0), Decision.BLOCK)

    def test_a_failing_new_test_is_a_new_bug(self):
        broken = TESTS + "\n\ndef test_sub_negative():\n    assert sub(-1, -2) == 99\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_tests=broken)
            verdict = _verify(root, "test_sub_negative")
            self.assertIs(verdict.outcome, Outcome.REFUTED, verdict.reason)
            self.assertIs(verdict.classification, FailureClass.NEW_BUG)

    def test_a_red_baseline_file_is_no_reference(self):
        """If the baseline's own tests in the file were failing, nothing can be compared."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_tests=self.NEW)
            (root / "baseline" / "calc.py").write_text(CALC.replace("return a + b", "return a * b"))
            verdict = _verify(root, "test_sub_negative")
            self.assertIs(verdict.outcome, Outcome.UNVERIFIED, verdict.reason)
            self.assertIs(Gate([]).decide([verdict], 4, 1.0), Decision.INCONCLUSIVE)


class TestEndToEnd(unittest.TestCase):
    def test_a_clean_patch_that_adds_a_test_to_an_existing_file_merges(self):
        """Until the oracle this was exit 2 on every run: the file's bytes differ."""
        new = TESTS + "\n\ndef test_sub_negative():\n    assert sub(-1, -2) == 1\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, patch_calc=CALC.replace("return a - b", "return (a - b)"), patch_tests=new)
            (root / "change.diff").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
                " def sub(a, b):\n-    return a - b\n+    return (a - b)\n"
            )
            (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": [1, 2, 5, 6]}}}))
            log = root / "ev.jsonl"
            code = main([
                "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                "--test-path", "test_calc.py", "--test-id", "test_sub_negative",
                "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                "--evidence-log", str(log),
                "--strength-confidence", "0",  # one-operator fixture; see AG-023 tests
            ])
            records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
            verdict = [r for r in records if r.get("kind") != "decision"][-1]
            self.assertEqual(code, EXIT_MERGE, verdict)
            self.assertEqual(verdict["oracle"], "baseline-file")


class TestDefinesTest(unittest.TestCase):
    def _file(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_shapes(self):
        path = self._file(
            "def test_a():\n    pass\n\n\nclass TestB:\n    def test_c(self):\n        pass\n\n\n"
            "async def test_d():\n    pass\n"
        )
        self.assertTrue(_defines_test(path, "test_a"))
        self.assertTrue(_defines_test(path, "test_a[1-2]"))
        self.assertTrue(_defines_test(path, "TestB::test_c"))
        self.assertTrue(_defines_test(path, "test_d"))
        self.assertTrue(_defines_test(path, ""))
        self.assertFalse(_defines_test(path, "test_z"))
        self.assertFalse(_defines_test(path, "TestB::test_z"))

    def test_unknown_is_none(self):
        self.assertIsNone(_defines_test(self._file("def (:\n"), "test_a"))
        self.assertIsNone(_defines_test(Path("run_tests.sh"), "case_1"))


class TestDeclaredTestSupport(unittest.TestCase):
    """A helper whose path says nothing: ``testing_utils.py`` at the root."""

    def _tree(self, root: Path) -> None:
        for side, calc, expected in (("baseline", "return a - b", 2), ("patch", "return a + b", 8)):
            (root / side).mkdir(parents=True)
            (root / side / "calc.py").write_text(f"def sub(a, b):\n    {calc}\n")
            (root / side / "testing_utils.py").write_text(f"EXPECTED = {expected}\n")
            (root / side / "test_calc.py").write_text(
                "from calc import sub\nfrom testing_utils import EXPECTED\n\n\n"
                "def test_sub():\n    assert sub(5, 3) == EXPECTED\n"
            )

    def _verify(self, root: Path):
        return Gate([]).verify_claim(CriticClaim("test_calc.py", "test_sub"), root / "baseline", root / "patch")

    def test_undeclared_it_is_source_and_comes_from_the_patch(self):
        """The control, and the reason the flag exists: nothing in the path says 'helper'."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            self.assertIs(self._verify(root).outcome, Outcome.VERIFIED)

    def test_declared_it_comes_from_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory, declared_test_support(["testing_utils.py"]):
            root = Path(directory)
            self._tree(root)
            verdict = self._verify(root)
            self.assertIs(verdict.outcome, Outcome.REFUTED, verdict.reason)
            self.assertIn("testing_utils.py", verdict.reason)

    def test_the_cli_flag_reaches_the_oracle_and_the_artefact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            log = root / "ev.jsonl"
            code = main([
                "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                "--test-path", "test_calc.py", "--test-id", "test_sub",
                "--test-support", "testing_utils.py",
                "--coverage-ratio", "1.0", "--coverage-source", "untrusted",
                "--evidence-log", str(log),
            ])
            self.assertEqual(code, EXIT_BLOCK)
            decision = [json.loads(line) for line in log.read_text().splitlines() if '"kind":"decision"' in line][-1]
            self.assertEqual(decision["execution"]["test_support"], ["testing_utils.py"])
        # and the declaration does not outlive the call
        self.assertFalse(is_test_path("testing_utils.py"))

    def test_conventions_from_other_languages(self):
        for path in ("web/button.test.js", "web/__tests__/x.js", "src/api.spec.ts",
                     "pkg/calc_test.go", "src/test/java/CalcTest.java", "spec/calc_spec.rb"):
            with self.subTest(path=path):
                self.assertTrue(is_test_path(path))
        for path in ("calc.py", "testing_utils.py", "numpy/testing/utils.py", "src/contest.py"):
            with self.subTest(path=path):
                self.assertFalse(is_test_path(path))


class TestCommandClaimsUseTheOracle(unittest.TestCase):
    """--test-command with a test-id: the id is a label, the transplant still applies."""

    def test_a_rewritten_shell_check_is_judged_by_the_baseline_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, calc, expected in (("baseline", "a - b", "2"), ("patch", "a + b", "8")):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(f"def sub(a, b):\n    return {calc}\n")
                (root / side / "check.sh").write_text(
                    'out=$(python3 -c "from calc import sub; print(sub(5, 3))")\n'
                    f'[ "$out" = "{expected}" ] || exit 1\n'
                )
            verdict = Gate([]).verify_claim(
                CriticClaim("check.sh", "case_sub"), root / "baseline", root / "patch",
                run_kwargs={"command": "sh check.sh", "env": {"PATH": "/usr/bin:/bin"}},
            )
            self.assertIs(verdict.outcome, Outcome.REFUTED, verdict.reason)
            self.assertEqual(verdict.oracle, "baseline")


class TestCollateralSuiteUsesTheBaselineTests(unittest.TestCase):
    """A bent test that is not any claim's: only the full suite can see it."""

    def test_a_bent_non_claim_test_is_a_collateral_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, add, expected in (("baseline", "a + b", 5), ("patch", "a + b + 1", 6)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(
                    f"def sub(a, b):\n    return a - b\n\n\ndef add(a, b):\n    return {add}\n"
                )
                (root / side / "test_sub.py").write_text(
                    "from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
                )
                (root / side / "test_add.py").write_text(
                    f"from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == {expected}\n"
                )
            log = root / "ev.jsonl"
            out = []
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()) as buffer:
                code = main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    "--test-path", "test_sub.py", "--test-id", "test_sub",
                    "--full-suite-path", ".",
                    "--coverage-ratio", "1.0", "--coverage-source", "untrusted",
                    "--evidence-log", str(log),
                ])
            payload = json.loads(buffer.getvalue())
            self.assertEqual(code, EXIT_BLOCK, payload["decision"])
            self.assertEqual(payload["full_suite_oracle"]["source"], "baseline")
            self.assertEqual(payload["full_suite_oracle"]["rewritten_test_files"], ["test_add.py"])
            self.assertEqual(payload["full_suite_exit_codes"][0], 0)
            self.assertNotEqual(payload["full_suite_exit_codes"][1], 0)


if __name__ == "__main__":
    unittest.main()
