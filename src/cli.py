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
from sandbox.runner import run_test
from verifiers.strength import changed_source_files, measure_mutation_score

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
    if not args.test_path or not args.test_id:
        raise ValueError("--test-path and --test-id are required unless --claim-json is used")
    return CriticClaim(
        args.test_path, args.test_id, BugKind(args.bug_kind), args.criterion_id, args.rationale
    )


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
    parser.add_argument("--coverage-ratio", type=float, default=1.0)
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
    parser.add_argument("--rounds-used", type=int, default=4)
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
            changed_paths=args.changed_path,
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

        if args.mutation_max <= 0:
            mutation_detail = {"measured": False, "reason": "disabled (--mutation-max 0)"}
        else:
            strength_obj, mutation_detail = measure_mutation_score(
                Path(args.baseline),
                Path(args.patch),
                claim.test_path,
                test_id="",
                max_mutants=args.mutation_max,
            )
            if strength_obj.is_measured:
                strength = strength_obj.mutation_score
            elif mutation_detail.get("changed_files") and verdict.outcome is Outcome.VERIFIED:
                # There was code to judge, the claim itself executed and came
                # back VERIFIED, and we still could not produce a score. That
                # is *unknown*, not strong, so it must not reach MERGE. When
                # the claim did not verify, its own outcome already decides and
                # strength would only add noise to the reason.
                strength_unverified = True

        full_codes: Optional[Sequence[int]] = None
        if args.full_suite_path:
            # Only pay for the baseline run when the patch side actually failed;
            # a passing patch side is not a collateral regression either way.
            patch_full = run_test(Path(args.patch), args.full_suite_path, "")
            if patch_full.exit_code != PYTEST_OK:
                base_full = run_test(Path(args.baseline), args.full_suite_path, "")
                full_codes = (base_full.exit_code, patch_full.exit_code)
        mutation_seconds = time.monotonic() - started

        decision = gate.decide(
            [verdict],
            args.rounds_used,
            args.coverage_ratio,
            suite_strength=strength,
            full_suite_exit_codes=full_codes,
            suite_strength_unverified=strength_unverified,
        )

        if log is not None:
            log.append_decision(
                decision.value,
                args.rounds_used,
                args.coverage_ratio,
                {
                    **_summary([verdict]),
                    # The floor only ever receives a real measurement, so a
                    # reader can tell "strong" from "not measured" from "weak".
                    "suite_strength": strength,
                    "suite_strength_unverified": strength_unverified,
                    "suite_strength_floor": args.suite_strength_floor,
                    "mutation": mutation_detail,
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
