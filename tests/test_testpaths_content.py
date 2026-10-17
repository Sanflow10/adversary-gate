"""A test's *name* is confirmed against who imports it.

A Python module under a ``tests`` directory, or named ``test_*``/``*_test.py``,
that the code under test imports is product code: it runs in production, so a
change to it changes behaviour. Classifying it as a test hides it -- kept out of
coverage and mutation and put back from the baseline, a change to it is never
judged. The classification now reads the imports: imported by product code, and
it is source. A helper imported only by other tests stays a test.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adversary_gate.cli import EXIT_BLOCK, EXIT_MERGE, main
from adversary_gate.verifiers.coverage import covered_diff_ratio
from adversary_gate.verifiers.testpaths import (
    declared_test_support,
    is_test_file_in,
    is_test_path,
    production_imported_tests,
)


def _tree(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class TestProductionImportedTests(unittest.TestCase):
    def test_a_module_the_product_imports_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "from tests.rates import RATE\n\n\ndef price(x):\n    return x * RATE\n",
                "tests/rates.py": "RATE = 0.9\n",
                "tests/test_price.py": "from app import price\n\n\ndef test_p():\n    assert price(10) == 9\n",
            })
            self.assertEqual(production_imported_tests(root), frozenset({"tests/rates.py"}))

    def test_a_chain_through_test_named_modules_is_followed(self):
        """app -> tests/rates.py -> tests/table.py: both run in production (3.0.8)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "from tests.rates import RATE\n",
                "tests/rates.py": "from tests.table import BASE\nRATE = BASE * 0.9\n",
                "tests/table.py": "BASE = 1.0\n",
                "tests/helper.py": "X = 1\n",
                "tests/test_app.py": "from tests.helper import X\nimport app\n\n\ndef test_a():\n    assert X\n",
            })
            self.assertEqual(production_imported_tests(root),
                             frozenset({"tests/rates.py", "tests/table.py"}))

    def test_a_top_level_module_under_src_is_matched(self):
        """``src/test_util.py`` is imported as ``test_util`` (3.0.8)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "src/app.py": "import test_util\n",
                "src/test_util.py": "RATE = 0.9\n",
                "tests/test_app.py": "def test_a():\n    pass\n",
            })
            self.assertEqual(production_imported_tests(root), frozenset({"src/test_util.py"}))

    def test_a_module_on_pytests_path_is_matched_by_its_bare_name(self):
        """AG-048 (3.0.9): pytest puts ``tests/`` on sys.path, so ``tests/requests.py``
        is what ``import requests`` in the product gets -- a library replaced by a
        file judged as a test."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "import requests\n\n\ndef get():\n    return requests.get\n",
                "tests/requests.py": "def get():\n    return 1\n",
                "tests/helpers.py": "X = 1\n",
                "tests/test_app.py": "import helpers\nimport app\n\n\ndef test_a():\n    assert helpers.X\n",
            })
            self.assertEqual(production_imported_tests(root), frozenset({"tests/requests.py"}))

    def test_a_product_module_in_latin_1_is_read(self):
        """It was read as UTF-8, failed, and was skipped: its import went unseen (3.0.8)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {"tests/rates.py": "RATE = 0.9\n",
                         "tests/test_price.py": "def test_p():\n    pass\n"})
            (root / "app.py").write_bytes(
                "# -*- coding: latin-1 -*-\nfrom tests.rates import RATE\nNAME = 'pre\u00e7o'\n"
                .encode("latin-1"))
            self.assertEqual(production_imported_tests(root), frozenset({"tests/rates.py"}))

    def test_a_helper_imported_only_by_tests_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "def price(x):\n    return x\n",
                "tests/helpers.py": "def make():\n    return 1\n",
                "tests/test_app.py": "from tests.helpers import make\n\n\ndef test_a():\n    assert make() == 1\n",
            })
            self.assertEqual(production_imported_tests(root), frozenset())

    def test_a_relative_import_from_product_is_followed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "pkg/__init__.py": "",
                "pkg/app.py": "from .tests.data import K\n\n\ndef f():\n    return K\n",
                "pkg/tests/__init__.py": "",
                "pkg/tests/data.py": "K = 3\n",
            })
            self.assertEqual(production_imported_tests(root), frozenset({"pkg/tests/data.py"}))

    def test_conftest_and_declared_are_never_reclassified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "import tests.conftest\nfrom tests.support import S\n",
                "tests/conftest.py": "import pytest\n",
                "tests/support.py": "S = 1\n",
            })
            with declared_test_support(["tests/support.py"]):
                self.assertEqual(production_imported_tests(root), frozenset())
                self.assertTrue(is_test_file_in("tests/support.py", root))
            # Without the declaration, support.py is product-imported -> source.
            self.assertEqual(production_imported_tests(root), frozenset({"tests/support.py"}))

    def test_a_submodule_reached_through_an_attribute_is_flagged(self):
        """``import tests`` names only the package; ``tests.rates.X`` reaches
        the submodule a conftest loaded. Statically visible, and until 3.0.11
        ``tests/rates.py`` stayed a test."""
        for app in ("import tests\n\n\ndef f():\n    return tests.rates.X\n",
                    "import tests as t\n\n\ndef f():\n    return t.rates.X\n",
                    "from tests import rates as r\n\n\ndef f():\n    return r.X\n"):
            with self.subTest(app=app.splitlines()[0]), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                _tree(root, {
                    "app.py": app,
                    "tests/__init__.py": "",
                    "tests/rates.py": "X = 1\n",
                    "tests/helper.py": "Y = 1\n",
                })
                found = production_imported_tests(root)
                self.assertIn("tests/rates.py", found)
                self.assertNotIn("tests/helper.py", found)


class TestIsTestFileIn(unittest.TestCase):
    def test_disguised_module_is_source_real_test_is_test(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "from tests.rates import RATE\n",
                "tests/rates.py": "RATE = 0.9\n",
                "tests/test_app.py": "def test_a():\n    assert 1\n",
            })
            self.assertFalse(is_test_file_in("tests/rates.py", root))     # imported by product
            self.assertTrue(is_test_file_in("tests/test_app.py", root))   # a real test
            self.assertFalse(is_test_file_in("app.py", root))             # plain source

    def test_no_tree_keeps_the_name_based_answer(self):
        self.assertTrue(is_test_file_in("tests/rates.py", None))
        self.assertEqual(is_test_file_in("tests/rates.py", None), is_test_path("tests/rates.py"))

    def test_a_non_python_test_in_a_test_dir_stays_a_test(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {"app.py": "x = 1\n", "web/__tests__/login.spec.js": "it('x')\n"})
            self.assertTrue(is_test_file_in("web/__tests__/login.spec.js", root))


class TestTheCoverageDenominatorCountsADisguisedModule(unittest.TestCase):
    def test_a_product_imported_test_named_file_stays_in_the_ratio(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "from tests.rates import RATE\n\n\ndef price(x):\n    return x * RATE\n",
                "tests/rates.py": "RATE = 0.9\n",
            })
            diff = (
                "--- a/tests/rates.py\n+++ b/tests/rates.py\n"
                "@@ -1 +1 @@\n-RATE = 0.8\n+RATE = 0.9\n"
            )
            cov = {"files": {"tests/rates.py": {"executed_lines": []}}}
            result = covered_diff_ratio(diff, cov, source_root=root)
            self.assertEqual(result.changed_lines, 1)
            self.assertEqual(result.ratio, 0.0)
            self.assertEqual(result.excluded_test_files, ())

    def test_a_real_test_file_is_still_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _tree(root, {
                "app.py": "def price(x):\n    return x\n",
                "tests/test_app.py": "from app import price\n\n\ndef test_p():\n    assert price(2) == 2\n",
            })
            diff = (
                "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
                "@@ -4 +4 @@\n-    assert price(2) == 1\n+    assert price(2) == 2\n"
            )
            result = covered_diff_ratio(diff, {"files": {}}, source_root=root)
            self.assertEqual(result.excluded_test_files, ("tests/test_app.py",))


class TestTheDisguisedModuleEndToEnd(unittest.TestCase):
    """The 3.0.6 case through the CLI, as a patch would bring it.

    ``calc.py`` imports ``rate`` from ``test_rules.py`` -- product code with a
    test's name. The patch makes an honest, covered edit to ``calc.py`` and
    changes ``rate()`` from 2 to 3, so ``price(3)`` goes from 6 to 9 while the
    claim still asserts 6.

    Measured on 3.0.5: the claim was judged with the baseline's
    ``test_rules.py`` put back as a test file, read ``no_regression`` and
    passed. With the collateral suite on, the suite caught the change and the
    answer was BLOCK; with it off (``--full-suite-path ""``) the answer was
    MERGE, exit 0. On 3.0.6 the claim itself fails on the patch: the module is
    source, never put back.
    """

    CALC = {"baseline": "    return x * rate()\n", "patch": "    return rate() * x\n"}
    RATE = {"baseline": "    return 2\n", "patch": "    return 3\n"}

    def _run(self, *extra):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                _tree(root / side, {
                    "calc.py": "from test_rules import rate\n\n\ndef price(x):\n" + self.CALC[side],
                    "test_rules.py": "def rate():\n" + self.RATE[side],
                    "tests/test_calc.py": "from calc import price\n\n\n"
                                          "def test_price():\n    assert price(3) == 6\n",
                })
            (root / "change.diff").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -5,1 +5,1 @@\n"
                "-    return x * rate()\n+    return rate() * x\n"
                "--- a/test_rules.py\n+++ b/test_rules.py\n@@ -2,1 +2,1 @@\n"
                "-    return 2\n+    return 3\n")
            (root / "cov.json").write_text(json.dumps({"files": {
                "calc.py": {"executed_lines": [1, 4, 5]},
                "test_rules.py": {"executed_lines": [1, 2]},
            }}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    "--claim", "tests/test_calc.py::test_price",
                    "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                    "--mutation-max", "0", *extra,
                ])
            return code, json.loads(out.getvalue())

    def test_without_the_collateral_suite_it_is_not_a_merge(self):
        code, payload = self._run("--full-suite-path", "")
        self.assertNotEqual(code, EXIT_MERGE, payload["reason"])
        self.assertEqual(code, EXIT_BLOCK, payload["reason"])

    def test_the_claim_itself_catches_it_not_only_the_suite(self):
        code, payload = self._run()
        self.assertEqual(code, EXIT_BLOCK, payload["reason"])
        claim = payload["claims"][0]
        self.assertEqual(claim["classification"], "regression", claim)
        self.assertEqual(claim["oracle"], "patch", claim)


if __name__ == "__main__":
    unittest.main()
