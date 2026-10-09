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


if __name__ == "__main__":
    unittest.main()
