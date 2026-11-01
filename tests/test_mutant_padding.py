"""The mutants' padding: one comment line, drawn over the canary's growth."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_MERGE, main


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


def _cli(root: Path, *extra: str, test_id: str = "test_ops"):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([
            "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
            "--test-path", "test_calc.py", "--test-id", test_id,
            "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
            "--full-suite-path", "",
            *extra,
        ])
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


class TestTheMutantsAreSizedLikeTheCanary(unittest.TestCase):
    """The canary and every mutant end in one comment line of random length, and
    the mutants' is drawn over the canary's: a claim that reads the size of the
    file cannot tell the two apart."""

    def _run(self, trailing_newline: bool):
        from adversary_gate.verifiers import strength
        asked, written = [], []

        def pad(length):
            asked.append(length)
            return "#" + "x" * max(1, length) + "\n"

        real_write = strength._write_like

        def write(target, text, like):
            written.append(text)
            real_write(target, text, like)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            source = (root / "patch" / "calc.py").read_text()
            if not trailing_newline:
                source = source.rstrip("\n")
                (root / "patch" / "calc.py").write_text(source)
            with mock.patch.object(strength, "_padding", pad), \
                    mock.patch.object(strength, "_write_like", write):
                code, payload = _cli(root)
        self.assertEqual(code, EXIT_MERGE, payload)
        padded = [t for t in written if t.endswith("x\n")]
        self.assertEqual(len(padded), len(asked))
        self.assertGreater(len(asked), 1)
        return source, asked, padded

    def test_the_comment_is_a_line_of_its_own(self):
        for trailing_newline in (True, False):
            with self.subTest(trailing_newline=trailing_newline):
                _, asked, padded = self._run(trailing_newline)
                for length, text in zip(asked, padded):
                    body = text[: -len("#" + "x" * max(1, length) + "\n")]
                    self.assertTrue(body.endswith("\n"), body[-40:])
                    self.assertFalse(body.endswith("\n\n"), body[-40:])

    def test_the_mutants_draw_over_the_canarys_growth(self):
        source, asked, padded = self._run(True)
        canary = padded[0][: -len("#" + "x" * max(1, asked[0]) + "\n")]
        grown = len(canary) - len(source)
        self.assertLess(asked[0], 64)
        for length in asked[1:]:
            self.assertGreaterEqual(length, grown)
            self.assertLess(length, grown + 64)


if __name__ == "__main__":
    unittest.main()
