"""Command-line interface.

Exit codes are part of the contract, because CI reads them:

    0  -> MERGE       every claim executed and none condemned the patch
    1  -> BLOCK       at least one claim was REFUTED by execution
    2  -> INCONCLUSIVE something could not be verified (does not merge)
    3  -> USAGE       argument or input error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from adversary_gate import __version__
from adversary_gate.core.exitmap import PYTEST_OK
from adversary_gate.core.gate import Gate, GateConfig, rewritten_test_files, transplant_tree
from adversary_gate.core.metrics import compute_metrics, format_report
from adversary_gate.core.types import (
    AcceptanceCriterion,
    AggressionLevel,
    BugKind,
    CriticClaim,
    Decision,
    GateVerdict,
    Outcome,
)
from adversary_gate.sandbox.runner import SandboxResult, bwrap_available, run_test
from adversary_gate.verifiers.coverage import covered_diff_ratio, validate_diff
from adversary_gate.verifiers.discovery import discover_claims
from adversary_gate.verifiers.strength import (
    DEFAULT_CONFIDENCE,
    DEFAULT_MAX_MUTANTS,
    classify_changes,
    measure_mutation_score,
)
from adversary_gate.verifiers.testpaths import set_test_support

EXIT_MERGE = 0
EXIT_BLOCK = 1
EXIT_INCONCLUSIVE = 2
EXIT_USAGE = 3

DECISION_EXIT = {
    Decision.MERGE: EXIT_MERGE,
    Decision.BLOCK: EXIT_BLOCK,
    Decision.INCONCLUSIVE: EXIT_INCONCLUSIVE,
}


class _Parser(argparse.ArgumentParser):
    """Custom ArgumentParser that exits with EXIT_USAGE (3) on error."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def _claim(args: argparse.Namespace, test_path: str, test_id: str) -> CriticClaim:
    return CriticClaim(
        test_path, test_id, BugKind(args.bug_kind), args.criterion_id, args.rationale
    )


def _claims_from_args(args: argparse.Namespace) -> List[CriticClaim]:
    """Every claim the caller named, in the order given (AG-031).

    A run used to verify exactly one claim, so a workflow had to name one fixed
    test for every pull request. Now there are four ways in, and they add up:
    ``--claim-json`` with any number of claims, ``--claim PATH::ID`` repeated,
    ``--test-path`` with ``--test-id`` repeated, and ``--discover-claims``,
    which is resolved later because it needs the diff and the coverage report.
    """
    if args.claim_json:
        source = Path(args.claim_json)
        raw = source.read_text(encoding="utf-8") if source.is_file() else args.claim_json
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or set(data) != {"claims"}
            or not isinstance(data["claims"], list)
            or not data["claims"]
        ):
            raise ValueError('claim JSON must be {"claims": [...]} with at least one claim')
        claims: List[CriticClaim] = [
            CriticClaim(
                item["test_path"],
                item["test_id"],
                BugKind(item.get("bug_kind", "deterministic")),
                item.get("cited_criterion_id"),
                item.get("rationale", ""),
            )
            for item in data["claims"]
        ]
    else:
        claims = []
    for raw in args.claim:
        test_path, separator, test_id = raw.partition("::")
        if not separator or not test_path or not test_id:
            raise ValueError(f"--claim must be PATH::TEST_ID, got {raw!r}")
        claims.append(_claim(args, test_path, test_id))

    if args.test_path:
        # A suite that is one arbitrary command has no node IDs to name, but the
        # claim still needs a non-empty identity for the circuit breaker and the
        # evidence trail. Say what it is rather than inventing a test name.
        ids = list(args.test_id) or (["(test-command)"] if args.test_command else [])
        if not ids:
            raise ValueError(
                "--test-id is required unless --test-command or --claim-json is used"
            )
        claims.extend(_claim(args, args.test_path, test_id) for test_id in ids)
    elif args.test_id:
        raise ValueError("--test-id needs --test-path (or name the test with --claim PATH::ID)")

    if not claims and not args.discover_claims:
        raise ValueError(
            "--test-path is required unless --claim, --claim-json or --discover-claims is used"
        )
    # The same test named twice is verified once: a second run is cost, not
    # evidence, and it would count twice in the summary.
    unique: Dict[Tuple[str, str], CriticClaim] = {}
    for claim in claims:
        unique.setdefault((claim.test_path, claim.test_id), claim)
    return list(unique.values())


