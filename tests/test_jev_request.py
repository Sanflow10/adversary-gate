"""What the Jev client sends, and where it agrees to send it (3.0.18).

The endpoint was checked by its first characters, so ``http://localhost.example.com``
read as this machine. And the request named no model, which TypeSafe's API
requires: every triage was refused and recorded as an error.
"""

from __future__ import annotations

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

from adversary_gate import cli  # noqa: E402
from adversary_gate.integrations import jev  # noqa: E402

ANSWER = {
    "answers": {
        "risk": {"choice": "low", "confidence": 0.99, "probabilities": {}},
        "contract_change": {"noul": 0.1, "probabilities": {}},
    },
    "model": "jev-1.13.0",
}


class _JevThatNeedsAModel:
    """Answers like TypeSafe's API: 422 without a model, the answer with one."""

    def __init__(self):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                status, data = (200, ANSWER) if body.get("model") else (422, {"detail": "model"})
                payload = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestTheRequestNamesAModel(unittest.TestCase):
    def setUp(self):
        self.fake = _JevThatNeedsAModel()
        self.addCleanup(self.fake.close)

    def test_the_model_is_sent_and_both_names_are_recorded(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("TYPESAFE_MODEL", None)
            default = jev.JevClient(api_key="sk-test", endpoint=self.fake.url)
        triage = jev.triage_diff("+x\n", default)
        self.assertIsNone(triage.error)
        self.assertEqual(self.fake.requests[-1]["model"], "jev-latest")
        self.assertEqual((triage.requested_model, triage.model), ("jev-latest", "jev-1.13.0"))
        with mock.patch.dict(os.environ, {"TYPESAFE_MODEL": "jev-1.12.0"}):
            from_env = jev.JevClient(api_key="sk-test", endpoint=self.fake.url)
            named = jev.JevClient(api_key="sk-test", endpoint=self.fake.url, model="jev-1.13.0")
        for client, name in ((from_env, "jev-1.12.0"), (named, "jev-1.13.0")):
            with self.subTest(name):
                self.assertEqual(jev.triage_diff("+x\n", client).requested_model, name)
                self.assertEqual(self.fake.requests[-1]["model"], name)

    def test_the_operator_names_the_model_on_the_command_line(self):
        args = cli.build_parser().parse_args([
            "--baseline", "b", "--patch", "p", "--triage", "jev",
            "--triage-endpoint", self.fake.url, "--triage-model", "jev-1.12.0",
        ])
        self.assertIn("triage_model", cli.POLICY_OPTIONS)
        with tempfile.TemporaryDirectory() as directory:
            args.diff = str(Path(directory) / "change.diff")
            Path(args.diff).write_text("+x\n")
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"}):
                record = cli._run_triage(args)
        self.assertEqual(self.fake.requests[-1]["model"], "jev-1.12.0")
        self.assertEqual(record["requested_model"], "jev-1.12.0")
        self.assertIsNone(record["error"])


class TestTheEndpointIsReadLikeUrllibReadsIt(unittest.TestCase):
    def test_only_https_or_plain_http_to_this_machine_by_name(self):
        for url in ("https://api.typesafe.ai/v1/systemone", "https://example.com:8443/v1",
                    "http://localhost/v1", "HTTP://LOCALHOST/v1", "http://127.0.0.1:8765/v1",
                    "http://[::1]:8765/v1"):
            with self.subTest(allowed=url):
                self.assertTrue(jev.endpoint_allowed(url))
        for url in ("http://localhost.example.invalid/v1", "http://127.0.0.1.example.invalid/v1",
                    "http://localhost@example.invalid/v1", "http://127.0.0.1:8765@example.invalid/v1",
                    "https://user:pass@example.invalid/v1", "https://user@example.invalid/v1",
                    "http://localhost./v1",
                    "http://localhost\\@example.invalid/v1", "http://localhost%2eexample.invalid/v1",
                    "http://localhost/v1#@example.invalid", "http://127.1/v1", "http://example.invalid/v1",
                    "https:///v1", "ftp://localhost/v1", "file:///etc/passwd", " http://localhost/v1",
                    "http://localhost:port/v1", "http://local host/v1", None):
            with self.subTest(refused=url):
                self.assertFalse(jev.endpoint_allowed(url))

    def test_a_refused_endpoint_is_never_contacted(self):
        with mock.patch.object(jev.urllib.request, "urlopen") as urlopen:
            triage = jev.triage_diff(
                "+x\n", jev.JevClient(api_key="sk", endpoint="http://localhost@example.invalid/v1"))
        urlopen.assert_not_called()
        self.assertIn("non-HTTPS", triage.error)
        self.assertEqual(triage.requested_model, "jev-latest")


if __name__ == "__main__":
    unittest.main()
