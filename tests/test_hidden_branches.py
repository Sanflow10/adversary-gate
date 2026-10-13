"""A branch that shares its line with the guard: line coverage cannot see it.

Measured on 3.0.3 with default flags: a patch that turned ``return x + 1`` into
``return 999 if x == 0 else x + 1``, next to two honest, well-tested
refactors, reached MERGE -- 5/5 mutants killed, lower bound 0.753, diff
coverage 1.0 -- although ``value(0)`` went from 1 to 999. Coverage is
measured per line, and the line ran, through the ``else``. The only mutant of
the guard (``==`` -> ``!=``) sends the tested input into the hidden arm, the
test kills it, and the kill counts as strength. Nothing ever took the
untested arm away.

The same shape, also measured: ``x == 0 and 999 or x + 1`` (an operand after
a short-circuit), the ternary split over two lines (its continuation is
covered through the statement's first line, E), and ``if x == 0: return 999``
with the body on the header's line. Each now gets a mutant that removes the
branch the tests may never take, first in its line's queue: it survives
exactly when no test takes that branch.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_MERGE, main
from adversary_gate.verifiers.strength import _apply, _branch_sites, measure_mutation_score


def _branch_mutants(source: str, edited=None):
    """Every branch mutant of ``source``, as the whole mutated module."""
    lines = source.split("\n")
    rows = set(range(1, len(lines) + 1)) if edited is None else set(edited)
    found = []
    for row, sites in sorted(_branch_sites(source, rows).items()):
        for start, end, replacement in sites:
            found.append("\n".join(_apply(lines, start, end, replacement)))
    return found


class TestABranchMutantRemovesTheArmTheTestsMayNotTake(unittest.TestCase):
    def test_a_conditional_expression_loses_each_arm(self):
        mutants = _branch_mutants("def value(x):\n    return 999 if x == 0 else x + 1\n")
        self.assertEqual(mutants, [
            "def value(x):\n    return (x + 1)\n",
            "def value(x):\n    return (999)\n",
        ])

    def test_the_payload_in_the_else_arm_is_reached_too(self):
        mutants = _branch_mutants("def value(x):\n    return x + 1 if x != 0 else 999\n")
        self.assertIn("def value(x):\n    return (x + 1)\n", mutants)

    def test_a_ternary_over_two_lines_is_one_construct(self):
        source = "def value(x):\n    return (999 if x == 0\n            else x + 1)\n"
        self.assertIn("def value(x):\n    return ((x + 1))\n", _branch_mutants(source, {3}))

    def test_an_operand_after_a_short_circuit_can_be_dropped(self):
        mutants = _branch_mutants("def value(x):\n    return x == 0 and 999 or x + 1\n")
        # The inner ``and`` without its last operand: 999 is never needed.
        self.assertIn("def value(x):\n    return (x == 0) or x + 1\n", mutants)

    def test_an_if_with_its_body_on_the_header_line_can_be_switched_off(self):
        source = "def value(x):\n    if x == 0: return 999\n    return x + 1\n"
        self.assertEqual(_branch_mutants(source, {2}),
                         ["def value(x):\n    if False: return 999\n    return x + 1\n"])

    def test_a_case_with_its_body_on_the_header_line_can_be_switched_off(self):
        source = ("def value(x):\n    match x:\n        case 0: return 999\n"
                  "        case _: return x + 1\n")
        mutants = _branch_mutants(source, {3})
        self.assertIn("def value(x):\n    match x:\n        case 0 if False: return 999\n"
                      "        case _: return x + 1\n", mutants)

    def test_an_if_whose_body_has_its_own_line_is_left_to_coverage(self):
        """Coverage already sees a body on its own line: no mutant, no extra cost."""
        source = "def value(x):\n    if x == 0:\n        return 999\n    return x + 1\n"
        self.assertEqual(_branch_mutants(source), [])

    def test_only_lines_the_patch_wrote(self):
        source = "a = 1 if b else 2\nc = 3 if d else 4\n"
        self.assertEqual(_branch_mutants(source, {2}), ["a = 1 if b else 2\nc = (4)\n", "a = 1 if b else 2\nc = (3)\n"])

    def test_columns_count_characters_not_bytes(self):
        """ast reports UTF-8 byte offsets; the splice works on characters."""
        source = 'msg = "ação"; out = "sim" if ok else "não"\n'
        self.assertIn('msg = "ação"; out = ("não")\n', _branch_mutants(source))

    def test_every_branch_mutant_parses(self):
        source = (
            "import re\n"
            "def f(a, b=None, *args, **kw):\n"
            "    g = lambda v: v if v else 0\n"
            "    x = [y if y > 0 else -y for y in a if y or b]\n"
            "    if a and b: return {k: (v or 0) for k, v in kw.items()}\n"
            "    return f'{a if b else 0}' + str(b and a or 1)\n"
        )
        for mutant in _branch_mutants(source):
            with self.subTest(mutant=mutant):
                ast.parse(mutant)


TEST_VALUE = "from calc import value\n\n\ndef test_positive():\n    assert value(5) == 6\n"
PAD_BASE = "def scale(a, b, c):\n    return a * b + c\n\n\ndef is_adult(age):\n    return not age < 18\n"
PAD_PATCH = "def scale(a, b, c):\n    return c + a * b\n\n\ndef is_adult(age):\n    return age >= 18\n"
PAD_TEST = ("\n\ndef test_scale():\n    from calc import scale\n    assert scale(2, 3, 4) == 10\n"
            "\n\ndef test_adult():\n    from calc import is_adult\n    assert is_adult(18) is True\n"
            "    assert is_adult(17) is False\n")
CLAIMS = ["test_calc.py::test_positive", "test_calc.py::test_scale", "test_calc.py::test_adult"]

#: What 3.0.3 merged with default flags (or merged once one more honest kill was added).
HIDDEN = {
    "ternary": "def value(x):\n    return 999 if x == 0 else x + 1\n",
    "else-arm": "def value(x):\n    return x + 1 if x != 0 else 999\n",
    "and-or": "def value(x):\n    return x == 0 and 999 or x + 1\n",
    "two-line ternary": "def value(x):\n    return (999 if x == 0\n            else x + 1)\n",
    "one-line if": "def value(x):\n    if x == 0: return 999\n    return x + 1\n",
}


def _tree(root: Path, value_patch: str, tests: str = TEST_VALUE) -> None:
    base = "def value(x):\n    return x + 1\n\n\n" + PAD_BASE
    patch = value_patch + "\n\n" + PAD_PATCH
    for side, text in (("baseline", base), ("patch", patch)):
        (root / side).mkdir(parents=True, exist_ok=True)
        (root / side / "calc.py").write_text(text)
        (root / side / "test_calc.py").write_text(tests + PAD_TEST)


class TestTheMeasuredCasesNoLongerLookStrong(unittest.TestCase):
    def _measure(self, value_patch: str, tests: str = TEST_VALUE, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, value_patch, tests)
            return measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                          targets=CLAIMS, **kwargs)

    def test_each_hidden_branch_leaves_a_survivor_that_vetoes_the_score(self):
        for name, value_patch in HIDDEN.items():
            with self.subTest(name):
                score, detail = self._measure(value_patch)
                self.assertFalse(score.is_strong, detail["mutants"])
                self.assertTrue(score.blind_spots, detail["mutants"])
                self.assertIn("taken by no claim", detail["reason"])
                self.assertIn("calc.py:2", detail["survivors"], detail["mutants"])
                survived = [m for m in detail["mutants"] if m["result"] == "survived"]
                self.assertTrue(all(m["kind"] == "branch" for m in survived), survived)

    def test_kills_elsewhere_cannot_dilute_the_branch_survivor(self):
        """Counted in the ratio, 1 survivor among 11 kills would clear the floor (lower 0.758)."""
        pads = range(2, 11)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, HIDDEN["ternary"], TEST_VALUE + "".join(
                f"\n\ndef test_pad{k}():\n    from calc import pad{k}\n    assert pad{k}(3, 1) == {3 * k + 1}\n"
                for k in pads))
            for side, body in (("baseline", "a * {k} + b"), ("patch", "b + a * {k}")):
                with (root / side / "calc.py").open("a") as calc:
                    calc.write("".join(f"\n\ndef pad{k}(a, b):\n    return {body.format(k=k)}\n" for k in pads))
            score, detail = measure_mutation_score(
                root / "baseline", root / "patch", "test_calc.py",
                targets=CLAIMS + [f"test_calc.py::test_pad{k}" for k in pads], max_mutants=30)
        self.assertGreaterEqual(detail["mutants_killed"], 11, detail["mutants"])
        self.assertFalse(score.is_strong, detail["mutants"])
        self.assertIn("taken by no claim", detail["reason"])

    def test_the_branch_mutants_come_first_on_their_line(self):
        """Both arms are always run, ahead of the line's operator mutants."""
        score, detail = self._measure(HIDDEN["ternary"])
        kinds = [m["kind"] for m in detail["mutants"] if m["line"] == 2]
        self.assertEqual(kinds[:2], ["branch", "branch"], detail["mutants"])
        self.assertEqual(detail["mutants_needed"], 5)  # 2 arms + one per line

    def test_a_test_for_each_arm_kills_both_and_the_score_stands(self):
        """The fix must not tax honest code: both arms tested, every mutant dies."""
        tests = TEST_VALUE + "\n\ndef test_zero():\n    assert value(0) == 999\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, HIDDEN["ternary"], tests)
            score, detail = measure_mutation_score(
                root / "baseline", root / "patch", "test_calc.py",
                targets=CLAIMS + ["test_calc.py::test_zero"])
        self.assertEqual(detail["survivors"], [], detail["mutants"])
        self.assertTrue(score.is_strong, detail)