#: An environment variable name the shell would accept.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _limit(text: str) -> Optional[int]:
    """``--timeout`` / ``--cpu-seconds``: a positive integer, or ``none``."""
    if text.strip().lower() == "none":
        return None
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer or 'none', got {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(
            f"{value} is not a limit; use a positive number, or 'none' to remove it"
        )
    return value


def _size(text: str) -> Optional[int]:
    """``--memory``: bytes, with an optional K/M/G suffix (powers of 1024), or ``none``."""
    raw = text.strip().upper()
    if raw == "NONE":
        return None
    match = re.fullmatch(r"(\d+)\s*([KMG]?)I?B?", raw)
    if not match:
        raise argparse.ArgumentTypeError(
            f"expected a size like 512M or 4G, or 'none', got {text!r}"
        )
    value = int(match.group(1)) * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2)]
    if value <= 0:
        raise argparse.ArgumentTypeError("a memory limit of zero is a typo; use 'none' to remove it")
    return value


def _run_environment(args: argparse.Namespace) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """What the test process may see beyond the minimal default (AG-024).

    ``--pass-env NAME`` copies a variable from the gate's own environment;
    ``--env NAME=VALUE`` sets one. The artefact records the names and never the
    values, because a value is exactly where a token would be.
    """
    env: Dict[str, str] = {}
    missing: List[str] = []
    for name in args.pass_env:
        if not _ENV_NAME.match(name):
            raise ValueError(f"--pass-env takes a variable name, got {name!r}")
        if name in os.environ:
            env[name] = os.environ[name]
        else:
            missing.append(name)
    set_names: List[str] = []
    for raw in args.env:
        name, separator, value = raw.partition("=")
        if not separator or not _ENV_NAME.match(name):
            raise ValueError(f"--env must be NAME=VALUE, got {raw!r}")
        env[name] = value
        set_names.append(name)
    record = {
        "env_passed": sorted(set(args.pass_env) - set(missing)),
        "env_passed_missing": sorted(missing),
        "env_set": sorted(set(set_names)),
    }
    return env, record


def _resolve_python(value: Optional[str]) -> str:
    """The interpreter that runs pytest: the project's, when ``--python`` names it (AG-025).

    Checked before anything runs, so a wrong path is a usage error and not a
    run that dies inside ``Popen`` and reads as an unexplained crash.
    """
    if not value:
        return sys.executable
    found = shutil.which(value) or value
    path = Path(found)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"--python {value!r} is not an executable interpreter")
    # Absolute, but not resolved: a venv's ``bin/python`` is a symlink to the
    # base interpreter, and following it would drop the venv -- and with it
    # every dependency the project installed there.
    return os.path.abspath(found)


def _decisive(verdicts: Sequence[GateVerdict]) -> Optional[GateVerdict]:
    """The verdict that explains the decision: the first REFUTED, else UNVERIFIED, else any."""
    for wanted in (Outcome.REFUTED, Outcome.UNVERIFIED):
        for verdict in verdicts:
            if verdict.outcome is wanted:
                return verdict
    return verdicts[0] if verdicts else None


def _run_record(verdict: GateVerdict) -> Dict[str, Any]:
    run = verdict.outcome_run
    return {
        "baseline": run.baseline.value,
        "patch": run.patch.value,
        "baseline_exit_codes": list(run.baseline_exit_codes),
        "patch_exit_codes": list(run.patch_exit_codes),
    }


def _verdict_record(verdict: GateVerdict) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "test_path": verdict.claim.test_path,
        "test_id": verdict.claim.test_id,
        "classification": verdict.classification.value,
        "outcome": verdict.outcome.value,
        "reason": verdict.reason,
        "oracle": verdict.oracle,
        "duration_seconds": round(verdict.duration_seconds, 6),
    }
    if verdict.outcome_run is not None:
        record["evidence"] = _run_record(verdict)
    return record


