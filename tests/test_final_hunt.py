"""What the 3.0.18 final hunt found, each with the case that must still pass.

- A patch that also deletes source, or changes source the engine cannot
  mutate, is never a small change for a person to sign.
- A test appended over a name the baseline file imports is not an append.
- Jev's client does not follow a redirect with the key.
- The Action's coverage pass may not rewrite the tree the gate then judges.
- A file move reaches the diff as a delete and an add, not as a rename.
- An untracked file name with a control character is refused.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import _only_appends_tests, _too_few_mutants, main  # noqa: E402
from adversary_gate.integrations import coverage_pass, gitevidence  # noqa: E402
from adversary_gate.integrations.jev import JevClient, TriageError  # noqa: E402

HAS_GIT = shutil.which("git") is not None
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}


def _tmp(test: unittest.TestCase) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ag-hunt-"))
    test.addCleanup(shutil.rmtree, root, True)
    return root


class TestAnUnmeasuredSourceIsNeverASmallChange(unittest.TestCase):
    SMALL = {"measured": True, "mutants_counted": 1, "mutants_killed": 1, "interval": [0.3784, 1.0]}

    def _args(self):
        return argparse.Namespace(suite_strength_floor=0.75, strength_confidence=0.80)

    def test_a_deleted_or_foreign_file_drops_the_small_change(self):
        self.assertIsNotNone(_too_few_mutants(self.SMALL, self._args()))
        for key, files in (("deleted_files", ["old.py"]), ("foreign_changed_files", ["calc.cpp"])):
            with self.subTest(key):
                self.assertIsNone(_too_few_mutants({**self.SMALL, key: files}, self._args()))

    def test_a_bound_already_at_the_floor_is_not_a_small_change(self):
        at_floor = {**self.SMALL, "interval": [0.75, 1.0]}
        self.assertIsNone(_too_few_mutants(at_floor, self._args()))

    def test_a_fix_that_also_deletes_a_module_asks_for_no_small_change_review(self):
        bug, fixed = "def sub(a, b):\n    return a + b\n", "def sub(a, b):\n    return a - b\n"
        tests = "from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
        root = _tmp(self)
        for side, calc in (("baseline", bug), ("patch", fixed)):
            (root / side).mkdir()
            (root / side / "calc.py").write_text(calc)
            (root / side / "test_calc.py").write_text(tests)
        (root / "baseline" / "old.py").write_text("def unused():\n    return 1\n")
        diff = "".join(difflib.unified_diff(bug.splitlines(True), fixed.splitlines(True),
                                            "a/calc.py", "b/calc.py"))
        diff += "--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def unused():\n-    return 1\n"
        (root / "change.diff").write_text(diff)
        (root / "cov.json").write_text(json.dumps(
            {"files": {"calc.py": {"executed_lines": [1, 2], "contexts": {"2": ["test_calc.test_sub"]}}}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(["--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                  "--max-rounds", "1", "--rounds-used", "1", "--full-suite-path", "",
                  "--claim", "test_calc.py::test_sub", "--diff", str(root / "change.diff"),
                  "--coverage-json", str(root / "cov.json")])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["decision"], "inconclusive")
        self.assertEqual(payload["mutation"]["deleted_files"], ["old.py"])
        self.assertIsNone(payload.get("human_review"))


class TestAnImportedNameIsKnown(unittest.TestCase):
    def _appends(self, old: str, tail: str) -> bool:
        root = _tmp(self)
        for side, text in (("b", old), ("p", old + tail)):
            (root / side).mkdir()
            (root / side / "test_a.py").write_text(text)
        return _only_appends_tests(root / "b", root / "p", "test_a.py")

    def test_a_test_over_an_imported_name_is_not_an_append(self):
        for name, old, tail in (
            ("from import", "from helpers import test_core\n", "\n\ndef test_core():\n    pass\n"),
            ("import as", "import helpers as test_core\n", "\n\ndef test_core():\n    pass\n"),
            ("dotted import", "import tests.common\n", "\n\ndef tests():\n    pass\n"),
            ("star import", "from helpers import *\n", "\n\ndef test_new():\n    pass\n"),
        ):
            with self.subTest(name):
                self.assertFalse(self._appends(old, tail))

    def test_a_new_name_next_to_imports_is_still_an_append(self):
        old = "from calc import sub\nimport os.path\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"
        self.assertTrue(self._appends(old, "\n\ndef test_sub_zero():\n    assert sub(0, 0) == 0\n"))


class TestJevDoesNotFollowARedirect(unittest.TestCase):
    def test_a_redirect_is_refused_and_the_key_goes_nowhere(self):
        seen = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)

        class Hop(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{sink.server_port}/")
                self.end_headers()

            def log_message(self, *args):
                pass

        hop = http.server.HTTPServer(("127.0.0.1", 0), Hop)
        for server in (sink, hop):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        client = JevClient(api_key="k", endpoint=f"http://127.0.0.1:{hop.server_port}/")
        with self.assertRaises(TriageError) as caught:
            client.ask({}, {})
        self.assertIn("HTTP 302", str(caught.exception))
        self.assertEqual(seen, [])


    def test_the_handler_raises_instead_of_building_the_next_request(self):
        import urllib.error
        import urllib.request
        from adversary_gate.integrations.jev import _RefuseRedirects

        request = urllib.request.Request("http://127.0.0.1/", headers={"Authorization": "Bearer k"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            _RefuseRedirects().redirect_request(request, None, 302, "Found", {}, "http://remote.example/")
        self.assertEqual(caught.exception.code, 302)


class TestTheActionsCoveragePassLeavesTheTreeAlone(unittest.TestCase):
    WRITE = "{}"

    def _pass(self, test_body: str):
        repo = _tmp(self) / "repo"
        repo.mkdir()
        (repo / "calc.py").write_text("def sub(a, b):\n    return a + b\n")
        for name in ("a.py", "b.py", "c.py"):
            (repo / name).write_text("X = 1\n")
        (repo / "test_calc.py").write_text("import pathlib\nHERE = pathlib.Path(__file__).parent\n"
                                           + test_body + "\n\ndef test_ok():\n    assert True\n")
        out = repo.parent / "cov"
        command = (f"{sys.executable} -c \"import json, os, sys, pytest; "
                   f"open(os.environ['COVERAGE_JSON'], 'w').write('{self.WRITE}'); "
                   "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider']))\"")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = coverage_pass.main(["--repo", str(repo), "--out-dir", str(out), "--command", command,
                                       "--python", sys.executable, "--sandbox", "none"])
        return code, err.getvalue()

    def test_a_test_that_rewrites_or_adds_judged_files_fails_the_pass(self):
        for name, body, shown in (
            ("rewrite", "(HERE / 'calc.py').write_text('def sub(a, b):\\n    return a - b\\n')", "calc.py"),
            ("delete", "(HERE / 'a.py').unlink()", "a.py"),
            ("new conftest", "(HERE / 'conftest.py').write_text('')", "conftest.py"),
            ("new link", "(HERE / 'conftest.py').symlink_to(HERE / 'a.py')", "conftest.py"),
            ("swapped for a link", "(HERE / 'a.py').unlink(); (HERE / 'a.py').symlink_to(HERE / 'b.py')",
             "a.py"),
            ("many", "[(HERE / n).write_text('X = 2\\n') for n in ('a.py', 'b.py', 'c.py', 'calc.py')]",
             "a.py, b.py, c.py (+1 more)"),
        ):
            with self.subTest(name):
                code, err = self._pass(body)
                self.assertEqual(code, 4, err)
                self.assertIn(f"changed the tree the gate judges ({shown})", err)

    def test_caches_and_new_data_files_are_not_the_tree(self):
        body = ("(HERE / '__pycache__').mkdir(exist_ok=True)\n"
                "(HERE / '__pycache__' / 'x.py').write_text('')\n"
                "(HERE / 'report.txt').write_text('ok')")
        code, err = self._pass(body)
        self.assertEqual(code, 0, err)
        self.assertNotIn("changed the tree", err)


@unittest.skipUnless(HAS_GIT, "needs git")
class TestTheEvidenceDiffNamesBothSidesOfAMove(unittest.TestCase):
    def _repo(self) -> Path:
        repo = _tmp(self) / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "conftest.py").write_text("import pytest\n")
        (repo / "test_a.py").write_text("def test_a():\n    assert True\n")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"],
                     ["update-ref", "refs/remotes/origin/main", "HEAD"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, env={**os.environ, **GIT_ENV})
        return repo

    def test_a_moved_file_is_a_delete_and_an_add(self):
        repo = self._repo()
        subprocess.run(["git", "-C", str(repo), "-c", "diff.renames=true", "mv", "tests/conftest.py",
                        "conftest.py"], check=True, env={**os.environ, **GIT_ENV})
        evidence = gitevidence.prepare(repo, "HEAD", repo.parent / "work", python=sys.executable,
                                       coverage=False)
        diff = evidence.diff.read_text()
        self.assertNotIn("rename from", diff)
        self.assertIn("--- a/tests/conftest.py", diff)
        self.assertIn("+++ b/conftest.py", diff)

    def test_an_untracked_name_with_a_newline_is_refused(self):
        repo = self._repo()
        (repo / "docs").mkdir()
        (repo / "docs" / "note.md\nhelper.py").write_text("X = 1\n")
        with self.assertRaises(gitevidence.EvidenceError) as caught:
            gitevidence.prepare(repo, "HEAD", repo.parent / "work", python=sys.executable, coverage=False)
        self.assertIn("control character", str(caught.exception))

    def test_an_untracked_name_with_del_is_refused(self):
        repo = self._repo()
        (repo / "note\x7f.py").write_text("X = 1\n")
        with self.assertRaises(gitevidence.EvidenceError) as caught:
            gitevidence.prepare(repo, "HEAD", repo.parent / "work", python=sys.executable, coverage=False)
        self.assertIn("control character", str(caught.exception))

    def test_an_untracked_name_with_a_space_is_kept(self):
        repo = self._repo()
        (repo / "my note.py").write_text("X = 1\n")
        evidence = gitevidence.prepare(repo, "HEAD", repo.parent / "work", python=sys.executable,
                                       coverage=False)
        self.assertIn("my note.py", evidence.diff.read_text())

    def test_the_actions_script_asks_for_no_renames_on_both_diffs(self):
        script = (ROOT / "scripts" / "prepare_evidence.sh").read_text()
        self.assertEqual(script.count("diff --no-ext-diff --no-renames --no-textconv"), 2)


if __name__ == "__main__":
    unittest.main()
