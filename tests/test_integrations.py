"""Hermes (MCP) and Jev (triage) integrations.

The invariant under test everywhere below: nothing an integration says can make
the gate more lenient. Jev can only raise floors; the agent can only name
evidence, never policy.

Jev is exercised against a local server speaking TypeSafe's published shape --
not against the live API, which is early-access.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, EXIT_MERGE, main
from adversary_gate.integrations import jev
from adversary_gate.integrations.gitevidence import EvidenceError, prepare
from adversary_gate.integrations import mcp_server

HAS_COVERAGE = importlib.util.find_spec("coverage") is not None
HAS_MCP = importlib.util.find_spec("mcp") is not None
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


# ----------------------------------------------------------------------
# a fake Jev
# ----------------------------------------------------------------------
class _FakeJev:
    def __init__(self, answer=None, status=200):
        self.answer = answer
        self.status = status
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fake.requests.append({"headers": dict(self.headers), "body": json.loads(body)})
                data = json.dumps(fake.answer).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _answer(risk="high", confidence=0.9, contract=0.2):
    return {
        "answers": {
            "risk": {"choice": risk, "confidence": confidence, "probabilities": {}},
            "contract_change": {"noul": contract, "probabilities": {}},
        },
        "model": "jev-1.13.0",
    }


class TestJevClient(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeJev(_answer())
        self.addCleanup(self.fake.close)
        self.client = jev.JevClient(api_key="sk-test", endpoint=self.fake.url)

    def test_request_shape(self):
        jev.triage_diff("+print('hi')\n", self.client)
        request = self.fake.requests[-1]
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(set(request["body"]["questions"]), {"risk", "contract_change"})
        self.assertEqual(request["body"]["questions"]["risk"]["type"], "choice")
        self.assertEqual(request["body"]["questions"]["contract_change"]["type"], "noul")
        self.assertIn("print('hi')", request["body"]["state"]["diff"])

    def test_answer_is_parsed(self):
        triage = jev.triage_diff("+x = 1\n", self.client)
        self.assertEqual((triage.risk, triage.confidence, triage.model), ("high", 0.9, "jev-1.13.0"))
        self.assertIsNone(triage.error)

    def test_the_diff_is_truncated_before_it_leaves(self):
        triage = jev.triage_diff("+" + "x" * (jev.MAX_STATE_BYTES * 2), self.client)
        self.assertTrue(triage.truncated)
        self.assertLessEqual(len(self.fake.requests[-1]["body"]["state"]["diff"].encode()), jev.MAX_STATE_BYTES)

    def test_errors_are_recorded_never_raised(self):
        for answer, status in ((_answer(risk="catastrophic"), 200), ({"oops": 1}, 200), (_answer(), 500)):
            with self.subTest(status=status, answer=answer):
                self.fake.answer, self.fake.status = answer, status
                triage = jev.triage_diff("+x\n", self.client)
                self.assertIsNotNone(triage.error)
                self.assertEqual(jev.escalation(triage, {"strength_confidence": 0.8, "coverage_floor": 0.8}), {})

    def test_no_key_and_no_plain_http_to_remote_hosts(self):
        self.assertIsNotNone(jev.triage_diff("+x\n", jev.JevClient(api_key="", endpoint=self.fake.url)).error)
        remote = jev.JevClient(api_key="sk", endpoint="http://example.com/v1/systemone")
        self.assertIn("non-HTTPS", jev.triage_diff("+x\n", remote).error)


class TestEscalationOnlyGoesUp(unittest.TestCase):
    CURRENT = {"strength_confidence": 0.80, "coverage_floor": 0.80}

    def _t(self, risk, confidence):
        return jev.Triage(risk=risk, confidence=confidence)

    def test_confident_high_raises_both(self):
        self.assertEqual(jev.escalation(self._t("high", 0.9), self.CURRENT),
                         {"strength_confidence": 0.95, "coverage_floor": 0.90})

    def test_anything_else_changes_nothing(self):
        for risk, confidence in (("high", 0.5), ("medium", 0.99), ("low", 0.99), (None, None)):
            with self.subTest(risk=risk, confidence=confidence):
                self.assertEqual(jev.escalation(self._t(risk, confidence), self.CURRENT), {})

    def test_never_lowers_and_never_reenables(self):
        strict = {"strength_confidence": 0.99, "coverage_floor": 0.95}
        self.assertEqual(jev.escalation(self._t("high", 0.9), strict), {})
        disabled = {"strength_confidence": 0.8, "coverage_floor": 0.0}
        self.assertNotIn("coverage_floor", jev.escalation(self._t("high", 0.9), disabled))


class TestTriageThroughTheCli(unittest.TestCase):
    """The same clean 5-operator patch: MERGE, unless Jev confidently calls it risky."""

    def _tree(self, root: Path):
        for side, text in (("baseline", CALC), ("patch", _clean_patch(CALC))):
            (root / side).mkdir()
            (root / side / "calc.py").write_text(text)
            (root / side / "test_calc.py").write_text(TESTS)
        base, patch = CALC.splitlines(), _clean_patch(CALC).splitlines()
        lines = [i + 1 for i, (a, b) in enumerate(zip(base, patch)) if a != b]
        (root / "change.diff").write_text("".join(
            f"--- a/calc.py\n+++ b/calc.py\n@@ -{n},1 +{n},1 @@\n-{base[n - 1]}\n+{patch[n - 1]}\n" for n in lines
        ))
        executed = sorted({n for line in lines for n in (line - 1, line)})
        (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": executed}}}))

    def _run(self, root: Path, fake: _FakeJev):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main([
                "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                "--test-path", "test_calc.py", "--test-id", "test_ops",
                "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                "--full-suite-path", "", "--triage", "jev", "--triage-endpoint", fake.url,
            ])
        return code, json.loads(out.getvalue())

    def test_low_risk_keeps_the_default_policy(self):
        fake = _FakeJev(_answer(risk="low", confidence=0.99))
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as directory:
            self._tree(Path(directory))
            code, payload = self._run(Path(directory), fake)
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertFalse(payload["execution"]["triage"]["escalated"])

    def test_confident_high_risk_tightens_until_five_mutants_are_not_enough(self):
        fake = _FakeJev(_answer(risk="high", confidence=0.9))
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as directory:
            self._tree(Path(directory))
            code, payload = self._run(Path(directory), fake)
        self.assertEqual(code, EXIT_INCONCLUSIVE, payload["decision"])
        self.assertEqual(payload["suite_strength_confidence"], 0.95)
        self.assertEqual(payload["execution"]["triage"]["applied"],
                         {"strength_confidence": 0.95, "coverage_floor": 0.90})

    def test_jev_down_is_recorded_and_changes_nothing(self):
        fake = _FakeJev(_answer(), status=503)
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as directory:
            self._tree(Path(directory))
            code, payload = self._run(Path(directory), fake)
        self.assertEqual(code, EXIT_MERGE, payload)
        self.assertIn("503", payload["execution"]["triage"]["error"])


# ----------------------------------------------------------------------
# git evidence + the MCP tool
# ----------------------------------------------------------------------
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}


def _git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(CALC)
    (repo / "test_calc.py").write_text(TESTS)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env={**os.environ, **GIT_ENV})
    return repo


@unittest.skipUnless(HAS_GIT and HAS_COVERAGE, "needs git and coverage")
class TestGitEvidence(unittest.TestCase):
    def test_working_tree_untracked_files_and_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            (repo / "calc.py").write_text(_clean_patch(CALC))
            (repo / "extra.py").write_text("def twice(x):\n    return 2 * x\n")
            evidence = prepare(repo, "HEAD", root / "work", python=sys.executable)
            self.assertEqual((evidence.baseline / "calc.py").read_text(), CALC)
            diff = evidence.diff.read_text()
            self.assertIn("+    return (a + b)", diff)
            self.assertIn("+++ b/extra.py", diff)
            self.assertEqual(evidence.untracked, ["extra.py"])
            report = json.loads(evidence.coverage.read_text())
            self.assertTrue(report["files"]["calc.py"]["contexts"])
            self.assertFalse((repo / ".coverage").exists())  # nothing written into the repo
            status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                    capture_output=True, text=True).stdout
            self.assertEqual(sorted(status.split()), sorted(["M", "calc.py", "??", "extra.py"]))

    def test_unknown_ref_is_an_evidence_error(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            with self.assertRaises(EvidenceError):
                prepare(repo, "no-such-ref", Path(directory) / "w", python=sys.executable)


@unittest.skipUnless(HAS_GIT and HAS_COVERAGE, "needs git and coverage")
class TestVerifyRepoTool(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT / "src")})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("ADVERSARY_GATE_POLICY", "ADVERSARY_GATE_BASE_REF", "ADVERSARY_GATE_PYTHON"):
            os.environ.pop(name, None)

    def test_clean_change_merges_and_names_who_chose_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            (repo / "calc.py").write_text(_clean_patch(CALC))
            result = mcp_server.verify_repo(str(repo))
        self.assertEqual(result["decision"], "merge", result["reason"])
        self.assertTrue(result["mergeable"])
        self.assertEqual(result["baseline_chosen_by"], "agent")
        self.assertEqual(result["claims"][0]["test_id"], "test_ops")

    def test_a_bug_plus_a_bent_test_is_blocked(self):
        """The Hermes failure mode: break the code, rewrite the test to agree."""
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            (repo / "calc.py").write_text(CALC.replace("return a + b", "return a + b + 1"))
            (repo / "test_calc.py").write_text(TESTS.replace("add(2, 3) == 5", "add(2, 3) == 6"))
            result = mcp_server.verify_repo(str(repo), claims=["test_calc.py::test_ops"])
        self.assertEqual(result["decision"], "block", result)
        self.assertEqual(result["claims"][0]["oracle"], "baseline")

    def test_the_agent_cannot_set_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            refused = mcp_server.verify_repo(str(repo), pytest_args=["-p", "evil"])
            self.assertEqual(refused["decision"], "inconclusive")
            self.assertIn("options", refused["reason"])
            with mock.patch.dict(os.environ, {"ADVERSARY_GATE_POLICY": "--diff /tmp/x"}):
                self.assertIn("evidence flags", mcp_server.verify_repo(str(repo))["reason"])

    def test_a_pinned_baseline_wins_over_the_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            (repo / "calc.py").write_text(_clean_patch(CALC))
            with mock.patch.dict(os.environ, {"ADVERSARY_GATE_BASE_REF": "HEAD"}):
                result = mcp_server.verify_repo(str(repo), base_ref="no-such-ref")
                policy = mcp_server.gate_policy()
        self.assertEqual(result["baseline_chosen_by"], "operator")
        self.assertEqual(result["decision"], "merge", result["reason"])
        self.assertNotIn("base_ref", policy["agent_controls"])


@unittest.skipUnless(HAS_GIT and HAS_COVERAGE and HAS_MCP, "needs git, coverage and the MCP SDK")
class TestOverTheMcpProtocol(unittest.TestCase):
    """What Hermes actually does: spawn the server over stdio and call the tool."""

    def test_list_and_call(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def scenario(repo: Path):
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "adversary_gate.integrations.mcp_server"],
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    result = await session.call_tool("verify_repo", {"repo": str(repo)})
                    return tools, result

        with tempfile.TemporaryDirectory() as directory:
            repo = _git_repo(Path(directory))
            (repo / "calc.py").write_text(_clean_patch(CALC))
            tools, result = asyncio.run(scenario(repo))
        self.assertTrue({"verify_repo", "gate_policy"} <= tools)
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if structured is None:
            structured = json.loads(result.content[0].text)
        structured = structured.get("result", structured)
        self.assertEqual(structured["decision"], "merge", structured.get("reason"))


class TestHermesFiles(unittest.TestCase):
    def test_the_skill_and_config_ship_and_print(self):
        for flag, needle in (("--print-hermes-skill", "name: adversary-gate"),
                             ("--print-hermes-config", "ADVERSARY_GATE_BASE_REF")):
            with self.subTest(flag=flag):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(mcp_server.main([flag]), 0)
                self.assertIn(needle, out.getvalue())


if __name__ == "__main__":
    unittest.main()