class TestEndToEnd(unittest.TestCase):
    """The case as it was reported: the CLI with default flags."""

    def test_the_hidden_branch_is_no_longer_a_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, HIDDEN["ternary"])
            diff = (
                "--- a/calc.py\n+++ b/calc.py\n"
                "@@ -2,1 +2,1 @@\n-    return x + 1\n+    return 999 if x == 0 else x + 1\n"
                "@@ -6,1 +6,1 @@\n-    return a * b + c\n+    return c + a * b\n"
                "@@ -10,1 +10,1 @@\n-    return not age < 18\n+    return age >= 18\n"
            )
            (root / "change.diff").write_text(diff)
            (root / "cov.json").write_text(json.dumps(
                {"files": {"calc.py": {"executed_lines": [1, 2, 5, 6, 9, 10]}}}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    *[arg for claim in CLAIMS for arg in ("--claim", claim)],
                    "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                    "--full-suite-path", "",
                ])
            payload = json.loads(out.getvalue())
        self.assertNotEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(payload["diff_coverage_ratio"], 1.0)
        steps = [s for s in payload["next_steps"] if s.get("action") == "kill_mutant"]
        self.assertTrue(any(s.get("line") == 2 for s in steps), payload["next_steps"])


if __name__ == "__main__":
    unittest.main()
