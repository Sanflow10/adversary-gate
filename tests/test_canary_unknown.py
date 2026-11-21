"""A claim cannot tell the canary and the mutants apart from the patch (3.0.18).

The canary's comment and probe were fixed strings, and the copies the gate
wrote got fresh times and their own sizes: a claim that passed on the patch's
file or on what marks the canary, and failed on anything else, killed every
mutant without checking what the code does.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_MERGE, main  # noqa: E402

BASE = "def f(x):\n    return x * 2\n"
#: A bug in ``h`` that no assertion below would catch, and five operator lines.
PATCH = (BASE + "\n\ndef g(x):\n    return x + 1\n\n\ndef h(x):\n    return x - 7\n"
         "\n\ndef k(x):\n    return x % 5\n\n\ndef m(x):\n    return x > 3\n"
         "\n\ndef n(x):\n    return x // 2\n")
CALLS = "    calc.g(1), calc.h(1), calc.k(1), calc.m(1), calc.n(1)\n"


def _run(root: Path, test: str, old_times: bool = False) -> tuple:
    for side, code in (("baseline", BASE), ("patch", PATCH)):
        (root / side / "tests").mkdir(parents=True)
        (root / side / "calc.py").write_text(code)
        (root / side / "tests" / "test_old.py").write_text(
            "from calc import f\n\n\ndef test_f():\n    assert f(2) == 4\n")
    if old_times:
        os.utime(root / "patch" / "calc.py", (1_600_000_000, 1_600_000_000))
    (root / "patch" / "tests" / "test_new.py").write_text(test)
    (root / "change.diff").write_text("".join(difflib.unified_diff(
        BASE.splitlines(True), PATCH.splitlines(True), "a/calc.py", "b/calc.py", n=0)))
    lines = list(range(1, PATCH.count("\n") + 1))
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


class TestTheCanaryCannotBeRecognised(unittest.TestCase):
    def test_a_claim_that_passes_on_the_old_markers_does_not_merge(self):
        digest = hashlib.sha256(PATCH.encode()).hexdigest()
        test = (
            "import hashlib\nimport pathlib\n\nimport calc\n\n\n"
            "def test_new():\n" + CALLS +
            "    src = pathlib.Path(calc.__file__).read_text()\n"
            f"    assert (hashlib.sha256(src.encode()).hexdigest() == {digest!r}\n"
            "            or '__adversary_probe__' in src or 'adversary-gate canary' in src)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _run(Path(directory), test)
        self.assertNotEqual(code, EXIT_MERGE, payload["reason"])

    def test_a_claim_that_reads_only_times_and_size_does_not_merge(self):
        """Not the bytes: the reference's copy keeps the file's old mtime, the
        canary is much larger than the patch's file; a mutant was neither."""
        test = (
            "import os\nimport pathlib\n\nimport calc\n\n\n"
            "def test_new():\n" + CALLS +
            "    stat = os.stat(calc.__file__)\n"
            "    if 'adversary-mutant-' in os.getcwd():\n"
            "        assert (stat.st_ctime_ns - stat.st_mtime_ns > 10 ** 9\n"
            f"                or stat.st_size > {len(PATCH)} + 40)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _run(Path(directory), test, old_times=True)
        self.assertNotEqual(code, EXIT_MERGE, payload["reason"])


if __name__ == "__main__":
    unittest.main()
