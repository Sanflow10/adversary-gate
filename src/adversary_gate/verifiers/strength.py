# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

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
claims' node ids -- not their whole files (AG-039). A claim judged by the
baseline oracle whose node the patch renamed or deleted is not collected in
the mutant tree (a copy of the patch): its runs exit 4 and count as
stillborn, which can only leave the score unmeasured, never inflate it. So ``suite_strength`` answers a precise question --
"could this claim's test detect deliberate damage in the code this patch
changed?" -- and not the broader "is the repository well tested?". The
broader question needs the whole suite and is answered by ``full_suite``,
which :mod:`cli` measures separately.
"""

from __future__ import annotations

import ast
import difflib
import io
import keyword
import math
import shutil
import tempfile
import time
import tokenize
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from adversary_gate.core.exitmap import PYTEST_OK, PYTEST_TESTS_FAILED
from adversary_gate.sandbox.runner import DEFAULT_PROCESSES, run_test
from adversary_gate.verifiers.testpaths import is_test_path as _is_test_path

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
    # AG-023: arithmetic beyond +/-. Until 2.3.0 a patch that turned
    # ``a + b`` into ``a * b`` reported "no mutable operator" and measured
    # nothing at all.
    "*": "/",
    "/": "*",
    "//": "*",
    "%": "*",
    "**": "*",
    "+=": "-=",
    "-=": "+=",
    "*=": "/=",
    "/=": "*=",
}

#: Tokens that are also *syntax* when they are not a binary operator:
#: ``*args``, ``**kwargs``, ``import *``, the keyword-only ``*`` and the
#: positional-only ``/`` in a signature. Swapping those either does not parse
#: or -- ``def f(a, *, b)`` -> ``def f(a, /, b)`` -- parses into an equivalent
#: program whose survival would be counted against the tests.
_BINARY_ONLY = frozenset({"*", "/", "**"})

#: What can sit to the left of a *binary* operator: an operand's last token.
_OPERAND_END = frozenset({")", "]", "}"})

#: Keyword substitutions, including the deletion of ``not`` (replaced by the
#: empty string, which leaves valid -- if double-spaced -- Python).
_KEYWORD_MUTATIONS: Dict[str, str] = {
    "and": "or",
    "or": "and",
    "not": "",
    "True": "False",
    "False": "True",
}

#: Position -> replacement, as ``(start, end)`` row/col pairs from tokenize.
Mutation = Tuple[Tuple[int, int], Tuple[int, int], str]


#: AG-023. The floor is applied to the lower bound of this two-sided Wilson
#: interval, not to the raw ratio. 1 mutant killed out of 1 is a ratio of 1.0
#: and evidence of almost nothing; at 80 % the lower bound reaches the 0.75
#: floor only from 5 of 5. ``0`` means "use the point estimate" -- the
#: pre-2.7 behaviour, said out loud with ``--strength-confidence 0``.
DEFAULT_CONFIDENCE = 0.80

#: Enough mutants for the default confidence to be reachable with room to
#: spare (5 is the minimum at 80 %); was 6, which made one survivor fatal.
DEFAULT_MAX_MUTANTS = 12


def wilson_interval(killed: int, total: int, confidence: float = DEFAULT_CONFIDENCE) -> Tuple[float, float]:
    """Two-sided Wilson score interval for ``killed / total``.

    Wilson rather than the normal approximation because the cases that matter
    here are exactly where the normal one breaks: small ``n`` and ratios at 0
    or 1, where it returns a zero-width interval around 1.0.
    """
    if total <= 0:
        return 0.0, 0.0
    p = killed / total
    if confidence <= 0:
        return p, p
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in [0, 1)")
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    z2 = z * z
    denominator = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class SuiteStrength:
    mutants_total: int
    mutants_killed: int
    mutation_score: float
    is_measured: bool
    #: Wilson bounds at ``confidence``; equal to the score when confidence is 0.
    lower: float = 0.0
    upper: float = 0.0
    confidence: float = DEFAULT_CONFIDENCE

    @property
    def is_strong(self) -> bool:
        return self.lower >= 0.75 if self.is_measured else False


def calculate_mutation_score(
    mutants_total: int,
    mutants_killed: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> SuiteStrength:
    """Calculate real mutation testing score from execution evidence."""
    if mutants_total <= 0:
        return SuiteStrength(0, 0, 0.0, is_measured=False, confidence=confidence)
    score = round(max(0.0, min(1.0, mutants_killed / mutants_total)), 4)
    lower, upper = wilson_interval(mutants_killed, mutants_total, confidence)
    return SuiteStrength(
        mutants_total=mutants_total,
        mutants_killed=mutants_killed,
        mutation_score=score,
        is_measured=True,
        lower=round(lower, 4),
        upper=round(upper, 4),
        confidence=confidence,
    )


# ----------------------------------------------------------------------
# what changed
# ----------------------------------------------------------------------
# Everything the mutation engine *cannot* break: ``_mutants_in`` tokenizes
# Python, so there is no way to break a .cpp or a .sql and watch the suite
# notice -- but a change to one of those files still changes the behaviour
# under test, and the artefact must not claim "no source changed" when it did.
#
# This is a **denylist**, deliberately. The previous shape was an allowlist
# (``FOREIGN_SOURCE_SUFFIXES``) of languages somebody had remembered to type:
# ``.sql``, ``.proto``, ``.pyi``, ``.vue``, ``.sol`` and every other suffix
# outside that list were invisible, so a patch changing only ``schema.sql``
# reported ``"no source file changed between baseline and patch"`` -- the
# exact false statement AG-012 was opened to remove -- and a mixed
# ``calc.py`` + ``schema.sql`` patch reached MERGE with
# ``foreign_changed_files: []``. A list of remembered languages has an end;
# a list of things that are *not* code does not, and anything unrecognised
# fails closed instead of open.
NON_SOURCE_SUFFIXES = frozenset({
    # documents
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".org", ".tex", ".pdf",
    # data and configuration
    ".json", ".jsonc", ".jsonl", ".jsonlines", ".ndjson", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf",
    ".lock", ".csv", ".tsv", ".xml", ".properties", ".env", ".schema",
    # images, media, fonts
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # packaged or compiled artefacts
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".jar", ".war", ".class",
    ".so", ".dll", ".dylib", ".a", ".o", ".obj", ".lib", ".exe",
    ".pyc", ".pyo", ".pyd", ".wasm", ".min", ".map", ".cache",
    # version-control and editor noise
    ".orig", ".rej", ".bak", ".swp", ".swo", ".tmp", ".log",
})

#: Suffixless files that name a document rather than a build input.
#: ``Makefile``, ``Dockerfile``, ``Procfile`` and friends are deliberately
#: absent: a change to them is a change we cannot judge, and guessing
#: "not code" is exactly what made AG-012 possible in the first place.
NON_SOURCE_NAMES = frozenset({
    "license", "licence", "copying", "notice", "authors", "contributors",
    "changelog", "changes", "readme", "code_of_conduct", "codeowners",
    "security",
    # dotfiles: these have no suffix, so the suffix table cannot see them.
    # ``.coverage`` in particular is written into the *patch* tree by the
    # coverage command, which made it look like an unreviewed source change.
    "coverage", "gitignore", "gitattributes",
})

#: Directories that are never patch content, whatever they contain.
#:
#: This is **not** the AG-013 allowlist coming back. That list decided which
#: languages count as code, which is how a ``.sql`` patch got past it. This
#: decides only that version-control bookkeeping and tool caches are not files
#: the author wrote -- a different question, with a different failure mode.
#:
#: Measured, not assumed: run the gate against any git repository and the
#: scan reports 26 paths under ``.git/`` -- ``.git/HEAD``, ``.git/config``,
#: ``.git/objects/...`` -- as "source we cannot judge", and forces
#: ``INCONCLUSIVE`` on every patch whose ``--patch`` happens to be a checkout.
#: That is what broke ``prepare_evidence.sh`` and the ``base-sha`` job here.
#: The old allowlist hid it only by accident, because none of those paths have
#: a suffix it recognised.
#:
#: Excluding a directory cannot open the AG-013 hole: a path the patch's own
#: diff names still arrives through ``changed_paths``, and a diff has never
#: legitimately named ``.git/HEAD``.
NON_SOURCE_DIRS = frozenset({
    # version control
    ".git", ".hg", ".svn", ".bzr", "_darcs",
    # interpreter and tool caches
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", ".hypothesis", ".ipynb_checkpoints", ".eggs", ".cache",
    ".pytype", ".dmypy_cache",
    # environments and vendored dependency trees
    ".venv", "venv", ".virtualenv", "node_modules",
    # coverage and profiling output
    "htmlcov",
    # build output. `pip install` writes these into the workspace the Action
    # runs in, so a materialised baseline (clean, from git) and the checkout
    # (not clean) always differ here. ``src/<name>.egg-info/PKG-INFO`` is the
    # one that actually bit: suffixless, not on the name list, therefore
    # "source we cannot judge", therefore INCONCLUSIVE on every base-sha run.
    "build", "dist",
})


#: Files a VCS writes in place of its directory: ``git worktree add`` and
#: submodules leave ``.git`` as a one-line ``gitdir:`` pointer.
VCS_POINTER_FILES = frozenset({".git"})


def _is_non_source(rel: str) -> bool:
    """True for things that are plainly not code under test."""
    path = Path(rel)
    if path.suffix.lower() in NON_SOURCE_SUFFIXES:
        return True
    if not path.suffix and path.name.lower().lstrip(".").replace(" ", "_") in NON_SOURCE_NAMES:
        return True
    return False


def _is_scan_noise(rel: str) -> bool:
    """True for directories the *directory scan* must not walk.

    Only the scan is subject to this, never the caller's diff. A materialised
    baseline contains no ``.git`` at all while the patch side is a checkout, so
    the two differ in twenty-six VCS paths on every single comparison; without
    this, any gate run whose ``--patch`` was a repository reported
    ``.git/HEAD`` as source it could not judge and forced INCONCLUSIVE. That is
    what turned ``prepare_evidence.sh`` from exit 0 into exit 2 in CI.

    The exemption stops at the scan because the diff is the author's own
    statement of what changed. A diff naming ``.git/hooks/post-checkout`` or
    ``node_modules/vendor.js`` still has to answer for it -- fail-closed, and
    the reason this exclusion cannot reopen AG-013.
    """
    path = Path(rel)
    # A ``.git`` *file* is the pointer a git worktree or submodule keeps where
    # the directory would be (AG-035). It is the same bookkeeping; only the
    # check on parent directories used to see it.
    if path.name in VCS_POINTER_FILES:
        return True
    return any(
        # ``<name>.egg-info`` is never a fixed name, so it needs a suffix test
        # rather than an entry in the set -- setuptools names it after the
        # distribution.
        part in NON_SOURCE_DIRS or part.endswith(".egg-info")
        for part in path.parts[:-1]
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


def changed_foreign_source_files(
    baseline_dir: Path,
    patch_dir: Path,
    changed_paths: Optional[Iterable[str]] = None,
) -> List[str]:
    """Relative paths of **non-Python** source files the patch changed.

    These are files whose behaviour the patch altered and that the mutation
    engine has no way to break. They are reported separately from
    ``changed_source_files`` because the two answer different questions:

    ``changed_source_files``
        What we *can* measure. Non-empty means a score is expected.
    ``changed_foreign_source_files``
        What changed and we *cannot* measure. Non-empty with no score is the
        difference between "no code changed, the question does not apply" and
        "code changed and we did not judge it".

    Before this existed, a patch touching only ``calculator.cpp`` reported
    ``reason: "no non-test source file changed"`` -- a false statement -- left
    ``suite_strength_unverified`` false, and reached MERGE on the strength of
    a Python test that never looked at the C++.

    ``changed_paths`` are the paths the caller's own unified diff names.
    They are *unioned* with the directory scan rather than replacing it: the
    diff is authoritative about what changed, and the scan catches anything a
    partial or hand-written diff left out. Either source alone can be wrong;
    their union can only be more conservative, and more conservative is the
    safe direction for a question whose answer is "we could not judge this".
    """

    def snapshot(root: Path) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        if not root.is_dir():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            if _is_test_path(rel) or _is_non_source(rel) or _is_scan_noise(rel):
                continue
            try:
                out[rel] = path.read_bytes()
            except OSError:
                continue
        return out

    base = snapshot(Path(baseline_dir))
    patch = snapshot(Path(patch_dir))
    candidates = {rel for rel in set(base) | set(patch) if base.get(rel) != patch.get(rel)}
    if changed_paths:
        # A path from the diff counts even if it no longer exists on either
        # side or the scan cannot see it: the diff says the patch touched it.
        candidates.update(
            rel for rel in changed_paths if rel and not rel.startswith(("/", "../"))
        )
    return sorted(
        rel
        for rel in candidates
        # .py is judged by the mutation engine, tests are not code under test,
        # and documented non-code should not force an INCONCLUSIVE on its own.
        if not rel.endswith(".py")
        and not _is_test_path(rel)
        and not _is_non_source(rel)
    )


def classify_changes(
    baseline_dir: Path,
    patch_dir: Path,
    changed_paths: Optional[Iterable[str]] = None,
) -> Dict[str, object]:
    """The "what changed" half of the evidence artefact.

    Exposed separately from :func:`measure_mutation_score` so that *every*
    code path can write the same three keys. It used to be reachable only
    from inside the mutation branch, which is why ``--mutation-max 0``
    produced an artefact missing ``changed_files`` and -- worse -- never
    evaluated the AG-012 guard that stops unjudged source reaching MERGE.
    """
    return {
        "changed_files": changed_source_files(Path(baseline_dir), Path(patch_dir)),
        "foreign_changed_files": changed_foreign_source_files(
            Path(baseline_dir), Path(patch_dir), changed_paths
        ),
        "deleted_files": deleted_source_files(Path(baseline_dir), Path(patch_dir)),
    }


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


#: Appended to the end of every logical line the patch wrote, in the canary copy.
_CANARY = "  # adversary-gate canary"
#: The statement the canary adds after (or before) code the patch wrote. Assigning
#: it changes the tree and the bytecode of the function that holds it, and nothing
#: the code computes. The name ends in ``__``, so a class does not mangle it.
_PROBE = "__adversary_probe__"
_PROBE_LINE = f"{_PROBE} = 0"
_SIMPLE = (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return, ast.Raise,
           ast.Assert, ast.Pass, ast.Delete, ast.Import, ast.ImportFrom, ast.Global,
           ast.Nonlocal, ast.Break, ast.Continue)


class CanaryUnavailable(Exception):
    """No neutral copy can be built for a file the patch changed (AG-046)."""


def _is_probe(node: ast.AST) -> bool:
    return (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == _PROBE)


class _WithoutProbes(ast.NodeTransformer):
    """The tree with every probe removed; the copy is neutral when it equals the patch's."""

    def generic_visit(self, node):
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                setattr(node, field, [item for item in value if not _is_probe(item)])
        return super().generic_visit(node)


