"""AG-024, AG-025 and AG-031: how tests execute, on what, and which tests.

* AG-024 -- the limits were fixed in code (30 s wall, 10 s CPU, 512 MiB address
  space) and a run the memory limit killed exited 1, which the exit map reads
  as a failing test: evidence against the patch that nobody observed.
* AG-025 -- pytest ran on the interpreter running the gate, and the Action
  replaced the caller's Python on PATH to get one. The project's dependencies
  were never on the interpreter that ran its tests.
* AG-031 -- a run verified exactly one hand-named claim, so a workflow named
  one fixed test for every pull request, whatever the patch touched.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import adversary_gate.sandbox.runner as runner  # noqa: E402
from adversary_gate.cli import EXIT_BLOCK, EXIT_INCONCLUSIVE, EXIT_MERGE, EXIT_USAGE, main  # noqa: E402
from adversary_gate.sandbox.runner import (  # noqa: E402
    HARNESS_DIED,
    _argv,
    _detect_resource_death,
    _limit_resources,
    run_test,
)
from adversary_gate.verifiers.discovery import NoTestContexts, discover_claims  # noqa: E402
from adversary_gate.verifiers.strength import measure_mutation_score  # noqa: E402

try:
    import coverage  # noqa: F401

    HAVE_COVERAGE = True
except ImportError:  # the Suite jobs install the package alone
    HAVE_COVERAGE = False

CALC = "def sub(a, b):\n    return a - b\n\n\ndef add(a, b):\n    return a + b\n"
CALC_PATCHED = "def sub(a, b):\n    return (a - b)\n\n\ndef add(a, b):\n    return a + b\n"
TESTS = (
    "from calc import add, sub\n\n\n"
    "def test_sub():\n    assert sub(5, 3) == 2\n\n\n"
    "def test_add():\n    assert add(2, 2) == 4\n\n\n"
    "class TestMore:\n    def test_sub_negative(self):\n        assert sub(-1, -1) == 0\n"
)
#: ``sub``'s body, line 2, is the one line the patch changes.
DIFF = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
    " def sub(a, b):\n-    return a - b\n+    return (a - b)\n"
)


def _tree(files: dict) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ag-exec-"))
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _fixture(*, patch_tests: str = TESTS) -> Path:
    """baseline/ and patch/ with the change above, plus its diff."""
    root = _tree({
        "baseline/calc.py": CALC,
        "baseline/test_calc.py": TESTS,
        "patch/calc.py": CALC_PATCHED,
        "patch/test_calc.py": patch_tests,
        "change.diff": DIFF,
    })
    return root


def _contexts_report(contexts: dict) -> dict:
    """The shape ``coverage json --show-contexts`` writes for calc.py."""
    return {
        "files": {
            "calc.py": {
                "executed_lines": [1, 2, 5, 6],
                "contexts": contexts,
            }
        }
    }


def _wrapper_python(directory: Path, marker: Path) -> Path:
    """An interpreter that leaves a mark, then is this one.

    Proves which interpreter a run used without needing a second Python
    installed: the marker only exists if *this* path was executed.
    """
    path = directory / "project-python"
    path.write_text(f'#!/bin/sh\necho run >> "{marker}"\nexec "{sys.executable}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_cli(root: Path, *extra: str):
    """main() with stdout captured as the JSON payload."""
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([
            "--baseline", str(root / "baseline"),
            "--patch", str(root / "patch"),
            "--max-rounds", "1",
            "--rounds-used", "1",
            "--full-suite-path", "",
            # One-operator fixtures: the strength *interval* (AG-023) has its own
            # tests; these are about something else, so the floor reads the ratio.
            "--strength-confidence", "0",
            *extra,
        ])
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


# ---------------------------------------------------------------- AG-024


class TestLimits(unittest.TestCase):
    def setUp(self):
        self.root = _tree({"test_ok.py": "def test_ok():\n    assert True\n"})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_none_applies_no_cpu_or_memory_limit(self):
        calls = []
        fake = types.SimpleNamespace(
            RLIMIT_CPU="cpu", RLIMIT_AS="as", RLIMIT_CORE="core", RLIMIT_FSIZE="fsize",
            RLIMIT_NOFILE="nofile", RLIMIT_NPROC="nproc",
            setrlimit=lambda which, value: calls.append(which),
        )
        with mock.patch.object(runner, "resource", fake):
            _limit_resources(None, None, 1024, 64, 64)()
        self.assertNotIn("cpu", calls)
        self.assertNotIn("as", calls)
        # The rest of the envelope does not loosen with them.
        self.assertIn("core", calls)
        self.assertIn("fsize", calls)

    def test_a_run_with_no_limits_still_runs(self):
        result = run_test(
            self.root, "test_ok.py", "test_ok",
            timeout_seconds=None, cpu_seconds=None, mem_bytes=None,
        )
        self.assertEqual(result.exit_code, 0)

    def test_zero_is_still_a_typo(self):
        with self.assertRaises(ValueError):
            run_test(self.root, "test_ok.py", "test_ok", mem_bytes=0)


class TestResourceDeath(unittest.TestCase):
    def test_markers_turn_a_failure_into_a_harness_death(self):
        for marker in runner.RESOURCE_DEATH_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(_detect_resource_death(1, "", f"...{marker}..."), HARNESS_DIED)

    def test_a_plain_failure_is_left_alone(self):
        self.assertEqual(_detect_resource_death(1, "assert 1 == 2", ""), 1)

    def test_only_exit_one_is_rewritten(self):
        self.assertEqual(_detect_resource_death(0, "MemoryError", ""), 0)
        self.assertEqual(_detect_resource_death(2, "MemoryError", ""), 2)

    def test_a_test_the_memory_limit_killed_is_not_evidence_against_the_patch(self):
        """Executed: 4 GiB under a 512 MiB address space raises MemoryError,
        pytest reports a failed test and exits 1 -- which used to be read as
        "the patch broke this test"."""
        root = _tree({"test_big.py": "def test_big():\n    blob = bytearray(4 * 1024 ** 3)\n"})
        try:
            result = run_test(root, "test_big.py", "test_big", mem_bytes=512 * 1024 ** 2)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(result.exit_code, HARNESS_DIED, result.output_tail)


class TestEnvironment(unittest.TestCase):
    def setUp(self):
        self.root = _tree({"test_ok.py": "def test_ok():\n    assert True\n"})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_home_is_private_and_removed_after_the_run(self):
        result = run_test(self.root, "", "", command='printf %s "$HOME"')
        home = result.output_tail
        self.assertIn("adversary-home-", home)
        self.assertNotEqual(home, os.path.expanduser("~"))
        self.assertFalse(Path(home).exists(), "the private HOME outlived its run")

    def test_a_home_the_caller_names_is_kept(self):
        result = run_test(self.root, "", "", command='printf %s "$HOME"', env={"HOME": "/elsewhere"})
        self.assertEqual(result.output_tail, "/elsewhere")


# ---------------------------------------------------------------- AG-025


class TestInterpreter(unittest.TestCase):
    def setUp(self):
        self.root = _tree({
            "test_ok.py": "def test_ok():\n    assert True\n",
            "test_bad.py": "def test_bad():\n    assert False\n",
        })
        self.marker = self.root / "used"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_argv_uses_the_named_interpreter(self):
        argv = _argv(self.root, "test_ok.py", "test_ok", None, python="/opt/project/bin/python")
        self.assertEqual(argv[:3], ["/opt/project/bin/python", "-m", "pytest"])

    def test_the_default_is_still_the_gate_interpreter(self):
        self.assertEqual(_argv(self.root, "t.py", "", None)[0], sys.executable)

    def test_pytest_runs_on_the_named_interpreter(self):
        python = _wrapper_python(self.root, self.marker)
        result = run_test(self.root, "test_ok.py", "test_ok", python=str(python))
        self.assertEqual(result.exit_code, 0, result.output_tail)
        self.assertTrue(self.marker.exists(), "the named interpreter never ran")

    def test_targets_run_together_and_any_failure_fails(self):
        argv = _argv(self.root, "ignored.py", "x", None, targets=["test_ok.py", "test_bad.py"])
        self.assertIn("test_ok.py", argv)
        self.assertIn("test_bad.py", argv)
        self.assertNotIn("ignored.py::x", argv)
        result = run_test(self.root, "", "", targets=["test_ok.py", "test_bad.py"])
        self.assertEqual(result.exit_code, 1)

    def test_bwrap_binds_the_named_interpreter_and_its_installation(self):
        venv = Path(tempfile.mkdtemp(prefix="ag-venv-"))
        try:
            (venv / "bin").mkdir()
            (venv / "bin" / "python").symlink_to(sys.executable)
            with mock.patch.object(runner, "bwrap_available", return_value=True):
                argv = runner.bwrap_prefix(self.root, python=str(venv / "bin" / "python"))
            bound = {argv[i + 1] for i, part in enumerate(argv) if part == "--ro-bind"}
            self.assertIn(str(venv.resolve()), bound)
            installation = Path(os.path.realpath(sys.executable)).parent.parent.resolve()
            if installation != Path("/usr"):
                self.assertIn(str(installation), bound)
        finally:
            shutil.rmtree(venv, ignore_errors=True)

    def test_mutants_run_on_the_named_interpreter_with_the_same_environment(self):
        """Before ``run_kwargs`` a mutant ran on the gate's interpreter, without
        the caller's environment and outside the sandbox they asked for."""
        root = _fixture()
        try:
            python = _wrapper_python(root, self.marker)
            strength, detail = measure_mutation_score(
                root / "baseline", root / "patch", "test_calc.py",
                max_mutants=2,
                run_kwargs={"python": str(python), "env": {"AG_PROBE": "1"}},
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertTrue(strength.is_measured, detail)
        self.assertTrue(self.marker.exists(), "no mutant ran on the named interpreter")


# ---------------------------------------------------------------- AG-031


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.root = _fixture()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _discover(self, contexts, **kwargs):
        return discover_claims(
            DIFF, _contexts_report(contexts), self.root / "baseline", self.root / "patch", **kwargs
        )

    def test_the_tests_that_ran_the_changed_line_are_the_claims(self):
        claims, detail = self._discover({
            "1": ["test_calc.test_sub", "test_calc.test_add", "test_calc.TestMore.test_sub_negative"],
            "2": ["test_calc.test_sub", "test_calc.TestMore.test_sub_negative"],
            "6": ["test_calc.test_add"],
        })
        self.assertEqual(
            claims,
            [("test_calc.py", "TestMore::test_sub_negative"), ("test_calc.py", "test_sub")],
        )
        self.assertEqual(detail["tests_found"], 2)
        self.assertEqual(detail["changed_source_files"], ["calc.py"])

    def test_pytest_cov_contexts_are_node_ids_already(self):
        claims, _ = self._discover({"2": ["test_calc.py::test_sub|run", "test_calc.py::test_p[1-2]|run"]})
        self.assertEqual(claims, [("test_calc.py", "test_p"), ("test_calc.py", "test_sub")])

    def test_a_test_file_the_patch_rewrote_is_kept_and_named(self):
        """The baseline oracle judges it; discovery no longer has to drop it."""
        rewritten = TESTS.replace("== 2", "== 8")
        shutil.rmtree(self.root)
        self.root = _fixture(patch_tests=rewritten)
        claims, detail = self._discover({"2": ["test_calc.test_sub"]})
        self.assertEqual(claims, [("test_calc.py", "test_sub")])
        self.assertEqual(detail["rewritten_test_files_judged_by_baseline"], ["test_calc.py"])

    def test_a_test_file_the_patch_added_is_kept(self):
        (self.root / "patch" / "test_new.py").write_text("from calc import sub\n\n\ndef test_new():\n    assert sub(1, 1) == 0\n")
        claims, detail = self._discover({"2": ["test_new.test_new"]})
        self.assertEqual(claims, [("test_new.py", "test_new")])
        self.assertEqual(detail["rewritten_test_files_judged_by_baseline"], [])

    def test_a_mixin_test_becomes_the_classes_that_run_it(self):
        """AG-037: coverage names a test by the class that *defines* it.

        more-itertools defines ``test_truthiness`` on ``PeekableMixinTests``,
        a plain class, and runs it through ``SeekableTest(PeekableMixinTests,
        TestCase)`` and ``PeekableTests(...)``. The context said
        ``PeekableMixinTests.test_truthiness``, pytest collects no such node,
        and both sides came back "usage error" -> INCONCLUSIVE.
        """
        (self.root / "patch" / "test_mix.py").write_text(
            "import unittest\n\nfrom calc import sub\n\n\n"
            "class SubMixin:\n    def test_sub_mixed(self):\n        assert sub(3, 1) == 2\n\n\n"
            "class TestPlain(SubMixin, unittest.TestCase):\n    pass\n\n\n"
            "class OtherSubTests(SubMixin, unittest.TestCase):\n    pass\n\n\n"
            "class TestOverrides(SubMixin, unittest.TestCase):\n"
            "    def test_sub_mixed(self):\n        assert True\n"
        )
        claims, detail = self._discover({"2": ["test_mix.SubMixin.test_sub_mixed"]})
        self.assertEqual(claims, [
            ("test_mix.py", "OtherSubTests::test_sub_mixed"),
            ("test_mix.py", "TestPlain::test_sub_mixed"),
        ])
        self.assertEqual(detail["unresolved_contexts"], {})

    def test_no_contexts_is_a_usage_error_not_an_empty_answer(self):
        report = {"files": {"calc.py": {"executed_lines": [1, 2]}}}
        with self.assertRaises(NoTestContexts):
            discover_claims(DIFF, report, self.root / "baseline", self.root / "patch")

    def test_contexts_that_name_no_test_are_no_contexts(self):
        """--show-contexts without dynamic contexts: every line is the empty context."""
        with self.assertRaises(NoTestContexts):
            self._discover({"1": [""], "2": [""]})

    def test_the_cap_is_applied_and_reported(self):
        claims, detail = self._discover(
            {"2": ["test_calc.test_sub", "test_calc.test_add", "test_calc.TestMore.test_sub_negative"]},
            max_claims=2,
        )
        self.assertEqual(len(claims), 2)
        self.assertEqual(detail["truncated"], 1)
        with self.assertRaises(ValueError):
            self._discover({"2": ["test_calc.test_sub"]}, max_claims=0)

    def test_an_ambiguous_module_is_reported_not_guessed(self):
        for side in ("a", "b"):
            (self.root / "patch" / side).mkdir()
            (self.root / "patch" / side / "test_dup.py").write_text("def test_x():\n    pass\n")
        claims, detail = self._discover({"2": ["test_dup.test_x"]})
        self.assertEqual(claims, [])
        self.assertIn("ambiguous", detail["unresolved_contexts"]["test_dup.test_x"])


class TestSeveralClaims(unittest.TestCase):
    def setUp(self):
        self.root = _fixture()
        self.coverage = self.root / "cov.json"
        self.coverage.write_text(json.dumps(_contexts_report({"2": ["test_calc.test_sub"]})))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _measured(self):
        return ["--diff", str(self.root / "change.diff"), "--coverage-json", str(self.coverage)]

    def test_repeated_test_ids_are_all_verified(self):
        code, payload = _run_cli(
            self.root, "--test-path", "test_calc.py", "--test-id", "test_sub",
            "--test-id", "test_add", *self._measured(),
        )
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertEqual(payload["claims_total"], 2)
        self.assertEqual({c["test_id"] for c in payload["claims"]}, {"test_sub", "test_add"})

    def test_one_refuted_claim_blocks_and_explains_the_decision(self):
        (self.root / "patch" / "calc.py").write_text(CALC_PATCHED.replace("a + b", "a - b"))
        code, payload = _run_cli(
            self.root, "--claim", "test_calc.py::test_sub", "--claim", "test_calc.py::test_add",
            *self._measured(),
        )
        self.assertEqual(code, EXIT_BLOCK, payload)
        self.assertEqual(payload["outcome"], "refuted")
        outcomes = {c["test_id"]: c["outcome"] for c in payload["claims"]}
        self.assertEqual(outcomes, {"test_sub": "verified", "test_add": "refuted"})

    def test_claim_json_takes_any_number_of_claims(self):
        claims = {"claims": [
            {"test_path": "test_calc.py", "test_id": "test_sub"},
            {"test_path": "test_calc.py", "test_id": "TestMore::test_sub_negative"},
        ]}
        code, payload = _run_cli(self.root, "--claim-json", json.dumps(claims), *self._measured())
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertEqual(payload["claims_total"], 2)

    def test_the_same_test_named_twice_is_run_once(self):
        code, payload = _run_cli(
            self.root, "--claim", "test_calc.py::test_sub", "--test-path", "test_calc.py",
            "--test-id", "test_sub", *self._measured(),
        )
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertEqual(payload["claims_total"], 1)

    def test_discovered_claims_are_verified(self):
        code, payload = _run_cli(self.root, "--discover-claims", *self._measured())
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertEqual(payload["discovery"]["claims_added"], ["test_calc.py::test_sub"])
        self.assertEqual(payload["claims_total"], 1)

    def test_discovering_nothing_is_inconclusive_not_merge(self):
        self.coverage.write_text(json.dumps(_contexts_report({"6": ["test_calc.test_add"]})))
        code, payload = _run_cli(self.root, "--discover-claims", *self._measured())
        self.assertEqual(code, EXIT_INCONCLUSIVE, payload)
        self.assertEqual(payload["claims_total"], 0)
        self.assertIsNone(payload["outcome"])
        self.assertIn("no claim to verify", payload["reason"])

    def test_usage_errors(self):
        cases = {
            "no claim at all": [],
            "malformed --claim": ["--claim", "test_calc.py"],
            "--test-id alone": ["--test-id", "test_sub"],
            "discovery without the artefacts": ["--discover-claims"],
            "discovery with a command suite": ["--discover-claims", "--test-command", "true", *self._measured()],
            "a report without contexts": ["--discover-claims", "--diff", str(self.root / "change.diff"),
                                          "--coverage-json", str(self._plain_report())],
            "--max-claims 0": ["--discover-claims", "--max-claims", "0", *self._measured()],
        }
        for label, extra in cases.items():
            with self.subTest(label):
                code, _ = _run_cli(self.root, *extra)
                self.assertEqual(code, EXIT_USAGE)

    def _plain_report(self) -> Path:
        path = self.root / "plain.json"
        path.write_text(json.dumps({"files": {"calc.py": {"executed_lines": [1, 2]}}}))
        return path

    @unittest.skipUnless(HAVE_COVERAGE, "coverage.py is not installed")
    def test_a_real_coverage_report_with_contexts(self):
        """The contract with coverage.py itself, not with a hand-written report."""
        import subprocess

        patch = self.root / "patch"
        rc = self.root / "coveragerc"
        rc.write_text("[run]\ndynamic_context = test_function\n")
        report = self.root / "real.json"
        env = dict(os.environ, COVERAGE_FILE=str(self.root / ".coverage"))
        subprocess.run(
            [sys.executable, "-m", "coverage", "run", "--rcfile", str(rc), "-m", "pytest", "-q",
             "-p", "no:cacheprovider"],
            cwd=patch, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "--show-contexts", "-o", str(report)],
            cwd=patch, env=env, check=True, capture_output=True,
        )
        code, payload = _run_cli(
            self.root, "--discover-claims", "--diff", str(self.root / "change.diff"),
            "--coverage-json", str(report),
        )
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertEqual(
            sorted(payload["discovery"]["claims_added"]),
            ["test_calc.py::TestMore::test_sub_negative", "test_calc.py::test_sub"],
        )


