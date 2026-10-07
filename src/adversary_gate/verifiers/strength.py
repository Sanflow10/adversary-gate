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
) -> Tuple[SuiteStrength, Dict[str, object]]:
    """Break the patch's own code and see whether ``test_path`` notices.

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
            before = (Path(baseline_dir) / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # One side is missing: added on the patch side, or deleted by it.
            # Neither has a pair to diff, so neither can produce a mutant.
            continue
        originals[rel] = source
        edited = changed_lines(before, source)
        if not edited:
            continue
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

        # AG-042: the claims, unmutated, once. A mutant whose run is killed at
        # the time limit made the tests hang -- detected -- but only if the
        # unmutated run finishes with room to spare. If the reference itself
        # is slow, a timeout says nothing about the mutant (the AG-039 trap:
        # counted as kills, a slow run would read as a perfect score).
        reference_fast = False
        if timeout_seconds:
            started = time.monotonic()
            reference = run_test(
                work, test_path, test_id,
                timeout_seconds=timeout_seconds, cpu_seconds=cpu_seconds,
                mem_bytes=mem_bytes, processes=processes, targets=targets,
                **dict(run_kwargs or {}),
            )
            elapsed = time.monotonic() - started
            reference_fast = reference.exit_code == PYTEST_OK and elapsed < timeout_seconds / 3
            detail["reference_run"] = {"exit_code": reference.exit_code, "seconds": round(elapsed, 3),
                                       "hang_counts_as_kill": reference_fast}

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
                "kind": "statement" if (rel, (start, end, replacement)) in coarse else "operator",
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
                    targets=targets,
                    **dict(run_kwargs or {}),
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