def _clean_start(line: str, col: int) -> bool:
    return line[:col].strip() == ""


def _clean_end(line: str, col: int) -> bool:
    rest = line[col:].strip()
    return rest == "" or rest.startswith("#")


def _indent_of(line: str, col: int) -> str:
    return line[:col]


def _innermost(tree: ast.AST, row: int) -> Optional[ast.AST]:
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.stmt, ast.excepthandler)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None or not node.lineno <= row <= end:
            continue
        if best is None or (node.lineno, -end) >= (best.lineno, -best.end_lineno):
            best = node
    return best


class _SharesLine(CanaryUnavailable):
    """The statement the probe would precede does not start its own line."""


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_docstring(node: ast.AST, parent: Optional[ast.AST]) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and isinstance(parent, (ast.Module, *_SCOPES)) and parent.body[0] is node)


def _module_head(tree: ast.Module) -> list:
    """The module docstring and the ``from __future__`` imports: they must stay first."""
    body = tree.body
    count = 1 if body and _is_docstring(body[0], tree) else 0
    while (count < len(body) and isinstance(body[count], ast.ImportFrom)
           and body[count].module == "__future__"):
        count += 1
    return body[:count]


def _block_probe(node: ast.AST, row: int, lines: List[str]) -> Tuple[str, int, str]:
    """Probe at the start of the first block of ``node`` that follows ``row``."""
    blocks = [getattr(node, field) for field in ("body", "orelse", "finalbody")
              if isinstance(getattr(node, field, None), list) and getattr(node, field)]
    blocks += [case.body for case in getattr(node, "cases", ()) if case.body]  # ``match``
    following = [block for block in blocks if block[0].lineno > row]
    if not following:
        raise CanaryUnavailable(f"line {row} has no statement after it to probe")
    block = min(following, key=lambda b: b[0].lineno)
    first = block[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        if len(block) == 1:
            # A docstring alone: the probe goes after it, so it stays a docstring.
            line = lines[first.end_lineno - 1]
            if not _clean_end(line, first.end_col_offset):
                raise _SharesLine(f"line {first.end_lineno} shares its line with other code")
            return ("after", first.end_lineno, _indent_of(lines[first.lineno - 1], first.col_offset))
        first = block[1]
    line = lines[first.lineno - 1]
    if not _clean_start(line, first.col_offset):
        raise _SharesLine(f"line {first.lineno} shares its line with other code")
    return ("before", first.lineno, _indent_of(line, first.col_offset))


def _before(node: ast.AST, row: int, lines: List[str], parents: dict, head: list) -> Tuple[str, int, str]:
    """A probe in front of ``node``, or of what holds it, inside the same function.

    Three places cannot take a statement in front of them, and each has its way out:
    the module's docstring and ``__future__`` imports (the probe goes after them),
    a docstring (after it: in front, ``__doc__`` would become ``None``), and a line
    that starts with other code, like ``if x: return y`` or ``a = 1; b = 2`` (in
    front of the statement that starts the line, as long as that one is still
    inside the same function or class).
    """
    if any(node is item for item in head):
        return ("after", head[-1].end_lineno, "")
    parent = parents.get(node)
    if _is_docstring(node, parent) and not isinstance(parent, ast.Module):
        start, end = lines[node.lineno - 1], lines[node.end_lineno - 1]
        if _clean_start(start, node.col_offset) and _clean_end(end, node.end_col_offset):
            return ("after", node.end_lineno, _indent_of(start, node.col_offset))
    current = node
    while True:
        holder = parents.get(current)
        if isinstance(current, ast.stmt):
            block = next((getattr(holder, field) for field in ("body", "orelse", "finalbody")
                          if any(item is current for item in getattr(holder, field, None) or ())),
                         [current])
            starter = next(item for item in block if item.lineno == current.lineno)
            if any(starter is item for item in head):  # ``from __future__ import x; y = 1``
                return ("after", head[-1].end_lineno, "")
            line = lines[starter.lineno - 1]
            if (_clean_start(line, starter.col_offset)
                    and not line[starter.col_offset:].startswith("elif")
                    and not _is_docstring(starter, holder)):  # ``"""doc"""; x = 1``
                return ("before", starter.lineno, _indent_of(line, starter.col_offset))
        if holder is None or isinstance(holder, (ast.Module, *_SCOPES)):
            raise _SharesLine(f"line {row} shares its line with other code")
        current = holder


def _probe_site(tree: ast.AST, lines: List[str], row: int, parents: dict, head: list
                ) -> Tuple[str, int, str]:
    """Where the probe for an edited code row goes: ``(side, row, indent)``."""
    for node in ast.walk(tree):
        decorators = getattr(node, "decorator_list", None)
        if decorators and any(d.lineno <= row <= d.end_lineno for d in decorators):
            return _block_probe(node, row, lines)
    node = _innermost(tree, row)
    if node is None:
        raise CanaryUnavailable(f"line {row} is outside every statement")
    if isinstance(node, _SIMPLE):
        # Before the statement, not after it: a probe after a return or a raise is
        # dead code, which the compiler drops, so the bytecode would not change.
        return _before(node, row, lines, parents, head)
    try:
        return _block_probe(node, row, lines)
    except _SharesLine:
        # A header whose block shares its line, like ``if x: return y``. In front of
        # the header is still the same function; in front of a ``def`` is not.
        if isinstance(node, _SCOPES):
            raise
        return _before(node, row, lines, parents, head)


def canary_source(source: str, edited: set) -> Optional[str]:
    """``source`` with a neutral change after every logical line that holds an edited row.

    Two kinds of change, both invisible to what the code computes:

    - a comment at the end of each such logical line, which changes the text of
      the code (a claim that hashes the file or reads it with
      ``inspect.getsource`` fails on it);
    - an assignment to ``__adversary_probe__`` after each statement the patch
      wrote (or at the start of the block that follows a header), which changes
      the tree and the bytecode (a claim that hashes ``ast.dump`` or
      ``__code__.co_code`` fails on it). Comments alone do not change those.

    A claim that fails on this copy reads the code, not what it does (AG-046).
    The copy is returned only when removing the probes gives back the patch's
    own tree, so it differs from the patch by the probes and nothing else.
    ``None`` when the patch wrote no code line at all: nothing to probe.
    Raises ``CanaryUnavailable`` when the copy cannot be built, so the claims
    cannot be told apart from ones that read the code -- that is never a pass.
    """
    lines = source.split("\n")
    first_row: Optional[int] = None
    cuts: List[Tuple[int, int]] = []
    code_rows: set = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING):
                continue
            if tok.type == tokenize.ENDMARKER:
                break
            if first_row is None:
                first_row = tok.start[0]
            if tok.type == tokenize.NEWLINE:
                logical = set(range(first_row, tok.start[0] + 1))
                if logical & edited:
                    cuts.append(tok.start)
                    code_rows |= logical
                first_row = None
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise CanaryUnavailable(f"the file cannot be tokenized: {exc}") from exc
    if not cuts:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CanaryUnavailable(f"the file does not parse: {exc}") from exc
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    head = _module_head(tree)
    sites = {}
    for row in sorted(code_rows & set(edited)):
        side, at, indent = _probe_site(tree, lines, row, parents, head)
        sites[(side, at)] = indent
    # Insertion points are found on the original lines; the comments do not move a line.
    inserts = []
    for side, at in sites:
        inserts.append((at if side == "after" else at - 1, sites[(side, at)] + _PROBE_LINE))
    for row, col in sorted(cuts, reverse=True):
        text = lines[row - 1]
        lines[row - 1] = text[:col] + _CANARY + text[col:]
    for index, text in sorted(inserts, reverse=True):
        lines.insert(index, text)
    result = "\n".join(lines)
    try:
        neutral = ast.parse(result)
    except SyntaxError as exc:
        raise CanaryUnavailable(f"the probes do not parse: {exc}") from exc
    if ast.dump(_WithoutProbes().visit(neutral)) != ast.dump(tree):
        raise CanaryUnavailable("the copy differs from the patch by more than the probes")
    return result