class TestExecutionOptions(unittest.TestCase):
    """The CLI end of AG-024/025: what was asked for reaches the runs, and the
    artefact says what the numbers were measured under."""

    def setUp(self):
        self.root = _fixture()
        self.base = ["--test-path", "test_calc.py", "--test-id", "test_sub", "--coverage-floor", "0"]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_python_is_used_and_recorded(self):
        marker = self.root / "used"
        python = _wrapper_python(self.root, marker)
        code, payload = _run_cli(self.root, *self.base, "--python", str(python))
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertEqual(payload["execution"]["python"], str(python))
        self.assertTrue(marker.exists())

    def test_a_python_that_is_not_there_is_a_usage_error(self):
        code, _ = _run_cli(self.root, *self.base, "--python", str(self.root / "nope"))
        self.assertEqual(code, EXIT_USAGE)

    def test_limits_are_parsed_and_recorded(self):
        code, payload = _run_cli(
            self.root, *self.base, "--timeout", "none", "--cpu-seconds", "20", "--memory", "1G",
        )
        self.assertEqual(code, EXIT_MERGE, payload)
        execution = payload["execution"]
        self.assertIsNone(execution["timeout_seconds"])
        self.assertEqual(execution["cpu_seconds"], 20)
        self.assertEqual(execution["memory_bytes"], 1024 ** 3)

    def test_bad_limits_are_usage_errors(self):
        for flag, value in (("--timeout", "0"), ("--cpu-seconds", "-1"), ("--memory", "0"),
                            ("--memory", "lots"), ("--timeout", "soon")):
            with self.subTest(flag=flag, value=value):
                code, _ = _run_cli(self.root, *self.base, flag, value)
                self.assertEqual(code, EXIT_USAGE)

    def test_environment_reaches_the_test_and_only_names_are_recorded(self):
        for side in ("baseline", "patch"):
            (self.root / side / "test_env.py").write_text(
                "import os\n\n\ndef test_env():\n"
                "    assert os.environ['AG_TOKEN'] == 's3cret-value'\n"
                "    assert os.environ['AG_PASSED'] == 'from-host'\n"
            )
        log = self.root / "evidence.jsonl"
        with mock.patch.dict(os.environ, {"AG_PASSED": "from-host"}):
            code, payload = _run_cli(
                self.root, "--test-path", "test_env.py", "--test-id", "test_env",
                "--coverage-floor", "0", "--mutation-max", "0",
                "--env", "AG_TOKEN=s3cret-value", "--pass-env", "AG_PASSED",
                "--pass-env", "AG_NOT_SET", "--evidence-log", str(log),
            )
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertEqual(payload["execution"]["env_set"], ["AG_TOKEN"])
        self.assertEqual(payload["execution"]["env_passed"], ["AG_PASSED"])
        self.assertEqual(payload["execution"]["env_passed_missing"], ["AG_NOT_SET"])
        self.assertNotIn("s3cret-value", json.dumps(payload))
        self.assertNotIn("s3cret-value", log.read_text())

    def test_malformed_environment_is_a_usage_error(self):
        for extra in (["--env", "NOVALUE"], ["--env", "1BAD=x"], ["--pass-env", "A-B"]):
            with self.subTest(extra=extra):
                code, _ = _run_cli(self.root, *self.base, *extra)
                self.assertEqual(code, EXIT_USAGE)


