"""The gate's own evidence log inside a judged tree is not a claim's rewrite (3.0.18)."""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, EXIT_MERGE, main  # noqa: E402
from adversary_gate.core.evidence_log import EvidenceLog  # noqa: E402

BASE = "def f(x):\n    return x * 2\n"
CALC = (BASE.replace("x * 2", "x * 3")
        + "\n\ndef h(x):\n    return x - 1\n\n\ndef k(x):\n    return x % 5\n"
        + "\n\ndef m(x):\n    return x > 3\n\n\ndef n(x):\n    return x // 2\n")


def _new_test(rewrite: bool) -> str:
    return (
        "import pathlib\n\nimport calc\n\n\n"
        "def test_new():\n"
        "    assert calc.f(1) == 3\n    assert calc.h(5) == 4\n"
        "    assert calc.k(7) == 2\n    assert calc.m(4) and not calc.m(3)\n    assert calc.n(9) == 4\n"
        + ("    (pathlib.Path(calc.__file__).parent / 'data.txt').write_text('changed')\n"
           if rewrite else "")
    )


class TestTheGatesOwnLogIsNotARewrite(unittest.TestCase):
    """The Action's default ``evidence.jsonl`` sits in the workspace it judges as the patch."""

    def _run(self, *, rewrite: bool = False, log_side: str = "patch",
             existing: bool = False, relative: bool = False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, code in (("baseline", BASE), ("patch", CALC)):
                (root / side / "tests").mkdir(parents=True)
                (root / side / "calc.py").write_text(code)
                (root / side / "tests" / "test_calc.py").write_text(
                    "from calc import f\n\n\ndef test_f():\n    assert f(1) in (2, 3)\n")
                (root / side / "data.txt").write_text("shipped")
            (root / "patch" / "tests" / "test_new.py").write_text(_new_test(rewrite))
            log = root / log_side / "evidence.jsonl"
            if existing:
                log.write_text('{"kind": "decision", "decision": "merge"}\n')
            (root / "change.diff").write_text("".join(difflib.unified_diff(
                BASE.splitlines(True), CALC.splitlines(True), "a/calc.py", "b/calc.py", n=0)))
            lines = list(range(1, CALC.count("\n") + 1))
            (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": lines}}}))
            here = os.getcwd()
            try:
                if relative:
                    os.chdir(log.parent)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main([
                        "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                        "--claim", "tests/test_new.py::test_new",
                        "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                        "--full-suite-path", "tests",
                        "--evidence-log", log.name if relative else str(log),
                    ])
            finally:
                os.chdir(here)
            payload = json.loads(out.getvalue())
            written = log.read_text().count('"kind"')
        return code, payload, written

    def test_a_log_in_the_patch_tree_does_not_stop_a_merge(self):
        code, payload, written = self._run()
        self.assertEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(payload["execution"]["claims_rewrote"], [])
        self.assertGreater(written, 0)

    def test_a_log_named_relative_to_the_workspace_that_already_held_records(self):
        code, payload, _ = self._run(existing=True, relative=True)
        self.assertEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(payload["execution"]["claims_rewrote"], [])

    def test_a_log_in_the_baseline_tree_is_left_out_too(self):
        code, payload, _ = self._run(log_side="baseline")
        self.assertEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(payload["execution"]["claims_rewrote"], [])

    def test_a_real_rewrite_beside_the_log_is_still_named(self):
        code, payload, _ = self._run(rewrite=True, existing=True)
        self.assertEqual(code, EXIT_INCONCLUSIVE, payload["reason"])
        self.assertEqual(payload["execution"]["claims_rewrote"], ["patch/data.txt"])


class TestTheLogNamesEveryFileItWrites(unittest.TestCase):
    def test_the_log_and_each_rotated_copy(self):
        path = Path("/x/evidence.jsonl")
        self.assertEqual(_log(path, backup_count=3).files(),
                         [path, Path("/x/evidence.jsonl.1"), Path("/x/evidence.jsonl.2"),
                          Path("/x/evidence.jsonl.3")])

    def test_no_rotation_is_the_log_alone(self):
        path = Path("/x/evidence.jsonl")
        self.assertEqual(_log(path, backup_count=0).files(), [path])


def _log(path: Path, *, backup_count: int) -> EvidenceLog:
    # Built without __init__, which creates the directory: only the names matter.
    log = EvidenceLog.__new__(EvidenceLog)
    log.path = path
    log.backup_count = backup_count
    return log


if __name__ == "__main__":
    unittest.main()
