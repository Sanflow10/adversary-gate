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
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.exitmap import PYTEST_OK
from core.gate import Gate, GateConfig
from core.metrics import compute_metrics, format_report
from core.types import (
    AcceptanceCriterion,
    AggressionLevel,
    BugKind,
    CriticClaim,
    Decision,
    Outcome,
)
from sandbox.runner import SandboxResult, bwrap_available, run_test
from verifiers.coverage import covered_diff_ratio, validate_diff
from verifiers.strength import classify_changes, measure_mutation_score

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


def _claim_from_args(args: argparse.Namespace) -> CriticClaim:
    if args.claim_json:
        source = Path(args.claim_json)
        raw = source.read_text(encoding="utf-8") if source.is_file() else args.claim_json
        data = json.loads(raw)
        if (
            set(data) != {"claims"}
            or not isinstance(data["claims"], list)
            or len(data["claims"]) != 1
        ):
            raise ValueError("claim JSON must contain exactly one claim")
        data = data["claims"][0]
        return CriticClaim(
            data["test_path"],
            data["test_id"],
            BugKind(data.get("bug_kind", "deterministic")),
            data.get("cited_criterion_id"),
            data.get("rationale", ""),
        )
    if not args.test_path:
        raise ValueError("--test-path is required unless --claim-json is used")
    if not args.test_id and not args.test_command:
        raise ValueError(
            "--test-id is required unless --test-command or --claim-json is used"
        )
    # A suite that is one arbitrary command has no node IDs to name, but the
    # claim still needs a non-empty identity for the circuit breaker and the
    # evidence trail. Say what it is rather than inventing a test name.
    test_id = args.test_id or "(test-command)"
    return CriticClaim(
        args.test_path, test_id, BugKind(args.bug_kind), args.criterion_id, args.rationale
    )


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


