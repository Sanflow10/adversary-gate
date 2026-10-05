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

import ast
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from adversary_gate.core.circuit_breaker import CircuitBreaker
from adversary_gate.core.contestation import Contestation, ContestationResult, adjudicate
from adversary_gate.core.exitmap import PYTEST_OK, PYTEST_TESTS_FAILED, describe
from adversary_gate.core.evidence_log import EvidenceLog
from adversary_gate.core.path_policy import PathPolicy, harness_drift
from adversary_gate.core.types import (
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
from adversary_gate.sandbox.runner import SandboxResult, run_test
from adversary_gate.verifiers.coverage import DiffCoverage, covered_diff_ratio
from adversary_gate.verifiers.stability import StabilityPolicy, policy_for
from adversary_gate.verifiers.testpaths import is_test_path


def _bytes_differ(before: Path, after: Path) -> bool:
    """Whether two files differ -- and ``True`` when either cannot be read.

    "Unreadable" must not read as "unchanged": the caller uses this to decide
    whether an oracle can be trusted, and unknown is not trusted.
    """
    try:
        return before.read_bytes() != after.read_bytes()
    except OSError:
        return True


#: Tooling state that never belongs in a transplanted tree.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".pytest_cache", "node_modules"
)
_TREE_SKIP = frozenset({".git", "__pycache__", ".pytest_cache", "node_modules", ".tox", ".nox"})

#: ``test_add[1-2]`` -> ``test_add``: a parametrised id names one function.
_PARAMS = re.compile(r"\[.*\]$")


def _defines_test(test_file: Path, test_id: str) -> Optional[bool]:
    """Whether ``test_id`` is written in ``test_file`` -- ``None`` when we cannot tell.

    Read from the AST rather than by running pytest, so the answer does not
    depend on the file being importable. A test that exists only through
    inheritance or generation reads as absent; the caller treats "absent" as
    the stricter branch (it still re-runs every test the baseline file had),
    so a miss here costs a run, never a false verdict.
    """
    if not test_id:
        return True  # the whole file is the claim
    if test_file.suffix != ".py":
        return None
    try:
        body = ast.parse(test_file.read_text()).body
    except (OSError, SyntaxError, ValueError):
        return None
    for name in (_PARAMS.sub("", part) for part in test_id.split("::")):
        node = next(
            (
                n for n in body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == name
            ),
            None,
        )
        if node is None:
            return False
        body = getattr(node, "body", [])
    return True


def _baseline_test_files(baseline_dir: Path):
    base = Path(baseline_dir)
    for current, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _TREE_SKIP and not (Path(current) / d / "pyvenv.cfg").is_file()]
        for name in files:
            source = Path(current) / name
            rel = source.relative_to(base).as_posix()
            if not source.is_symlink() and is_test_path(rel):
                yield rel, source


def _rewritten_test_files(baseline_dir: Path, patch_dir: Path) -> List[str]:
    """Baseline test files the patch changed or deleted."""
    return sorted(
        rel for rel, source in _baseline_test_files(baseline_dir)
        if _bytes_differ(source, Path(patch_dir) / rel)
    )


def _transplant(baseline_dir: Path, patch_dir: Path, test_path: Optional[str], into: Path) -> Path:
    """The patch's code with the baseline's tests put back.

    Not only ``test_path``: every file the baseline had that counts as a test
    file (``is_test_path`` -- ``test_*``, ``*_test.py``, anything under a
    ``tests`` directory) is restored, changed or deleted by the patch alike.
    Otherwise the baseline's test would run on top of the patch's
    ``tests/helpers.py``, and a rewritten helper is a rewritten answer. Test
    files the patch *added* stay: nothing on the baseline speaks for them.
    """
    tree = into / "tree"
    shutil.copytree(patch_dir, tree, symlinks=True, ignore=_COPY_IGNORE)
    restore = dict(_baseline_test_files(baseline_dir))
    if test_path:
        restore.setdefault(test_path, Path(baseline_dir) / test_path)
    for rel, source in restore.items():
        target = tree / rel
        if target.is_symlink() or target.is_dir():
            continue  # the harness/path policies own these shapes
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return tree


