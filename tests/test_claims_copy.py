"""The claims run in copies of the trees (3.0.18).

They ran in the trees every later measurement reads. A claim that rewrote the
patch's source had mutation measure other code, and the source put back to
the baseline's bytes read as "no source changed": MERGE, with the trees the
gate was handed rewritten on disk.
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, EXIT_MERGE, main  # noqa: E402

BASE = "def f(x):\n    return x * 2\n\n\ndef g(x):\n    return x + 1\n"
OLD_TEST = "from calc import g\n\n\ndef test_g():\n    assert g(1) == 2\n"


def _run(root: Path, calc: str, new_test: str) -> tuple:
    for side, code in (("baseline", BASE), ("patch", calc)):
        (root / side / "tests").mkdir(parents=True)
        (root / side / "calc.py").write_text(code)
        (root / side / "tests" / "test_calc.py").write_text(OLD_TEST)
    (root / "patch" / "tests" / "test_new.py").write_text(new_test)
    (root / "change.diff").write_text("".join(difflib.unified_diff(
        BASE.splitlines(True), calc.splitlines(True), "a/calc.py", "b/calc.py", n=0)))
    lines = list(range(1, calc.count("\n") + 1))
    (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": lines}}}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([
            "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
            "--claim", "tests/test_new.py::test_new",
            "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
            "--full-suite-path", "tests",
        ])
    return code, json.loads(out.getvalue())


class TestAClaimCannotRewriteWhatIsMeasured(unittest.TestCase):
    def test_source_put_back_to_the_baseline_by_a_claim_does_not_merge(self):
        calc = BASE.replace("x * 2", "x * 3")
        new_test = (
            "import pathlib\n\nimport calc\n\n\n"
            "def test_new():\n"
            "    assert calc.f(1) is not None\n"
            f"    pathlib.Path(calc.__file__).write_text({BASE!r})\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, payload = _run(root, calc, new_test)
            shipped = (root / "patch" / "calc.py").read_text()
        self.assertNotEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(shipped, calc)  # the trees handed in are not written
        self.assertEqual(payload["execution"]["claims_rewrote"], ["patch/calc.py"])

    def test_a_claim_that_rewrites_only_after_passing_is_inconclusive(self):
        """Here the measurement alone would merge: the source is rewritten to
        the same code, so only the rewrite itself says something is wrong."""
        calc = (BASE.replace("x * 2", "x * 3")
                + "\n\ndef h(x):\n    return x - 1\n\n\ndef k(x):\n    return x % 5\n"
                + "\n\ndef m(x):\n    return x > 3\n\n\ndef n(x):\n    return x // 2\n")
        new_test = (
            "import pathlib\n\nimport calc\n\n\n"
            "def test_new():\n"
            "    assert calc.f(1) == 3\n    assert calc.h(5) == 4\n"
            "    assert calc.k(7) == 2\n    assert calc.m(4) and not calc.m(3)\n    assert calc.n(9) == 4\n"
            "    path = pathlib.Path(calc.__file__)\n"
            "    path.write_text(path.read_text() + '\\n')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _run(Path(directory), calc, new_test)
        self.assertEqual(code, EXIT_INCONCLUSIVE, payload["reason"])
        self.assertIn("claims' runs changed", payload["reason"])

    def test_a_claim_that_writes_a_new_file_still_merges(self):
        calc = (BASE.replace("x * 2", "x * 3")
                + "\n\ndef h(x):\n    return x - 1\n\n\ndef k(x):\n    return x % 5\n"
                + "\n\ndef m(x):\n    return x > 3\n\n\ndef n(x):\n    return x // 2\n")
        new_test = (
            "import pathlib\n\nimport calc\n\n\n"
            "def test_new():\n"
            "    assert calc.f(1) == 3\n    assert calc.h(5) == 4\n"
            "    assert calc.k(7) == 2\n    assert calc.m(4) and not calc.m(3)\n    assert calc.n(9) == 4\n"
            "    pathlib.Path(calc.__file__).with_name('report.txt').write_text('ok')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _run(Path(directory), calc, new_test)
        self.assertEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(payload["execution"]["claims_rewrote"], [])


if __name__ == "__main__":
    unittest.main()
