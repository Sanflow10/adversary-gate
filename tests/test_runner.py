"""Unit tests for the sandbox runner.

``sandbox/runner.py`` was the least-covered module in the project (63%) --
which is the wrong way round for the one file that actually spawns the code
under test. Everything here is about *how* a run happens rather than what the
gate decides, so these tests stay fast and do not go through the CLI.

Two groups:

* the execution contract a custom ``--test-command`` depends on (0/1/harness
  /timeout, and pytest's own heuristics not being applied to a foreign
  command);
* the shape of the optional ``bwrap`` wrapper, asserted structurally so the
  isolation properties do not need a hostile fixture to be pinned.
"""

from __future__ import annotations

import sys
import subprocess
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)

import sandbox.runner as runner  # noqa: E402
from sandbox.runner import (  # noqa: E402
    DEFAULT_PROCESSES,
    OUTPUT_TAIL_BYTES,
    SandboxResult,
    _detect_harness_error,
    _limit_resources,
    bwrap_available,
    bwrap_prefix,
    build_target,
    run_test,
)

HAVE_BWRAP = bwrap_available()


def _bwrap_usable_here():
    """Can this environment run bwrap at all?

    A different question from whether :func:`bwrap_prefix` is correct. GitHub's
    runner denies the loopback configuration inside a fresh network namespace
    -- ``bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`` -- so
    every end-to-end test fails for a reason that has nothing to do with this
    code, while the coverage floor the job is named after passes at 89.8%.

    The probe runs **raw** bwrap with none of our arguments, which is what
    keeps the distinction honest: if raw bwrap works and a test below still
    fails, the bug is ours and it is reported as a failure rather than being
    absorbed into a skip.
    """
    if not HAVE_BWRAP:
        return False, "bwrap is not installed"
    try:
        proc = subprocess.run(
            ["bwrap", "--unshare-net", "--ro-bind", "/", "/", "--", "true"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"bwrap could not be started: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        return False, f"bwrap cannot create a namespace here: {detail}"
    return True, ""


def _repo(files: dict) -> Path:
    """A throwaway tree the runner can be pointed at."""
    root = Path(tempfile.mkdtemp())
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class TestBuildTarget(unittest.TestCase):
    def test_path_and_id_become_a_node_id(self):
        self.assertEqual(build_target("tests/test_x.py", "test_add"),
                         "tests/test_x.py::test_add")

    def test_empty_id_is_just_the_path(self):
        self.assertEqual(build_target("tests/test_x.py", ""), "tests/test_x.py")

    def test_empty_id_is_not_a_trailing_separator(self):
        self.assertNotIn("::", build_target("tests/test_x.py", ""))


class TestExecutionContract(unittest.TestCase):
    """The pass/fail/harness convention ``--test-command`` documents."""

    def setUp(self):
        self.root = _repo({"test_ok.py": "def test_ok():\n    assert True\n",
                           "test_bad.py": "def test_bad():\n    assert False\n"})

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_pytest_pass_is_zero(self):
        self.assertEqual(run_test(self.root, "test_ok.py", "test_ok").exit_code, 0)

    def test_pytest_failure_is_one(self):
        self.assertEqual(run_test(self.root, "test_bad.py", "test_bad").exit_code, 1)

    def test_custom_command_success_is_zero(self):
        result = run_test(self.root, "", "", command="true")
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)

    def test_custom_command_failure_is_one(self):
        self.assertEqual(run_test(self.root, "", "", command="exit 1").exit_code, 1)

    def test_custom_command_arbitrary_exit_is_passed_through(self):
        """7 is neither pass nor fail -- the caller's own convention decides."""
        self.assertEqual(run_test(self.root, "", "", command="exit 7").exit_code, 7)

    def test_pytest_heuristics_are_not_applied_to_a_custom_command(self):
        """`_detect_harness_error` rewrites exit codes on pytest's own text.

        A caller whose command happens to print "pytest: error:" must not have
        its real exit code turned into 4 (UNRUNNABLE).
        """
        result = run_test(
            self.root, "", "", command="echo 'pytest: error: nonsense'; exit 1"
        )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(
            _detect_harness_error(1, "", "pytest: error: nonsense"), 4
        )

    def test_timeout_is_minus_one_and_marked(self):
        result = run_test(self.root, "", "", command="sleep 5", timeout_seconds=1)
        self.assertEqual(result.exit_code, -1)
        self.assertTrue(result.timed_out)

    def test_output_is_kept_for_the_evidence_log(self):
        result = run_test(self.root, "", "", command="printf 'hello-evidence'")
        self.assertIn("hello-evidence", result.output_tail)

    def test_output_tail_is_truncated_not_dropped(self):
        result = run_test(
            self.root, "", "", command=f"printf '%.0sa' $(seq 1 {OUTPUT_TAIL_BYTES * 3})"
        )
        self.assertLessEqual(len(result.output_tail), OUTPUT_TAIL_BYTES + 30)
        self.assertIn("...[truncated]...", result.output_tail)

    def test_environment_is_filtered_not_inherited(self):
        """The runner passes a minimal env; host secrets do not ride along."""
        import os

        os.environ["ADVERSARY_GATE_SECRET_PROBE"] = "leaked-value"
        try:
            result = run_test(
                self.root, "", "",
                command='printf %s "${ADVERSARY_GATE_SECRET_PROBE:-absent}"',
            )
            self.assertEqual(result.output_tail, "absent")
        finally:
            del os.environ["ADVERSARY_GATE_SECRET_PROBE"]

    def test_repository_directory_is_the_working_directory(self):
        result = run_test(self.root, "", "", command="pwd")
        self.assertEqual(result.output_tail, str(self.root.resolve()))


class TestHarnessErrorDetection(unittest.TestCase):
    def test_missing_pytest_is_unrunnable(self):
        self.assertEqual(
            _detect_harness_error(1, "", "No module named pytest"), 3
        )

    def test_pytest_usage_error_is_unrunnable(self):
        self.assertEqual(
            _detect_harness_error(4, "", "pytest: error: unrecognized arguments"), 4
        )

    def test_plain_failure_is_left_alone(self):
        self.assertEqual(_detect_harness_error(1, "", "assert False"), 1)


class TestRunValidation(unittest.TestCase):
    def setUp(self):
        self.root = _repo({"test_ok.py": "def test_ok():\n    assert True\n"})

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_non_positive_limits_are_rejected(self):
        for field in ("timeout_seconds", "cpu_seconds", "mem_bytes",
                      "file_bytes", "open_files", "processes"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    run_test(self.root, "test_ok.py", "test_ok", **{field: 0})

    def test_missing_repository_is_rejected(self):
        with self.assertRaises(ValueError):
            run_test(self.root / "nowhere", "test_ok.py", "test_ok")

    def test_unknown_sandbox_is_rejected(self):
        with self.assertRaises(ValueError):
            run_test(self.root, "test_ok.py", "test_ok", sandbox="docker")

    @unittest.skipIf(HAVE_BWRAP, "bwrap present: the missing-binary path is unreachable")
    def test_requesting_bwrap_without_bwrap_is_an_error_not_a_silent_run(self):
        with self.assertRaises(ValueError):
            run_test(self.root, "test_ok.py", "test_ok", sandbox="bwrap")

    def test_missing_bwrap_is_detected_even_when_it_is_installed(self):
        """Pin the branch itself, not this machine's package list.

        The whole point of the flag is that a caller who asks for isolation and
        does not get it finds out *before* anything executes; whether this
        happens to have bubblewrap says nothing about whether that holds.
        """
        with mock.patch.object(runner, "bwrap_available", return_value=False):
            with self.assertRaises(ValueError) as ctx:
                run_test(self.root, "test_ok.py", "test_ok", sandbox="bwrap")
        self.assertIn("bwrap", str(ctx.exception))

    def test_custom_env_is_merged_onto_the_safe_default(self):
        """The runner ships a minimal env (no host secrets); a caller may add to
        it, and that addition must survive to the child."""
        result = run_test(
            self.root, "", "", command='printf %s "$AG_TEST_FLAG"', env={"AG_TEST_FLAG": "set-by-caller"}
        )
        self.assertEqual(result.output_tail, "set-by-caller")

    def test_host_env_does_not_leak_through_the_merge(self):
        """Merging a caller env must not reopen the door the default closes."""
        import os

        os.environ["AG_SHOULD_NOT_APPEAR"] = "leaked"
        try:
            result = run_test(
                self.root, "", "",
                command='printf %s "${AG_SHOULD_NOT_APPEAR:-absent}"',
                env={"AG_TEST_FLAG": "set-by-caller"},
            )
            self.assertEqual(result.output_tail, "absent")
        finally:
            del os.environ["AG_SHOULD_NOT_APPEAR"]

    def test_network_isolation_demand_without_declaration_raises(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                run_test(
                    self.root, "test_ok.py", "test_ok",
                    require_network_isolation=True,
                )

    def test_default_process_limit_is_the_measured_one(self):
        self.assertEqual(DEFAULT_PROCESSES, 4096)

    def test_limit_factory_returns_a_callable(self):
        self.assertTrue(callable(_limit_resources(1, 1, 1, 1, 1)))


class TestBwrapPrefix(unittest.TestCase):
    """Structural assertions: the isolation properties, pinned without needing
    to stage an actual escape attempt."""

    @classmethod
    def setUpClass(cls):
        cls.root = _repo({"anything.py": "pass\n"})
        cls.argv = bwrap_prefix(cls.root) if HAVE_BWRAP else []

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.root, ignore_errors=True)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_network_is_unshared(self):
        self.assertIn("--unshare-net", self.argv)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_pid_namespace_is_unshared(self):
        self.assertIn("--unshare-pid", self.argv)

    @classmethod
    def _env_value(cls, name: str):
        """Value bwrap will set for ``name``, ignoring the other --setenv pairs."""
        argv = cls.argv
        for i, item in enumerate(argv):
            if item == "--setenv" and i + 2 < len(argv) and argv[i + 1] == name:
                return argv[i + 2]
        return None

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_guests_get_a_private_tmp_and_home(self):
        self.assertIn("--tmpfs", self.argv)
        tmp_idx = self.argv.index("--tmpfs")
        self.assertEqual(self.argv[tmp_idx + 1], "/tmp")
        self.assertEqual(self._env_value("HOME"), "/tmp")

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_repository_is_the_only_writable_bind(self):
        binds = [
            self.argv[i + 2]
            for i, flag in enumerate(self.argv)
            if flag in ("--bind", "--ro-bind")
        ]
        writable = [
            self.argv[i + 2]
            for i, flag in enumerate(self.argv)
            if flag == "--bind"
        ]
        self.assertEqual(writable, [str(self.root.resolve())])
        self.assertEqual(len(binds), len(set(binds)), "duplicate binds")

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_system_tree_is_read_only(self):
        ro = [
            self.argv[i + 2]
            for i, flag in enumerate(self.argv)
            if flag == "--ro-bind"
        ]
        self.assertIn("/usr", ro)
        self.assertIn("/etc", ro)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_user_site_is_bound_and_named_so_pytest_can_still_be_imported(self):
        """$HOME becomes /tmp inside the guest, which would otherwise move the
        user site-packages with it and kill the run before it started."""
        value = self._env_value("PYTHONUSERBASE")
        if value is None:
            self.skipTest("no user site to bind on this interpreter")
        self.assertTrue(Path(value).is_absolute(), value)
        # The same directory must be bound read-only, or the env var points at
        # a path that does not exist inside the namespace.
        bound = [
            self.argv[i + 2]
            for i, flag in enumerate(self.argv)
            if flag == "--ro-bind"
        ]
        self.assertIn(value, bound)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_command_separator_is_last(self):
        self.assertEqual(self.argv[-1], "--")
        self.assertIn("--chdir", self.argv)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_guest_process_lands_in_the_repository(self):
        self.assertEqual(self.argv[self.argv.index("--chdir") + 1],
                         str(self.root.resolve()))


@unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
class TestBwrapEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Skip only when the *environment* cannot run bwrap.

        ``TestBwrapPrefix`` keeps running on such a machine: it asserts the
        shape of our argv without executing anything, so a regression in
        ``bwrap_prefix`` is still caught there rather than hidden by this.
        """
        usable, reason = _bwrap_usable_here()
        if not usable:
            raise unittest.SkipTest(f"bwrap unusable in this environment: {reason}")

    def setUp(self):
        self.root = _repo({"test_ok.py": "def test_ok():\n    assert True\n"})

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_pytest_still_passes_inside_the_namespace(self):
        result = run_test(self.root, "test_ok.py", "test_ok", sandbox="bwrap")
        self.assertEqual(result.exit_code, 0, result.output_tail)

    def test_pytest_failure_still_fails_inside_the_namespace(self):
        (self.root / "test_bad.py").write_text("def test_bad():\n    assert False\n")
        result = run_test(self.root, "test_bad.py", "test_bad", sandbox="bwrap")
        self.assertEqual(result.exit_code, 1, result.output_tail)

    def test_no_outbound_network(self):
        result = run_test(
            self.root, "", "",
            command=(
                "python3 -c \"import socket;"
                "socket.create_connection(('1.1.1.1', 80), 3)\" "
                "2>/dev/null && echo OPEN || echo BLOCKED"
            ),
            sandbox="bwrap",
        )
        self.assertIn("BLOCKED", result.output_tail)

    def test_system_tree_is_not_writable(self):
        result = run_test(
            self.root, "", "",
            command="touch /etc/should-not-work 2>/dev/null && echo WROTE || echo READONLY",
            sandbox="bwrap",
        )
        self.assertIn("READONLY", result.output_tail)

    def test_repo_is_writable_and_tmp_persists_within_the_run(self):
        result = run_test(
            self.root, "", "",
            command="touch ./made-here && echo WROTE_REPO",
            sandbox="bwrap",
        )
        self.assertIn("WROTE_REPO", result.output_tail)
        self.assertTrue((self.root / "made-here").exists())

    def test_host_processes_are_invisible(self):
        """Count *pids* only.

        ``/proc`` also holds ``meminfo``, ``cpuinfo`` and friends, which are
        present in any namespace -- so a bare ``listdir`` counts static files
        and would make the guest look populated no matter what.
        """
        result = run_test(
            self.root, "", "",
            command=(
                "python3 -c \"import os;"
                "p=[e for e in os.listdir('/proc') if e.isdigit()];"
                "print(len(p) < 15 and 'HIDDEN' or 'VISIBLE:'+str(len(p)))\""
            ),
            sandbox="bwrap",
        )
        self.assertIn("HIDDEN", result.output_tail, result.output_tail)

    def test_custom_command_runs_inside_the_namespace(self):
        result = run_test(
            self.root, "", "", command="echo all-good && true", sandbox="bwrap"
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("all-good", result.output_tail)


class TestPrefixConstruction(unittest.TestCase):
    """The two skip rules in ``bwrap_prefix``.

    A path that does not exist, and a path already covered elsewhere: both are
    invisible until something tries to mount the same tree twice. Asserted by
    patching the system path list and the interpreter prefixes, so the rules
    are exercised regardless of whether this particular box is venv'd.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = _repo({"anything.py": "pass\n"})

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.root, ignore_errors=True)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_nonexistent_system_path_is_skipped(self):
        missing = str(self.root / "does-not-exist")
        with mock.patch.object(runner, "_SYSTEM_RO", [missing, "/usr"]):
            argv = runner.bwrap_prefix(self.root)
        self.assertNotIn(missing, argv)
        self.assertIn("/usr", argv)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_system_path_inside_the_repository_is_not_bound(self):
        """Binding the repo's own copy of /usr would shadow the real one."""
        inside = self.root / "usr"
        inside.mkdir(exist_ok=True)
        with mock.patch.object(runner, "_SYSTEM_RO", [str(inside), "/usr"]):
            argv = runner.bwrap_prefix(self.root)
        self.assertNotIn(str(inside), argv)
        self.assertIn("/usr", argv)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_interpreter_prefix_inside_the_repository_is_not_bound(self):
        inside = self.root / "python"
        inside.mkdir(exist_ok=True)
        with mock.patch.object(runner.sys, "prefix", str(inside)), \
                mock.patch.object(runner.sys, "base_prefix", str(inside)):
            argv = runner.bwrap_prefix(self.root)
        self.assertNotIn(str(inside), argv)

    @unittest.skipUnless(HAVE_BWRAP, "bwrap not installed")
    def test_interpreter_prefix_outside_the_repository_is_bound_exactly_once(self):
        """A venv interpreter lives outside /usr and must be reachable from the
        guest -- but bound once, not once per loop iteration."""
        base = Path(tempfile.mkdtemp())
        outside = base / "py"
        outside.mkdir(parents=True)
        try:
            with mock.patch.object(runner.sys, "prefix", str(outside)), \
                    mock.patch.object(runner.sys, "base_prefix", str(outside)):
                argv = runner.bwrap_prefix(self.root)
            # --ro-bind X X puts the path in argv twice: once as source, once
            # as destination. Four or more would mean the duplicate guard failed.
            self.assertEqual(argv.count(str(outside)), 2)
            self.assertNotIn(str(outside), [
                argv[i + 2] for i, flag in enumerate(argv) if flag == "--bind"
            ])
        finally:
            import shutil

            shutil.rmtree(base, ignore_errors=True)


class TestResourceLimits(unittest.TestCase):
    """``_apply`` runs in the forked child, where coverage never sees it.

    Called directly against a stand-in for the ``resource`` module -- otherwise
    every RLIMIT in this program ships with no test at all, on the theory that
    "the child process is hard to observe".
    """

    @staticmethod
    def _fake_resource(include_nproc: bool = True):
        calls: list = []
        attrs = {
            "setrlimit": lambda name, limits: calls.append((name, limits)),
            "RLIMIT_CPU": "CPU",
            "RLIMIT_AS": "AS",
            "RLIMIT_CORE": "CORE",
            "RLIMIT_FSIZE": "FSIZE",
            "RLIMIT_NOFILE": "NOFILE",
        }
        if include_nproc:
            attrs["RLIMIT_NPROC"] = "NPROC"
        return types.SimpleNamespace(**attrs), calls

    def test_every_documented_limit_is_applied_with_hard_bounds(self):
        fake, calls = self._fake_resource()
        with mock.patch.object(runner, "resource", fake):
            _limit_resources(7, 1024, 2048, 33, 512)()
        self.assertEqual(
            calls,
            [
                ("CPU", (7, 7)),
                ("AS", (1024, 1024)),
                # Core dumps off unconditionally: an untrusted test should not
                # be able to drop a file the operator then has to go find.
                ("CORE", (0, 0)),
                ("FSIZE", (2048, 2048)),
                ("NOFILE", (33, 33)),
                ("NPROC", (512, 512)),
            ],
        )

    def test_nproc_is_skipped_where_the_platform_lacks_it(self):
        fake, calls = self._fake_resource(include_nproc=False)
        with mock.patch.object(runner, "resource", fake):
            _limit_resources(7, 1024, 2048, 33, 512)()
        self.assertNotIn("NPROC", [name for name, _ in calls])

    def test_absent_resource_module_is_a_no_op_not_a_crash(self):
        """Windows and some sandboxes have no `resource` at all."""
        with mock.patch.object(runner, "resource", None):
            _limit_resources(1, 1, 1, 1, 1)()

    def test_missing_preexec_does_not_stop_the_run(self):
        """With `resource` unavailable `run_test` passes no preexec_fn."""
        root = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(runner, "resource", None):
                result = run_test(root, "", "", command="true")
            self.assertEqual(result.exit_code, 0)
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


class TestSandboxResult(unittest.TestCase):
    def test_output_tail_joins_stderr_before_stdout(self):
        result = SandboxResult(1, "OUT", "ERR", False)
        self.assertTrue(result.output_tail.startswith("ERR"))

    def test_output_tail_strips_whitespace(self):
        self.assertEqual(SandboxResult(0, "  x  ", "", False).output_tail, "x")

    def test_empty_output_is_empty(self):
        self.assertEqual(SandboxResult(0, "", "", False).output_tail, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
