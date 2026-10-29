"""A pinned baseline is a commit id or one remote-tracking ref (3.0.18)."""

from __future__ import annotations

import importlib.util
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

from adversary_gate.integrations.gitevidence import EvidenceError
from adversary_gate.integrations import mcp_server

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
class TestAPinIsTheOperators(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT / "src")})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("ADVERSARY_GATE_POLICY", "ADVERSARY_GATE_BASE_REF", "ADVERSARY_GATE_PYTHON",
                     "ADVERSARY_GATE_REQUIRE_PINNED_BASE"):
            os.environ.pop(name, None)

    def _git(self, repo, *args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env={**os.environ, **GIT_ENV})

    def test_a_pin_on_a_local_branch_is_not_the_agents_to_move(self):
        """A pin on a local ``main`` took any commit the agent made there as the
        oracle -- while the same ``main`` unpinned was refused for not being
        pushed (3.0.18)."""
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            self._git(repo, "branch", "-M", "main")
            (repo / "test_calc.py").write_text("def test_ops():\n    pass\n")
            self._git(repo, "commit", "-qam", "weaken")
            (repo / "calc.py").write_text(CALC.replace("return a + b", "return a - b"))
            with mock.patch.dict(os.environ, {"ADVERSARY_GATE_BASE_REF": "main"}), \
                    mock.patch.object(mcp_server, "prepare") as never:
                result = mcp_server.verify_repo(str(repo))
        never.assert_not_called()
        self.assertEqual(result["decision"], "inconclusive")
        self.assertIn("remote-tracking", result["reason"])

    def test_a_local_tag_does_not_shadow_a_pinned_remote_ref(self):
        """git's lookup tries tags and branches before remotes: a local tag
        named ``origin/main`` would have been the oracle."""
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            (repo / "test_calc.py").write_text("def test_ops():\n    pass\n")
            self._git(repo, "commit", "-qam", "weaken")
            self._git(repo, "tag", "origin/main")
            with mock.patch.dict(os.environ, {"ADVERSARY_GATE_BASE_REF": "origin/main"}), \
                    mock.patch.object(mcp_server, "prepare") as never:
                result = mcp_server.verify_repo(str(repo))
        never.assert_not_called()
        self.assertEqual(result["decision"], "inconclusive")
        self.assertIn("refs/tags/origin/main", result["reason"])

    def test_a_pin_by_commit_id_or_with_local_pins_allowed_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            (repo / "calc.py").write_text(_clean_patch(CALC))
            with mock.patch.dict(os.environ, {"ADVERSARY_GATE_BASE_REF": head}):
                by_id = mcp_server.verify_repo(str(repo))
            with mock.patch.dict(os.environ, {"ADVERSARY_GATE_BASE_REF": "HEAD",
                                              "ADVERSARY_GATE_ALLOW_LOCAL_PIN": "1"}):
                allowed = mcp_server.verify_repo(str(repo))
                policy = mcp_server.gate_policy()
        self.assertEqual(by_id["decision"], "merge", by_id["reason"])
        self.assertEqual(allowed["decision"], "merge", allowed["reason"])
        self.assertIn("ALLOW_LOCAL_PIN", policy["baseline_pin"])

    def test_an_older_pushed_commit_is_not_the_agents_baseline(self):
        """Pushed was enough: the root commit, from before a test existed or was
        tightened, judged the patch (3.0.18)."""
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            root_commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                         capture_output=True, text=True).stdout.strip()
            (repo / "extra.py").write_text("X = 1\n")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-qm", "more")
            self._git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            with mock.patch.object(mcp_server, "prepare") as never:
                older = mcp_server.verify_repo(str(repo), base_ref=root_commit)
            never.assert_not_called()
            with mock.patch.object(mcp_server, "prepare",
                                   side_effect=EvidenceError("stop")) as reached:
                mcp_server.verify_repo(str(repo), base_ref="origin/main")
                # HEAD behind the remote tip: the fork point is HEAD itself.
                self._git(repo, "reset", "-q", "--hard", root_commit)
                mcp_server.verify_repo(str(repo))
        self.assertEqual(older["decision"], "inconclusive")
        self.assertIn("older than", older["reason"])
        self.assertEqual(reached.call_count, 2)


if __name__ == "__main__":
    unittest.main()
