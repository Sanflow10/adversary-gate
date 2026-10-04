"""AdversaryGate as an MCP server, for agents that write code (Hermes, Claude Code...).

    pip install "adversary-gate[mcp]"
    adversary-gate-mcp                      # stdio server
    adversary-gate-mcp --print-hermes-skill # the Hermes skill that uses it
    adversary-gate-mcp --print-hermes-config

**The agent says what to judge; the operator says how strictly, and against
what.** Tool arguments name the repository, the claims and which tests the
coverage run collects. Everything that decides the answer comes from the
environment the operator sets in the agent's MCP configuration:

* ``ADVERSARY_GATE_POLICY``   extra CLI flags (``--sandbox bwrap --triage jev``);
* ``ADVERSARY_GATE_BASE_REF`` the baseline (e.g. ``origin/main``). Whoever
  picks the baseline picks the oracle: an agent could commit a rewritten test
  and name that commit. Unset, the agent may pass ``base_ref`` and the answer
  says ``"baseline_chosen_by": "agent"``;
* ``ADVERSARY_GATE_PYTHON``   the interpreter. Its site-packages are harness
  too -- a plugin installed there runs inside pytest -- so it must be one the
  agent cannot write to.

An agent that could pass ``--coverage-floor 0`` would be grading itself -- the
failure this project exists to catch, one level up.

The gate runs as a subprocess, not in-process: its CLI prints JSON on stdout,
and stdout here is the MCP channel.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional

from adversary_gate import __version__
from adversary_gate.integrations.gitevidence import EvidenceError, prepare

#: Flags only the operator may set. A tool call carrying one is refused.
_EVIDENCE_FLAGS = ("--baseline", "--patch", "--diff", "--coverage-json", "--claim",
                   "--discover-claims", "--python", "--evidence-log")
#: ``--python`` is set from ADVERSARY_GATE_PYTHON, not through the policy flags.

EXIT_MEANING = {0: "merge", 1: "block", 2: "inconclusive", 3: "usage error", 4: "environment error"}


def policy_args() -> List[str]:
    """The operator's flags, from ``ADVERSARY_GATE_POLICY``."""
    args = shlex.split(os.environ.get("ADVERSARY_GATE_POLICY", ""))
    clash = [a for a in args if a.split("=", 1)[0] in _EVIDENCE_FLAGS]
    if clash:
        raise ValueError(f"ADVERSARY_GATE_POLICY may not set evidence flags: {clash}")
    return args


def _run_gate(argv: List[str], timeout: int) -> Dict[str, Any]:
    src = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")]))}
    proc = subprocess.run(
        [sys.executable, "-m", "adversary_gate", *argv],
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        payload = None
    return {"exit_code": proc.returncode, "payload": payload, "stderr": proc.stderr[-2000:]}


def _summary(result: Dict[str, Any]) -> Dict[str, Any]:
    payload = result["payload"] or {}
    code = result["exit_code"]
    return {
        "decision": payload.get("decision") or EXIT_MEANING.get(code, "error"),
        "exit_code": code,
        "mergeable": code == 0,
        "fix_proven": payload.get("fix_proven"),
        "reason": payload.get("reason") or (result["stderr"].strip().splitlines() or [""])[-1],
        "claims": [
            {k: c.get(k) for k in ("test_path", "test_id", "classification", "outcome", "oracle", "reason")}
            for c in payload.get("claims", [])
        ],
        "diff_coverage": payload.get("diff_coverage_ratio"),
        "suite_strength": payload.get("suite_strength"),
        "suite_strength_lower": payload.get("suite_strength_lower"),
        "triage": (payload.get("execution") or {}).get("triage"),
        "how_to_read": (
            "merge: every claim executed and cleared the floors -- permission for a "
            "human to look, not to ship. block: something broke; fix the code, not "
            "the tests. inconclusive: something was not measured; say what, do not "
            "call the work done."
        ),
        "artefact": payload,
    }


def verify_repo(
    repo: str,
    base_ref: str = "HEAD",
    claims: Optional[List[str]] = None,
    pytest_args: Optional[List[str]] = None,
    timeout_seconds: int = 900,
) -> Dict[str, Any]:
    """Judge the working tree of ``repo`` against a baseline, by execution.

    claims: pytest node ids (``path::test``) to verify; empty -> discovered from
    the tests that executed a changed line. pytest_args: test paths or node ids
    the coverage run collects (default: the whole suite); options are refused.
    base_ref: used only when the operator did not pin ADVERSARY_GATE_BASE_REF.
    """
    try:
        policy = policy_args()
        interpreter = os.environ.get("ADVERSARY_GATE_PYTHON") or sys.executable
        pinned = os.environ.get("ADVERSARY_GATE_BASE_REF")
        base = pinned or base_ref
        options = [a for a in (pytest_args or []) if a.startswith("-")]
        if options:
            raise ValueError(f"pytest_args takes test paths only, not options: {options}")
        with tempfile.TemporaryDirectory(prefix="adversary-mcp-") as scratch:
            evidence = prepare(Path(repo), base, Path(scratch), python=interpreter,
                               pytest_args=pytest_args or ())
            argv = [
                "--baseline", str(evidence.baseline), "--patch", str(evidence.patch),
                "--diff", str(evidence.diff), "--coverage-json", str(evidence.coverage),
                "--python", interpreter,
                "--evidence-log", str(Path(scratch) / "evidence.jsonl"),
            ]
            for claim in claims or []:
                argv += ["--claim", claim]
            if not claims:
                argv.append("--discover-claims")
            summary = _summary(_run_gate(argv + policy, timeout_seconds))
            summary["untracked_files_judged"] = evidence.untracked
            summary["baseline"] = base
            summary["baseline_chosen_by"] = "operator" if pinned else "agent"
            return summary
    except (EvidenceError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"decision": "inconclusive", "exit_code": None, "mergeable": False,
                "reason": f"the gate could not run: {exc}", "claims": []}


def gate_policy() -> Dict[str, Any]:
    """What this server enforces. Read-only: the agent cannot change it."""
    try:
        policy = policy_args()
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "version": __version__,
        "operator_flags": policy,
        "defaults": "coverage floor 0.80; strength floor 0.75 on the 80% Wilson lower bound; "
                    "baseline oracle on rewritten tests; fail-closed",
        "baseline": os.environ.get("ADVERSARY_GATE_BASE_REF") or "chosen by the agent (not pinned)",
        "python": os.environ.get("ADVERSARY_GATE_PYTHON") or sys.executable,
        "agent_controls": ["repo", "claims", "pytest_args (paths only)"]
        + ([] if os.environ.get("ADVERSARY_GATE_BASE_REF") else ["base_ref"]),
    }


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server
    server = Server("adversary-gate")
    server.tool()(verify_repo)
    server.tool()(gate_policy)
    return server


def _packaged(name: str) -> str:
    root = resources.files("adversary_gate.integrations")
    return root.joinpath("hermes").joinpath(name).read_text(encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="adversary-gate-mcp", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--print-hermes-skill", action="store_true", help="print the Hermes SKILL.md")
    parser.add_argument("--print-hermes-config", action="store_true", help="print the config.yaml snippet")
    args = parser.parse_args(argv)
    if args.print_hermes_skill:
        print(_packaged("SKILL.md"), end="")
        return 0
    if args.print_hermes_config:
        print(_packaged("config.example.yaml"), end="")
        return 0
    try:
        server = build_server()
    except ImportError:
        print('adversary-gate-mcp needs the MCP SDK: pip install "adversary-gate[mcp]"', file=sys.stderr)
        return 3
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