def _summary(verdicts) -> Dict[str, Any]:
    return {
        "claims_total": len(verdicts),
        "verified": sum(1 for v in verdicts if v.outcome.value == "verified"),
        "refuted": sum(1 for v in verdicts if v.outcome.value == "refuted"),
        "unverified": sum(1 for v in verdicts if v.outcome.value == "unverified"),
        "classifications": [v.classification.value for v in verdicts],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="adversary-gate")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--test-path")
    parser.add_argument("--test-id")
    parser.add_argument("--claim-json")
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
        "--mutation-max",
        type=int,
        default=6,
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
    try:
        parser = build_parser()
        args = parser.parse_args(argv)

        criteria: List[AcceptanceCriterion] = []
        for raw in args.criterion:
            if ":" not in raw:
                raise ValueError("--criterion must use ID:TEXT")
            identifier, text = raw.split(":", 1)
            if not identifier or not text:
                raise ValueError("--criterion must have a non-empty ID and text")
            criteria.append(AcceptanceCriterion(identifier, text))

        claim = _claim_from_args(args)

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
            from core.evidence_log import EvidenceLog

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
        verdict = gate.verify_claim(
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
            run_kwargs={"command": args.test_command, "sandbox": args.sandbox},
        )

        # ------------------------------------------------------------------
        # Patch-level measurements. Both used to be parameters that defaulted
        # to "fine" and were never assigned anything else, so the two gates in
        # ``decide`` that consume them were unreachable. They are computed
        # here, from execution, and recorded -- a strength value that never
        # reaches the evidence artefact cannot be audited either.
        # ------------------------------------------------------------------
        started = time.monotonic()
        strength: Optional[float] = None
        strength_unverified = False
        mutation_detail: Dict[str, Any]

        # The diff's paths (resolved above) are asked of the directory scan's
        # question too: "the patch changed source we cannot judge" does not
        # become false when somebody turns mutation off.
        changed_paths = diff_paths

        if args.mutation_max <= 0:
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
                "reason": "disabled (--mutation-max 0); what changed below is "
                "still measured, the mutation score is not",
                **classify_changes(Path(args.baseline), Path(args.patch), changed_paths),
                "mutants": [],
                "survivors": [],
                "mutants_counted": 0,
                "stillborn": 0,
            }
            if verdict.outcome is Outcome.VERIFIED and (
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
                claim.test_path,
                test_id="",
                max_mutants=args.mutation_max,
                changed_paths=changed_paths,
            )
            if strength_obj.is_measured:
                # A measured score covers exactly the Python files it mutated
                # -- never the whole patch. Whether it is allowed to stand is
                # decided just below.
                strength = strength_obj.mutation_score

            if verdict.outcome is Outcome.VERIFIED:
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
                    # There was code to judge, the claim itself executed and came
                    # back VERIFIED, and we still could not produce a score. That
                    # is *unknown*, not strong, so it must not reach MERGE. When
                    # the claim did not verify, its own outcome already decides and
                    # strength would only add noise to the reason.
                    strength_unverified = True

        full_codes: Optional[Sequence[int]] = None
        # ``--test-command`` implies the collateral run uses it too, unless
        # ``--full-suite-command`` overrides. A repository that does not run
        # pytest must not silently lose the collateral-regression check just
        # because it declared how its tests execute.
        full_command = args.full_suite_command or args.test_command
        full_suite_ran = bool(full_command) or bool(args.full_suite_path)

        def _full_run(directory: Path) -> SandboxResult:
            if full_command:
                return run_test(
                    directory,
                    "",
                    "",
                    command=full_command,
                    sandbox=args.sandbox,
                    require_network_isolation=args.require_network_isolation,
                )
            return run_test(
                directory,
                args.full_suite_path,
                "",
                sandbox=args.sandbox,
                require_network_isolation=args.require_network_isolation,
            )

        if full_suite_ran:
            # Only pay for the baseline run when the patch side actually failed;
            # a passing patch side is not a collateral regression either way.
            # The artefact records ``full_suite_ran`` so a missing
            # ``full_suite_exit_codes`` can be told apart from a clean run --
            # v2.0.1 wrote ``null`` for both, which made "the suite passed" and
            # "the suite never ran" indistinguishable in the evidence.
            patch_full = _full_run(Path(args.patch))
            if patch_full.exit_code != PYTEST_OK:
                base_full = _full_run(Path(args.baseline))
                full_codes = (base_full.exit_code, patch_full.exit_code)
        mutation_seconds = time.monotonic() - started

        decision = gate.decide(
            [verdict],
            args.rounds_used,
            coverage_ratio,
            suite_strength=strength,
            full_suite_exit_codes=full_codes,
            suite_strength_unverified=strength_unverified,
        )

        if log is not None:
            log.append_decision(
                decision.value,
                args.rounds_used,
                coverage_ratio,
                {
                    **_summary([verdict]),
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
                    "mutation": mutation_detail,
                    "full_suite_ran": full_suite_ran,
                    "full_suite_exit_codes": list(full_codes) if full_codes else None,
                },
            )

        payload = {
            "classification": verdict.classification.value,
            "outcome": verdict.outcome.value,
            "decision": decision.value,
            "reason": verdict.reason,
            "duration_seconds": round(verdict.duration_seconds, 6),
            # None means "not measured", never "perfect".
            "suite_strength": strength,
            "suite_strength_unverified": strength_unverified,
            "suite_strength_floor": args.suite_strength_floor,
            "mutation": mutation_detail,
            "diff_coverage_ratio": coverage_ratio,
            "diff_coverage_source": coverage_source,
            "diff_coverage": coverage_detail,
            "full_suite_ran": full_suite_ran,
            "measurement_seconds": round(mutation_seconds, 6),
        }
        if full_codes is not None:
            payload["full_suite_exit_codes"] = list(full_codes)
        if verdict.outcome_run is not None:
            payload["evidence"] = {
                "baseline": verdict.outcome_run.baseline.value,
                "patch": verdict.outcome_run.patch.value,
                "baseline_exit_codes": list(verdict.outcome_run.baseline_exit_codes),
                "patch_exit_codes": list(verdict.outcome_run.patch_exit_codes),
            }
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
