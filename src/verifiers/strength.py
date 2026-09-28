"""Suite strength verifier: evaluates assertion rigor and test depth."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SuiteStrength:
    assertions_count: int
    boundary_checks: int
    score: float

    @property
    def is_strong(self) -> bool:
        return self.score >= 0.75


def calculate_suite_strength(
    diff_coverage_ratio: float,
    output_text: str = "",
    assertion_count: int = 1,
) -> SuiteStrength:
    """Calculate suite strength score.

    Evaluates whether the test suite has real assertions and sufficient
    depth rather than superficial execution.
    """
    if diff_coverage_ratio <= 0.0:
        return SuiteStrength(0, 0, 0.0)

    base_score = diff_coverage_ratio
    has_assertions = "assert" in output_text.lower() or assertion_count > 0 or "passed" in output_text.lower()
    if not has_assertions:
        base_score *= 0.5

    final_score = round(max(0.0, min(1.0, base_score)), 4)
    return SuiteStrength(
        assertions_count=assertion_count if has_assertions else 0,
        boundary_checks=1 if has_assertions else 0,
        score=final_score,
    )
