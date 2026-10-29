"""A Gate used again judges the new trees, not the old ones (3.0.18)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.core.gate import Gate
from adversary_gate.core.types import CriticClaim, Outcome

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


class TestAGateUsedAgain(unittest.TestCase):
    NEW = TESTS + "\n\ndef test_sub_negative():\n    assert sub(-1, -2) == 1\n"

    def test_a_gate_used_again_checks_the_old_tests_again(self):
        """The answer was kept on the Gate, keyed by the trees' paths: a Gate used
        again on new trees at the same paths -- as a library -- answered from the
        old ones, and a patch that broke an old test in the file verified (3.0.18)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = Gate([])
            _tree(root, patch_calc=CALC.replace("return a - b", "return (a - b)"), patch_tests=self.NEW)
            first = gate.verify_claim(CriticClaim("test_calc.py", "test_sub_negative"),
                                      root / "baseline", root / "patch")
            _tree(root, patch_calc=CALC.replace("return a + b", "return a + b + 1"),
                  patch_tests=self.NEW)
            second = gate.verify_claim(CriticClaim("test_calc.py", "test_sub_negative"),
                                       root / "baseline", root / "patch")
            self.assertIs(first.outcome, Outcome.VERIFIED, first.reason)
            self.assertIs(second.outcome, Outcome.REFUTED, second.reason)


if __name__ == "__main__":
    unittest.main()
