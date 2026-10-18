"""Regression suite for AG-032: the patch configured the runner that judged it.

Executed against ``2ba13a7`` (2.3.0) and pinned here as it was measured: a
patch breaks ``add`` (``a + b`` -> ``a + b + 1``), adds a ``.pytest.ini`` with
``addopts = -p agplug`` and an ``agplug.py`` whose hook rewrites the report to
"passed" only while ``calc.py`` has exactly the broken bytes. Mutants change
those bytes, so the plugin steps aside and every mutant dies for real. Result:
``MERGE``, exit 0, ``diff_coverage: 0.8``, ``suite_strength: 1.0`` -- and
``add(2, 3) == 6``.

Two things let it through, and each has its own test below:

* the denylist knew ``pytest.ini`` but not ``.pytest.ini``, ``pytest.toml`` or
  ``.pytest.toml``, all of which pytest 9 reads;
* the policy only saw the paths somebody *reported*. A diff that left the
  harness file out was never compared against anything.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, main
from adversary_gate.core.gate import Gate
from adversary_gate.core.path_policy import PathPolicy, harness_drift
from adversary_gate.core.types import CriticClaim, Decision, Outcome

BASE_CALC = "def add(a, b):\n    return a + b\n"
BUGGY_CALC = "def add(a, b):\n    return a + b + 1\n"
TEST_CALC = (
    "from calc import add\n\n\ndef test_add():\n"
    "    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n"
)
PLUGIN = (
    "import hashlib, pathlib, pytest\n"
    'H = "{digest}"\n'
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    out = yield\n"
    '    if hashlib.sha256(pathlib.Path("calc.py").read_bytes()).hexdigest() == H:\n'
    '        out.get_result().outcome = "passed"\n'
)
INI = "[pytest]\naddopts = -p agplug\n"
TOML = '[pytest]\naddopts = ["-p", "agplug"]\n'

#: Every config file name pytest 9 reads, with content in that file's format.
CONFIG_NAMES = ("pytest.ini", ".pytest.ini", "tox.ini", "pytest.toml", ".pytest.toml")


def _config_text(name: str) -> str:
    return TOML if name.endswith(".toml") else INI

CALC_HUNK = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n-    return a + b\n+    return a + b + 1\n"
)


def _plugin() -> str:
    return PLUGIN.format(digest=hashlib.sha256(BUGGY_CALC.encode()).hexdigest())


def _added(name: str, text: str) -> str:
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def _attack(root: Path, ini_name: str = ".pytest.ini") -> None:
    for side in ("baseline", "patch"):
        (root / side).mkdir(parents=True, exist_ok=True)
        (root / side / "test_calc.py").write_text(TEST_CALC)
    (root / "baseline" / "calc.py").write_text(BASE_CALC)
    (root / "patch" / "calc.py").write_text(BUGGY_CALC)
    (root / "patch" / "agplug.py").write_text(_plugin())
    (root / "patch" / ini_name).write_text(_config_text(ini_name))
    # Every changed source line executed, as ``coverage json`` reports it once
    # the plugin has loaded: the coverage floor is not what stops this.
    (root / "cov.json").write_text(
        json.dumps(
            {
                "files": {
                    "calc.py": {"executed_lines": [1, 2]},
                    "agplug.py": {"executed_lines": list(range(1, 8))},
                }
            }
        )
    )


def _cli(root: Path, diff: str) -> tuple[int, dict]:
    (root / "change.diff").write_text(diff)
    log = root / "evidence.jsonl"
    code = main(
        [
            "--baseline", str(root / "baseline"),
            "--patch", str(root / "patch"),
            "--test-path", "test_calc.py",
            "--test-id", "test_add",
            "--diff", str(root / "change.diff"),
            "--coverage-json", str(root / "cov.json"),
            "--evidence-log", str(log),
        ]
    )
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return code, [r for r in records if r.get("kind") != "decision"][-1]


class TestTheAttackAsMeasured(unittest.TestCase):
    def test_the_plugin_really_does_hide_the_bug(self):
        """Guard on the fixture: if pytest stops honouring a name, its test below proves nothing."""
        import subprocess

        for name in CONFIG_NAMES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _attack(root, ini_name=name)
                run = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     "test_calc.py"],
                    cwd=root / "patch", capture_output=True, text=True,
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_reported_dotfile_ini_is_inconclusive(self):
        """The 2.3.0 run, with the harness file named in the diff: exit 2, not 0."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _attack(root)
            diff = CALC_HUNK + _added(".pytest.ini", INI) + _added("agplug.py", _plugin())
            code, verdict = _cli(root, diff)
            self.assertEqual(code, EXIT_INCONCLUSIVE, verdict)
            self.assertEqual(verdict["outcome"], "unverified")
            self.assertIn(".pytest.ini", verdict["reason"])

    def test_a_diff_that_leaves_the_harness_out_is_still_caught(self):
        """The tree is compared, not the report: omit the ini from --diff, same answer."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _attack(root)
            code, verdict = _cli(root, CALC_HUNK)
            self.assertEqual(code, EXIT_INCONCLUSIVE, verdict)
            self.assertIn("test harness differs from the baseline", verdict["reason"])
            self.assertIn(".pytest.ini", verdict["reason"])

    def test_every_pytest_config_name(self):
        """All the names pytest 9 reads, each on its own, at the library boundary."""
        for name in CONFIG_NAMES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _attack(root, ini_name=name)
                verdict = Gate([]).verify_claim(
                    CriticClaim("test_calc.py", "test_add"), root / "baseline", root / "patch"
                )
                self.assertIs(verdict.outcome, Outcome.UNVERIFIED, verdict.reason)
                self.assertIs(Gate([]).decide([verdict], 4, 1.0), Decision.INCONCLUSIVE)


class TestHarnessDrift(unittest.TestCase):
    def _trees(self, root: Path):
        (root / "baseline").mkdir()
        (root / "patch").mkdir()
        return root / "baseline", root / "patch"

    def test_nested_and_startup_files_count(self):
        for rel in ("pkg/pytest.ini", "sitecustomize.py", "usercustomize.py",
                    "evil.pth", "sub/conftest.py", "sub/pyproject.toml"):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as directory:
                baseline, patch = self._trees(Path(directory))
                (patch / rel).parent.mkdir(parents=True, exist_ok=True)
                (patch / rel).write_text("x = 1\n")
                drift = harness_drift(baseline, patch)
                self.assertEqual([v.path for v in drift], [rel])

    def test_changed_and_removed_are_drift_too(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline, patch = self._trees(Path(directory))
            (baseline / "conftest.py").write_text("a = 1\n")
            (patch / "conftest.py").write_text("a = 2\n")
            (baseline / "tox.ini").write_text("[pytest]\n")
            rules = {v.path: v.rule for v in harness_drift(baseline, patch)}
            self.assertIn("changed", rules["conftest.py"])
            self.assertIn("removed", rules["tox.ini"])

    def test_identical_harness_is_not_drift(self):
        """No false alarm: a project with a conftest the patch left alone still verifies."""
        with tempfile.TemporaryDirectory() as directory:
            baseline, patch = self._trees(Path(directory))
            for side in (baseline, patch):
                (side / "conftest.py").write_text("import pytest\n")
                (side / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
            self.assertEqual(harness_drift(baseline, patch), [])

    def test_tooling_state_is_not_the_project(self):
        """A virtualenv or .git inside the checkout carries .pth files nobody wrote."""
        with tempfile.TemporaryDirectory() as directory:
            baseline, patch = self._trees(Path(directory))
            venv = patch / ".venv-ci"
            (venv / "lib").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
            (venv / "lib" / "distutils-precedence.pth").write_text("import os\n")
            (patch / ".git").mkdir()
            (patch / ".git" / "setup.cfg").write_text("")
            self.assertEqual(harness_drift(baseline, patch), [])


class TestReportedPathsDenylist(unittest.TestCase):
    def test_every_pytest_config_name_is_denied(self):
        policy = PathPolicy()
        for path in (".pytest.ini", "pytest.toml", ".pytest.toml", "a/pytest.ini",
                     "sitecustomize.py", "x.pth", "a/b/x.pth"):
            with self.subTest(path=path):
                self.assertTrue(policy.violations([path]), path)


class TestARecursiveDenyGlobAlsoMatchesAtTheRoot(unittest.TestCase):
    """AG-049: the root fallback stripped ``**/`` with ``lstrip``, which removes
    a set of characters, so ``**/*.env`` became the literal ``.env`` and a
    root-level ``app.env`` passed with no violation (3.0.10)."""

    def test_root_level_env_file_is_a_violation(self):
        policy = PathPolicy()
        for path in ("app.env", "prod.env", ".env", "sub/app.env"):
            with self.subTest(path=path):
                self.assertTrue(policy.violations([path]), path)

    def test_every_recursive_glob_matches_at_root_and_below(self):
        import fnmatch
        policy = PathPolicy()
        for pattern in policy.denylist_globs:
            if not pattern.startswith("**/"):
                continue
            tail = pattern.removeprefix("**/")
            sample = tail.replace("**", "x").replace("*", "x")
            for path in (sample, "deep/dir/" + sample):
                with self.subTest(pattern=pattern, path=path):
                    self.assertTrue(fnmatch.fnmatch(sample, tail))
                    self.assertTrue(policy.violations([path]), path)


