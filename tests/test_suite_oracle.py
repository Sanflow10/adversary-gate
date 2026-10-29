"""The whole suite is judged by the baseline's tests alone (3.0.18).

One run of the whole suite collects every test module in one process. A test
module the patch added loaded a plugin (``pytest_plugins``) or replaced a
function at import, and the baseline's tests passed on code that fails them:
a regression reached MERGE. The run that judges the suite now leaves the
patch's added test files out.
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

from adversary_gate.cli import EXIT_BLOCK, EXIT_MERGE, main  # noqa: E402

BASE = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
NEW = (
    "\n\ndef double(x):\n    return {double}\n\n\ndef triple(x):\n    return x * 3\n"
    "\n\ndef half(x):\n    return x / 2\n\n\ndef mod3(x):\n    return x % 3\n"
    "\n\ndef dec(x):\n    return x - 1\n\n\ndef big(x):\n    return x > 10\n"
)
OLD_TEST = (
    "from calc import add, sub\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_sub():\n    assert sub(5, 3) == 2\n"
)
NEW_TEST = (
    "import calc\n{head}\n\n"
    "def test_double():\n    assert calc.double(3) == 6\n\n\n"
    "def test_more():\n    assert calc.triple(4) == 12\n    assert calc.half(8) == 4\n"
    "    assert calc.mod3(7) == 1\n    assert calc.dec(5) == 4\n"
    "    assert calc.big(11) and not calc.big(10)\n"
)
#: Turns test_add's failure into a pass, only on the patch's exact code.
PLUGIN = (
    "import pathlib\n\nimport pytest\n\n\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    report = outcome.get_result()\n"
    "    src = pathlib.Path(str(item.config.rootpath), 'calc.py').read_text()\n"
    "    if 'return a + b + 1' in src and report.failed and 'test_add' in item.nodeid:\n"
    "        report.outcome = 'passed'\n"
)


def _run(root: Path, calc: str, files: dict) -> tuple:
    for side, code, extra in (("baseline", BASE, {}), ("patch", calc, files)):
        (root / side / "tests").mkdir(parents=True)
        (root / side / "calc.py").write_text(code)
        (root / side / "tests" / "test_calc.py").write_text(OLD_TEST)
        for rel, text in extra.items():
            (root / side / rel).write_text(text)
    diff = "".join(difflib.unified_diff(BASE.splitlines(True), calc.splitlines(True),
                                        "a/calc.py", "b/calc.py", n=0))
    (root / "change.diff").write_text(diff)
    lines = list(range(1, calc.count("\n") + 1))
    (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": lines}}}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([
            "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
            "--claim", "tests/test_double.py::test_double", "--claim", "tests/test_double.py::test_more",
            "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
            "--full-suite-path", "tests",
        ])
    return code, json.loads(out.getvalue())


class TestAnAddedTestModuleCannotVouchForTheOldOnes(unittest.TestCase):
    def test_a_plugin_an_added_test_loads_does_not_hide_a_regression(self):
        calc = BASE.replace("return a + b", "return a + b + 1") + NEW.format(double="add(x, x) - 1")
        files = {"tests/helper_plugin.py": PLUGIN,
                 "tests/test_double.py": NEW_TEST.format(head='\npytest_plugins = ["helper_plugin"]\n')}
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _run(Path(directory), calc, files)
        self.assertEqual(code, EXIT_BLOCK, payload["reason"])
        self.assertIn("full suite", payload["reason"])
        self.assertEqual(payload["full_suite_oracle"]["added_test_files"],
                         ["tests/helper_plugin.py", "tests/test_double.py"])

    def test_a_function_an_added_test_replaces_at_import_does_not_hide_a_regression(self):
        """No plugin: a module collected first swaps ``calc.add`` for one that
        answers right to the old test only."""
        calc = BASE.replace("return a + b", "return a + b + 1") + NEW.format(double="add(x, x) - 1")
        swap = (
            "import sys\n\nimport calc\n\n_bugged = calc.add\n\n\n"
            "def _add(a, b):\n"
            "    caller = sys._getframe(1).f_code.co_filename\n"
            "    return _bugged(a, b) - (1 if caller.endswith('test_calc.py') else 0)\n\n\n"
            "calc.add = _add\n"
        )
        files = {"tests/test_aaa.py": swap, "tests/test_double.py": NEW_TEST.format(head="")}
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _run(Path(directory), calc, files)
        self.assertEqual(code, EXIT_BLOCK, payload["reason"])
        self.assertIn("full suite", payload["reason"])

    def test_an_honest_added_test_still_merges(self):
        calc = BASE + NEW.format(double="add(x, x)")
        files = {"tests/test_double.py": NEW_TEST.format(head="")}
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _run(Path(directory), calc, files)
        self.assertEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(payload["full_suite_oracle"]["added_test_files"], ["tests/test_double.py"])


if __name__ == "__main__":
    unittest.main()
