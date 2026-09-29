"""Mutation testing & suite strength verifier.

Real suite strength is determined by Mutation Score (mutants_killed / total_mutants),
NOT by parsing stdout strings. A test suite that passes when patch logic is mutated
has a Mutation Score of 0.0 (weak/superficial test suite).

There are two functions here and the difference between them is the whole point:

``calculate_mutation_score``
    Pure arithmetic over counts somebody else produced. Correct, cheap, and
    -- considered alone -- possible to wire to a constant. It was: ``gate.py``
    called ``calculate_suite_strength(1.0, patch_output)`` with a hardcoded
    coverage and a default ``assertion_count=1`` that made ``has_assertions``
    unconditionally true, so the score was ``1.0`` for every input including
    an empty one. A floor that can never trip is worse than no floor, because
    it reads as assurance.

``measure_mutation_score``
    Produces those counts by *executing* the claim's tests against mutants of
    the code the patch changed. This is the function that has to exist for the
    ``suite_strength_floor`` in :meth:`core.gate.Gate.decide` to be reachable
    at all, and it is what makes the floor a measurement rather than a slogan.

Mutant lifecycle, because not every outcome says something about the suite:

    ``0``  tests passed        -> mutant **survived**, suite could not see it
    ``1``  tests failed        -> mutant **killed**, suite caught the damage
    other  collection/usage/internal error, or sandbox timeout
                               -> mutant **stillborn**: the code no longer
                                  even runs, so it is evidence about the
                                  mutant, not about the suite. Excluded from
                                  both sides of the ratio rather than counted
                                  as a kill, which would inflate the score.

Scope note: mutants are generated only from non-test ``.py`` files that
*differ between baseline and patch*, and they are executed against the
claim's ``test_path``. So ``suite_strength`` answers a precise question --
"could this claim's test detect deliberate damage in the code this patch
changed?" -- and not the broader "is the repository well tested?". The
broader question needs the whole suite and is answered by ``full_suite``,
which :mod:`cli` measures separately.
"""

from __future__ import annotations

import difflib
import io
import shutil
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.exitmap import PYTEST_OK, PYTEST_TESTS_FAILED
from sandbox.runner import DEFAULT_PROCESSES, run_test

#: Directories never worth copying or mutating.
_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".venv", "venv", "node_modules", ".pytest_cache"
)

#: One-token operator substitutions. Every replacement is syntactically valid
#: on its own, so a mutant that will not parse is a generation bug rather than
#: a stillborn mutant.
_OPERATOR_MUTATIONS: Dict[str, str] = {
    "==": "!=",
    "!=": "==",
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "+": "-",
    "-": "+",
}

#: Keyword substitutions, including the deletion of ``not`` (replaced by the
#: empty string, which leaves valid -- if double-spaced -- Python).
_KEYWORD_MUTATIONS: Dict[str, str] = {
    "and": "or",
    "or": "and",
    "not": "",
}

#: Position -> replacement, as ``(start, end)`` row/col pairs from tokenize.
Mutation = Tuple[Tuple[int, int], Tuple[int, int], str]


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


# ----------------------------------------------------------------------
# what changed
# ----------------------------------------------------------------------
def _is_test_path(rel: str) -> bool:
    """True for test modules and fixtures -- never mutation targets."""
    path = Path(rel)
    return (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or path.name == "conftest.py"
        or "tests" in path.parts[:-1]
    )


def _python_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p
        for p in root.rglob("*.py")
        if p.is_file() and "__pycache__" not in p.parts and not _is_test_path(
            p.relative_to(root).as_posix()
        )
    )


def changed_source_files(baseline_dir: Path, patch_dir: Path) -> List[str]:
    """Relative paths of non-test ``.py`` files the patch actually changed.

    Added, modified and deleted files all count as changed: a deletion is a
    change to the code under test even though there is nothing left to mutate
    in the patch side.
    """

    def snapshot(root: Path) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        for path in _python_files(root):
            rel = path.relative_to(root).as_posix()
            try:
                out[rel] = path.read_bytes()
            except OSError:
                continue
        return out

    base = snapshot(Path(baseline_dir))
    patch = snapshot(Path(patch_dir))
    # The union, not the patch side: iterating ``patch`` alone silently drops
    # every file the patch deleted, and a deletion is exactly the kind of
    # change a suite-strength floor exists to notice. Measured on v2.0.1:
    # deleting ``pkg/gone.py`` returned ``[]``, which left ``changed_files``
    # empty, kept ``suite_strength_unverified`` false and let the patch reach
    # MERGE with no strength measurement at all.
    return sorted(rel for rel in set(base) | set(patch) if base.get(rel) != patch.get(rel))


