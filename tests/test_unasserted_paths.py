"""Paths the suite ran but did not check (3.0.18).

The gate's own mutation run left these alive: a link whose target a claim
changed, a refused pin reported as mergeable, a review answer that is not an
object, a repository with no commit yet. Each test pins what the code does.
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate import cli  # noqa: E402
from adversary_gate.cli import EXIT_INCONCLUSIVE, main  # noqa: E402
from adversary_gate.integrations import mcp_server  # noqa: E402
from adversary_gate.integrations.gitevidence import _snapshot, prepare  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def _action_review():
    # Imported when a test runs, not with this file: a mutant that runs main()
    # at import then fails these tests instead of stopping the whole collection.
    from adversary_gate.integrations import action_review
    return action_review


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


class TestALinkIsComparedByItsTarget(unittest.TestCase):
    def test_a_link_pointed_elsewhere_is_a_change(self):
        with tempfile.TemporaryDirectory() as directory:
            original, copy = Path(directory, "a"), Path(directory, "b")
            for tree in (original, copy):
                tree.mkdir()
                (tree / "calc.py").write_text("X = 1\n")
                (tree / "other.py").write_text("X = 2\n")
            os.symlink("calc.py", original / "kept")
            os.symlink("calc.py", copy / "kept")
            os.symlink("calc.py", original / "moved")
            os.symlink("other.py", copy / "moved")
            os.symlink("calc.py", original / "replaced")
            (copy / "replaced").write_text("X = 1\n")
            changed = cli._changed_in_copy(original, copy)
        self.assertEqual(sorted(changed), ["moved", "replaced"])

    def test_a_stray_bytecode_file_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as directory:
            original, copy = Path(directory, "a"), Path(directory, "b")
            for tree, data in ((original, b"old"), (copy, b"newer")):
                tree.mkdir()
                (tree / "calc.py").write_text("X = 1\n")
                (tree / "calc.pyc").write_bytes(data)
            self.assertEqual(cli._changed_in_copy(original, copy), [])

    def test_the_evidence_snapshot_holds_a_links_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text("X = 1\n")
            os.symlink("calc.py", root / "link")
            held = _snapshot(root, ["link", "calc.py", "gone"])
        self.assertEqual(held["link"], ("link", "calc.py"))
        self.assertEqual(held["calc.py"][0], "file")
        self.assertNotIn("gone", held)


class TestAClaimThatRewritesManyFilesSaysSo(unittest.TestCase):
    def _run(self, count: int):
        base = "def f(x):\n    return x * 2\n"
        calc = (base.replace("x * 2", "x * 3")
                + "\n\ndef h(x):\n    return x - 1\n\n\ndef k(x):\n    return x % 5\n"
                + "\n\ndef m(x):\n    return x > 3\n\n\ndef n(x):\n    return x // 2\n")
        new_test = (
            "import pathlib\n\nimport calc\n\n\n"
            "def test_new():\n"
            "    assert calc.f(1) == 3\n    assert calc.h(5) == 4\n"
            "    assert calc.k(7) == 2\n    assert calc.m(4) and not calc.m(3)\n    assert calc.n(9) == 4\n"
            "    here = pathlib.Path(calc.__file__).parent\n"
            f"    for i in range({count}):\n"
            "        (here / f'data{i}.txt').write_text('changed')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, code in (("baseline", base), ("patch", calc)):
                (root / side / "tests").mkdir(parents=True)
                (root / side / "calc.py").write_text(code)
                (root / side / "tests" / "test_calc.py").write_text(
                    "from calc import f\n\n\ndef test_f():\n    assert f(1) in (2, 3)\n")
                for i in range(count):
                    (root / side / f"data{i}.txt").write_text("shipped")
            (root / "patch" / "tests" / "test_new.py").write_text(new_test)
            (root / "change.diff").write_text("".join(difflib.unified_diff(
                base.splitlines(True), calc.splitlines(True), "a/calc.py", "b/calc.py", n=0)))
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
            payload = json.loads(out.getvalue())
        self.assertEqual(code, EXIT_INCONCLUSIVE, payload["reason"])
        self.assertEqual(len(payload["execution"]["claims_rewrote"]), count)
        return payload

    def test_the_reason_counts_the_rest_and_the_step_is_to_read_it(self):
        payload = self._run(7)
        self.assertIn("(+2 more)", payload["reason"])
        step = payload["next_steps"][-1]
        self.assertEqual(step["action"], "see_reason")
        self.assertEqual(step["flags"], [])
        self.assertIn("(+2 more)", step["why"])

    def test_five_rewritten_files_are_all_named(self):
        payload = self._run(5)
        self.assertNotIn("more)", payload["reason"])
        for i in range(5):
            self.assertIn(f"data{i}.txt", payload["reason"])


class TestTheReviewStepReadsOnlyAnObject(unittest.TestCase):
    def _run(self, stdin: str, *argv: str) -> tuple:
        review = _action_review()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "out")
            with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = review.main([*argv, "--output", str(output)])
            return code, output.read_text()

    def test_an_answer_that_is_not_an_object_requires_no_review(self):
        for stdin in ("[]", '"text"', "3"):
            with self.subTest(stdin=stdin):
                code, written = self._run(stdin, "--exit-code", "2", "--mode", "review")
                self.assertEqual(code, 2)
                self.assertEqual(written, "review-required=false\n")

    def test_every_option_is_required(self):
        review = _action_review()
        full = {"--exit-code": "2", "--mode": "review", "--output": "out"}
        for missing in full:
            argv = [x for k, v in full.items() if k != missing for x in (k, v)]
            with self.subTest(missing=missing), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stop:
                    review.main(argv)
                self.assertEqual(stop.exception.code, 2)


@unittest.skipUnless(HAS_GIT, "needs git")
class TestARefusedBaselineIsNeverMergeable(unittest.TestCase):
    def setUp(self):
        for name in ("ADVERSARY_GATE_POLICY", "ADVERSARY_GATE_BASE_REF", "ADVERSARY_GATE_PYTHON",
                     "ADVERSARY_GATE_REQUIRE_PINNED_BASE", "ADVERSARY_GATE_ALLOW_LOCAL_PIN"):
            patcher = mock.patch.dict(os.environ)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(name, None)

    def _repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        (repo / "calc.py").write_text("X = 1\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        return repo

    def test_a_refused_pin_is_not_mergeable(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(Path(directory))
            for pin in ("HEAD", "no-such-ref"):
                with self.subTest(pin=pin), mock.patch.dict(os.environ, {"ADVERSARY_GATE_BASE_REF": pin}), \
                        mock.patch.object(mcp_server, "prepare") as never:
                    result = mcp_server.verify_repo(str(repo))
                never.assert_not_called()
                self.assertEqual(result["decision"], "inconclusive")
                self.assertIs(result["mergeable"], False)
                self.assertEqual(result["next_steps"][0]["action"], "see_reason")

    def test_a_pin_that_names_nothing_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repo(Path(directory))
            _, refused = mcp_server._operator_base(repo, "no-such-ref")
        self.assertIn("no ref (HEAD, or a name git does not know)", refused)

    def test_a_repository_with_no_commit_checked_out_takes_the_remote_tip(self):
        """HEAD names no commit yet (a fresh clone before checkout): the base
        is the remote tip itself, so it is taken, not an error."""
        with tempfile.TemporaryDirectory() as directory:
            source = self._repo(Path(directory))
            fresh = Path(directory, "fresh")
            fresh.mkdir()
            _git(fresh, "init", "-q")
            _git(fresh, "fetch", "-q", str(source), "HEAD:refs/remotes/origin/main")
            tip = subprocess.run(["git", "-C", str(fresh), "rev-parse", "origin/main"],
                                 capture_output=True, text=True).stdout.strip()
            commit, refused = mcp_server._agent_base(fresh, "origin/main")
        self.assertEqual((commit, refused), (tip, ""))


@unittest.skipUnless(HAS_GIT, "needs git")
class TestTheEvidenceTakesWhatACommitWouldHold(unittest.TestCase):
    def test_bytecode_caches_are_not_new_files_and_a_nested_workdir_is_made(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            (repo / "calc.py").write_text("X = 1\n")
            _git(repo, "init", "-q")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "base")
            (repo / "__pycache__").mkdir()
            (repo / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\0cache")
            (repo / "added.py").write_text("Y = 2\n")
            evidence = prepare(repo, "HEAD", root / "not" / "yet" / "made",
                               python=sys.executable, coverage=False)
        self.assertEqual(evidence.untracked, ["added.py"])



class TestAnEnvironmentInThePatchAddsNoTests(unittest.TestCase):
    """An installed environment ships test files of its own; they are not tests
    the patch added, and taking them out of the oracle tree breaks nothing."""

    def test_a_directory_with_pyvenv_cfg_is_not_walked(self):
        from adversary_gate.core.gate import added_test_files
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                (root / side / "tests").mkdir(parents=True)
                (root / side / "tests" / "test_calc.py").write_text("def test_a():\n    pass\n")
            (root / "patch" / "tests" / "test_new.py").write_text("def test_b():\n    pass\n")
            env = root / "patch" / "env"
            (env / "lib" / "tests").mkdir(parents=True)
            (env / "pyvenv.cfg").write_text("home = /usr/bin\n")
            (env / "lib" / "tests" / "test_vendored.py").write_text("def test_c():\n    pass\n")
            self.assertEqual(added_test_files(root / "baseline", root / "patch"), ["tests/test_new.py"])
            (env / "pyvenv.cfg").unlink()
            self.assertIn("env/lib/tests/test_vendored.py",
                          added_test_files(root / "baseline", root / "patch"))


class TestASmallChangeIsNamedOnlyWhenEverythingElseHeld(unittest.TestCase):
    """The review hand-off names the one INCONCLUSIVE where every claim held,
    coverage met and the whole suite passed on both sides."""

    MUTATION = {"measured": True, "mutants_counted": 1, "mutants_killed": 1, "interval": [0.2, 1.0]}

    def _review(self, full_codes, *, outcomes=("verified",), coverage_ratio=1.0):
        import argparse
        import types
        from adversary_gate.core.types import Decision, Outcome
        args = argparse.Namespace(suite_strength_floor=0.75, strength_confidence=0.80, coverage_floor=0.8)
        verdicts = [types.SimpleNamespace(outcome=Outcome(o)) for o in outcomes]
        return cli._small_change_review(
            Decision.INCONCLUSIVE, verdicts, full_codes, args,
            coverage_ratio=coverage_ratio, mutation=dict(self.MUTATION), fix_proven=True, unjudged=False)

    def test_no_claim_a_claim_that_did_not_hold_or_short_coverage_drops_it(self):
        for gap in ({"outcomes": ()}, {"outcomes": ("verified", "refuted")},
                    {"outcomes": ("unverified",)}, {"coverage_ratio": None},
                    {"coverage_ratio": 0.5}):
            with self.subTest(**gap):
                self.assertIsNone(self._review((0, 0), **gap))

    def test_a_suite_that_passed_on_both_sides_keeps_the_review(self):
        for full_codes in (None, (0, 0), [0, 0]):
            with self.subTest(full_codes=full_codes):
                review = self._review(full_codes)
                self.assertIsNotNone(review)
                self.assertEqual(review["kind"], "small_change")
                self.assertEqual(review["mutants_killed"], 1)
                self.assertIn("but 1 of 1 is too few", review["why"])

    def test_a_suite_that_failed_on_either_side_drops_it(self):
        for full_codes in ((0, 1), (1, 0)):
            with self.subTest(full_codes=full_codes):
                self.assertIsNone(self._review(full_codes))


class TestWhatTheCopyAndTheOracleLeaveOut(unittest.TestCase):
    def test_git_and_cache_directories_are_not_the_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            original, copy = Path(directory) / "original", Path(directory) / "copy"
            for rel in (".git/HEAD", "__pycache__/calc.cpython-312.opt", ".pytest_cache/v/lastfailed",
                        "pkg/__pycache__/notes.txt", "calc.py"):
                (original / rel).parent.mkdir(parents=True, exist_ok=True)
                (original / rel).write_text("x\n")
            copy.mkdir()
            (copy / "calc.py").write_text("x\n")
            self.assertEqual(cli._changed_in_copy(original, copy), [])
            (copy / "calc.py").write_text("y\n")
            self.assertEqual(cli._changed_in_copy(original, copy), ["calc.py"])

    def test_no_oracle_record_when_the_patch_kept_every_test_file(self):
        self.assertIsNone(cli._full_oracle([], [], 0))
        self.assertEqual(cli._full_oracle([], ["tests/test_new.py"], 1),
                         {"source": "baseline", "rewritten_test_files": [],
                          "added_test_files": ["tests/test_new.py"], "exit_code": 1})


if __name__ == "__main__":
    unittest.main()