if __name__ == "__main__":
    unittest.main()


class TestAPatchCannotWriteAVirtualenvMarker(unittest.TestCase):
    """G: the harness check skips a directory with ``pyvenv.cfg`` on both sides
    (a CI virtualenv in the checkout is tooling state), so a patch that wrote
    one hid every harness file under it. The diff is what tells them apart: a
    ``pyvenv.cfg`` in it is denied (3.0.7)."""

    def test_a_reported_pyvenv_cfg_is_a_violation(self):
        policy = PathPolicy()
        for path in ("pyvenv.cfg", "pkg/pyvenv.cfg", "a/b/pyvenv.cfg"):
            with self.subTest(path=path):
                self.assertTrue(policy.violations([path]), path)


class TestAVirtualenvMarkerHidesOnlyAVirtualenv(unittest.TestCase):
    """W1 (3.0.11): an empty ``tests/pyvenv.cfg``, or one kept out of the diff by
    .gitignore, made the tree walk skip ``tests/`` and a ``conftest.py`` there
    configured the runner unseen. Only a virtualenv's own parts are skipped."""

    def _drift(self, patch: dict) -> set:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, files in (("baseline", {}), ("patch", patch)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text("x = 1\n")
                for rel, text in files.items():
                    (root / side / rel).parent.mkdir(parents=True, exist_ok=True)
                    (root / side / rel).write_text(text)
            return {v.path for v in harness_drift(root / "baseline", root / "patch")}

    def test_a_conftest_beside_a_pyvenv_cfg_is_seen(self):
        self.assertIn("tests/conftest.py",
                      self._drift({"tests/pyvenv.cfg": "", "tests/conftest.py": "x = 1\n"}))

    def test_a_real_virtualenv_is_still_skipped(self):
        self.assertEqual(self._drift({
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
            ".venv/lib/python3.12/site-packages/x.pth": "import os\n",
            ".venv/lib/python3.12/site-packages/pkg/conftest.py": "x = 1\n",
        }), set())

    def test_the_harness_reason_is_not_hidden_by_a_red_suite(self):
        """W4: the claim was refused for the harness, but the top-level reason
        named the full suite failing on both sides instead (3.0.11)."""
        import argparse

        from adversary_gate.cli import _reason
        from adversary_gate.core.types import FailureClass, GateVerdict
        verdict = GateVerdict(CriticClaim("tests/test_calc.py", "test_f0"), FailureClass.INVALID,
                              Outcome.UNVERIFIED, "test harness differs from the baseline: x")
        args = argparse.Namespace(full_suite_timeout=1, full_suite_cpu_seconds=1,
                                  full_suite_memory=1, coverage_floor=0.8)
        reason = _reason(Decision.INCONCLUSIVE, [verdict], verdict, (1, 5), args,
                         coverage_ratio=1.0, strength_judged=None, strength_unverified=False,
                         mutation=None)
        self.assertIn("harness", reason)

    def test_a_claim_inside_a_virtualenv_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests" / "lib").mkdir(parents=True)
            (root / "tests" / "pyvenv.cfg").write_text("")
            (root / "tests" / "lib" / "test_f.py").write_text("def test_f0():\n    pass\n")
            error = Gate([]).validate_claim(CriticClaim("tests/lib/test_f.py", "test_f0"), root)
        self.assertIn("virtualenv", error or "")


class TestAModuleTheRunnerWouldImportIsTheHarness(unittest.TestCase):
    """AG-048: ``python -m pytest`` puts the tree's root first on ``sys.path``,
    so a top-level ``inspect.py`` or ``pytest/`` the patch adds runs in place of
    the real module, on the patch side only. It configures the runner as much as
    a ``pytest.ini`` does (3.0.9)."""

    def _trees(self, root: Path, base: dict, patch: dict):
        for side, files in (("baseline", base), ("patch", {**base, **patch})):
            for rel, text in files.items():
                (root / side / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / side / rel).write_text(text)
        return root / "baseline", root / "patch"

    def _found(self, base: dict, patch: dict) -> set:
        with tempfile.TemporaryDirectory() as directory:
            return {v.path for v in harness_drift(*self._trees(Path(directory), base, patch))}

    def test_a_stdlib_name_at_the_root_is_caught(self):
        self.assertIn("inspect.py", self._found({"calc.py": "x = 1\n"}, {"inspect.py": "x = 1\n"}))

    def test_a_package_named_like_the_runner_is_caught(self):
        self.assertIn("pytest", self._found({"calc.py": "x = 1\n"}, {"pytest/__init__.py": "x = 1\n"}))
        self.assertIn("pytest.py", self._found({"calc.py": "x = 1\n"}, {"pytest.py": "x = 1\n"}))

    def test_changing_one_the_baseline_had_is_caught(self):
        self.assertIn("json.py", self._found({"json.py": "a = 1\n"}, {"json.py": "a = 2\n"}))

    def test_an_ordinary_module_is_not(self):
        self.assertEqual(self._found({"calc.py": "x = 1\n"}, {"money.py": "y = 1\n"}), set())

    def test_a_directory_the_baseline_had_keeps_its_files_free(self):
        """``test/`` is a stdlib name and a common test directory."""
        self.assertEqual(self._found({"test/test_a.py": "def test_a():\n    pass\n"},
                                     {"test/test_b.py": "def test_b():\n    pass\n"}), set())

    def test_a_nested_stdlib_name_is_not_on_the_path(self):
        """Inside a package, ``pkg/inspect.py`` is ``pkg.inspect``: it shadows nothing."""
        self.assertEqual(self._found({"pkg/__init__.py": "", "pkg/tests/__init__.py": "",
                                      "pkg/tests/test_a.py": "def test_a():\n    pass\n"},
                                     {"pkg/inspect.py": "x = 1\n"}), set())

    # pytest's default import mode puts more than the root on sys.path:
    # measured on pytest 9.1.1 (3.0.9), a ``wave.py`` beside each of these was
    # imported in place of the standard library's.

    def test_a_test_directory_without_init_is_on_the_path(self):
        base = {"tests/test_a.py": "def test_a():\n    pass\n"}
        self.assertIn("tests/wave.py", self._found(base, {"tests/wave.py": "x = 1\n"}))

    def test_a_deep_test_directory_is_on_the_path(self):
        base = {"a/b/test_a.py": "def test_a():\n    pass\n"}
        self.assertIn("a/b/wave.py", self._found(base, {"a/b/wave.py": "x = 1\n"}))

    def test_a_conftest_directory_is_on_the_path(self):
        base = {"sub/conftest.py": "", "tests/test_a.py": "def test_a():\n    pass\n"}
        self.assertIn("sub/wave.py", self._found(base, {"sub/wave.py": "x = 1\n"}))

    def test_the_parent_of_a_test_package_is_on_the_path(self):
        base = {"x/pkg/__init__.py": "", "x/pkg/test_a.py": "def test_a():\n    pass\n"}
        self.assertIn("x/wave.py", self._found(base, {"x/wave.py": "x = 1\n"}))

    def test_a_test_file_the_patch_adds_puts_its_directory_on_the_path(self):
        self.assertIn("new/wave.py", self._found({"calc.py": "x = 1\n"},
                                                 {"new/test_n.py": "def test_n():\n    pass\n",
                                                  "new/wave.py": "x = 1\n"}))

    def test_a_directory_put_on_the_path_by_a_test_is_covered(self):
        """``sys.path.insert(0, "src")`` in a baseline test (this repository does
        it) puts ``src/`` first: measured, ``src/wave.py`` was imported."""
        base = {"tests/test_a.py": "import sys\nsys.path.insert(0, 'src')\n", "src/app.py": "X = 1\n"}
        self.assertIn("src/wave.py", self._found(base, {"src/wave.py": "x = 1\n"}))

    def test_a_namespace_directory_is_not(self):
        """A directory without ``__init__.py`` never wins over a real module."""
        base = {"tests/conftest.py": "", "tests/test_a.py": "def test_a():\n    pass\n"}
        self.assertEqual(self._found(base, {"tests/http/test_x.py": "def test_x():\n    pass\n"}), set())
        self.assertEqual(self._found({"calc.py": "x = 1\n"}, {"site/index.html": "<p>\n"}), set())

    def test_a_package_with_a_standard_name_is(self):
        self.assertIn("json", self._found({"calc.py": "x = 1\n"}, {"json/__init__.py": "x = 1\n"}))

    def test_a_virtualenvs_site_packages_is_not(self):
        base = {"calc.py": "x = 1\n"}
        self.assertEqual(self._found(base, {".venv/pyvenv.cfg": "home = /usr\n",
                                            ".venv/lib/python3.12/site-packages/six.py": "x = 1\n",
                                            ".venv/lib/python3.12/site-packages/typing.py": "x\n"}), set())

    def test_a_helper_beside_the_tests_is_not(self):
        """``tests/helpers.py`` imported as ``helpers`` is how prepend mode is used."""
        base = {"tests/test_a.py": "def test_a():\n    pass\n"}
        self.assertEqual(self._found(base, {"tests/helpers.py": "x = 1\n"}), set())

    def test_a_virtualenv_ci_creates_at_the_root_is_not(self):
        """``venv`` is a stdlib name; a CI virtualenv in the checkout is tooling."""
        self.assertEqual(self._found({"calc.py": "x = 1\n"},
                                     {"venv/pyvenv.cfg": "home = /usr\n", "venv/bin/x": ""}), set())