def deleted_source_files(baseline_dir: Path, patch_dir: Path) -> List[str]:
    """Relative paths of non-test ``.py`` files present in baseline, gone in patch.

    Reported separately because a deleted file has nothing left to mutate:
    its correctness rests on the collateral full-suite run, not on a score.
    """

    def names(root: Path) -> set:
        return {p.relative_to(root).as_posix() for p in _python_files(root)}

    return sorted(names(Path(baseline_dir)) - names(Path(patch_dir)))


def changed_lines(baseline_text: str, patch_text: str) -> set:
    """1-based line numbers on the **patch** side that differ from baseline.

    File granularity is too coarse for a strength score: a file with one
    edited line and twenty untouched ones would be mutated mostly in code the
    patch did not write, and tests that cover the untouched lines would kill
    those mutants and report a high score for a diff nobody checked. Only the
    lines the patch actually introduced are mutation targets.
    """
    old = baseline_text.splitlines()
    new = patch_text.splitlines()
    differing: set = set()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, _i1, _i2, start, stop in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            differing.update(range(start + 1, stop + 1))
    return differing


# ----------------------------------------------------------------------
# what to break
# ----------------------------------------------------------------------
def _mutants_in(source: str, limit: int) -> List[Mutation]:
    """Ordered, deterministic mutation sites in one module."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return []

    found: List[Mutation] = []
    for token in tokens:
        replacement: Optional[str] = None
        if token.type == tokenize.OP:
            replacement = _OPERATOR_MUTATIONS.get(token.string)
        elif token.type == tokenize.NAME:
            replacement = _KEYWORD_MUTATIONS.get(token.string)
        if replacement is None:
            continue
        found.append((token.start, token.end, replacement))
        if len(found) >= limit:
            break
    return found


def _apply(lines: List[str], start: Tuple[int, int], end: Tuple[int, int], repl: str) -> List[str]:
    """Splice one mutation into source lines. Rows are 1-based, columns 0-based."""
    (start_row, start_col), (end_row, end_col) = start, end
    result = list(lines)
    if start_row == end_row:
        line = result[start_row - 1]
        result[start_row - 1] = line[:start_col] + repl + line[end_col:]
    else:
        # Replacement spans lines: collapse them into one.
        merged = result[start_row - 1][:start_col] + repl + result[end_row - 1][end_col:]
        result[start_row - 1 : end_row] = [merged]
    return result


# ----------------------------------------------------------------------
# the measurement
# ----------------------------------------------------------------------
def measure_mutation_score(
    baseline_dir: Path,
    patch_dir: Path,
    test_path: str,
    test_id: str = "",
    *,
    max_mutants: int = 6,
    timeout_seconds: int = 30,
    cpu_seconds: int = 10,
    mem_bytes: int = 512 * 1024 * 1024,
    processes: int = DEFAULT_PROCESSES,
) -> Tuple[SuiteStrength, Dict[str, object]]:
    """Break the patch's own code and see whether ``test_path`` notices.

    Returns the score plus a detail dict for the evidence artefact, because a
    strength number with no survivor list cannot be audited: the previous
    version shipped a ``suite_strength`` field that was absent from the log
    entirely, so nobody could ever have caught that it was constant.

    ``is_measured`` is False when there was nothing to mutate, when no mutant
    could be built, or when every mutant was stillborn. Callers must treat
    that as *unknown*, never as strong -- ``SuiteStrength.is_strong`` already
    does.
    """
    if max_mutants <= 0:
        raise ValueError("max_mutants must be positive")
    if not test_path:
        raise ValueError("test_path is required to measure suite strength")

    patch_dir = Path(patch_dir)
    changed = changed_source_files(baseline_dir, patch_dir)
    # Fixed shape on every return path: an artefact whose optional keys
    # disappear when nothing was measured cannot be queried reliably.
    detail: Dict[str, object] = {
        "measured": False,
        "reason": "",
        "changed_files": changed,
        "deleted_files": deleted_source_files(baseline_dir, patch_dir),
        "mutants": [],
        "survivors": [],
        "mutants_counted": 0,
        "stillborn": 0,
    }

    def finish(result: SuiteStrength, reason: str = "") -> Tuple[SuiteStrength, Dict[str, object]]:
        detail["measured"] = result.is_measured
        detail["reason"] = reason
        # Both sides of the ratio, so the score in the artefact can be
        # recomputed from the artefact instead of trusted.
        detail["mutants_counted"] = result.mutants_total
        detail["mutants_killed"] = result.mutants_killed
        return result, detail

    if not changed:
        return finish(
            SuiteStrength(0, 0, 0.0, is_measured=False),
            "no non-test source file changed between baseline and patch",
        )

    originals: Dict[str, str] = {}
    plan: List[Tuple[str, Mutation]] = []
    for rel in changed:
        try:
            source = (patch_dir / rel).read_text(encoding="utf-8")
            before = (Path(baseline_dir) / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # One side is missing: added on the patch side, or deleted by it.
            # Neither has a pair to diff, so neither can produce a mutant.
            continue
        originals[rel] = source
        edited = changed_lines(before, source)
        if not edited:
            continue
        # Scan generously, then keep only sites on lines this patch wrote.
        for site in _mutants_in(source, 10_000):
            if site[0][0] in edited:
                plan.append((rel, site))
            if len(plan) >= max_mutants:
                break
        if len(plan) >= max_mutants:
            break
    plan = plan[:max_mutants]

    if not plan:
        deleted = detail["deleted_files"]
        if deleted and len(deleted) == len(changed):
            return finish(
                SuiteStrength(0, 0, 0.0, is_measured=False),
                "the patch only deletes source files; nothing is left to mutate, "
                "so the collateral full-suite run is the only evidence available",
            )
        return finish(
            SuiteStrength(0, 0, 0.0, is_measured=False),
            "lines changed by this patch contain no mutable operator",
        )

    with tempfile.TemporaryDirectory(prefix="adversary-mutant-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(patch_dir, work, ignore=_IGNORE)

        counted = killed = 0
        survivors: List[str] = []
        for rel, (start, end, replacement) in plan:
            target = work / rel
            original = originals[rel]
            mutant_source = "\n".join(_apply(original.split("\n"), start, end, replacement))
            entry: Dict[str, object] = {
                "file": rel,
                "line": start[0],
                "replaces": replacement or "<deleted>",
            }
            detail["mutants"].append(entry)  # type: ignore[union-attr]
            if mutant_source == original:
                entry["result"] = "no-op"
                continue
            try:
                target.write_text(mutant_source, encoding="utf-8")
                outcome = run_test(
                    work,
                    test_path,
                    test_id,
                    timeout_seconds=timeout_seconds,
                    cpu_seconds=cpu_seconds,
                    mem_bytes=mem_bytes,
                    processes=processes,
                )
                code = outcome.exit_code
            except Exception as exc:  # noqa: BLE001 - a broken harness is not suite evidence
                entry["result"] = "harness-error"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                continue
            finally:
                if target.exists():
                    target.write_text(original, encoding="utf-8")

            if code == PYTEST_OK:
                counted += 1
                entry["result"] = "survived"
                survivors.append(f"{rel}:{start[0]}")
            elif code == PYTEST_TESTS_FAILED:
                counted += 1
                killed += 1
                entry["result"] = "killed"
            else:
                detail["stillborn"] = int(detail["stillborn"]) + 1  # type: ignore[arg-type]
                entry["result"] = "stillborn"
                entry["exit_code"] = code

        detail["survivors"] = survivors

    result = calculate_mutation_score(counted, killed)
    if result.is_measured:
        return finish(result, "")
    return finish(
        result,
        "every mutant was stillborn; the test target never executed the mutated code",
    )
