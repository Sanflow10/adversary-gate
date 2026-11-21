"""The baseline-file check is kept for one run, in the scans the run passes (3.0.18).

Two new tests in one file still check the baseline's tests once per side, as
long as both claims get the same ``scans``. A call without them checks again:
the answer is not kept on the Gate between runs.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.core.gate import Gate  # noqa: E402
from adversary_gate.core.types import CriticClaim, Outcome  # noqa: E402

CALC = "def sub(a, b):\n    return a - b\n"
TESTS = "from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
NEW = (TESTS + "\n\ndef test_sub_negative():\n    assert sub(-1, -2) == 1\n"
       "\n\ndef test_sub_zero():\n    assert sub(0, 0) == 0\n")


class TestTheCheckIsKeptForTheRun(unittest.TestCase):
    def test_shared_scans_check_once_and_a_new_run_checks_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, calc, tests in (("baseline", CALC, TESTS),
                                      ("patch", CALC.replace("a - b", "(a - b)"), NEW)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(calc)
                (root / side / "test_calc.py").write_text(tests)
            gate = Gate([])
            calls = []
            real_runs = gate._runs

            def counting(directory, test_path, test_id, policy, limits):
                if test_id == "":
                    calls.append(str(directory))
                return real_runs(directory, test_path, test_id, policy, limits)

            gate._runs = counting
            scans = {}
            for test_id in ("test_sub_negative", "test_sub_zero"):
                verdict = gate.verify_claim(CriticClaim("test_calc.py", test_id),
                                            root / "baseline", root / "patch", scans=scans)
                self.assertIs(verdict.outcome, Outcome.VERIFIED, verdict.reason)
                self.assertEqual(verdict.oracle, "baseline-file")
            self.assertEqual(len(calls), 2, calls)  # one baseline, one patch
            gate.verify_claim(CriticClaim("test_calc.py", "test_sub_zero"),
                              root / "baseline", root / "patch", scans={})
            self.assertEqual(len(calls), 4, calls)  # a new run's scans: checked again


if __name__ == "__main__":
    unittest.main()