def _resolve_coverage(args: argparse.Namespace):
    """Turn coverage *inputs* into a coverage *value*, or admit there is none.

    Three sources, strongest first:

    ``computed``
        ``--diff`` plus ``--coverage-json``. The ratio is derived from the two
        artefacts and their SHA-256 digests go into the detail, so the number
        in the evidence artefact can be recomputed instead of believed.

    ``untrusted``
        ``--coverage-ratio`` supplied by the caller. Permitted, but only when
        the caller says so out loud via ``--coverage-source untrusted``.
        v2.0.1 accepted the number silently, defaulted it to ``1.0``, and the
        GitHub Action never passed one at all -- so ``diff_coverage >= 80%``
        was satisfied by a default value on every single Action run.

    ``none``
        No artefact and no claim. Reported as ``None`` rather than ``1.0``,
        because "we did not measure" is not "we measured and it was perfect".

    Returns ``(ratio, source, detail)``. ``ratio`` is ``None`` only for
    ``none``. Anything the caller declares is a ``ValueError``, which ``main``
    turns into exit code 3.
    """
    import hashlib

    diff_path = Path(args.diff) if args.diff else None
    cov_path = Path(args.coverage_json) if args.coverage_json else None

    if (diff_path is None) != (cov_path is None):
        raise ValueError("--diff and --coverage-json must be provided together")

    if diff_path is not None:
        if not diff_path.is_file():
            raise ValueError(f"--diff is not a readable file: {args.diff}")
        if not cov_path.is_file():
            raise ValueError(f"--coverage-json is not a readable file: {args.coverage_json}")
        if args.coverage_source == "untrusted":
            raise ValueError(
                "--coverage-source untrusted contradicts --diff/--coverage-json; "
                "pick one way of answering the coverage question"
            )
        diff_bytes = diff_path.read_bytes()
        cov_bytes = cov_path.read_bytes()
        payload = json.loads(cov_bytes.decode("utf-8"))
        measured = covered_diff_ratio(diff_bytes.decode("utf-8", "replace"), payload)
        detail: Dict[str, Any] = {
            "source": "computed",
            "ratio": round(measured.ratio, 6),
            "changed_lines": measured.changed_lines,
            "covered_lines": measured.covered_lines,
            # Test files are not in the ratio (AG-022). Said here so that
            # ``changed_lines`` is not mistaken for "every line the diff added".
            "test_lines_excluded": measured.excluded_test_lines,
            "test_files_excluded": list(measured.excluded_test_files),
            "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
            "coverage_json_sha256": hashlib.sha256(cov_bytes).hexdigest(),
        }
        if measured.changed_lines == 0 and measured.excluded_test_lines:
            detail["note"] = (
                "the diff adds only test lines; there is no source line to cover, "
                "so the ratio is vacuously 1.0"
            )
        return measured.ratio, "computed", detail

    if args.coverage_ratio is not None:
        if args.coverage_source == "computed":
            raise ValueError("--coverage-source computed requires --diff and --coverage-json")
        if args.coverage_source != "untrusted":
            raise ValueError(
                "--coverage-ratio is a caller-supplied claim, not executed evidence; "
                "re-declare it with '--coverage-source untrusted', or pass --diff and "
                "--coverage-json so the gate can measure it itself"
            )
        if not 0.0 <= args.coverage_ratio <= 1.0:
            raise ValueError("--coverage-ratio must be between 0 and 1")
        return args.coverage_ratio, "untrusted", {
            "source": "untrusted",
            "ratio": args.coverage_ratio,
            "note": "supplied by the caller; no diff or coverage artefact was read",
        }

    if args.coverage_source == "untrusted":
        raise ValueError("--coverage-source untrusted requires --coverage-ratio")
    if args.coverage_source == "computed":
        raise ValueError("--coverage-source computed requires --diff and --coverage-json")

    return None, "none", {
        "source": "none",
        "ratio": None,
        "note": "no --diff/--coverage-json artefact and no --coverage-ratio claim",
    }


def _diff_paths(args: argparse.Namespace) -> Optional[List[str]]:
    """File paths named by ``--diff``, or ``None`` when the caller gave none.

    "What changed" is a property of the patch, not of the measurement budget,
    so it is answered from the caller's own diff as well as from the directory
    scan. ``validate_diff`` refuses a diff whose hunks it cannot attribute
    instead of handing back an empty list, which would read as "nothing
    changed" -- the same false statement that opened AG-012.
    """
    if not args.diff:
        return None
    text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    return sorted(validate_diff(text))


def _run_triage(args: argparse.Namespace) -> Dict[str, Any]:
    """Ask Jev, apply only upward changes to ``args``, return the record."""
    from adversary_gate.integrations.jev import DEFAULT_THRESHOLD, JevClient, escalation, triage_diff

    threshold = DEFAULT_THRESHOLD if args.triage_threshold is None else args.triage_threshold
    if not 0 < threshold <= 1:
        raise ValueError("--triage-threshold must be in (0, 1]")
    if not args.diff:
        return {"source": "jev", "error": "no --diff to triage", "escalated": False, "applied": {}}
    diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    triage = triage_diff(diff_text, JevClient(endpoint=args.triage_endpoint), threshold)
    raised = escalation(
        triage,
        {"strength_confidence": args.strength_confidence, "coverage_floor": args.coverage_floor},
    )
    for name, value in raised.items():
        setattr(args, name, value)
    triage.escalated = bool(raised)
    triage.applied = raised
    return triage.to_dict()


def _full_oracle(rewritten: Sequence[str], exit_code: Optional[int]) -> Optional[Dict[str, Any]]:
    """What the collateral run judged the patch's code with, when it was not the patch's tests."""
    if not rewritten:
        return None
    return {
        "source": "baseline",
        "rewritten_test_files": list(rewritten),
        "exit_code": exit_code,
    }


