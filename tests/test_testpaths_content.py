"""A test's *name* is confirmed against who imports it.

A Python module under a ``tests`` directory, or named ``test_*``/``*_test.py``,
that the code under test imports is product code: it runs in production, so a
change to it changes behaviour. Classifying it as a test hides it -- kept out of
coverage and mutation and put back from the baseline, a change to it is never
judged. The classification now reads the imports: imported by product code, and
it is source. A helper imported only by other tests stays a test.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


if __name__ == "__main__":
    unittest.main()