def rewritten_test_files(baseline_dir: Path, patch_dir: Path) -> List[str]:
    """Public: the baseline test files (declared support included) the patch changed or deleted."""
    return _rewritten_test_files(baseline_dir, patch_dir)


def transplant_tree(baseline_dir: Path, patch_dir: Path, into: Path) -> Path:
    """Public: a copy of the patch's tree with every baseline test file put back."""
    return _transplant(baseline_dir, patch_dir, None, into)


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

    def validate_changed_paths(
        self, changed_paths: Iterable[str], repo_dir: Optional[Path] = None
    ) -> List[str]:
        """Violations for ``changed_paths``, resolved against ``repo_dir`` when given.

        ``repo_dir`` is what makes the symlink check in :class:`PathPolicy`
        reachable at all -- without it the policy can only inspect the string.
        Callers that hold a directory (``Gate.verify_claim`` holds the patch)
        must pass it, or the policy's guarantee is dead code.
        """
        return [str(v) for v in self.path_policy.violations(changed_paths, repo_dir)]

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
        from adversary_gate.core.exitmap import classify_exit

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
            # There used to be a blanket guard here: "baseline must be PASS or
            # FAIL, otherwise UNVERIFIED with 'baseline result was not
            # consistent across runs'". It rejected ExecState.NOT_APPLICABLE
            # and ExecState.TIMED_OUT -- and since every StabilityPolicy runs
            # more than once (3 / 100 / 5), it fired on *every* claim.
            # Two branches below that are written to handle exactly those two
            # states, NEW_BUG ("test is absent on baseline and fails on
            # patch") and "baseline timed out; no clean reference", were
            # therefore unreachable, and a patch that adds a new test was
            # reported as flaky instead of verified. The guard is gone; each
            # state now reaches the branch that names it.
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

        # --- patch is clean: say *how* (AG-030) -----------------------------
        if patch is ExecState.PASS:
            if base is ExecState.FAIL:
                return GateVerdict(
                    claim,
                    FailureClass.FIXED,
                    Outcome.VERIFIED,
                    "test fails on baseline and passes on patch: the patch fixes "
                    "what this test checks",
                    run_outcome,
                )
            if base is ExecState.PASS:
                return GateVerdict(
                    claim,
                    FailureClass.NO_REGRESSION,
                    Outcome.VERIFIED,
                    "test passes on baseline and on patch: nothing it checks "
                    "regressed (this is not evidence of a fix)",
                    run_outcome,
                )
            return GateVerdict(
                claim,
                FailureClass.CLAIM_DISCARDED,
                Outcome.VERIFIED,
                "test passes on the patch (it does not exist on the baseline)",
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
        timeout_seconds: Optional[int] = 30,
        cpu_seconds: Optional[int] = 10,
        mem_bytes: Optional[int] = 512 * 1024 * 1024,
        require_network_isolation: bool = False,
        run_kwargs: Optional[dict] = None,
        file_limits: Optional[dict] = None,
    ) -> GateVerdict:
        """``file_limits`` (``timeout_seconds``/``cpu_seconds``/``mem_bytes``)
        bound runs of a *whole test file* -- the check that every test the
        baseline shipped in a file still passes. Absent, they are the per-test
        limits, which killed real files on both sides (AG-036)."""
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

        # The patch directory is what ``changed_paths`` describes, so it is the
        # root the policy resolves against. Passing no root here (as v2.0.1 did)
        # disables the symlink half of the policy entirely.
        violations = self.validate_changed_paths(changed_paths, patch_dir)
        if violations:
            return finish(
                GateVerdict(
                    claim,
                    FailureClass.INVALID,
                    Outcome.UNVERIFIED,
                    "protected path violation: " + "; ".join(str(v) for v in violations),
                )
            )

        # AG-032. The paths above are only the ones somebody reported; this
        # compares the trees, so a harness file the diff left out -- or one
        # whose name the denylist never listed -- still cannot configure the
        # runner that judges the patch.
        drift = harness_drift(baseline_dir, patch_dir)
        if drift:
            return finish(
                GateVerdict(
                    claim,
                    FailureClass.INVALID,
                    Outcome.UNVERIFIED,
                    "test harness differs from the baseline: "
                    + "; ".join(str(v) for v in drift)
                    + ". The patch does not get to configure the runner that "
                    "judges it; review the harness change on its own",
                )
            )

        error = self.validate_claim(claim, patch_dir)
        if error:
            return finish(
                GateVerdict(claim, FailureClass.INVALID, Outcome.UNVERIFIED, error)
            )

        policy = policy_for(claim.bug_kind)
        baseline_applicable = (baseline_dir / claim.test_path).is_file()
        limits = dict(
            timeout_seconds=timeout_seconds,
            cpu_seconds=cpu_seconds,
            mem_bytes=mem_bytes,
            require_network_isolation=require_network_isolation,
            extra=extra,
        )

        # AG-021. The claim's test is read from the *patch* tree, so a patch
        # that rewrites it also writes the answer it will be judged against.
        # Only a test that already existed on the baseline can have been
        # rewritten; a test the patch adds has no earlier answer to change.
        # A helper is part of the answer too: a patch that leaves the claim's
        # file alone and bends ``tests/helpers.py`` rewrote the test all the
        # same, so any baseline test file the patch changed engages the oracle.
        rewritten = baseline_applicable and (
            _bytes_differ(baseline_dir / claim.test_path, patch_dir / claim.test_path)
            or bool(_rewritten_test_files(baseline_dir, patch_dir))
        )
        if not rewritten:
            verdict, details = self._judge(claim, baseline_dir, patch_dir, baseline_applicable, policy, limits)
            verdict = self._fail_to_pass(verdict, claim, baseline_dir, patch_dir, policy, limits)
            return finish(self._explain(verdict, details))

        # The baseline oracle. Whatever the patch did to the test file, the
        # baseline's copy of it is what the patch's code answers to: it is
        # transplanted into a copy of the patch tree and run there.
        present = _defines_test(baseline_dir / claim.test_path, claim.test_id)
        if extra.get("command"):
            # A command suite reads ``test_id`` as a label, not as a node to
            # collect: whatever the command runs, it runs from the transplanted
            # files. Nothing to locate, so nothing to give up on.
            present = True
        if present is None:
            return finish(self._rewritten_unjudgeable(claim, baseline_dir, patch_dir, policy, limits))

        with tempfile.TemporaryDirectory(prefix="adversary-oracle-") as scratch:
            tree = _transplant(baseline_dir, patch_dir, claim.test_path, Path(scratch))
            if present:
                verdict, details = self._judge(claim, baseline_dir, tree, True, policy, limits)
                verdict.oracle = "baseline"
                changed = _rewritten_test_files(baseline_dir, patch_dir) or [claim.test_path]
                verdict.reason = (
                    f"judged by the baseline's version of {claim.test_path} and its test "
                    f"files (the patch rewrote {', '.join(changed)}): {verdict.reason}"
                )
                if verdict.outcome is Outcome.REFUTED:
                    # AG-038: a fix that changes behaviour on purpose, and edits
                    # the old test to match, is blocked here exactly like a bug
                    # hidden behind a bent assertion (more-itertools d71c4ad).
                    # From execution alone the two are the same patch.
                    verdict.reason += (
                        ". If this behaviour change is intended, the edit to the "
                        "baseline's test is the thing to review: a human approves it, "
                        "the gate cannot tell it from a test bent to hide a bug"
                    )
                return finish(self._explain(verdict, details))

            # The claim is a test the patch added to a file that already
            # existed. Nothing on the baseline can judge the new test itself --
            # it is judged like any added test -- but every test the baseline
            # shipped in that file must still hold on the patch's code, or the
            # rewrite is where a weakened assertion hides.
            kept = self._baseline_file_holds(
                claim, baseline_dir, tree, policy, {**limits, **(file_limits or {})}
            )
        if kept is not None:
            return finish(kept)
        verdict, details = self._judge(claim, baseline_dir, patch_dir, False, policy, limits)
        verdict = self._fail_to_pass(verdict, claim, baseline_dir, patch_dir, policy, limits)
        verdict.oracle = "baseline-file"
        verdict.reason = (
            f"{claim.test_id} is new in {claim.test_path}; every test the baseline "
            f"shipped in that file passes on the patch, and the new one: {verdict.reason}"
        )
        return finish(self._explain(verdict, details))

    # ------------------------------------------------------------------
    # oracle plumbing
    # ------------------------------------------------------------------
    def _runs(self, directory: Path, test_path: str, test_id: str, policy, limits) -> tuple:
        codes: List[int] = []
        output = ""
        for _ in range(policy.runs):
            result = run_test(
                directory,
                test_path,
                test_id,
                limits["timeout_seconds"],
                limits["cpu_seconds"],
                limits["mem_bytes"],
                require_network_isolation=limits["require_network_isolation"],
                **limits["extra"],
            )
            codes.append(result.exit_code)
            # Retain output from the final run. The original threw every
            # SandboxResult away except its exit_code, which is why the
            # evidence log contained no evidence.
            output = result.output_tail
        return codes, output

    def _judge(
        self, claim, baseline_dir: Path, patch_dir: Path, baseline_applicable: bool, policy, limits
    ) -> tuple:
        """Run the claim on both sides and classify; ``patch_dir`` may be a transplant."""
        baseline_codes: List[int] = []
        baseline_output = ""
        if baseline_applicable:
            baseline_codes, baseline_output = self._runs(
                baseline_dir, claim.test_path, claim.test_id, policy, limits
            )
        patch_codes, patch_output = self._runs(
            patch_dir, claim.test_path, claim.test_id, policy, limits
        )

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
        return self.classify(claim, run_outcome), (baseline_detail, patch_detail)

    def _fail_to_pass(self, verdict: GateVerdict, claim, baseline_dir: Path, patch_dir: Path,
                      policy, limits) -> GateVerdict:
        """A test the patch added, run against the baseline's *code* (SWE-bench's FAIL_TO_PASS).

        Only a ``discarded`` verdict is examined: a test with no baseline copy
        that passes on the patch. The baseline tree gets the patch's test files
        laid over it and the claim runs there. Every run failing with exit 1
        upgrades it to ``fixed``: the patch makes pass a test that fails on the
        code it replaced. Passing there too, or not running at all (exit 2: it
        imports something the patch added), leaves it ``discarded`` and says
        which -- a collection error is not a failing assertion, so a test that
        only imports a new name cannot prove a fix. The decision never changes:
        ``discarded`` and ``fixed`` are both VERIFIED; only ``fix_proven`` does.
        """
        if verdict.classification is not FailureClass.CLAIM_DISCARDED:
            return verdict
        with tempfile.TemporaryDirectory(prefix="adversary-f2p-") as scratch:
            tree = Path(scratch) / "tree"
            shutil.copytree(baseline_dir, tree, symlinks=True, ignore=_COPY_IGNORE)
            for rel, source in _baseline_test_files(patch_dir):
                target = tree / rel
                if target.is_symlink() or target.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            codes, output = self._runs(tree, claim.test_path, claim.test_id, policy, limits)
        if codes and all(code == PYTEST_TESTS_FAILED for code in codes):
            run = verdict.outcome_run
            fixed = GateVerdict(
                claim,
                FailureClass.FIXED,
                Outcome.VERIFIED,
                "the test the patch added fails on the baseline's code "
                f"({len(codes)}/{len(codes)} runs) and passes on the patch: the patch "
                "fixes what this test checks",
                ExecutionOutcome(
                    ExecState.FAIL,
                    run.patch if run is not None else ExecState.PASS,
                    baseline_failures=len(codes),
                    patch_failures=run.patch_failures if run is not None else 0,
                    run_count=policy.runs,
                    baseline_exit_codes=tuple(codes),
                    patch_exit_codes=run.patch_exit_codes if run is not None else (),
                    baseline_output=output,
                    patch_output=run.patch_output if run is not None else "",
                ),
            )
            fixed.oracle = verdict.oracle
            return fixed
        if codes and all(code == PYTEST_OK for code in codes):
            verdict.reason = (
                f"{verdict.reason}; it also passes on the baseline's code, so it does not "
                "tell the two apart (not evidence of a fix)"
            )
        else:
            verdict.reason = (
                f"{verdict.reason}; it could not run against the baseline's code "
                f"(exit codes {list(codes)}: typically it imports something the patch "
                "added), so it is not evidence of a fix"
            )
        return verdict

    @staticmethod
    def _explain(verdict: GateVerdict, details: tuple) -> GateVerdict:
        if verdict.outcome is Outcome.UNVERIFIED:
            # Attach *how* each side behaved: an unverified verdict that does
            # not say what broke is indistinguishable from a clean one in a
            # log, which is how self-deception survives an audit trail.
            verdict.reason = f"{verdict.reason} [{details[0]}; {details[1]}]"
        return verdict

    def _baseline_file_holds(
        self, claim, baseline_dir: Path, tree: Path, policy, limits
    ) -> Optional[GateVerdict]:
        """``None`` when the baseline's whole test file passes on both sides.

        Otherwise the verdict that says why the new test cannot be judged on
        its own: the baseline's tests fail on the patch's code (REFUTED), or
        there is no clean reference to compare against (UNVERIFIED).
        """
        base_codes, base_out = self._runs(baseline_dir, claim.test_path, "", policy, limits)
        tree_codes, tree_out = self._runs(tree, claim.test_path, "", policy, limits)
        base_state, _, base_detail = self._resolve_side(base_codes, policy, label="baseline file")
        tree_state, tree_stable, tree_detail = self._resolve_side(
            tree_codes, policy, label="baseline file on patch"
        )
        run_outcome = ExecutionOutcome(
            base_state,
            tree_state,
            baseline_failures=sum(1 for c in base_codes if c != 0),
            patch_failures=sum(1 for c in tree_codes if c != 0),
            run_count=policy.runs,
            baseline_exit_codes=tuple(base_codes),
            patch_exit_codes=tuple(tree_codes),
            baseline_output=base_out,
            patch_output=tree_out,
        )
        if base_state is ExecState.PASS and tree_state is ExecState.PASS:
            return None
        if base_state is ExecState.PASS and tree_state is ExecState.FAIL and tree_stable:
            verdict = GateVerdict(
                claim,
                FailureClass.REGRESSION,
                Outcome.REFUTED,
                f"the patch rewrote {claim.test_path}, and a test the baseline shipped "
                "in it fails on the patch's code",
                run_outcome,
            )
        else:
            verdict = GateVerdict(
                claim,
                FailureClass.INVALID,
                Outcome.UNVERIFIED,
                f"the patch rewrote {claim.test_path} and added {claim.test_id} to it, "
                "but the baseline's tests in that file give no clean reference "
                f"[{base_detail}; {tree_detail}]",
                run_outcome,
            )
        verdict.oracle = "baseline-file"
        return verdict

    def _rewritten_unjudgeable(self, claim, baseline_dir, patch_dir, policy, limits) -> GateVerdict:
        """A rewritten test we cannot transplant: the pre-oracle rule (AG-021)."""
        verdict, details = self._judge(claim, baseline_dir, patch_dir, True, policy, limits)
        if verdict.outcome is Outcome.VERIFIED:
            # Only VERIFIED is withdrawn. REFUTED stays: a rewritten test that
            # still fails on the patch is direct evidence against it.
            verdict = GateVerdict(
                claim,
                FailureClass.INVALID,
                Outcome.UNVERIFIED,
                f"the patch rewrote the claim's own test ({claim.test_path}) and the "
                "baseline's version could not be located to judge it with; a verdict "
                "from a test the patch itself changed is not evidence",
                verdict.outcome_run,
            )
        return self._explain(verdict, details)

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------
    def decide(
        self,
        verdicts: Sequence[GateVerdict],
        rounds_used: int,
        diff_coverage_ratio: Optional[float],
        suite_strength: Optional[float] = None,
        full_suite_passed: bool = True,
        full_suite_exit_codes: Optional[Sequence[int]] = None,
        suite_strength_unverified: bool = False,
    ) -> Decision:
        """Patch-level decision. Fail-closed by construction.

        The only way to reach ``MERGE`` is for every verdict to be
        ``VERIFIED``, coverage to clear the floor, suite strength to clear the
        strength floor, and the full suite to still pass on the patch side.
        Missing verdicts, weak suites, malformed inputs, unverified claims and
        open circuits all land on ``INCONCLUSIVE``, which is reportable and
        non-merging.

        Precedence is deliberate and is ``BLOCK`` > ``INCONCLUSIVE`` >
        ``MERGE``, ordered so that direct evidence always outranks missing
        evidence:

        1. malformed control input (``rounds_used``)   -> ``INCONCLUSIVE``
        2. any verdict ``REFUTED``                    -> ``BLOCK``   (we *saw* it break)
        3. full suite regressed                       -> ``BLOCK``   (we saw it break elsewhere)
        4. coverage missing / out of range / below floor -> ``INCONCLUSIVE``
        5. weak / unmeasured suite strength           -> ``INCONCLUSIVE``
        6. any verdict ``UNVERIFIED``                 -> ``INCONCLUSIVE``
        7. suite could not be judged                  -> ``INCONCLUSIVE``
        8. exhausted budget, no verdicts              -> ``INCONCLUSIVE``

        Order matters in both directions. Coverage used to be checked *before*
        ``REFUTED``, so a patch whose claim execution proved a regression was
        reported as ``INCONCLUSIVE`` whenever coverage was low -- missing
        evidence outranking evidence we watched happen, which is the opposite
        of the rule this method documents. It now sits below both ``BLOCK``
        routes.

        ``diff_coverage_ratio`` is ``Optional[float]`` on purpose: ``None``
        means *no coverage evidence was produced*, which is not the same as
        ``0.0`` (measured and empty) and not the same as ``1.0`` (perfect).
        ``None`` does not clear the floor. The single exception is a
        ``coverage_floor`` of ``0``, which is the caller explicitly saying the
        requirement is disabled rather than satisfied.

        ``full_suite_exit_codes`` is ``(baseline, patch)`` and supersedes the
        boolean ``full_suite_passed`` when present. Both sides are needed: a
        single ``exit != 0`` would blame the patch for failures that were
        already there, which is the same collapse ``exitmap.py`` documents for
        claim execution.

        ``suite_strength_unverified`` says "there was code to judge and we
        could not judge it". It is distinct from ``suite_strength is None``,
        which means "no code changed, the question does not apply".
        """
        if not 0 <= rounds_used <= self.max_rounds:
            return Decision.INCONCLUSIVE

        outcomes = {verdict.outcome for verdict in verdicts}

        # Direct evidence of breakage outranks everything below it.
        if Outcome.REFUTED in outcomes:
            return Decision.BLOCK

        full_suite_unverifiable = False
        if full_suite_exit_codes is not None:
            if len(full_suite_exit_codes) != 2:
                return Decision.INCONCLUSIVE
            baseline_code, patch_code = full_suite_exit_codes
            if patch_code == 0:
                pass  # patch side clean: nothing regressed there, whatever baseline did
            elif baseline_code == 0:
                # Only the patch fails the suite it used to pass: collateral
                # regression, and we watched it happen.
                return Decision.BLOCK
            else:
                # Neither side is clean, so the failure cannot be attributed to
                # the patch. Blocking would blame it for what was already there;
                # merging would ignore that we still do not know. Both reasons
                # this check needs two sides instead of one ``exit != 0``.
                full_suite_unverifiable = True
        elif not full_suite_passed:
            return Decision.BLOCK

        # --- coverage: below, never above, the two BLOCK routes ------------
        # ``None`` is "we produced no coverage evidence", and unproduced
        # proof is never proof of clean code. A floor of 0 is the caller
        # saying the requirement is disabled, not that it was met.
        if diff_coverage_ratio is None:
            if self.config.coverage_floor > 0.0:
                return Decision.INCONCLUSIVE
        elif not 0.0 <= diff_coverage_ratio <= 1.0:
            return Decision.INCONCLUSIVE
        elif diff_coverage_ratio < self.config.coverage_floor:
            return Decision.INCONCLUSIVE

        if suite_strength is not None and suite_strength < self.config.suite_strength_floor:
            return Decision.INCONCLUSIVE
        if suite_strength_unverified:
            return Decision.INCONCLUSIVE

        if Outcome.UNVERIFIED in outcomes:
            return Decision.INCONCLUSIVE
        if full_suite_unverifiable:
            return Decision.INCONCLUSIVE

        if self.aggression is AggressionLevel.NUCLEAR and rounds_used < self.max_rounds:
            return Decision.INCONCLUSIVE
        if not verdicts:
            # Nothing was ever checked. "No verdicts" is not "no problems".
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