#: A file the patch added is paired with the baseline file it most resembles
#: when at least this share of its lines match (git's own rename threshold).
PAIRING_THRESHOLD = 0.5
#: Past these sizes the comparison is not attempted and the added file stays
#: unmutated, as before -- never "all new", which could score a renamed module's
#: untouched code.
_PAIRING_MAX_FILES = 400
_PAIRING_MAX_LINES = 5000


def pair_added_file(rel: str, source: str, baseline_dir: Path) -> Tuple[Optional[str], str, float, bool]:
    """``(paired_path, baseline_text, similarity, comparable)`` for a file the patch added.

    Only the lines the patch wrote are mutation targets, and for an added file
    that is "every line" *unless* it is a renamed or copied module: a patch
    that moves ``pkg/a.py`` to ``pkg/b.py`` and edits one line wrote one line,
    and mutating the rest would score code nobody changed -- tests of the old
    module would kill those mutants and report strength for a diff nobody
    checked. So the added file is compared with every baseline source file and
    diffed against the most similar one when it resembles it enough.
    ``comparable`` is False when the comparison was too large to run.
    """
    new_lines = source.splitlines()
    candidates = [p for p in _python_files(Path(baseline_dir))]
    if len(candidates) > _PAIRING_MAX_FILES or len(new_lines) > _PAIRING_MAX_LINES:
        return None, "", 0.0, False
    best: Tuple[Optional[str], str, float] = (None, "", 0.0)
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        old_lines = text.splitlines()
        if len(old_lines) > _PAIRING_MAX_LINES:
            return None, "", 0.0, False
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        if matcher.real_quick_ratio() < best[2] or matcher.quick_ratio() < best[2]:
            continue
        ratio = matcher.ratio()
        if ratio > best[2]:
            best = (path.relative_to(Path(baseline_dir)).as_posix(), text, ratio)
    if best[0] is not None and best[2] >= PAIRING_THRESHOLD:
        return best[0], best[1], round(best[2], 3), True
    return None, "", round(best[2], 3), True


