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
