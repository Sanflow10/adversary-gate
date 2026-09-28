"""Mutation testing & suite strength verifier.

Real suite strength is determined by Mutation Score (mutants_killed / total_mutants),
NOT by parsing stdout strings. A test suite that passes when patch logic is mutated
has a Mutation Score of 0.0 (weak/superficial test suite).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SuiteStrength:
    mutants_total: int
    mutants_killed: int
    mutation_score: float
    is_measured: bool

    @property
    def is_strong(self) -> bool:
        return self.mutation_score >= 0.75 if self.is_measured else False


def calculate_mutation_score(
    mutants_total: int,
    mutants_killed: int,
) -> SuiteStrength:
    """Calculate real mutation testing score from execution evidence."""
    if mutants_total <= 0:
        return SuiteStrength(0, 0, 0.0, is_measured=False)
    score = round(max(0.0, min(1.0, mutants_killed / mutants_total)), 4)
    return SuiteStrength(
        mutants_total=mutants_total,
        mutants_killed=mutants_killed,
        mutation_score=score,
        is_measured=True,
    )