class TestActionContract(unittest.TestCase):
    """action.yml, read as data: the AG-025 fix and the icon are not regressions-in-waiting."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "action.yml").read_text()

    def test_setup_python_never_touches_the_callers_path(self):
        import re

        uses = re.findall(r"uses: actions/setup-python@\S+\n(?:\s+with:\n(?:\s{8}.+\n)+)?", self.text)
        self.assertTrue(uses, "action.yml no longer sets up the gate's Python")
        for block in uses:
            self.assertIn("update-environment: false", block)

    def test_branding_icon_is_one_github_accepts(self):
        import re

        icon = re.search(r"(?m)^\s+icon:\s*'([^']+)'", self.text).group(1)
        # 'shield-check' is not in the Feather subset Actions branding allows.
        self.assertEqual(icon, "shield")

    def test_test_path_is_optional_and_the_new_inputs_exist(self):
        import re

        block = re.search(r"(?ms)^  test-path:\n(.*?)(?=^  \S)", self.text).group(1)
        self.assertIn("required: false", block)
        for name in ("python", "claims", "discover-claims", "max-claims", "timeout",
                     "cpu-seconds", "memory", "pass-env", "env"):
            self.assertRegex(self.text, rf"(?m)^  {re.escape(name)}:\n")


if __name__ == "__main__":
    unittest.main()