# ----------------------------------------------------------------------
# what to break
# ----------------------------------------------------------------------
_TRIVIA = frozenset({tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT})


def _is_operand_end(token: Optional[tokenize.TokenInfo]) -> bool:
    """Whether ``token`` can end the left operand of a binary operator."""
    if token is None:
        return False
    if token.type in (tokenize.NUMBER, tokenize.STRING):
        return True
    if token.type == tokenize.NAME:
        return not keyword.iskeyword(token.string) or token.string in ("True", "False", "None")
    return token.type == tokenize.OP and token.string in _OPERAND_END


def _mutants_in(source: str, limit: int) -> List[Mutation]:
    """Ordered, deterministic mutation sites in one module."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return []

    found: List[Mutation] = []
    previous: Optional[tokenize.TokenInfo] = None
    for token in tokens:
        replacement: Optional[str] = None
        if token.type == tokenize.OP:
            replacement = _OPERATOR_MUTATIONS.get(token.string)
            if replacement is not None and token.string in _BINARY_ONLY and not _is_operand_end(previous):
                replacement = None
        elif token.type == tokenize.NAME:
            replacement = _KEYWORD_MUTATIONS.get(token.string)
        if token.type not in _TRIVIA:
            previous = token
        if replacement is None:
            continue
        found.append((token.start, token.end, replacement))
        if len(found) >= limit:
            break
    return found


_COMPOUND = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try,
             ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
#: Statements that carry no behaviour of their own to break.
_INERT = (ast.Pass, ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)


def _statement_sites(source: str, edited: set, operator_rows: set) -> Tuple[Dict[int, List[Mutation]], List[int]]:
    """Census support: a site for every changed statement that has no operator site.

    Returns ``(sites by line, uncovered lines)``. A changed statement -- the
    lines it spans, or a compound statement's header -- needs at least one
    mutation site inside it for a census to cover the change. Statements the
    operator scan already reached are left alone; for the others the change
    itself is broken: a ``return``'s value becomes ``None``, an ``if``/``while``
    condition is negated, a ``for`` iterates nothing, any other statement is
    deleted (``pass``). Headers of ``def``/``class``/``with``/``try`` have no
    such mutation: their lines come back as *uncovered*, and a census that
    leaves a changed line uncovered is not one. Docstrings, ``pass`` and
    imports carry nothing to break and need no site.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}, sorted(edited)
    sites: Dict[int, List[Mutation]] = {}
    uncovered: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or isinstance(node, _INERT):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # a docstring
        if isinstance(node, _COMPOUND):
            body = getattr(node, "body", None) or []
            last = (body[0].lineno - 1) if body else node.lineno
            span = set(range(node.lineno, max(node.lineno, last) + 1))
        else:
            span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if not (span & edited) or (span & operator_rows):
            continue
        mutation: Optional[Mutation] = None
        if isinstance(node, (ast.If, ast.While)):
            text = ast.get_source_segment(source, node.test)
            if text is not None:
                mutation = ((node.test.lineno, node.test.col_offset),
                            (node.test.end_lineno, node.test.end_col_offset), f"not ({text})")
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            mutation = ((node.iter.lineno, node.iter.col_offset),
                        (node.iter.end_lineno, node.iter.end_col_offset), "()")
        elif isinstance(node, ast.Return) and node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            mutation = ((node.value.lineno, node.value.col_offset),
                        (node.value.end_lineno, node.value.end_col_offset), "None")
        elif not isinstance(node, _COMPOUND) and not isinstance(node, ast.Return):
            mutation = ((node.lineno, node.col_offset), (node.end_lineno, node.end_col_offset), "pass")
        if mutation is None:
            uncovered.extend(sorted(span & edited))
        else:
            sites.setdefault(mutation[0][0], []).append(mutation)
    return sites, sorted(set(uncovered))


