"""Stability policy: how many runs a claim needs before it counts.

The policies are unchanged from the original design. What is new is that
``gate.py`` now consults them through ``_resolve_side``, which reads raw exit
codes rather than a pre-collapsed failure count -- so a run that never
executed (exit 2/3/4/5) cannot masquerade as a run that failed.
"""

from __future__ import annotations

from dataclasses import dataclass

from adversary_gate.core.types import BugKind


@dataclass(frozen=True)
class StabilityPolicy:
    runs: int
    min_required_patch_failures: int


def policy_for(bug_kind: BugKind) -> StabilityPolicy:
    if bug_kind == BugKind.CONCURRENCY:
        # Races are probabilistic: one reproduction in 100 runs is a finding.
        return StabilityPolicy(runs=100, min_required_patch_failures=1)
    if bug_kind == BugKind.PERFORMANCE:
        # Regressions need a margin to separate from scheduler noise.
        return StabilityPolicy(runs=5, min_required_patch_failures=4)
    return StabilityPolicy(runs=3, min_required_patch_failures=3)
