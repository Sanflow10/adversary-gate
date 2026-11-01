"""Which rules a decision was made under, and when they are checked (3.0.18).

Kept apart from test_integrations.py, which holds tests whose behaviour 3.0.18
changed on purpose: a new test is judged against the file it lives in."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate import cli
from adversary_gate.cli import main
from adversary_gate.integrations import mcp_server

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


class TestTheReportRecordsItsRules(unittest.TestCase):
    """The clean 5-operator patch of test_integrations, through the CLI with triage."""

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

    def test_the_report_records_the_rules_it_was_judged_by(self):
        """Which floors a decision cleared was not in the report: the coverage
        floor appeared only in a failing reason, and nothing told two runs under
        different rules apart (3.0.18). The policy is recorded as it stood when
        the decision was made -- after triage raised it -- with one hash."""
        reports = {}
        for risk, confidence in (("low", 0.99), ("high", 0.9)):
            fake = _FakeJev(_answer(risk=risk, confidence=confidence))
            self.addCleanup(fake.close)
            with tempfile.TemporaryDirectory() as directory:
                self._tree(Path(directory))
                reports[risk] = self._run(Path(directory), fake)[1]
        low, high = reports["low"], reports["high"]
        self.assertEqual(low["coverage_floor"], 0.80)
        self.assertEqual(high["coverage_floor"], 0.90)
        self.assertEqual(high["policy"]["strength_confidence"], 0.95)
        self.assertEqual(low["policy"]["triage"], "jev")
        self.assertEqual(len(low["policy_sha256"]), 64)
        self.assertNotEqual(low["policy_sha256"], high["policy_sha256"])
        self.assertTrue(low["gate_version"])

    def test_every_option_is_policy_or_evidence(self):
        """A new option has to be put in one list: a deciding one left out of the
        policy would be a rule the report does not show."""
        from adversary_gate import cli
        dests = {action.dest for action in cli.build_parser()._actions}
        self.assertEqual(set(cli.POLICY_OPTIONS) | set(cli.EVIDENCE_OPTIONS), dests)
        self.assertFalse(set(cli.POLICY_OPTIONS) & set(cli.EVIDENCE_OPTIONS))
        args = cli.build_parser().parse_args(
            ["--baseline", "b", "--patch", "p", "--env", "TOKEN=s3cret", "--env", "MODE=x"])
        self.assertEqual(cli._policy(args)["env"], ["MODE", "TOKEN"])  # names, never values

    def test_a_refused_network_sends_nothing_to_triage(self):
        """Triage posted the diff before the network-isolation check refused the
        run: the refusal came after the one call it exists to prevent (3.0.18)."""
        fake = _FakeJev(_answer())
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            env = {key: value for key, value in os.environ.items()
                   if key != "ADVERSARY_NETWORK_ISOLATED"}
            env["TYPESAFE_API_KEY"] = "sk-test"
            with mock.patch.dict(os.environ, env, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    "--test-path", "test_calc.py", "--test-id", "test_ops",
                    "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                    "--triage", "jev", "--triage-endpoint", fake.url,
                    "--require-network-isolation",
                ])
        self.assertEqual(code, 3)
        self.assertEqual(fake.requests, [])

    def test_the_evidence_log_records_the_rules_too(self):
        fake = _FakeJev(_answer(risk="low", confidence=0.99))
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            log = root / "evidence.jsonl"
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"}), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(io.StringIO()):
                main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    "--test-path", "test_calc.py", "--test-id", "test_ops",
                    "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                    "--full-suite-path", "", "--triage", "jev", "--triage-endpoint", fake.url,
                    "--evidence-log", str(log),
                ])
            payload = json.loads(out.getvalue())
            records = [json.loads(line) for line in log.read_text().splitlines()]
        decision = [r for r in records if "policy_sha256" in json.dumps(r)]
        self.assertTrue(decision, records)
        self.assertIn(payload["policy_sha256"], json.dumps(decision[-1]))

    def test_bwrap_missing_is_refused_before_anything_runs(self):
        """The refusal moved above triage (3.0.18); it still answers exit 3."""
        fake = _FakeJev(_answer())
        self.addCleanup(fake.close)
        from adversary_gate import cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            with mock.patch.object(cli, "bwrap_available", return_value=False), \
                    mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    "--test-path", "test_calc.py", "--test-id", "test_ops",
                    "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                    "--triage", "jev", "--triage-endpoint", fake.url, "--sandbox", "bwrap",
                ])
        self.assertEqual(code, 3, err.getvalue())
        self.assertIn("bwrap", err.getvalue())
        self.assertEqual(fake.requests, [])


class TestPolicyFlagsAreSpelledOut(unittest.TestCase):
    def test_an_abbreviated_policy_flag_is_refused(self):
        """The gate's parser took ``--sand bwrap`` for ``--sandbox bwrap``; the
        coverage pass read the exact name only, so it ran outside the sandbox
        the gate then used (3.0.18). Evidence flags abbreviated slipped past the
        refusal the same way."""
        for policy in ("--sand bwrap", "--sandbox=bwrap --evidence-l /tmp/x"):
            with self.subTest(policy=policy), \
                    mock.patch.dict(os.environ, {"ADVERSARY_GATE_POLICY": policy}):
                with self.assertRaisesRegex(ValueError, "spell out"):
                    mcp_server.policy_args()
        with mock.patch.dict(os.environ, {"ADVERSARY_GATE_POLICY":
                                          "--sandbox bwrap --coverage-floor=0.9"}):
            self.assertEqual(mcp_server.policy_args(),
                             ["--sandbox", "bwrap", "--coverage-floor=0.9"])


if __name__ == "__main__":
    unittest.main()


class TestTheReportSaysHowItRan(unittest.TestCase):
    """The execution block and the policy hash, from a run without triage."""

    def _run(self, *extra: str, resolved=None, full_suite=False, shape=None, spy=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            TestTheReportRecordsItsRules._tree(None, root)
            if shape is not None:
                shape(root)
            out = io.StringIO()
            patches = [mock.patch.dict(os.environ, {"ADVERSARY_NETWORK_ISOLATED": "1"})]
            if resolved is not None:
                patches.append(mock.patch.object(cli, "resolve_sandbox", return_value=resolved))
            if spy is not None:
                patches.append(mock.patch.object(cli, spy, wraps=getattr(cli, spy)))
            with contextlib.ExitStack() as stack:
                self.spied = [stack.enter_context(patch) for patch in patches][-1]
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                code = main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    "--test-path", "test_calc.py", "--test-id", "test_ops",
                    "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                    *(() if full_suite else ("--full-suite-path", "")), *extra,
                ])
        return code, json.loads(out.getvalue())

    def test_no_suite_run_records_no_oracle(self):
        def add_a_test(root):
            (root / "patch" / "test_extra.py").write_text("def test_more():\n    assert True\n")
        _, payload = self._run("--sandbox", "none", shape=add_a_test)
        self.assertIsNone(payload["full_suite_oracle"])
        _, payload = self._run("--sandbox", "none", shape=add_a_test, full_suite=True)
        self.assertEqual(payload["full_suite_oracle"]["added_test_files"], ["test_extra.py"])

    def test_untouched_tests_build_no_oracle_tree(self):
        _, payload = self._run("--sandbox", "none", full_suite=True, spy="suite_oracle_tree")
        self.assertIsNone(payload["full_suite_oracle"])
        self.spied.assert_not_called()

    def test_a_link_in_the_patch_stays_a_link_where_the_claims_run(self):
        def link(root):
            for side in ("baseline", "patch"):
                (root / side / "notes.txt").write_text("rates\n")
                os.symlink("notes.txt", root / side / "current.txt")
        code, payload = self._run("--sandbox", "none", shape=link)
        self.assertEqual(code, 0, payload.get("reason"))
        self.assertFalse(payload.get("claims_rewrote"), payload.get("reason"))

    def test_an_isolated_network_lets_the_run_go_on(self):
        code, payload = self._run("--require-network-isolation", "--sandbox", "none")
        self.assertEqual(code, 0, payload.get("reason"))
        self.assertTrue(payload["policy"]["require_network_isolation"])

    def test_the_sandbox_note_is_null_unless_auto_fell_back(self):
        _, payload = self._run("--sandbox", "none")
        self.assertIsNone(payload["execution"]["sandbox_note"])
        _, payload = self._run("--sandbox", "auto", resolved=("none", "bwrap is not on PATH"))
        self.assertEqual(payload["execution"]["sandbox"], "none")
        self.assertEqual(payload["execution"]["sandbox_note"], "bwrap is not on PATH")

    def test_the_policy_hash_is_of_the_sorted_policy(self):
        import hashlib
        _, payload = self._run("--sandbox", "none")
        policy = payload["policy"]
        self.assertNotEqual(list(policy), sorted(policy))  # else the order could not matter
        self.assertEqual(payload["policy_sha256"], hashlib.sha256(
            json.dumps(policy, sort_keys=True, default=str).encode("utf-8")).hexdigest())