def _spread(by_line: Mapping[Tuple[str, int], List[Mutation]], limit: int) -> List[Tuple[str, Mutation]]:
    """At most ``limit`` sites, one per changed line before any line gets two.

    AG-023: sites used to be taken in file order until the budget ran out, so
    a patch whose first changed line had six operators was judged on that
    line alone and every later line went unmutated. Round-robin over the lines
    the patch wrote (files in sorted order, lines ascending) keeps the sample
    deterministic and spreads it across the change.
    """
    queues = [(key, list(sites)) for key, sites in sorted(by_line.items())]
    plan: List[Tuple[str, Mutation]] = []
    depth = 0
    while len(plan) < limit and any(depth < len(sites) for _, sites in queues):
        for (rel, _line), sites in queues:
            if depth < len(sites):
                plan.append((rel, sites[depth]))
                if len(plan) >= limit:
                    break
        depth += 1
    return plan


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
    max_mutants: int = DEFAULT_MAX_MUTANTS,
    confidence: float = DEFAULT_CONFIDENCE,
    timeout_seconds: Optional[int] = 30,
    cpu_seconds: Optional[int] = 10,
    mem_bytes: Optional[int] = 512 * 1024 * 1024,
    processes: int = DEFAULT_PROCESSES,
    changed_paths: Optional[Iterable[str]] = None,
    targets: Optional[Sequence[str]] = None,
    run_kwargs: Optional[Mapping[str, Any]] = None,
    census_min: int = 0,
    alongside: Optional[Future] = None,
    parallel: int = 1,
) -> Tuple[SuiteStrength, Dict[str, object]]:
    """Break the patch's own code and see whether ``test_path`` notices.

    ``alongside`` is work started before this call (the full suite). It is
    waited for before the first run: a run that fails because something else
    touched the same thing outside the repository exits 1 like a kill, and
    nothing in the run tells the two apart (AG-043).

    ``parallel`` > 1 runs that many mutants at once, each worker on its own
    copy of the tree -- the operator's choice, for claims that share nothing
    outside the repository, because a neighbour that makes a run fail is
    counted as a kill. A wall-clock kill beside other runs is repeated alone
    after all of them finish. A kill by the CPU-time limit is not -- CPU time
    is the process's own -- unless the unmutated reference itself used a
    third of that limit.

    ``census_min`` > 0 turns on the census rule: when every mutation site of
    the change was executed -- nothing cut by the budget, every changed
    executable line with a site, no stillborn mutant -- and there are at least
    ``census_min`` of them, the score is the exact ratio, not a Wilson lower
    bound: an interval bounds a sample, and this was not one. Line-level
    sites (``_statement_sites``) are generated only then.

    Returns the score plus a detail dict for the evidence artefact, because a
    strength number with no survivor list cannot be audited: the previous
    version shipped a ``suite_strength`` field that was absent from the log
    entirely, so nobody could ever have caught that it was constant.

    ``is_measured`` is False when there was nothing to mutate, when no mutant
    could be built, or when every mutant was stillborn. Callers must treat
    that as *unknown*, never as strong -- ``SuiteStrength.is_strong`` already
    does.

    ``targets`` runs several test files or node ids against each mutant in one
    invocation: a mutant is killed when any of them fails. ``run_kwargs`` is
    how the caller's execution choices -- interpreter, environment, sandbox --
    reach the mutant runs too. Before it existed a mutant ran unsandboxed even
    under ``--sandbox bwrap``, and always on the gate's own interpreter.
    """
    if max_mutants <= 0:
        raise ValueError("max_mutants must be positive")
    if not test_path and not targets:
        raise ValueError("test_path is required to measure suite strength")

    patch_dir = Path(patch_dir)
    # Fixed shape on every return path: an artefact whose optional keys
    # disappear when nothing was measured cannot be queried reliably. The
    # three "what changed" keys come from :func:`classify_changes`, which the
    # CLI also calls on the ``--mutation-max 0`` path, so both routes describe
    # the same patch the same way.
    detail: Dict[str, object] = {
        "measured": False,
        "reason": "",
        **classify_changes(baseline_dir, patch_dir, changed_paths),
        "mutants": [],
        "survivors": [],
        "mutants_counted": 0,
        "stillborn": 0,
        "census": {"exact": False, "why_not": "off (--census-min-mutants 0)" if census_min <= 0 else "nothing measured"},
        "reference_run": None,
        "source_sensitive_claims": False,
        "canary": {"files": [], "exit_code": None},
        "added_files": {},
    }
    changed = list(detail["changed_files"])  # type: ignore[arg-type]
    foreign = list(detail["foreign_changed_files"])  # type: ignore[arg-type]

    def finish(result: SuiteStrength, reason: str = "") -> Tuple[SuiteStrength, Dict[str, object]]:
        detail["measured"] = result.is_measured
        detail["reason"] = reason
        # Both sides of the ratio, so the score in the artefact can be
        # recomputed from the artefact instead of trusted.
        detail["mutants_counted"] = result.mutants_total
        detail["mutants_killed"] = result.mutants_killed
        # AG-023: the number the floor is applied to, and the interval it
        # comes from -- recomputable from the two counts above.
        detail["score"] = result.mutation_score if result.is_measured else None
        detail["confidence"] = result.confidence
        detail["interval"] = [result.lower, result.upper] if result.is_measured else None
        return result, detail

    if not changed:
        if foreign:
            # Not "nothing changed" -- something changed and it is outside
            # what this engine can break. Saying otherwise put a false claim
            # in the evidence artefact and let the patch merge unjudged.
            shown = ", ".join(foreign[:5])
            more = f" (+{len(foreign) - 5} more)" if len(foreign) > 5 else ""
            return finish(
                SuiteStrength(0, 0, 0.0, is_measured=False),
                f"{len(foreign)} non-Python source file(s) changed that suite "
                f"strength cannot judge: {shown}{more}",
            )
        return finish(
            SuiteStrength(0, 0, 0.0, is_measured=False),
            "no source file changed between baseline and patch",
        )

    originals: Dict[str, str] = {}
    pairing: Dict[str, Dict[str, object]] = {}
    edited_rows: Dict[str, set] = {}
    by_line: Dict[Tuple[str, int], List[Mutation]] = {}
    uncovered_lines: List[str] = []
    #: Statement-level sites (census only). They prove a census covers the
    #: change; they never count toward a sample -- deleting a statement is an
    #: easy kill, and counted it diluted the one operator mutant that looked
    #: like the real bug (more-itertools d71c4ad: 4/5 -> MERGE on a real bug).
    coarse: set = set()
    for rel in changed:
        try:
            source = (patch_dir / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # deleted by the patch (or unreadable): nothing left to mutate
        try:
            before = (Path(baseline_dir) / rel).read_text(encoding="utf-8")
        except FileNotFoundError:
            # Added by the patch. Mutated against the baseline file it was
            # moved or copied from, or in every line when it resembles none.
            paired, before, similarity, comparable = pair_added_file(rel, source, Path(baseline_dir))
            if not comparable:
                pairing[rel] = {"paired_with": None, "similarity": None, "mutated": False,
                                "why": "too large to compare with the baseline"}
                continue
            pairing[rel] = {"paired_with": paired, "similarity": similarity, "mutated": True}
        except (OSError, UnicodeDecodeError):
            continue
        originals[rel] = source
        edited = changed_lines(before, source)
        if not edited:
            continue
        edited_rows[rel] = edited
        # Scan generously, then keep only sites on lines this patch wrote,
        # grouped by line.
        operator_rows = set()
        for site in _mutants_in(source, 10_000):
            if site[0][0] in edited:
                by_line.setdefault((rel, site[0][0]), []).append(site)
                operator_rows.add(site[0][0])
        if census_min > 0:
            extra, missing = _statement_sites(source, edited, operator_rows)
            for row, sites in extra.items():
                by_line.setdefault((rel, row), []).extend(sites)
                coarse.update((rel, site) for site in sites)
            uncovered_lines.extend(f"{rel}:{n}" for n in missing)
    total_sites = sum(len(v) for v in by_line.values())
    plan = _spread(by_line, max_mutants)
    detail["added_files"] = pairing
    # Fail closed: an added module that could not be paired with the baseline
    # was never mutated, so its code is unjudged. Scoring the rest of the patch
    # would present that number as the strength of the whole patch (a mixed
    # patch on a baseline over the pairing limits reached MERGE this way).
    unpaired = [rel for rel, info in pairing.items() if not info["mutated"]]
    if unpaired:
        shown = ", ".join(sorted(unpaired)[:5])
        more = f" (+{len(unpaired) - 5} more)" if len(unpaired) > 5 else ""
        return finish(
            SuiteStrength(0, 0, 0.0, is_measured=False),
            f"added file(s) too large to pair with the baseline, so their code was "
            f"never mutated and nothing about the patch can be measured: {shown}{more}",
        )

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

        def _run(directory: Path = work):
            return run_test(
                directory, test_path, test_id,
                timeout_seconds=timeout_seconds, cpu_seconds=cpu_seconds,
                mem_bytes=mem_bytes, processes=processes, targets=targets,
                **dict(run_kwargs or {}),
            )

        # AG-043: nothing runs beside the mutants. A run that fails with exit 1
        # is a kill, and a run that failed because the suite touched the same
        # thing outside the repository (a fixed /tmp path, a port, a test
        # database) looks exactly like one. 2.14.0 re-ran only the kills at a
        # limit; a weak test went from 0/8 to 4/8 with the suite alongside.
        if alongside is not None:
            wait([alongside])  # its exception, if any, is the caller's to raise

        # AG-042: the claims, unmutated, once. A mutant whose run is killed at
        # the time limit made the tests hang -- detected -- but only if the
        # unmutated run finishes with room to spare. If the reference itself
        # is slow, a timeout says nothing about the mutant (the AG-039 trap:
        # counted as kills, a slow run would read as a perfect score).
        # The reference runs with or without a time limit: it is what every
        # mutant is judged against, not only the yardstick for hangs (UX-4).
        started = time.monotonic()
        reference = _run()
        elapsed = time.monotonic() - started
        reference_passed = reference.exit_code == PYTEST_OK
        reference_fast = bool(timeout_seconds) and reference_passed and elapsed < timeout_seconds / 3
        detail["reference_run"] = {"exit_code": reference.exit_code, "seconds": round(elapsed, 3),
                                   "hang_counts_as_kill": reference_fast}
        if not reference_passed and (reference.timed_out or reference.exit_code < 0):
            # Stopped by --timeout, --cpu-seconds or --memory: the claims neither
            # passed nor failed, so the reason names the limit (AG-039, F).
            return finish(
                SuiteStrength(0, 0, 0.0, is_measured=False),
                f"the claims, run unmutated, were killed by a limit (exit {reference.exit_code}): "
                "--timeout, --cpu-seconds or --memory stopped them before they could pass or "
                "fail, so suite strength is not measured",
            )
        if not reference_passed:
            # D: a mutant whose run also exits 1 fails for the same reason the
            # unmutated patch does, so it is not a kill. Nothing is measured.
            return finish(
                SuiteStrength(0, 0, 0.0, is_measured=False),
                f"the claims did not pass on the unmutated patch (exit {reference.exit_code}): "
                "a mutant that fails the same way is not a kill, so suite strength is not measured",
            )
        # A run killed by a signal *other* than the wall clock (the CPU-time
        # limit, mostly) does not depend on what else the machine was doing --
        # CPU time is the process's own -- unless the reference itself came
        # close to that limit. A wall-clock kill under load does depend on it.
        cpu_room = not cpu_seconds or elapsed < cpu_seconds / 3

        # AG-046: the claims on a copy whose only difference is a comment after
        # each line the patch wrote. The code does exactly the same, so a claim
        # that stops passing was not checking what the code does -- it was
        # reading the code (a hash of the file, ``inspect.getsource``). Such a
        # claim "kills" every mutant, so its score says nothing; it cannot be
        # measured, and the reason says why.
        canary_files = {}
        for rel, rows in edited_rows.items():
            try:
                neutral = canary_source(originals[rel], rows)
            except CanaryUnavailable as exc:
                # Fail closed (AG-046 follow-up): a file the patch changed without a
                # neutral copy is a file whose claims cannot be told apart from ones
                # that read the code. Skipping it would pass them unexamined.
                return finish(
                    SuiteStrength(0, 0, 0.0, is_measured=False),
                    f"no neutral copy of {rel} (comments and a no-op assignment) can be built "
                    f"({exc}), so the "
                    "claims cannot be checked for reading the code instead of running it",
                )
            if neutral is not None:
                canary_files[rel] = neutral
        # The unmutated claims passed (checked above), so a claim that fails on
        # the neutral copy is reading the code, not running it.
        if canary_files:
            for rel, neutral in canary_files.items():
                (work / rel).write_text(neutral, encoding="utf-8")
            try:
                probe = _run()
                if probe.exit_code != PYTEST_OK:
                    # Once more: a claim that reads the text fails again, a run
                    # a busy machine cut short does not. Two failures decide.
                    probe = _run()
            finally:
                for rel in canary_files:
                    (work / rel).write_text(originals[rel], encoding="utf-8")
            detail["canary"] = {"files": sorted(canary_files), "exit_code": probe.exit_code}
            if probe.exit_code != PYTEST_OK:
                sensitive = probe.exit_code == PYTEST_TESTS_FAILED
                detail["source_sensitive_claims"] = sensitive
                return finish(
                    SuiteStrength(0, 0, 0.0, is_measured=False),
                    ("the claims fail on a copy of the patch whose only changes are a comment after "
                     "each line it wrote and a no-op assignment beside it: they read the text of the "
                     "code or its structure, not what it does, so breaking the code proves nothing "
                     "about them"
                     if sensitive else
                     f"the claims could not be run on the neutral copy of the patch (exit code "
                     f"{probe.exit_code}), so nothing was measured"),
                )
        else:
            detail["canary"] = {"files": [], "exit_code": None}

        def _ambiguous(outcome) -> bool:
            """Killed at a limit that the load around this run may have reached for it."""
            return reference_fast and (
                outcome.timed_out or (outcome.exit_code < 0 and not cpu_room)
            )

        # Every mutant gets its entry up front, in plan order: the artefact
        # (and the census) must not depend on which worker finished first.
        jobs: List[Tuple[Dict[str, object], Path, str, str]] = []
        for rel, (start, end, replacement) in plan:
            original = originals[rel]
            mutant_source = "\n".join(_apply(original.split("\n"), start, end, replacement))
            entry: Dict[str, object] = {
                "file": rel,
                "line": start[0],
                "replaces": replacement or "<deleted>",
                "kind": "statement" if (rel, (start, end, replacement)) in coarse else "operator",
            }
            detail["mutants"].append(entry)  # type: ignore[union-attr]
            if mutant_source == original:
                entry["result"] = "no-op"
                continue
            jobs.append((entry, Path(rel), mutant_source, original))

        def _attempt(directory: Path, job) -> Any:
            """One run of one mutant in ``directory``: the outcome, or the exception."""
            _entry, rel, mutant_source, original = job
            target = directory / rel
            try:
                target.write_text(mutant_source, encoding="utf-8")
                return _run(directory)
            except Exception as exc:  # noqa: BLE001 - a broken harness is not suite evidence
                return exc
            finally:
                if target.exists():
                    target.write_text(original, encoding="utf-8")

        # (outcome, contended): contended runs had a neighbour.
        outcomes: List[Tuple[Any, bool]] = []
        workers = max(1, min(parallel, len(jobs)))
        if workers == 1:
            outcomes = [(_attempt(work, job), False) for job in jobs]
        else:
            # Opt-in (--mutation-workers): each worker breaks its own copy, and
            # every run beside another one is "contended". A run its neighbours
            # may have slowed past the wall clock is repeated alone below
            # before it can count. A run a neighbour made *fail* -- the same
            # /tmp path, the same port -- is not told apart from a kill, which
            # is why this is the operator's choice and off by default (AG-043).
            copies = [work] + [Path(tmp) / f"repo{i}" for i in range(1, workers)]
            for copy in copies[1:]:
                shutil.copytree(work, copy)

            def _lane(index: int) -> List[Tuple[int, Tuple[Any, bool]]]:
                return [(i, (_attempt(copies[index], jobs[i]), True))
                        for i in range(index, len(jobs), workers)]

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="adversary-mutant") as pool:
                lanes = [pool.submit(_lane, i) for i in range(workers)]
                by_index = dict(pair for lane in lanes for pair in lane.result())
            outcomes = [by_index[i] for i in range(len(jobs))]

        counted = killed = 0
        survivors: List[str] = []
        for job, (outcome, contended) in zip(jobs, outcomes):
            entry = job[0]
            if not isinstance(outcome, BaseException) and contended and _ambiguous(outcome):
                # Slowed or hung? Only a run with nothing beside it says, and
                # its answer is the result. Every lane has finished by now.
                entry["rerun_alone"] = outcome.exit_code
                outcome = _attempt(work, job)
            if isinstance(outcome, BaseException):
                entry["result"] = "harness-error"
                entry["error"] = f"{type(outcome).__name__}: {outcome}"
                continue
            code = outcome.exit_code
            where = f"{entry['file']}:{entry['line']}"
            if code == PYTEST_OK:
                counted += 1
                entry["result"] = "survived"
                survivors.append(where)
            elif code == PYTEST_TESTS_FAILED:
                counted += 1
                killed += 1
                entry["result"] = "killed"
            elif reference_fast and (outcome.timed_out or code < 0):
                # The unmutated claims finish well inside the limit; this
                # mutant made them never finish. That is a detection.
                counted += 1
                killed += 1
                entry["result"] = "killed"
                entry["by"] = "timeout"
                entry["exit_code"] = code
            else:
                detail["stillborn"] = int(detail["stillborn"]) + 1  # type: ignore[arg-type]
                entry["result"] = "stillborn"
                entry["exit_code"] = code

        detail["survivors"] = survivors

    entries = detail["mutants"]  # type: ignore[assignment]
    odd = [m for m in entries if m.get("result") not in ("killed", "survived")]  # type: ignore[union-attr]
    why_not = ""
    if census_min <= 0:
        why_not = "off (--census-min-mutants 0)"
    elif total_sites > len(plan):
        why_not = f"budget cut the sample: {total_sites} sites, {len(plan)} executed"
    elif uncovered_lines:
        why_not = "changed lines with no mutation site: " + ", ".join(uncovered_lines[:8])
    elif odd:
        why_not = f"{len(odd)} mutant(s) neither killed nor survived (stillborn, no-op or harness error)"
    elif counted < census_min:
        why_not = f"{counted} mutant(s), below the minimum of {census_min} for a census"
    elif killed < counted:
        # Exact both ways: with every site enumerated, a survivor is not
        # sampling noise but a named line the tests cannot see.
        why_not = f"{counted - killed} mutant(s) survived: in a census a survivor is a known blind spot"
    exact = not why_not
    if not exact:
        # A sample: operator mutants only -- statement deletions are easy
        # kills and would pad it.
        counted = sum(1 for m in entries if m.get("kind") == "operator" and m.get("result") in ("killed", "survived"))  # type: ignore[union-attr]
        killed = sum(1 for m in entries if m.get("kind") == "operator" and m.get("result") == "killed")  # type: ignore[union-attr]
    detail["census"] = {"exact": exact, "sites": total_sites, "minimum": census_min,
                        **({} if exact else {"why_not": why_not})}
    result = calculate_mutation_score(counted, killed, 0.0 if exact else confidence)
    if result.is_measured:
        note = ""
        if foreign:
            # The score just produced covers the Python files it mutated and
            # nothing else. Leaving ``reason`` blank let a 1.0 read as the
            # strength of the whole patch while part of the change was never
            # executed; say what the number is actually a number for.
            note = (
                f"score covers the {len(changed)} Python file(s) mutated only; "
                f"{len(foreign)} non-Python source file(s) in this patch were "
                "never judged"
            )
        return finish(result, note)
    codes = [m.get("exit_code") for m in detail["mutants"] if m.get("result") == "stillborn"]  # type: ignore[union-attr]
    if codes and all(isinstance(c, int) and c < 0 for c in codes):
        # Killed by a limit, not a statement about the mutants (AG-039: the
        # old message blamed the test target for what --cpu-seconds did).
        return finish(
            result,
            f"every mutant run was killed by a limit (exit codes {sorted(set(codes))}): "
            "--timeout, --cpu-seconds or --memory stopped the tests before they "
            "could pass or fail, so nothing was measured",
        )
    return finish(
        result,
        "every mutant was stillborn (exit codes "
        f"{sorted(set(c for c in codes if c is not None))}): the tests could not be "
        "collected or run against the mutated code",
    )