def _summary(verdicts) -> Dict[str, Any]:
    return {
        "claims_total": len(verdicts),
        "verified": sum(1 for v in verdicts if v.outcome.value == "verified"),
        "refuted": sum(1 for v in verdicts if v.outcome.value == "refuted"),
        "unverified": sum(1 for v in verdicts if v.outcome.value == "unverified"),
        "classifications": [v.classification.value for v in verdicts],
        # AG-030: the sentence a reviewer wants -- "this patch fixes what it
        # says it fixes" -- is true only when a claim went FAIL -> PASS.
        "claims_fixed": sum(1 for v in verdicts if v.classification.value == "fixed"),
        "claims_no_regression": sum(1 for v in verdicts if v.classification.value == "no_regression"),
        "fix_proven": any(v.classification.value == "fixed" for v in verdicts),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="adversary-gate")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--test-path")
    parser.add_argument(
        "--test-id",
        action="append",
        default=[],
        help="test inside --test-path; repeat it to verify several tests in one run",
    )
    parser.add_argument(
        "--claim",
        action="append",
        default=[],
        metavar="PATH::TEST_ID",
        help="a test to verify, by pytest node id; repeatable",
    )
    parser.add_argument("--claim-json", help='{"claims": [...]}, one or more claims')
    parser.add_argument(
        "--test-support",
        action="append",
        default=[],
        metavar="GLOB",
        help="a path (fnmatch glob, repo-relative; '*' crosses '/') that is test "
        "support, not code under test -- e.g. testing_utils.py or 'helpers/*'. "
        "Repeatable. Treated like a test file everywhere: out of diff coverage "
        "and mutation, and put back from the baseline by the baseline oracle, so "
        "a patch cannot bend it to agree with a bug. test_*, *_test.py, "
        "conftest.py and anything under tests/ need no declaration.",
    )
    parser.add_argument(
        "--triage",
        choices=("none", "jev"),
        default="none",
        help="ask a triage model how risky the diff is before judging it. 'jev' "
        "sends the diff (truncated to 64 KiB) to TypeSafe's API "
        "(TYPESAFE_API_KEY). A confident 'high' only raises the floors for this "
        "run; nothing it says can make the decision more lenient.",
    )
    parser.add_argument("--triage-endpoint", help="override the Jev endpoint (default: TypeSafe's)")
    parser.add_argument(
        "--triage-threshold",
        type=float,
        default=None,
        metavar="P",
        help="confidence a 'high' needs before it raises the floors (default 0.70)",
    )
    parser.add_argument(
        "--discover-claims",
        action="store_true",
        help="verify the tests that executed the changed source lines, read from "
        "the coverage report's per-test contexts (needs --diff and "
        "--coverage-json written with --show-contexts). Tests in files the "
        "patch rewrote are judged with the baseline's copy of the file.",
    )
    parser.add_argument(
        "--max-claims",
        type=int,
        default=10,
        metavar="N",
        help="cap on discovered claims; each claim costs several test runs (default 10)",
    )
    parser.add_argument(
        "--bug-kind", default="deterministic", choices=[x.value for x in BugKind]
    )
    parser.add_argument("--criterion-id")
    parser.add_argument("--criterion", action="append", default=[], metavar="ID:TEXT")
    parser.add_argument("--rationale", default="")
    parser.add_argument(
        "--aggression", default="normal", choices=[x.value for x in AggressionLevel]
    )
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument(
        "--coverage-ratio",
        type=float,
        default=None,
        metavar="FLOAT",
        help="a coverage number supplied by the caller. This is a claim, not "
        "evidence, so it is rejected unless --coverage-source untrusted is "
        "also given. Prefer --diff and --coverage-json, which are measured.",
    )
    parser.add_argument(
        "--coverage-source",
        default="auto",
        choices=["auto", "computed", "untrusted"],
        help="auto (default) computes coverage from --diff/--coverage-json when "
        "both are present and otherwise reports 'no evidence'; computed "
        "requires them; untrusted requires --coverage-ratio.",
    )
    parser.add_argument(
        "--diff",
        metavar="PATH",
        help="unified diff of the patch, the 'changed lines' side of coverage",
    )
    parser.add_argument(
        "--coverage-json",
        metavar="PATH",
        help="coverage.py JSON report, the 'executed lines' side of coverage",
    )
    parser.add_argument("--coverage-floor", type=float, default=0.80)
    parser.add_argument("--suite-strength-floor", type=float, default=0.75)
    parser.add_argument(
        "--strength-confidence",
        type=float,
        default=DEFAULT_CONFIDENCE,
        metavar="C",
        help="confidence of the Wilson interval around the mutation score; the "
        "strength floor is applied to its lower bound (AG-023). Default 0.80: "
        "5 of 5 mutants killed clears 0.75, 1 of 1 does not. 0 applies the "
        "floor to the raw ratio instead.",
    )
    parser.add_argument(
        "--mutation-max",
        type=int,
        default=DEFAULT_MAX_MUTANTS,
        metavar="N",
        help="max mutants to execute when measuring suite strength (0 disables the measurement)",
    )
    parser.add_argument(
        "--full-suite-path",
        default=".",
        metavar="DIR",
        help="where the collateral full-suite run happens ('' disables it)",
    )
    parser.add_argument(
        "--test-command",
        metavar="CMD",
        help="shell command to run instead of pytest, in each of --baseline and "
        "--patch. Convention: exit 0 passes, 1 fails, 2/3/4 are harness errors "
        "(-> UNVERIFIED -> INCONCLUSIVE), any other non-zero exit is neither. "
        "Unlocks non-pytest and non-Python suites; without it a non-pytest repo "
        "reports 'no tests collected' (exit 5) and can never reach MERGE.",
    )
    parser.add_argument(
        "--full-suite-command",
        metavar="CMD",
        help="command for the collateral full-suite run. Defaults to "
        "--test-command when that is given, else pytest on --full-suite-path.",
    )
    parser.add_argument(
        "--sandbox",
        default="none",
        choices=["none", "bwrap"],
        help="wrap every test run in bubblewrap: no network, private PID and "
        "/tmp, read-only system, read-write only the repository. Opt-in "
        "defence in depth -- it needs 'bwrap' installed, and it is not a "
        "substitute for running the whole gate inside a container or VM. "
        "When you use it, --require-network-isolation becomes truthful: the "
        "network really is gone, so ADVERSARY_NETWORK_ISOLATED=1 is no longer "
        "a wish.",
    )
    parser.add_argument(
        "--python",
        metavar="PATH",
        help="interpreter that runs pytest -- the project's, where its dependencies "
        "are installed. Defaults to the interpreter running the gate.",
    )
    parser.add_argument(
        "--timeout",
        type=_limit,
        default=30,
        metavar="SECONDS|none",
        help="wall-clock limit for each test run, mutants and full suite included (default 30)",
    )
    parser.add_argument(
        "--cpu-seconds",
        type=_limit,
        default=10,
        metavar="SECONDS|none",
        help="CPU-time limit for each test run (default 10)",
    )
    parser.add_argument(
        "--memory",
        type=_size,
        default=512 * 1024**2,
        metavar="SIZE|none",
        help="address-space limit for each test run, e.g. 512M or 4G (default 512M). "
        "The JVM and Node cannot start under one: use 'none' for them.",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help="copy a variable from this environment into the test runs (e.g. PATH, "
        "HOME); repeatable. The default environment is minimal on purpose.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set a variable in the test runs; repeatable. Only names are recorded.",
    )
    parser.add_argument("--rounds-used", type=int, default=4)
    parser.add_argument(
        "--require-network-isolation",
        action="store_true",
        help="refuse to run unless ADVERSARY_NETWORK_ISOLATED=1 is set, i.e. "
        "unless an outer sandbox has declared the network isolated. This is a "
        "declaration check, not an isolation mechanism.",
    )
    parser.add_argument("--evidence-log", help="path to the evidence artefact (JSONL)")
    parser.add_argument("--model", help="model that produced the patch")
    parser.add_argument("--commit", help="commit sha of the patch")
    parser.add_argument("--report", action="store_true", help="print the metrics report to stderr")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """One decision. ``--test-support`` is module state; it never outlives the call."""
    try:
        return _main(argv)
    finally:
        set_test_support(())


