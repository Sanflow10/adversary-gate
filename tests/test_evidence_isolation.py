"""The MCP's coverage pass cannot reach what the gate later reads (3.0.18)."""

from __future__ import annotations

import importlib.util
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

from adversary_gate.integrations.gitevidence import EvidenceError, prepare

HAS_COVERAGE = importlib.util.find_spec("coverage") is not None
HAS_GIT = shutil.which("git") is not None

#: Five operations, five killable mutants: MERGE at the default 80 % interval.
CALC = "".join(
    f"def {name}(a, b):\n    return a {op} b\n\n\n"
    for name, op in (("add", "+"), ("sub", "-"), ("mul", "*"), ("div", "/"), ("mod", "%"))
)
TESTS = (
    "from calc import add, sub, mul, div, mod\n\n\n"
    "def test_ops():\n"
    "    assert add(2, 3) == 5\n    assert sub(5, 3) == 2\n    assert mul(3, 4) == 12\n"
    "    assert div(8, 2) == 4\n    assert mod(7, 3) == 1\n"
)


def _clean_patch(text: str) -> str:
    for op in ("+", "-", "*", "/", "%"):
        text = text.replace(f"return a {op} b", f"return (a {op} b)")
    return text


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}


def _git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(CALC)
    (repo / "test_calc.py").write_text(TESTS)
    # The base is "pushed": an agent-chosen baseline must be on a
    # remote-tracking branch (3.0.11).
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"],
                 ["update-ref", "refs/remotes/origin/main", "HEAD"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env={**os.environ, **GIT_ENV})
    return repo


@unittest.skipUnless(HAS_GIT and HAS_COVERAGE, "needs git and coverage")
class TestTheCoveragePassIsIsolated(unittest.TestCase):
    def test_the_coverage_pass_cannot_rewrite_the_oracle_or_the_judged_tree(self):
        """Until 3.0.18 the patch's tests ran in the tree the gate then judged,
        with the whole scratch directory writable -- under bwrap too -- and
        ``COVERAGE_FILE`` named it. A new test rewrote the baseline's copy of a
        test and the patch's code before the gate read either."""
        probe = (
            "import os, pathlib\n\n\n"
            "def test_probe():\n"
            "    work = pathlib.Path(os.environ['COVERAGE_FILE']).parent\n"
            "    for rel, text in (('baseline/test_calc.py', 'def test_ops():\\n    pass\\n'),\n"
            "                      ('patch/calc.py', 'REWRITTEN = 1\\n')):\n"
            "        if (work / rel).is_file():\n"
            "            (work / rel).write_text(text)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            (repo / "calc.py").write_text(_clean_patch(CALC))
            (repo / "test_probe.py").write_text(probe)
            evidence = prepare(repo, "HEAD", root / "work", python=sys.executable)
            self.assertEqual((evidence.baseline / "test_calc.py").read_text(), TESTS)
            self.assertEqual((evidence.patch / "calc.py").read_text(), _clean_patch(CALC))

    def test_the_extractor_imports_nothing_the_coverage_pass_wrote(self):
        """The report is extracted with ``python -c``, outside any sandbox, and
        ``-c`` puts the working directory first on sys.path. Until 3.0.18 that
        directory was the one the patch's tests could write: a ``json.py``
        planted there ran in the gate's own process. (``coverage``, not ``json``:
        a ``.pth`` file may have imported ``json`` before the path is searched.)"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "planted-module-ran"
            probe = (
                "import os, pathlib\n\n\n"
                "def test_probe():\n"
                "    work = pathlib.Path(os.environ['COVERAGE_FILE']).parent\n"
                f"    (work / 'coverage.py').write_text(\"open({str(marker)!r}, 'w').close()\\n\")\n"
            )
            repo = _git_repo(root)
            (repo / "calc.py").write_text(_clean_patch(CALC))
            (repo / "test_probe.py").write_text(probe)
            evidence = prepare(repo, "HEAD", root / "work", python=sys.executable)
            self.assertFalse(marker.exists())
            self.assertIn("calc.py", json.loads(evidence.coverage.read_text())["files"])

    def test_a_scratch_path_made_while_the_tests_ran_is_refused(self):
        """Without a sandbox the tests can write anywhere the user can: a
        baseline directory that exists before the gate writes it is not one
        the gate wrote."""
        probe = (
            "import os, pathlib\n\n\n"
            "def test_probe():\n"
            "    work = pathlib.Path(os.environ['COVERAGE_FILE']).parents[2]\n"
            "    (work / 'baseline').mkdir(exist_ok=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            (repo / "test_probe.py").write_text(probe)
            with self.assertRaisesRegex(EvidenceError, "already exists"):
                prepare(repo, "HEAD", root / "work", python=sys.executable)

    def test_a_tree_that_changed_after_the_diff_is_refused(self):
        """The judged tree is copied after the coverage pass: a file that changed
        since the diff was taken -- a test of the pass, or another writer --
        would be judged with lines no diff names (3.0.18). Links are compared
        by target."""
        from adversary_gate.integrations import gitevidence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            (repo / "link.py").symlink_to("calc.py")
            (repo / "calc.py").write_text(_clean_patch(CALC))
            same = prepare(repo, "HEAD", root / "same", python=sys.executable, coverage=False)
            self.assertTrue((same.patch / "link.py").is_symlink())
            real_copy = gitevidence._copy_files

            def late_writer(source, dest, rels):
                if dest.name == "patch":
                    (source / "calc.py").write_text(CALC)
                return real_copy(source, dest, rels)

            with mock.patch.object(gitevidence, "_copy_files", side_effect=late_writer):
                with self.assertRaisesRegex(EvidenceError, "changed while the evidence"):
                    prepare(repo, "HEAD", root / "late", python=sys.executable, coverage=False)


    def test_a_file_rewritten_in_the_same_tick_is_in_the_diff(self):
        """git checks a file's bytes only when the file is not older than its
        index; the gate's copy of the index was newer, so a file rewritten at
        the same size in the clock second of ``git add`` was in no diff -- one
        run in a hundred (3.0.18). Pinned here without the race: the times put
        back, ctime left out of git's check (the repository's own setting)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            subprocess.run(["git", "-C", str(repo), "config", "core.trustctime", "false"], check=True)
            past = (1_600_000_000, 1_600_000_000)
            os.utime(repo / "calc.py", past)
            subprocess.run(["git", "-C", str(repo), "update-index", "--really-refresh"],
                           check=True, capture_output=True)
            changed = CALC.replace("return a + b", "return a - b")
            self.assertEqual(len(changed), len(CALC))
            (repo / "calc.py").write_text(changed)
            os.utime(repo / "calc.py", past)
            evidence = prepare(repo, "HEAD", root / "work", python=sys.executable, coverage=False)
            self.assertIn("+    return a - b", evidence.diff.read_text())

if __name__ == "__main__":
    unittest.main()
