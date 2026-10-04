"""Contestation protocol.

Unchanged in substance -- this is the part of the original design with no
prior art found, and it holds up:

* A prose objection never changes a verdict. Only executable counter-evidence
  can, and it must re-execute the *same* ``test_path`` the original claim
  referenced. An unrelated passing test (``assert True`` in a new file) proves
  nothing about the disputed claim, so matching ``original_test_id`` by name
  is not sufficient.

The docstring below states the invariant explicitly because it is the thing
that makes the gate defeatable-by-cheating if dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

from adversary_gate.core.types import ExecState, ExecutionOutcome, GateVerdict, Outcome


@dataclass(frozen=True)
class Contestation:
    original_test_id: str
    counter_test_path: str
    counter_test_id: str
    reason: str = ""


@dataclass(frozen=True)
class ContestationResult:
    accepted: bool
    original_verdict: GateVerdict
    reason: str
    outcome: Outcome = Outcome.UNVERIFIED


def adjudicate(
    original: GateVerdict,
    contest: Contestation,
    counter_outcome: ExecutionOutcome,
) -> ContestationResult:
    """Accept only executable counter-evidence that passes on the patch.

    A prose objection never changes a verdict. The counter-test must be
    attributable to the original test and must execute successfully on the
    patch; otherwise the original verdict remains authoritative.

    Critically, the counter-test must re-execute the SAME test_path the
    original claim referenced -- an unrelated passing test (e.g. ``assert
    True`` in a new file) proves nothing about the disputed claim and must
    not be accepted just because ``contest.original_test_id`` matches by name.
    Without this, any accepted verdict could be defeated by submitting a
    trivial, unrelated passing test with the right id.
    """
    if contest.original_test_id != original.claim.test_id:
        return ContestationResult(
            False, original, "counterclaim targets a different test_id"
        )
    if contest.counter_test_path != original.claim.test_path:
        return ContestationResult(
            False,
            original,
            "counter-evidence must re-execute the original test_path; "
            "an unrelated test proves nothing about the disputed claim",
        )
    if counter_outcome.patch is ExecState.UNRUNNABLE:
        return ContestationResult(
            False,
            original,
            "counter-test did not execute; a run that never happened "
            "cannot overturn a verdict",
        )
    if counter_outcome.patch is not ExecState.PASS:
        return ContestationResult(
            False, original, "counter-test did not pass on the patch"
        )
    return ContestationResult(
        True,
        original,
        "original test re-executed and passed on the patch",
        Outcome.VERIFIED,
    )