def _main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        # Every call sets it, empty included: a previous main() in the same
        # process must not leave its declarations behind.
        set_test_support(args.test_support)

        criteria: List[AcceptanceCriterion] = []
        for raw in args.criterion:
            if ":" not in raw:
                raise ValueError("--criterion must use ID:TEXT")
            identifier, text = raw.split(":", 1)
            if not identifier or not text:
                raise ValueError("--criterion must have a non-empty ID and text")
            criteria.append(AcceptanceCriterion(identifier, text))

        claims = _claims_from_args(args)
        if args.max_claims < 1:
            raise ValueError("--max-claims must be at least 1")
        if not 0 <= args.strength_confidence < 1:
            raise ValueError("--strength-confidence must be in [0, 1); 0 uses the raw ratio")
        if args.discover_claims and args.test_command:
            # A command suite has no node ids, so there is nothing a coverage
            # context could be resolved to.
            raise ValueError("--discover-claims needs pytest node ids; it cannot be used with --test-command")
        if args.discover_claims and not (args.diff and args.coverage_json):
            raise ValueError("--discover-claims needs --diff and --coverage-json (written with --show-contexts)")

        # Resolved before anything executes: an unresolvable coverage question
        # is a usage error (exit 3), not something to discover after paying
        # for the test runs.
        coverage_ratio, coverage_source, coverage_detail = _resolve_coverage(args)

        # What the patch touched, asked of the caller's own diff. It serves two
        # questions that must agree: which paths the policy refuses (below,
        # AG-021) and which source this engine cannot judge (further down).
        # Kept *after* ``_resolve_coverage`` on purpose: that is what turns a
        # missing or contradictory ``--diff`` into a usage error (exit 3), and
        # reading the file first let ``FileNotFoundError`` escape as exit 2.
        diff_paths = _diff_paths(args)

        # How every test run executes -- the claims, the mutants and the full
        # suite alike (AG-024, AG-025). One set of choices, recorded once, so
        # the artefact says what the numbers were measured under.
        python = _resolve_python(args.python)
        run_env, env_record = _run_environment(args)
        limits = {
            "timeout_seconds": args.timeout,
            "cpu_seconds": args.cpu_seconds,
            "mem_bytes": args.memory,
        }
        triage_record: Optional[Dict[str, Any]] = None
        if args.triage == "jev":
            triage_record = _run_triage(args)

        execution: Dict[str, Any] = {
            "python": python,
            "timeout_seconds": args.timeout,
            "cpu_seconds": args.cpu_seconds,
            "memory_bytes": args.memory,
            "sandbox": args.sandbox,
            **env_record,
            "test_support": list(args.test_support),
            "triage": triage_record,
        }

        discovery: Optional[Dict[str, Any]] = None
        if args.discover_claims:
            found, discovery = discover_claims(
                Path(args.diff).read_text(encoding="utf-8", errors="replace"),
                json.loads(Path(args.coverage_json).read_text(encoding="utf-8")),
                Path(args.baseline),
                Path(args.patch),
                max_claims=args.max_claims,
            )
            named = {(c.test_path, c.test_id) for c in claims}
            added = [node for node in found if node not in named]
            claims.extend(_claim(args, path, test_id) for path, test_id in added)
            discovery["claims_added"] = [f"{path}::{test_id}" for path, test_id in added]

        if args.require_network_isolation and os.environ.get(
            "ADVERSARY_NETWORK_ISOLATED"
        ) != "1":
            # Fail fast instead of letting the first run_test raise halfway
            # through the measurements.
            raise ValueError(
                "--require-network-isolation was given but ADVERSARY_NETWORK_ISOLATED "
                "is not '1'; run inside an outer sandbox that has isolated the network"
            )

        if args.sandbox == "bwrap" and not bwrap_available():
            # Checked before anything executes: asking for isolation and
            # quietly not getting it is the failure mode this flag exists to
            # prevent.
            raise ValueError(
                "--sandbox bwrap was requested but 'bwrap' is not on PATH; "
                "install bubblewrap (apt install bubblewrap) or drop the flag"
            )

        log = None
        if args.evidence_log:
            from adversary_gate.core.evidence_log import EvidenceLog

            context = {}
            if args.model:
                context["model"] = args.model
            if args.commit:
                context["commit"] = args.commit
            log = EvidenceLog(Path(args.evidence_log), context=context)

        config = GateConfig(
            coverage_floor=args.coverage_floor,
            suite_strength_floor=args.suite_strength_floor,
        )
        gate = Gate(
            criteria,
            AggressionLevel(args.aggression),
            args.max_rounds,
            config=config,
            evidence_log=log,
        )
        # The interpreter, the environment and the sandbox reach every run
        # through the same mapping; ``command`` only the claims and the full
        # suite, because mutants are judged by pytest on the claims' files.
        run_options = {"sandbox": args.sandbox, "python": python, "env": run_env}
        verdicts: List[GateVerdict] = [
            gate.verify_claim(
                claim,
                Path(args.baseline),
                Path(args.patch),
                # ``--changed-path`` used to be the *only* input to the path
                # policy, and the Action never passes it -- so the protection for
                # conftest.py, pytest config and the like existed and was never
                # asked about a real patch (AG-021). The diff is the authoritative
                # statement of what changed; it is unioned, not substituted.
                changed_paths=sorted({*args.changed_path, *(diff_paths or [])}),
                require_network_isolation=args.require_network_isolation,
                # How each side is executed: an arbitrary command when the
                # repository is not pytest, and/or wrapped in bwrap when the patch
                # is not trusted. Both used to be impossible without forking.
                run_kwargs={"command": args.test_command, **run_options},
                **limits,
            )
            for claim in claims
        ]
        all_verified = bool(verdicts) and all(v.outcome is Outcome.VERIFIED for v in verdicts)

        # ------------------------------------------------------------------
        # Patch-level measurements. Both used to be parameters that defaulted
        # to "fine" and were never assigned anything else, so the two gates in
        # ``decide`` that consume them were unreachable. They are computed
        # here, from execution, and recorded -- a strength value that never
        # reaches the evidence artefact cannot be audited either.
        # ------------------------------------------------------------------
        started = time.monotonic()
        strength: Optional[float] = None
        strength_judged: Optional[float] = None
        strength_unverified = False
        mutation_detail: Dict[str, Any]

        # The diff's paths (resolved above) are asked of the directory scan's
        # question too: "the patch changed source we cannot judge" does not
        # become false when somebody turns mutation off.
        changed_paths = diff_paths

        # Each claim's test file judges the mutants; a mutant dies when any of
        # them fails. One file keeps the single-claim invocation unchanged.
        test_files = sorted({claim.test_path for claim in claims})

        if args.mutation_max <= 0 or not claims:
            # ``--mutation-max 0`` is the documented escape hatch for the
            # strength *measurement*, the same way ``--coverage-floor 0`` is
            # for coverage -- so ``suite_strength`` stays ``None`` and no
            # score is demanded. What it must NOT switch off is the AG-012
            # guard: the check that source exists outside this engine lived
            # only inside the branch below, so passing 0 produced an artefact
            # with no ``changed_files``/``foreign_changed_files`` keys and let
            # a patch touching only ``calculator.cpp`` reach MERGE.
            mutation_detail = {
                "measured": False,
                "reason": (
                    "disabled (--mutation-max 0); what changed below is "
                    "still measured, the mutation score is not"
                    if args.mutation_max <= 0
                    else "no claim to measure the mutants against"
                ),
                **classify_changes(Path(args.baseline), Path(args.patch), changed_paths),
                "mutants": [],
                "survivors": [],
                "mutants_counted": 0,
                "stillborn": 0,
            }
            if all_verified and (
                mutation_detail["foreign_changed_files"]
                or mutation_detail["deleted_files"]
            ):
                # There was source to judge and this engine cannot break it --
                # either because it is not Python, or because the patch deleted
                # it and there are no lines left to mutate. Both hold no matter
                # how many mutants we were allowed.
                strength_unverified = True
        else:
            strength_obj, mutation_detail = measure_mutation_score(
                Path(args.baseline),
                Path(args.patch),
                test_files[0],
                test_id="",
                max_mutants=args.mutation_max,
                confidence=args.strength_confidence,
                changed_paths=changed_paths,
                targets=test_files if len(test_files) > 1 else None,
                run_kwargs=run_options,
                **limits,
            )
            if strength_obj.is_measured:
                # A measured score covers exactly the Python files it mutated
                # -- never the whole patch. Whether it is allowed to stand is
                # decided just below.
                strength = strength_obj.mutation_score
                # What the floor judges: the lower bound, not the ratio.
                strength_judged = strength_obj.lower

            if all_verified:
                if (
                    mutation_detail.get("foreign_changed_files")
                    or mutation_detail.get("deleted_files")
                ):
                    # The patch changed source this engine cannot break, or
                    # removed source nothing can be run against. Even when the
                    # Python half measured cleanly, presenting that number as
                    # the strength of the whole patch would claim more than was
                    # observed -- a mixed Python + C++ patch used to merge with
                    # the C++ never executed (AG-012), and a mixed
                    # modified + deleted patch merged on the score of the file
                    # it modified alone (AG-018): a deleted file has no lines
                    # left to mutate, so it never lowered the one number that
                    # authorised the decision and rode underneath it instead.
                    strength_unverified = True
                elif not strength_obj.is_measured and mutation_detail.get("changed_files"):
                    # There was code to judge, every claim executed and came
                    # back VERIFIED, and we still could not produce a score.
                    # That is *unknown*, not strong, so it must not reach
                    # MERGE. When a claim did not verify, its own outcome
                    # already decides and strength would only add noise.
                    strength_unverified = True

        full_codes: Optional[Sequence[int]] = None
        # ``--test-command`` implies the collateral run uses it too, unless
        # ``--full-suite-command`` overrides. A repository that does not run
        # pytest must not silently lose the collateral-regression check just
        # because it declared how its tests execute.
        full_command = args.full_suite_command or args.test_command
        full_suite_ran = bool(full_command) or bool(args.full_suite_path)
        rewritten_tests = rewritten_test_files(Path(args.baseline), Path(args.patch))
        oracle_full_code: Optional[int] = None

        def _full_run(directory: Path) -> SandboxResult:
            if full_command:
                return run_test(
                    directory,
                    "",
                    "",
                    command=full_command,
                    require_network_isolation=args.require_network_isolation,
                    **run_options,
                    **limits,
                )
            return run_test(
                directory,
                args.full_suite_path,
                "",
                require_network_isolation=args.require_network_isolation,
                **run_options,
                **limits,
            )

        if full_suite_ran:
            # Only pay for the baseline run when the patch side actually failed;
            # a passing patch side is not a collateral regression either way.
            # The artefact records ``full_suite_ran`` so a missing
            # ``full_suite_exit_codes`` can be told apart from a clean run --
            # v2.0.1 wrote ``null`` for both, which made "the suite passed" and
            # "the suite never ran" indistinguishable in the evidence.
            patch_full = _full_run(Path(args.patch))
            # The baseline oracle, for the whole suite: when the patch changed
            # tests the baseline already had, the suite the patch must still
            # pass is the *baseline's*, run against the patch's code. The
            # patch's own copy alone would let a bent test vouch for itself
            # outside the claims, where no claim was looking.
            if rewritten_tests:
                with tempfile.TemporaryDirectory(prefix="adversary-oracle-") as scratch:
                    tree = transplant_tree(Path(args.baseline), Path(args.patch), Path(scratch))
                    oracle_full = _full_run(tree)
                oracle_full_code = oracle_full.exit_code
                if patch_full.exit_code == PYTEST_OK and oracle_full.exit_code != PYTEST_OK:
                    patch_full = oracle_full
            if patch_full.exit_code != PYTEST_OK:
                base_full = _full_run(Path(args.baseline))
                full_codes = (base_full.exit_code, patch_full.exit_code)
        mutation_seconds = time.monotonic() - started

        decision = gate.decide(
            verdicts,
            args.rounds_used,
            coverage_ratio,
            suite_strength=strength_judged,
            full_suite_exit_codes=full_codes,
            suite_strength_unverified=strength_unverified,
        )

        if log is not None:
            log.append_decision(
                decision.value,
                args.rounds_used,
                coverage_ratio,
                {
                    **_summary(verdicts),
                    # Where the coverage number came from, next to the number:
                    # a ratio with no provenance is exactly the artefact AG-002
                    # was about.
                    "diff_coverage_source": coverage_source,
                    "diff_coverage": coverage_detail,
                    # The floor only ever receives a real measurement, so a
                    # reader can tell "strong" from "not measured" from "weak".
                    "suite_strength": strength,
                    "suite_strength_unverified": strength_unverified,
                    "suite_strength_floor": args.suite_strength_floor,
                    "suite_strength_lower": strength_judged,
                    "suite_strength_confidence": args.strength_confidence,
                    "mutation": mutation_detail,
                    "full_suite_ran": full_suite_ran,
                    "full_suite_exit_codes": list(full_codes) if full_codes else None,
                    "full_suite_oracle": _full_oracle(rewritten_tests if full_suite_ran else (), oracle_full_code),
                    "execution": execution,
                    "discovery": discovery,
                },
            )

        # The top-level fields describe the verdict that explains the decision
        # (the first REFUTED, else the first UNVERIFIED), so a reader of one
        # claim's output sees the same keys as before; ``claims`` has them all.
        decisive = _decisive(verdicts)
        payload: Dict[str, Any] = {
            "classification": decisive.classification.value if decisive else None,
            "outcome": decisive.outcome.value if decisive else None,
            "decision": decision.value,
            "reason": decisive.reason if decisive else (
                "no claim to verify: discovery found no test that executed a "
                "changed source line, and none was named"
            ),
            "duration_seconds": round(sum(v.duration_seconds for v in verdicts), 6),
            "claims_total": len(verdicts),
            "claims_fixed": sum(1 for v in verdicts if v.classification.value == "fixed"),
            "fix_proven": any(v.classification.value == "fixed" for v in verdicts),
            "claims": [_verdict_record(v) for v in verdicts],
            # None means "not measured", never "perfect".
            "suite_strength": strength,
            "suite_strength_unverified": strength_unverified,
            "suite_strength_floor": args.suite_strength_floor,
            "suite_strength_lower": strength_judged,
            "suite_strength_confidence": args.strength_confidence,
            "mutation": mutation_detail,
            "diff_coverage_ratio": coverage_ratio,
            "diff_coverage_source": coverage_source,
            "diff_coverage": coverage_detail,
            "full_suite_ran": full_suite_ran,
            "full_suite_oracle": _full_oracle(rewritten_tests if full_suite_ran else (), oracle_full_code),
            "measurement_seconds": round(mutation_seconds, 6),
            "execution": execution,
        }
        if discovery is not None:
            payload["discovery"] = discovery
        if full_codes is not None:
            payload["full_suite_exit_codes"] = list(full_codes)
        if decisive is not None and decisive.outcome_run is not None:
            payload["evidence"] = _run_record(decisive)
        print(json.dumps(payload, indent=2, default=str))

        if args.report and log is not None:
            print(file=sys.stderr)
            print(format_report(compute_metrics(log.read_all())), file=sys.stderr)

        return DECISION_EXIT[decision]

    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:
        print(f"uncaught error: {exc}", file=sys.stderr)
        return EXIT_INCONCLUSIVE


if __name__ == "__main__":
    raise SystemExit(main())
