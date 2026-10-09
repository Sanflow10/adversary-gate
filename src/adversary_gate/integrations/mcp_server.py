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
  says ``"baseline_chosen_by": "agent"``. Pinning names the baseline; it is
  not a defence against an agent that can write the repository's ``.git``
  (refs, objects, configuration) -- run such an agent where it cannot;
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
import inspect
import json
import os
import re
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
        # What would turn this into a MERGE, in actions a coding agent can take:
        # fix_code, cover lines, kill a named mutant, add a *new* test -- never
        # edit a baseline test, a harness file or a floor.
        "next_steps": payload.get("next_steps", []),
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
    """Judge the working tree of a git repository against a baseline, by running its tests.

    When to call: after changing code in ``repo`` and before reporting the task
    as done. Call ``gate_policy`` first if you need to know the floors or
    whether the baseline is pinned. Python/pytest only; changes in other
    languages come back inconclusive.

    What it does: writes the baseline commit's tree (every file as committed),
    diffs it against the working tree (uncommitted and untracked files
    included), runs the suite under coverage.py, then runs the gate: the
    claims on the baseline and on the patch, the baseline's copy of any test
    the patch rewrote, and mutation testing on the changed lines. Nothing is
    committed or reverted.

    Side effects: the repository's tests execute -- its code runs, so they may
    do whatever they do when you run pytest yourself. The coverage run uses the
    repository as working directory; the gate's own files go to a temporary
    directory that is removed afterwards. It can take minutes. A run longer
    than ``timeout_seconds`` returns inconclusive, never a pass.

    Arguments: ``repo`` is a path inside the git repository. ``claims`` are
    pytest node ids (``path::test``) to verify; empty -> discovered from the
    tests that executed a changed line. ``pytest_args`` are test paths or node
    ids the coverage run collects (default: the whole suite); options such as
    ``-k`` are refused. ``base_ref`` is used only when the operator did not pin
    ADVERSARY_GATE_BASE_REF.

    Returns ``decision`` (merge / block / inconclusive), ``mergeable``,
    ``fix_proven``, ``reason``, per-claim results with the oracle used,
    ``diff_coverage``, ``suite_strength``, ``next_steps`` and the full
    artefact. merge permits a human review, not a release; block means fix the
    code, not the tests; inconclusive means something was not measured -- say
    what. ``next_steps`` lists what would earn a MERGE (cover these lines, kill
    this surviving mutant, add a new test, fix the code); do them and call
    again. It never asks you to edit an existing test, a harness file or a
    floor, and doing so does not help: the baseline's tests are the oracle.
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
            summary["baseline_sha"] = evidence.baseline_sha
            summary["baseline_chosen_by"] = "operator" if pinned else "agent"
            return summary
    except (EvidenceError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"decision": "inconclusive", "exit_code": None, "mergeable": False,
                "reason": f"the gate could not run: {exc}", "claims": []}


def _pin_kind(pinned: Optional[str]) -> str:
    """What the operator's pin holds, for ``gate_policy``: a commit id names one
    commit for good; a ref name names whatever the ref points at when a run starts."""
    if not pinned:
        return "none"
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pinned):
        return "commit id"
    return "ref name (resolved at the start of each run; a commit id never moves)"


def gate_policy() -> Dict[str, Any]:
    """Show the verification policy this server enforces: floors, baseline, interpreter, and what the caller may set.

    When to call: before ``verify_repo``, to learn whether the baseline is
    pinned by the operator or chosen by you, which interpreter runs the tests,
    and which floors a merge has to clear -- or after an inconclusive or block
    answer, to explain it.

    Read-only and fast: it runs no tests and changes nothing. The policy comes
    from the server's environment (ADVERSARY_GATE_POLICY, _BASE_REF, _PYTHON),
    set by whoever configured the server; no tool can change it.

    Returns ``version``, ``operator_flags``, ``defaults``, ``baseline``,
    ``python`` and ``agent_controls`` (the arguments of ``verify_repo`` you
    decide). An invalid operator policy returns ``error`` instead.
    """
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
        "baseline_pin": _pin_kind(os.environ.get("ADVERSARY_GATE_BASE_REF")),
        "python": os.environ.get("ADVERSARY_GATE_PYTHON") or sys.executable,
        "agent_controls": ["repo", "claims", "pytest_args (paths only)"]
        + ([] if os.environ.get("ADVERSARY_GATE_BASE_REF") else ["base_ref"]),
    }


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server
    instructions = (
        "Call verify_repo before reporting a code change as done. merge: a human may "
        "look; block: fix the code, not the tests; inconclusive: say what was not "
        "measured. Follow next_steps (cover lines, kill a named mutant, add a new "
        "test) and call again; never edit existing tests to get there. Policy, "
        "baseline and interpreter are the operator's (gate_policy)."
    )
    try:
        server = Server("adversary-gate", instructions=instructions, version=__version__)
    except TypeError:  # pragma: no cover - mcp 1.x FastMCP has no version argument
        server = Server("adversary-gate", instructions=instructions)
    _register(server, verify_repo, title="Verify a repository change",
              read_only=False, idempotent=False, open_world=True)
    _register(server, gate_policy, title="Show the gate policy",
              read_only=True, idempotent=True, open_world=False)
    return server


def _register(server, fn, *, title: str, read_only: bool, idempotent: bool,
              open_world: bool) -> None:
    """Behaviour hints for MCP clients. verify_repo is open-world: the
    repository's tests run, and an operator policy may add ``--triage``, which
    sends the diff to a service. Neither tool deletes anything of its own."""
    try:
        from mcp.types import ToolAnnotations
        # camelCase validates on mcp 1.x and 2.x alike.
        annotations = ToolAnnotations.model_validate({
            "title": title, "readOnlyHint": read_only, "destructiveHint": False,
            "idempotentHint": idempotent, "openWorldHint": open_world,
        })
    except Exception:  # pragma: no cover - SDKs without ToolAnnotations
        annotations = None
    # Pass only what this SDK's tool() accepts. Never retry on an error: a
    # registration that fails must fail loudly, not leave the tool out.
    accepted = inspect.signature(server.tool).parameters
    kwargs = {k: v for k, v in (("title", title), ("annotations", annotations))
              if k in accepted and v is not None}
    server.tool(**kwargs)(fn)


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
