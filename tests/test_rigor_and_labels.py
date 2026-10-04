"""AG-023 (rigour of the mutation score) and AG-030 (fix vs. no regression).

AG-023, as audited: "mutation score with n = 1..6, sites taken in file order,
no confidence interval". Three changes answer it, one test class each:

* the floor reads the lower bound of a Wilson interval (80 % by default), so
  1 killed mutant out of 1 no longer clears 0.75 on its own;
* sites are spread across the changed lines instead of taken in file order;
* the default budget is 12 mutants, so the interval is reachable.

AG-030: FAIL -> PASS and PASS -> PASS both read "test passes on the patch".
They now carry ``fixed`` and ``no_regression``, and the artefact says whether
the patch *proved* a fix.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, EXIT_MERGE, EXIT_USAGE, main
from adversary_gate.core.gate import Gate
from adversary_gate.core.metrics import compute_metrics
from adversary_gate.core.types import (
    CriticClaim,
    ExecState,
    ExecutionOutcome,
    FailureClass,
    Outcome,
)
from adversary_gate.verifiers.strength import (
    DEFAULT_MAX_MUTANTS,
    _spread,
    calculate_mutation_score,
    wilson_interval,
)

#: Five operations, one per line; one assertion kills each operator mutant.
CALC = (
    "def add(a, b):\n    return a + b\n\n\n"
    "def sub(a, b):\n    return a - b\n\n\n"
    "def mul(a, b):\n    return a * b\n\n\n"
    "def div(a, b):\n    return a / b\n\n\n"
    "def mod(a, b):\n    return a % b\n"
)
TESTS = (
    "from calc import add, sub, mul, div, mod\n\n\n"
    "def test_ops():\n"
    "    assert add(2, 3) == 5\n    assert sub(5, 3) == 2\n    assert mul(3, 4) == 12\n"
    "    assert div(8, 2) == 4\n    assert mod(7, 3) == 1\n"
)
RETURN_LINES = [2, 6, 10, 14, 18]


def _tree(root: Path, n_changed: int) -> None:
    """Baseline CALC; the patch parenthesises the first ``n_changed`` returns."""
    lines = CALC.split("\n")
    for line in RETURN_LINES[:n_changed]:
        body = lines[line - 1].split("return ", 1)[1]
        lines[line - 1] = f"    return ({body})"
    for side, text in (("baseline", CALC), ("patch", "\n".join(lines))):
        (root / side).mkdir(parents=True, exist_ok=True)
        (root / side / "calc.py").write_text(text)
        (root / side / "test_calc.py").write_text(TESTS)
    diff = "".join(
        f"--- a/calc.py\n+++ b/calc.py\n@@ -{n},1 +{n},1 @@\n-{CALC.split(chr(10))[n - 1]}\n+{lines[n - 1]}\n"
        for n in RETURN_LINES[:n_changed]
    )
    (root / "change.diff").write_text(diff)
    executed = sorted({1, 2, 5, 6, 9, 10, 13, 14, 17, 18})
    (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": executed}}}))


def _cli(root: Path, *extra: str):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([
            "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
            "--test-path", "test_calc.py", "--test-id", "test_ops",
            "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
            "--full-suite-path", "",
            *extra,
        ])
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


class TestWilsonInterval(unittest.TestCase):
    def test_known_values(self):
        """Against hand-computed Wilson bounds at 80 % (z = 1.2816)."""
        cases = {(1, 1): 0.378, (4, 4): 0.709, (5, 5): 0.753, (6, 6): 0.785, (5, 6): 0.575}
        for (killed, total), lower in cases.items():
            with self.subTest(killed=killed, total=total):
                self.assertAlmostEqual(wilson_interval(killed, total, 0.80)[0], lower, places=3)

    def test_zero_confidence_is_the_raw_ratio(self):
        self.assertEqual(wilson_interval(3, 4, 0), (0.75, 0.75))

    def test_bounds_bracket_the_ratio(self):
        for killed, total in ((0, 3), (2, 7), (9, 10), (12, 12)):
            lower, upper = wilson_interval(killed, total, 0.95)
            self.assertLessEqual(lower, killed / total)
            self.assertGreaterEqual(upper, killed / total)
            self.assertGreaterEqual(lower, 0.0)
            self.assertLessEqual(upper, 1.0)

    def test_strength_reads_the_lower_bound(self):
        self.assertFalse(calculate_mutation_score(1, 1).is_strong)
        self.assertFalse(calculate_mutation_score(4, 4).is_strong)
        self.assertTrue(calculate_mutation_score(5, 5).is_strong)
        self.assertTrue(calculate_mutation_score(1, 1, confidence=0).is_strong)

    def test_the_default_budget_can_reach_the_floor(self):
        self.assertGreaterEqual(DEFAULT_MAX_MUTANTS, 5)


class TestSpreadSampling(unittest.TestCase):
    def test_every_line_before_any_line_twice(self):
        """Six operators on line 1 used to eat the whole budget."""
        site = lambda row, col: ((row, col), (row, col + 1), "-")  # noqa: E731
        by_line = {
            ("a.py", 1): [site(1, c) for c in range(6)],
            ("a.py", 7): [site(7, 0)],
            ("b.py", 3): [site(3, 0), site(3, 4)],
        }
        plan = _spread(by_line, 4)
        self.assertEqual(
            [(rel, s[0][0]) for rel, s in plan],
            [("a.py", 1), ("a.py", 7), ("b.py", 3), ("a.py", 1)],
        )
        self.assertEqual(len(_spread(by_line, 100)), 9)


class TestTheFloorEndToEnd(unittest.TestCase):
    def test_one_killed_mutant_is_inconclusive_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, payload = _cli(root)
            self.assertEqual(code, EXIT_INCONCLUSIVE, payload["decision"])
            self.assertEqual(payload["suite_strength"], 1.0)
            self.assertLess(payload["suite_strength_lower"], 0.75)
            self.assertEqual(payload["suite_strength_confidence"], 0.80)

    def test_five_killed_mutants_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            code, payload = _cli(root)
            self.assertEqual(code, EXIT_MERGE, payload)
            self.assertEqual(payload["mutation"]["mutants_killed"], 5)
            self.assertGreaterEqual(payload["suite_strength_lower"], 0.75)
            lines = sorted(m["line"] for m in payload["mutation"]["mutants"])
            self.assertEqual(lines, RETURN_LINES)  # one per changed line

    def test_confidence_zero_restores_the_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, payload = _cli(root, "--strength-confidence", "0")
            self.assertEqual(code, EXIT_MERGE, payload)

    def test_out_of_range_confidence_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, _ = _cli(root, "--strength-confidence", "1")
            self.assertEqual(code, EXIT_USAGE)


class TestFixedIsNotNoRegression(unittest.TestCase):
    def _classify(self, base: ExecState):
        failures = 1 if base is ExecState.FAIL else 0
        return Gate([]).classify(
            CriticClaim("t.py", "t"),
            ExecutionOutcome(base, ExecState.PASS, baseline_failures=failures, run_count=1),
        )

    def test_fail_to_pass_is_a_fix(self):
        verdict = self._classify(ExecState.FAIL)
        self.assertIs(verdict.classification, FailureClass.FIXED)
        self.assertIs(verdict.outcome, Outcome.VERIFIED)
        self.assertIn("fixes", verdict.reason)

    def test_pass_to_pass_is_no_regression(self):
        verdict = self._classify(ExecState.PASS)
        self.assertIs(verdict.classification, FailureClass.NO_REGRESSION)
        self.assertIs(verdict.outcome, Outcome.VERIFIED)
        self.assertIn("not evidence of a fix", verdict.reason)

    def test_a_new_test_is_neither(self):
        verdict = self._classify(ExecState.NOT_APPLICABLE)
        self.assertIs(verdict.classification, FailureClass.CLAIM_DISCARDED)

    def test_the_artefact_says_whether_a_fix_was_proven(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            # The baseline had the bug the patch removes: mod was a * b.
            broken = CALC.replace("return a % b", "return a * b")
            (root / "baseline" / "calc.py").write_text(broken)
            (root / "change.diff").write_text(
                (root / "change.diff").read_text().replace("-    return a % b", "-    return a * b")
            )
            code, payload = _cli(root)
            self.assertEqual(code, EXIT_MERGE, payload)
            self.assertTrue(payload["fix_proven"])
            self.assertEqual(payload["claims_fixed"], 1)
            self.assertEqual(payload["claims"][0]["classification"], "fixed")

            _tree(root, 5)
            code, payload = _cli(root)
            self.assertFalse(payload["fix_proven"])
            self.assertEqual(payload["claims"][0]["classification"], "no_regression")

    def test_metrics_count_them_apart(self):
        records = [
            {"outcome": "verified", "classification": "fixed"},
            {"outcome": "verified", "classification": "no_regression"},
            {"outcome": "verified", "classification": "no_regression"},
        ]
        metrics = compute_metrics(records)
        self.assertEqual((metrics.fixed, metrics.no_regression), (1, 2))


if __name__ == "__main__":
    unittest.main()
