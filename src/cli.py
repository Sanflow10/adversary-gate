"""Command-line interface.

Exit codes are part of the contract, because CI reads them:

    0  -> MERGE       every claim executed and none condemned the patch
    1  -> BLOCK       at least one claim was REFUTED by execution
    2  -> INCONCLUSIVE something could not be verified (does not merge)
    3  -> usage / input error

The original returned ``0 if verdict.accepted else 1``, i.e. a *confirmed
regression* produced exit 0. Plugged into a CI step that treats 0 as success,
a proven bug authorized the merge -- the opposite of a gate. A second flaw:
``--aggression`` and ``--max-rounds`` were parsed and passed to ``Gate`` but
never read, because ``should_accept_patch`` was never called. Both are gone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.gate import Gate
from core.metrics import compute_metrics, format_report
from core.types import (
    AcceptanceCriterion,
    AggressionLevel,
    BugKind,
    CriticClaim,
    Decision,
)

EXIT_MERGE = 0
EXIT_BLOCK = 1
EXIT_INCONCLUSIVE = 2
EXIT_USAGE = 3

DECISION_EXIT = {
    Decision.MERGE: EXIT_MERGE,
    Decision.BLOCK: EXIT_BLOCK,
    Decision.INCONCLUSIVE: EXIT_INCONCLUSIVE,
}


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
    parser = argparse.ArgumentParser(prog="adversary-gate")
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
    parser.add_argument("--rounds-used", type=int, default=4)
    parser.add_argument("--evidence-log", help="path to the evidence artefact (JSONL)")
    parser.add_argument("--model", help="model that produced the patch")
    parser.add_argument("--commit", help="commit sha of the patch")
    parser.add_argument("--report", action="store_true", help="print the metrics report")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
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

        gate = Gate(
            criteria,
            AggressionLevel(args.aggression),
            args.max_rounds,
            evidence_log=log,
        )
        verdict = gate.verify_claim(
            claim,
            Path(args.baseline),
            Path(args.patch),
            changed_paths=args.changed_path,
        )

        decision = gate.decide([verdict], args.rounds_used, args.coverage_ratio)

        if log is not None:
            log.append_decision(
                decision.value,
                args.rounds_used,
                args.coverage_ratio,
                _summary([verdict]),
            )

        payload = {
            "classification": verdict.classification.value,
            "outcome": verdict.outcome.value,
            "decision": decision.value,
            "reason": verdict.reason,
            "duration_seconds": round(verdict.duration_seconds, 6),
        }
        if verdict.outcome_run is not None:
            payload["evidence"] = {
                "baseline": verdict.outcome_run.baseline.value,
                "patch": verdict.outcome_run.patch.value,
                "baseline_exit_codes": list(verdict.outcome_run.baseline_exit_codes),
                "patch_exit_codes": list(verdict.outcome_run.patch_exit_codes),
            }
        print(json.dumps(payload, indent=2))

        if args.report and log is not None:
            print()
            print(format_report(compute_metrics(log.read_all())))

        return DECISION_EXIT[decision]

    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
