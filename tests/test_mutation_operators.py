"""AG-023 (operators): what the mutation engine can break.

Measured on 2.3.0: a patch that changed ``a + b`` into ``a * b`` reported
``"lines changed by this patch contain no mutable operator"`` and produced no
score -- the table only knew comparisons, ``+``/``-`` and ``and``/``or``/``not``.
The gate stayed fail-closed (``INCONCLUSIVE``), but a gate that cannot measure
ordinary arithmetic is a gate that says ``INCONCLUSIVE`` to most real patches.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.verifiers.strength import _apply, _mutants_in


def _mutated(source: str):
    lines = source.splitlines(True)
    for start, end, replacement in _mutants_in(source, 1000):
        original = lines[start[0] - 1][start[1]:end[1]]
        yield original, replacement, "".join(_apply(lines, start, end, replacement))


class TestArithmeticIsMutable(unittest.TestCase):
    def test_the_measured_case_now_has_a_mutant(self):
        found = [(o, r) for o, r, _ in _mutated("def mul(a, b):\n    return a * b\n")]
        self.assertEqual(found, [("*", "/")])

    def test_each_new_operator(self):
        cases = {
            "x = a / b\n": ("/", "*"),
            "x = a // b\n": ("//", "*"),
            "x = a % b\n": ("%", "*"),
            "x = a ** b\n": ("**", "*"),
            "x += 1\n": ("+=", "-="),
            "x -= 1\n": ("-=", "+="),
            "x *= 2\n": ("*=", "/="),
            "x /= 2\n": ("/=", "*="),
            "x = True\n": ("True", "False"),
            "x = False\n": ("False", "True"),
            "x = f(a)[0] * 2\n": ("*", "/"),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual([(o, r) for o, r, _ in _mutated(source)], [expected])


class TestStarSyntaxIsNotAnOperator(unittest.TestCase):
    """Every mutant must parse, and none may be an equivalent program."""

    SOURCE = (
        "from os import *\n"
        "def f(a, *args, b=1, **kw):\n"
        "    g(*args, **kw)\n"
        "    return [*args], {**kw}, lambda *q, **k: q\n"
        "def h(a, b=1, /, *, c):\n"
        "    pass\n"
    )

    def test_no_star_or_slash_in_syntax_position_is_mutated(self):
        ast.parse(self.SOURCE)
        self.assertEqual(list(_mutated(self.SOURCE)), [])

    def test_every_generated_mutant_parses(self):
        source = (
            "def f(a, *args, flag=True, **kw):\n"
            "    x = a * 2 / 3 // 4 % 5 ** 6\n"
            "    x += 1; x -= 1; x *= 2; x /= 2\n"
            "    return (x) * 2 if not False else g(*args)\n"
        )
        mutants = list(_mutated(source))
        self.assertEqual(len(mutants), 13)
        for original, replacement, text in mutants:
            with self.subTest(mutant=f"{original} -> {replacement}"):
                ast.parse(text)


if __name__ == "__main__":
    unittest.main()


class TestConstantsAreMutatedOnlyWhenAsked(unittest.TestCase):
    """3.0.11: ``n -> n+1`` on integer literals, behind ``--mutate-constants``.
    Off by default because a constant no test observes survives and counts
    against the tests."""

    def test_off_by_default(self):
        self.assertEqual(_mutants_in("x = 2\n", 100), [])

    def test_integers_only_when_on(self):
        sites = _mutants_in("x = 2\ny = 0x10\nz = 1.5\nw = 2j\n", 100, constants=True)
        self.assertEqual([s[2] for s in sites], ["3", "17"])


class TestAMutantThatRebuildsTheBaselineIsNotCounted(unittest.TestCase):
    """3.0.11: a patch that turned ``-`` into ``+`` had one mutant, ``-``: the
    baseline itself. Killing it proved what the claim already did, and a test
    asserting ``!= 1`` scored the same as one asserting ``== 3``."""

    def _measure(self, test_body: str, **kwargs):
        import tempfile
        from adversary_gate.verifiers.strength import measure_mutation_score
        base = "def add(a, b):\n    return a - b + 1\n"
        patch = "def add(a, b):\n    return a + b + 1\n"
        test = f"from calc import add\n\n\ndef test_add():\n    {test_body}\n"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for side, src in (("baseline", base), ("patch", patch)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(src)
                (root / side / "test_calc.py").write_text(test)
            return measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                          targets=["test_calc.py::test_add"], **kwargs)

    def test_the_baseline_mutant_is_dropped(self):
        # Two "+" on the line: the first turned back is the baseline, the
        # second ("a + b - 1") is not. Only the second is left.
        _, detail = self._measure("assert add(1, 1) == 3")
        operators = [m for m in detail["mutants"] if m["kind"] == "operator"]
        self.assertEqual([m["replaces"] for m in operators], ["-"], detail["mutants"])

    def test_the_reason_says_the_only_mutant_was_the_baseline(self):
        """It said "the changed lines hold no operator site" for ``a + b`` (3.0.11)."""
        import tempfile
        from adversary_gate.verifiers.strength import measure_mutation_score
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for side, src in (("baseline", "a - b"), ("patch", "a + b")):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(f"def add(a, b):\n    return {src}\n")
                (root / side / "test_calc.py").write_text(
                    "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
            _, detail = measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                               targets=["test_calc.py::test_add"])
        self.assertIn("back into the baseline", detail.get("reason", ""), detail)
        self.assertEqual(detail["baseline_mutants_skipped"], ["calc.py:2"])

    def test_with_constants_a_weak_test_scores_below_a_strong_one(self):
        _, weak = self._measure("assert add(1, 1) != 1", mutate_constants=True, confidence=0)
        _, strong = self._measure("assert add(1, 1) == 3", mutate_constants=True, confidence=0)
        self.assertLess(weak["mutants_killed"], strong["mutants_killed"], (weak, strong))
