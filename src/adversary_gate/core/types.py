"""Core data model for an evidence-based gate.

The previous model used two booleans: ``GateVerdict.accepted`` and
``should_accept_patch() -> bool``. That shape cannot express "I could not
run this", which is why every infrastructure failure silently became an
approval. The three-valued ``Outcome`` below exists so that uncertainty is
a first-class result instead of a default.

Thesis mapping:
    VERIFIED    -> execution happened and the evidence settles the claim
    REFUTED     -> execution happened and the evidence condemns the patch
    UNVERIFIED  -> execution did not happen (harness, environment, missing
                   test id, flaky signal). Never mergeable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ExecState(str, Enum):
    """What a single pytest invocation actually did.

    ``UNRUNNABLE`` is the important one: it means the process exited without
    running the requested test (collection error, internal error, bad node
    id, nothing collected). It is *not* a test failure.
    """

    PASS = "pass"
    FAIL = "fail"
    TIMED_OUT = "timed_out"
    UNRUNNABLE = "unrunnable"
    NOT_APPLICABLE = "n/a"


class Outcome(str, Enum):
    """Three-valued verdict. Replaces the old ``accepted: bool``."""

    VERIFIED = "verified"
    REFUTED = "refuted"
    UNVERIFIED = "unverified"


class Decision(str, Enum):
    """What the gate says about the patch as a whole."""

    MERGE = "merge"
    BLOCK = "block"
    INCONCLUSIVE = "inconclusive"


class FailureClass(str, Enum):
    REGRESSION = "regression"
    NEW_BUG = "new_bug"
    INCOMPLETE_FIX = "incomplete_fix"
    HANG = "hang"
    CLAIM_DISCARDED = "discarded"
    FLAKY = "flaky"
    UNRUNNABLE = "unrunnable"
    INVALID = "invalid"
    CIRCUIT_OPEN = "circuit_open"
    NOT_EXECUTED = "not_executed"


class BugKind(str, Enum):
    DETERMINISTIC = "deterministic"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"


class AggressionLevel(str, Enum):
    NORMAL = "normal"
    PARANOID = "paranoid"
    NUCLEAR = "nuclear"


#: Outcome for every ``FailureClass``. Keeping this as data (not scattered
#: through ``if`` branches) is what makes the fail-open regression testable:
#: any class missing here is a bug, not a style choice.
CLASSIFICATION_OUTCOME: dict[FailureClass, Outcome] = {
    FailureClass.REGRESSION: Outcome.REFUTED,
    FailureClass.NEW_BUG: Outcome.REFUTED,
    FailureClass.INCOMPLETE_FIX: Outcome.REFUTED,
    FailureClass.HANG: Outcome.REFUTED,
    FailureClass.CLAIM_DISCARDED: Outcome.VERIFIED,
    FailureClass.FLAKY: Outcome.UNVERIFIED,
    FailureClass.UNRUNNABLE: Outcome.UNVERIFIED,
    FailureClass.INVALID: Outcome.UNVERIFIED,
    FailureClass.CIRCUIT_OPEN: Outcome.UNVERIFIED,
    FailureClass.NOT_EXECUTED: Outcome.UNVERIFIED,
}


def outcome_of(classification: FailureClass) -> Outcome:
    return CLASSIFICATION_OUTCOME[classification]


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    text: str


@dataclass
class CriticClaim:
    test_path: str
    test_id: str
    bug_kind: BugKind = BugKind.DETERMINISTIC
    cited_criterion_id: Optional[str] = None
    rationale: str = ""


@dataclass
class ExecutionOutcome:
    """Result of running the same target against baseline and patch.

    ``baseline_exit_codes`` and ``patch_exit_codes`` are recorded per run so
    an auditor can see *how* each side failed, not just that it did.
    """

    baseline: ExecState
    patch: ExecState
    baseline_failures: int = 0
    patch_failures: int = 0
    run_count: int = 1
    baseline_exit_codes: tuple[int, ...] = ()
    patch_exit_codes: tuple[int, ...] = ()
    #: Truncated stdout/stderr from the last run on each side. Without this
    #: the log records that a test failed but not *how* -- which is the
    #: difference between an audit trail and a counter.
    baseline_output: str = ""
    patch_output: str = ""
    full_suite_passed: bool = True
    full_suite_exit_code: int = 0

    @property
    def unrunnable(self) -> bool:
        return (
            self.baseline is ExecState.UNRUNNABLE
            or self.patch is ExecState.UNRUNNABLE
        )


@dataclass
class GateVerdict:
    claim: CriticClaim
    classification: FailureClass
    outcome: Outcome
    reason: str
    outcome_run: Optional[ExecutionOutcome] = None
    duration_seconds: float = 0.0
    #: Which copy of the claim's test produced the verdict: ``patch`` (the
    #: test file is untouched, or new), ``baseline`` (the patch rewrote it and
    #: the baseline's copy was run against the patch's code) or
    #: ``baseline-file`` (a test added to a rewritten file, judged after every
    #: test the baseline had in that file passed on the patch).
    oracle: str = "patch"
    # No ``suite_strength`` here on purpose. It used to be a field defaulting
    # to 1.0, it was never assigned anything else, and patch-level strength is
    # now measured in ``verifiers.strength.measure_mutation_score`` and passed
    # to ``Gate.decide`` by the CLI. A field that reads "perfect" when nobody
    # measured it is how a floor becomes unreachable.

    # Kept as a property so existing call sites still read naturally, but it
    # is now derived -- there is no independent bool to drift out of sync.
    @property
    def accepted(self) -> bool:
        return self.outcome is Outcome.REFUTED

    @property
    def verified(self) -> bool:
        return self.outcome is Outcome.VERIFIED

