"""Edges of the reason and next_steps the 3.0.18 self-judgment left untested.

A reason or a list of next steps built with no mutation record at all, or
with a key that holds None, says what it can instead of raising. A patch
that bends more than three baseline test files names three and counts the
rest. Jev's endpoint refuses DEL like any other control character, and a
triage with no client builds the default one.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, _next_steps, _reason, main  # noqa: E402
from adversary_gate.core.gate import Decision  # noqa: E402
from adversary_gate.core.types import Outcome  # noqa: E402
from adversary_gate.integrations import jev  # noqa: E402

CALC = "def sub(a, b):\n    return a - b\n"
TESTS = "from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
BENT = TESTS.replace("assert sub(5, 3) == 2", "assert sub(5, 3) == sub(5, 3)")


def _args():
    return argparse.Namespace(suite_strength_floor=0.75, strength_confidence=0.80,
                              coverage_floor=0.80, diff=None, coverage_json=None)


class TestNoMutationRecordIsStillAnAnswer(unittest.TestCase):
    def test_a_weak_strength_with_no_record_names_the_floor(self):
        held = [mock.Mock(outcome=Outcome.VERIFIED)]
        reason = _reason(Decision.INCONCLUSIVE, held, None, None, _args(), coverage_ratio=1.0,
                         strength_judged=0.5, mutation=None)
        self.assertIn("below the 0.75 floor", reason)
        self.assertNotIn("next_steps", reason)

    def test_next_steps_with_no_record_or_a_null_list_review_nothing(self):
        held = [mock.Mock(outcome=Outcome.VERIFIED)]
        for mutation in (None, {"rewritten_tests_unmeasured": None}):
            with self.subTest(mutation=mutation):
                steps = _next_steps(Decision.INCONCLUSIVE, held, None, _args(),
                                    coverage_ratio=1.0, mutation=mutation)
                self.assertNotIn("human_review_test_edit", [s["action"] for s in steps])


class TestManyBentTestFilesAreCounted(unittest.TestCase):
    def _run(self, count: int):
        root = Path(tempfile.mkdtemp(prefix="ag-bent-"))
        self.addCleanup(shutil.rmtree, root, True)
        diff = []
        for side in ("baseline", "patch"):
            (root / side).mkdir()
            (root / side / "calc.py").write_text(CALC)
        for index in range(count):
            name = f"test_{chr(ord('a') + index)}.py"
            (root / "baseline" / name).write_text(TESTS)
            (root / "patch" / name).write_text(BENT)
            diff += difflib.unified_diff(TESTS.splitlines(True), BENT.splitlines(True),
                                         f"a/{name}", f"b/{name}")
        (root / "change.diff").write_text("".join(diff))
        (root / "cov.json").write_text(json.dumps(
            {"files": {"calc.py": {"executed_lines": [1, 2], "contexts": {"2": ["test_a.test_sub"]}}}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                         "--max-rounds", "1", "--rounds-used", "1", "--full-suite-path", "",
                         "--strength-confidence", "0", "--claim", "test_a.py::test_sub",
                         "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json")])
        return code, json.loads(out.getvalue())

    def test_four_bent_files_name_three_and_count_one(self):
        code, payload = self._run(4)
        self.assertEqual(code, EXIT_INCONCLUSIVE, payload)
        self.assertIn("(test_a.py, test_b.py, test_c.py (+1 more))", payload["reason"])

    def test_two_bent_files_are_both_named_and_nothing_is_counted(self):
        code, payload = self._run(2)
        self.assertEqual(code, EXIT_INCONCLUSIVE, payload)
        self.assertIn("(test_a.py, test_b.py)", payload["reason"])
        self.assertNotIn("more)", payload["reason"])


class TestJevEdges(unittest.TestCase):
    def test_a_del_character_in_the_endpoint_is_refused(self):
        self.assertTrue(jev.endpoint_allowed("https://example.com/x"))
        self.assertFalse(jev.endpoint_allowed("https://example.com/\x7f"))

    def test_a_triage_with_no_client_builds_the_default_one(self):
        class Stub:
            model = "stub-model"

            def ask(self, state, questions):
                return {"model": "stub-model",
                        "answers": {"risk": {"choice": "low", "confidence": 0.9}}}

        with mock.patch.object(jev, "JevClient", Stub):
            result = jev.triage_diff("diff --git a/x b/x\n")
        self.assertEqual(result.requested_model, "stub-model")
        self.assertIsNone(result.error)
        self.assertEqual(result.risk, "low")


if __name__ == "__main__":
    unittest.main()
