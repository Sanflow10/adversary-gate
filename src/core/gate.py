"""The gate: turns executed evidence into a three-valued decision.

Previous shape (removed, deliberately):

    accepted: bool                    # every error path -> False
    should_accept_patch(...) -> bool   # and False meant "approve"

Measured consequence of that shape -- all six failure routes returned True
from ``should_accept_patch``: protected-path violation, missing critic test
file, builder deleting the critic's test, open circuit breaker, flaky result,
and zero verdicts. The gate could not distinguish "verified clean" from
"could not verify", which is precisely the self-deception the product claims
to remove.

New shape:

    decide(...) -> MERGE | BLOCK | INCONCLUSIVE

``INCONCLUSIVE`` is the whole point: it is reportable, audit-friendly, and
it does not merge.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from core.circuit_breaker import CircuitBreaker
from core.contestation import Contestation, ContestationResult, adjudicate
from core.exitmap import describe
from core.evidence_log import EvidenceLog
from core.path_policy import PathPolicy
from core.types import (
    AcceptanceCriterion,
    AggressionLevel,
    BugKind,
    CriticClaim,
    Decision,
    ExecutionOutcome,
    ExecState,
    FailureClass,
    GateVerdict,
    Outcome,
    outcome_of,
)
from sandbox.runner import SandboxResult, run_test
from verifiers.coverage import DiffCoverage, covered_diff_ratio
from verifiers.stability import StabilityPolicy, policy_for
from verifiers.strength import calculate_suite_strength


@dataclass(frozen=True)
class GateConfig:
    coverage_floor: float = 0.80
    suite_strength_floor: float = 0.75


class Gate:
    """Acceptance decisions derived from execution only."""

    def __init__(
        self,
        criteria: Sequence[AcceptanceCriterion],
        aggression: AggressionLevel = AggressionLevel.NORMAL,
        max_rounds: int = 4,
        path_policy: Optional[PathPolicy] = None,
        config: Optional[GateConfig] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        evidence_log: Optional[EvidenceLog] = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if len({c.id for c in criteria}) != len(criteria):
            raise ValueError("acceptance criterion IDs must be unique")
        self.criteria = {criterion.id: criterion for criterion in criteria}
        self.aggression = aggression
        self.max_rounds = max_rounds
        self.path_policy = path_policy or PathPolicy()
        self.config = config or GateConfig()
        if not 0.0 <= self.config.coverage_floor <= 1.0:
            raise ValueError("coverage_floor must be between 0 and 1")
        if not 0.0 <= self.config.suite_strength_floor <= 1.0:
            raise ValueError("suite_strength_floor must be between 0 and 1")
        self.circuit_breaker = circuit_breaker
        self.evidence_log = evidence_log


    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def validate_claim(
        self, claim: CriticClaim, repo_dir: Optional[Path] = None
    ) -> Optional[str]:
        path = Path(claim.test_path) if claim.test_path else None
        if path is None or path.is_absolute():
            return "test_path must be a relative path"
        if ".." in path.parts:
            return "test_path cannot contain '..'"
        if not claim.test_id:
            return "test_id is required"
        if claim.cited_criterion_id is not None and claim.cited_criterion_id not in self.criteria:
            return f"unknown acceptance criterion: {claim.cited_criterion_id}"
        if repo_dir is not None:
            root = repo_dir.resolve()
            unresolved = root / path
            # is_symlink() must be checked on the *unresolved* path: resolve()
            # dereferences, so checking afterwards can never see the link.
            # ``unresolved`` is included because the target itself may be the
            # link -- checking only ``parents`` misses it.
            if any(part.is_symlink() for part in (root, unresolved, *unresolved.parents)):
                return "test_path traverses a symlink"
            candidate = unresolved.resolve()
            if root not in candidate.parents:
                return "test_path escapes repository"
            if not candidate.is_file():
                return f"test file does not exist: {claim.test_path}"
        return None

    def validate_changed_paths(self, changed_paths: Iterable[str]) -> List[str]:
        return self.path_policy.violations(changed_paths)

    # ------------------------------------------------------------------
    # per-run state resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_side(
        codes: Sequence[int],
        policy: StabilityPolicy,
        *,
        label: str,
    ) -> tuple[ExecState, bool, str]:
        """Collapse a side's exit codes into one state.

        Returns ``(state, consistent, detail)``. Any non-execution code wins
        immediately -- a run that never happened cannot be averaged in with
        runs that did.
        """
        from core.exitmap import classify_exit

        if not codes:
            return ExecState.NOT_APPLICABLE, True, f"{label}: not executed"

        states = [classify_exit(code) for code in codes]

        unrunnable = [
            code for code, state in zip(codes, states) if state is ExecState.UNRUNNABLE
        ]
        if unrunnable:
            return (
                ExecState.UNRUNNABLE,
                False,
                f"{label}: execution did not happen ({describe(unrunnable[0])})",
            )

        if any(state is ExecState.TIMED_OUT for state in states):
            return (
                ExecState.TIMED_OUT,
                len(set(states)) == 1,
                f"{label}: timed out",
            )

        failures = states.count(ExecState.FAIL)
        total = len(states)
        if failures == 0:
            return ExecState.PASS, True, f"{label}: passed {total}/{total}"
        if failures >= policy.min_required_patch_failures:
            return ExecState.FAIL, True, f"{label}: failed {failures}/{total}"
        # Some failed, not enough to satisfy the stability policy.
        return ExecState.FAIL, False, f"{label}: failed {failures}/{total} (unstable)"

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------
    def classify(self, claim: CriticClaim, run_outcome: ExecutionOutcome) -> GateVerdict:
        """Turn a baseline/patch execution pair into a three-valued verdict."""
        base = run_outcome.baseline
        patch = run_outcome.patch

        # --- infrastructure failures are never evidence -------------------
        if base is ExecState.UNRUNNABLE or patch is ExecState.UNRUNNABLE:
            side = "baseline" if base is ExecState.UNRUNNABLE else "patch"
            return GateVerdict(
                claim,
                FailureClass.UNRUNNABLE,
                Outcome.UNVERIFIED,
                f"{side} did not execute; a run that never happened proves nothing",
                run_outcome,
            )

        policy = policy_for(claim.bug_kind)
        if run_outcome.run_count > 1:
            baseline_consistent = base in (ExecState.PASS, ExecState.FAIL)
            if not baseline_consistent:
                return GateVerdict(
                    claim,
                    FailureClass.FLAKY,
                    Outcome.UNVERIFIED,
                    "baseline result was not consistent across runs",
                    run_outcome,
                )
            if base is ExecState.FAIL and run_outcome.baseline_failures not in (
                0,
                run_outcome.run_count,
            ):
                return GateVerdict(
                    claim,
                    FailureClass.FLAKY,
                    Outcome.UNVERIFIED,
                    "baseline failures were mixed across runs",
                    run_outcome,
                )
            # Patch-side stability: a result that reproduces only sometimes
            # cannot condemn a patch. All-pass and all-fail are both stable;
            # a partial count must clear the bug-kind's threshold.
            patch_failures = run_outcome.patch_failures
            stable = patch_failures in (0, run_outcome.run_count) or (
                patch_failures >= policy.min_required_patch_failures
            )
            if patch is ExecState.FAIL and not stable:
                return GateVerdict(
                    claim,
                    FailureClass.FLAKY,
                    Outcome.UNVERIFIED,
                    f"patch failed {patch_failures}/{run_outcome.run_count} runs; "
                    f"policy for {claim.bug_kind.value} requires "
                    f"{policy.min_required_patch_failures}",
                    run_outcome,
                )

        # --- timeout asymmetry: hang is a real regression ------------------
        if patch is ExecState.TIMED_OUT:
            if base is ExecState.PASS:
                return GateVerdict(
                    claim,
                    FailureClass.HANG,
                    Outcome.REFUTED,
                    "test terminates on baseline but hangs on patch",
                    run_outcome,
                )
            return GateVerdict(
                claim,
                FailureClass.FLAKY,
                Outcome.UNVERIFIED,
                "timeout on patch without a clean baseline comparison",
                run_outcome,
            )
        if base is ExecState.TIMED_OUT:
            return GateVerdict(
                claim,
                FailureClass.FLAKY,
                Outcome.UNVERIFIED,
                "baseline timed out; no clean reference to compare against",
                run_outcome,
            )

        # --- incomplete fix requires a cited criterion ---------------------
        if base is ExecState.FAIL and patch is ExecState.FAIL:
            if claim.cited_criterion_id not in self.criteria:
                return GateVerdict(
                    claim,
                    FailureClass.INVALID,
                    Outcome.UNVERIFIED,
                    "incomplete-fix claims require a valid cited acceptance criterion",
                    run_outcome,
                )
            return GateVerdict(
                claim,
                FailureClass.INCOMPLETE_FIX,
                Outcome.REFUTED,
                "test fails on baseline and patch with a valid criterion",
                run_outcome,
            )

        # --- the two real bug signals --------------------------------------
        if base is ExecState.PASS and patch is ExecState.FAIL:
            return GateVerdict(
                claim,
                FailureClass.REGRESSION,
                Outcome.REFUTED,
                "test passes on baseline and fails on patch",
                run_outcome,
            )
        if base is ExecState.NOT_APPLICABLE and patch is ExecState.FAIL:
            return GateVerdict(
                claim,
                FailureClass.NEW_BUG,
                Outcome.REFUTED,
                "test is absent on baseline and fails on patch",
                run_outcome,
            )

        # --- patch is clean -------------------------------------------------
        if patch is ExecState.PASS:
            return GateVerdict(
                claim,
                FailureClass.CLAIM_DISCARDED,
                Outcome.VERIFIED,
                "test passes on the patch",
                run_outcome,
            )

        # Nothing above matched: we cannot say, and we will not guess.
        return GateVerdict(
            claim,
            FailureClass.INVALID,
            Outcome.UNVERIFIED,
            "unsupported baseline/patch result combination",
            run_outcome,
        )

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def verify_claim(
        self,
        claim: CriticClaim,
        baseline_dir: Path,
        patch_dir: Path,
        *,
        changed_paths: Iterable[str] = (),
        timeout_seconds: int = 30,
        cpu_seconds: int = 10,
        mem_bytes: int = 512 * 1024 * 1024,
        require_network_isolation: bool = False,
        run_kwargs: Optional[dict] = None,
    ) -> GateVerdict:
        started = time.monotonic()
        extra = run_kwargs or {}

        def finish(verdict: GateVerdict) -> GateVerdict:
            verdict.duration_seconds = time.monotonic() - started
            if self.circuit_breaker is not None:
                # Circuit opens on *unverified* claims: repeated inability to
                # run is a harness problem, and it must not approve anything.
                self.circuit_breaker.record(claim.test_id, verdict.outcome)
            if self.evidence_log is not None:
                self.evidence_log.append(verdict)
            return verdict

        if self.circuit_breaker is not None and self.circuit_breaker.is_open(claim.test_id):
            return finish(
                GateVerdict(
                    claim,
                    FailureClass.CIRCUIT_OPEN,
                    Outcome.UNVERIFIED,
                    "circuit breaker is open for this test_id; not re-running",
                )
            )

        violations = self.validate_changed_paths(changed_paths)
        if violations:
            return finish(
                GateVerdict(
                    claim,
                    FailureClass.INVALID,
                    Outcome.UNVERIFIED,
                    "protected path violation: " + "; ".join(str(v) for v in violations),
                )
            )

        error = self.validate_claim(claim, patch_dir)
        if error:
            return finish(
                GateVerdict(claim, FailureClass.INVALID, Outcome.UNVERIFIED, error)
            )

        policy = policy_for(claim.bug_kind)
        baseline_applicable = (baseline_dir / claim.test_path).is_file()

        baseline_codes: List[int] = []
        patch_codes: List[int] = []
        # Retain output from the final run on each side. The original threw
        # every SandboxResult away except its exit_code, which is why the
        # evidence log contained no evidence.
        baseline_output = ""
        patch_output = ""

        for _ in range(policy.runs):
            if baseline_applicable:
                result = run_test(
                    baseline_dir,
                    claim.test_path,
                    claim.test_id,
                    timeout_seconds,
                    cpu_seconds,
                    mem_bytes,
                    require_network_isolation=require_network_isolation,
                    **extra,
                )
                baseline_codes.append(result.exit_code)
                baseline_output = result.output_tail
            result = run_test(
                patch_dir,
                claim.test_path,
                claim.test_id,
                timeout_seconds,
                cpu_seconds,
                mem_bytes,
                require_network_isolation=require_network_isolation,
                **extra,
            )
            patch_codes.append(result.exit_code)
            patch_output = result.output_tail

        baseline_state, _, baseline_detail = self._resolve_side(
            baseline_codes, policy, label="baseline"
        )
        patch_state, _, patch_detail = self._resolve_side(patch_codes, policy, label="patch")

        if not baseline_applicable:
            baseline_state = ExecState.NOT_APPLICABLE

        run_outcome = ExecutionOutcome(
            baseline_state,
            patch_state,
            baseline_failures=sum(1 for c in baseline_codes if c != 0),
            patch_failures=sum(1 for c in patch_codes if c != 0),
            run_count=policy.runs,
            baseline_exit_codes=tuple(baseline_codes),
            patch_exit_codes=tuple(patch_codes),
            baseline_output=baseline_output,
            patch_output=patch_output,
        )

        verdict = self.classify(claim, run_outcome)
        strength = calculate_suite_strength(1.0, patch_output)
        verdict.suite_strength = strength.score

        if verdict.outcome is Outcome.UNVERIFIED:
            # Attach *how* each side behaved: an unverified verdict that does
            # not say what broke is indistinguishable from a clean one in a
            # log, which is how self-deception survives an audit trail.
            verdict.reason = f"{verdict.reason} [{baseline_detail}; {patch_detail}]"
        return finish(verdict)

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------
    def decide(
        self,
        verdicts: Sequence[GateVerdict],
        rounds_used: int,
        diff_coverage_ratio: float,
        suite_strength: Optional[float] = None,
        full_suite_passed: bool = True,
    ) -> Decision:
        """Patch-level decision. Fail-closed by construction.

        The only way to reach ``MERGE`` is for every verdict to be
        ``VERIFIED``, coverage to clear the floor, and suite strength to clear
        the strength floor. Missing verdicts, weak suites, malformed inputs,
        unverified claims and open circuits all land on ``INCONCLUSIVE``, which
        is reportable and non-merging.
        """
        if not 0 <= rounds_used <= self.max_rounds:
            return Decision.INCONCLUSIVE
        if not 0.0 <= diff_coverage_ratio <= 1.0:
            return Decision.INCONCLUSIVE
        if diff_coverage_ratio < self.config.coverage_floor:
            return Decision.INCONCLUSIVE

        effective_strength = (
            min((v.suite_strength for v in verdicts), default=1.0)
            if suite_strength is None
            else suite_strength
        )
        if effective_strength < self.config.suite_strength_floor:
            return Decision.INCONCLUSIVE

        if not full_suite_passed:
            return Decision.BLOCK

        if self.aggression is AggressionLevel.NUCLEAR and rounds_used < self.max_rounds:
            return Decision.INCONCLUSIVE
        if not verdicts:
            # Nothing was ever checked. "No verdicts" is not "no problems".
            return Decision.INCONCLUSIVE

        outcomes = {verdict.outcome for verdict in verdicts}
        if Outcome.REFUTED in outcomes:
            return Decision.BLOCK
        if Outcome.UNVERIFIED in outcomes:
            return Decision.INCONCLUSIVE
        return Decision.MERGE


    def should_accept_patch(
        self, verdicts: Sequence[GateVerdict], rounds_used: int, diff_coverage_ratio: float
    ) -> bool:
        """Compatibility shim. Prefer :meth:`decide`."""
        return self.decide(verdicts, rounds_used, diff_coverage_ratio) is Decision.MERGE

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def diff_coverage(self, diff_text: str, coverage_json: Path | dict) -> DiffCoverage:
        return covered_diff_ratio(diff_text, coverage_json)

    def adjudicate_contestation(
        self,
        original: GateVerdict,
        contest: Contestation,
        counter_outcome: ExecutionOutcome,
    ) -> ContestationResult:
        return adjudicate(original, contest, counter_outcome)
