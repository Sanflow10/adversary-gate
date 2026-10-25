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
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from adversary_gate.core.exitmap import PYTEST_OK, PYTEST_TESTS_FAILED
from adversary_gate.sandbox.runner import DEFAULT_PROCESSES, run_test
from adversary_gate.verifiers.testpaths import is_test_path as _is_test_path
from adversary_gate.verifiers.testpaths import is_test_rel as _is_test_rel
from adversary_gate.verifiers.testpaths import production_imported_tests as _production_imported_tests

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
#: The sample is filled up to this many when the change has sites to spare.
MIN_SAMPLE = 12

#: The ceiling. Every changed line is broken at least once, whatever the
#: sample: until 3.0.3 the 12 mutants were simply the first 12 changed lines
#: in file order, and three untested boundary changes after them merged. A
#: change that needs more than this to be broken once per line is not
#: measured, and the reason says how far to raise ``--mutation-max``.
DEFAULT_MAX_MUTANTS = 60


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


def kills_to_clear(floor: float, confidence: float = DEFAULT_CONFIDENCE, cap: int = 1000) -> Optional[int]:
    """The fewest mutants, every one killed, whose lower bound reaches ``floor``."""
    # A small change has few mutation sites, and n out of n killed bounds the
    # strength at no more than this allows: 1/1 is 0.38 at 0.80 confidence.
    # Saying how many it takes tells a too-small sample apart from weak tests.
    # None when no count up to ``cap`` reaches the floor.
    for n in range(1, cap + 1):
        if wilson_interval(n, n, confidence)[0] >= floor:
            return n
    return None


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
    #: Branch or statement-level mutants that survived: parts of the change no
    #: claim checks. The ratio stays what it measured; it is not strong.
    blind_spots: int = 0

    @property
    def is_strong(self) -> bool:
        return self.lower >= 0.75 and not self.blind_spots if self.is_measured else False


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
    # packaged artefacts and build intermediates. Not what a runtime loads:
    # ``.so``/``.pyd``/``.dll``/``.dylib``, ``.pyc``/``.pyo``, ``.jar``,
    # ``.class``, ``.wasm`` and ``.exe`` are code -- Python imports
    # ``rules.so`` before ``rules.py`` -- and were on this list until 3.0.7, so
    # a compiled module placed next to a source one replaced it unjudged.
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".war",
    ".a", ".o", ".obj", ".lib",
    ".min", ".map", ".cache",
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


#: What a test compares output against. Documents are not: a project's tests
#: often read its README, and an edited README is not a rewritten answer.
_DATA_SUFFIXES = frozenset({".json", ".jsonc", ".jsonl", ".jsonlines", ".ndjson", ".yml",
                            ".yaml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv",
                            ".xml", ".properties", ".txt"})

#: What zipimport loads from an archive on sys.path.
_IMPORTABLE_SUFFIXES = (".py", ".pyc", ".pyo", ".so", ".pyd")

#: ELF, PE and Mach-O (both byte orders, 32 and 64 bit): what ctypes can load.
#: A universal (fat) Mach-O, the usual shape of a macOS ``.dylib``, opens with
#: its own big-endian magic, 32 or 64 bit; it was missed until 3.0.16. Java's
#: ``.class`` shares ``cafebabe``, and is code as well.
_NATIVE_MAGIC = frozenset({b"\x7fELF", b"MZ\x90\x00", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                           b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                           b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"})

#: The flags word of a ``.pyc`` header: by timestamp (0), by hash (1, 3).
_PYC_FLAGS = frozenset({b"\x00\x00\x00\x00", b"\x01\x00\x00\x00", b"\x03\x00\x00\x00"})


def _carries_code(path: Path) -> bool:
    """True for a zip archive that holds a module Python can import.

    ``sys.path.insert(0, "deps.zip")`` imports from it, whatever the file is
    called -- ``logo.png`` works as well. As a non-source suffix (or a test
    fixture) it was never judged, and a patch whose logic sat inside one with
    a covered import line in front reached MERGE (3.0.12). Decided by content.

    A compiled module or native library is code too, wherever it sits:
    ``tests/_bin/ratelib.<tag>.so`` behind ``sys.path.insert`` held the logic
    and, test-pathed, was never judged (3.0.14). Its name or its magic says so.
    """
    import zipfile
    if path.name.lower().endswith(_IMPORTABLE_SUFFIXES[1:]):
        return True
    try:
        with open(path, "rb") as handle:
            head = handle.read(17)
            magic = head[:4]
            if magic[:2] == b"MZ":
                # Windows checks ``MZ`` and the ``PE\0\0`` that offset 0x3C
                # points to, not the two bytes after ``MZ``: a DLL opening
                # ``MZP`` (Delphi writes it) was not code until 3.0.16.
                handle.seek(0x3C)
                handle.seek(int.from_bytes(handle.read(4), "little"))
                if handle.read(4) == b"PE\x00\x00":
                    return True
    except OSError:
        return False
    if magic in _NATIVE_MAGIC:
        return True
    # Bytecode, by content: ``SourcelessFileLoader`` loads a ``.pyc`` under any
    # name, and ``tests/_bin/rate.dat`` holding one reached MERGE (3.0.15).
    # Every CPython magic ends ``\r\n``, then the flags, then a code object.
    if head[2:4] == b"\r\n" and head[4:8] in _PYC_FLAGS and head[16:17] in (b"c", b"\xe3"):
        return True
    try:
        with zipfile.ZipFile(path) as archive:
            return any(name.lower().endswith(_IMPORTABLE_SUFFIXES) for name in archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
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
    disguised = _production_imported_tests(root)
    return sorted(
        p
        for p in root.rglob("*.py")
        if p.is_file() and "__pycache__" not in p.parts and not _is_test_rel(
            p.relative_to(root).as_posix(), disguised
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

    code: set = set()

    def snapshot(root: Path) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        if not root.is_dir():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            # A ``.py`` name is no proof of source: ``ctypes`` loads a native
            # library by any name, and ``tests/_bin/rate.py`` holding one was
            # skipped as a test and reached MERGE (3.0.15). Decided by content.
            if _carries_code(path):
                code.add(rel)
            elif _is_test_path(rel) or _is_scan_noise(rel):
                continue
            try:
                out[rel] = path.read_bytes()
            except OSError:
                continue
        return out

    base = snapshot(Path(baseline_dir))
    patch = snapshot(Path(patch_dir))
    candidates = {rel for rel in set(base) | set(patch) if base.get(rel) != patch.get(rel)}
    named_by_diff = {rel for rel in (changed_paths or ()) if rel and not rel.startswith(("/", "../"))}
    if named_by_diff:
        # A path from the diff counts even if it no longer exists on either
        # side or the scan cannot see it: the diff says the patch touched it.
        candidates.update(named_by_diff)
    # A document or data file is code once Python runs it: ``exec(open(
    # "NOTICE.txt").read())`` behind one covered line reached MERGE with the
    # logic in the text file (3.0.12). Said by its content, or by a changed
    # module that runs text it reads.
    loads_text = any(_runs_text(patch[rel]) for rel in candidates
                     if rel.endswith(".py") and rel in patch)
    for rel in candidates:
        if not rel.endswith(".py") and _is_non_source(rel) and rel in patch \
                and (loads_text or _is_python(patch[rel])):
            code.add(rel)
    # A test folder is no hiding place for text a module runs, changed or not:
    # ``exec(open("tests/data/rules.txt").read())`` reached MERGE (3.0.15), and
    # so did a patch that changed only that text, its module already there.
    runners = _resident_runners(Path(patch_dir))
    if runners:
        # Only text that parses as Python and defines something counts there,
        # so a fixture of prose or numbers stays a fixture.
        # Outside test folders this repeats what the scan judged; one file at
        # a time, so a large tree is never held in memory twice.
        # A test-folder module is no hiding place either when a module runs it
        # by name: ``exec(open("tests/data/rules.py").read())`` reached
        # MERGE (3.0.15). Named in a string, so a changed test it never names
        # stays a test; a name built at run time is the open case.
        named = _string_constants(runners)
        for path in sorted(Path(patch_dir).rglob("*")):
            rel = path.relative_to(patch_dir).as_posix()
            # Not the environment or VCS trees: a checkout's ``.venv/bin/pip``
            # is Python text no patch wrote (the scan skips them as well).
            if _is_scan_noise(rel):
                continue
            if rel.endswith(".py") and not (_is_test_path(rel) and _names_file(named, rel)):
                continue
            # A regular file only: git keeps a link to ``/dev/zero``, and reading
            # one never ends.
            if not path.is_file():
                continue
            data = path.read_bytes()
            try:
                same = (Path(baseline_dir) / rel).read_bytes() == data
            except OSError:
                same = False  # new in the patch
            # A formula defines nothing: ``eval(open("formula.txt").read())``
            # on ``a * 3`` reached MERGE (3.0.15). Named by such a module, an
            # expression that computes is code; a named table or JSON is data.
            if not same and (_is_python(data) or (_names_file(named, rel) and _is_formula(data))):
                code.add(rel)
                candidates.add(rel)
    # A data file a test reads is what the test checks against: the patch that
    # changed the code and rewrote ``golden/expected.json`` to match kept the
    # old test passing and reached MERGE (3.0.14). Named by a test, it is
    # judged like a rewritten test would be: not at all, so not silently.
    # Only what the patch wrote: with a diff, what it names -- the checkout
    # also holds what the run itself wrote (``coverage.json``, the evidence).
    named = _named_by_tests(Path(baseline_dir), Path(patch_dir))
    for rel in (named_by_diff if named_by_diff else candidates):
        if Path(rel).suffix.lower() in _DATA_SUFFIXES and not _is_test_path(rel) \
                and _mentions(named, rel):
            code.add(rel)
    return sorted(
        rel
        for rel in candidates
        # .py is judged by the mutation engine, tests are not code under test,
        # and documented non-code should not force an INCONCLUSIVE on its own.
        if rel in code
        or (not rel.endswith(".py") and not _is_test_path(rel) and not _is_non_source(rel))
    )


#: Calls that run source text a module read from somewhere. A child process
#: runs it as well: ``subprocess.run([sys.executable, "rules.conf"])`` with the
#: logic in plain assignments reached MERGE (3.0.14).
_TEXT_RUNNERS = frozenset({"exec", "eval", "compile", "run_path", "run_module",
                           "SourceFileLoader", "spec_from_file_location",
                           "spec_from_loader", "exec_module", "load_module",
                           "run", "Popen", "call", "check_call", "check_output",
                           "getoutput", "getstatusoutput", "system", "popen",
                           "execv", "execve", "execvp", "execvpe", "execl", "execle",
                           "execlp", "execlpe", "spawnv", "spawnve", "spawnvp",
                           "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                           "posix_spawn", "posix_spawnp", "startfile",
                           "create_subprocess_exec", "create_subprocess_shell"})


def _runs_text(source: bytes) -> bool:
    """True when ``source`` calls one of :data:`_TEXT_RUNNERS`, by any spelling."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else \
                func.attr if isinstance(func, ast.Attribute) else None
            if name in _TEXT_RUNNERS:
                return True
        elif isinstance(node, ast.Name) and node.id in _TEXT_RUNNERS:
            return True  # passed around, aliased: ``run = exec``
    return False


def _resident_runners(root: Path) -> List[bytes]:
    """Every non-test module on ``root`` that runs text: see :func:`_runs_text`.

    A test-folder module the code under test imports is not a test: through
    one, ``tests/helpers.py`` running ``tests/data/rules.txt``, a patch that
    changed only the text reached MERGE (3.0.15).
    """
    out: List[bytes] = []
    disguised = _production_imported_tests(root)
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if _is_test_rel(rel, disguised) or _is_scan_noise(rel) or not path.is_file():
            continue
        source = path.read_bytes()
        if _runs_text(source):
            out.append(source)
    return out


def _string_constants(sources: Iterable[bytes]) -> List[str]:
    """Every string literal in ``sources``, f-string pieces included.

    Only for sources :func:`_runs_text` already parsed.
    """
    return [node.value for source in sources for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _names_file(strings: Iterable[str], rel: str) -> bool:
    """True when a string holds the file name of ``rel``, or is its stem.

    The stem alone, or ending a path (``"tests/data/" + "rules" + ".py"``):
    inside any string it named ``common`` or ``util`` in one library string in
    ten.
    """
    name = rel.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    return any(name in text or text == stem or text.endswith("/" + stem) for text in strings)


def _named_by_tests(*roots: Path) -> bytes:
    """Every test module's text on both sides, joined: what the tests may name."""
    texts: List[bytes] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if _is_scan_noise(rel) or not _is_test_path(rel) or not path.is_file():
                continue
            try:
                texts.append(path.read_bytes())
            except OSError:
                continue
    return b"\n".join(texts)


def _mentions(text: bytes, rel: str) -> bool:
    """True when ``text`` names the file ``rel``, or its stem, or a folder it sits in."""
    parts = rel.split("/")
    words = {parts[-1], parts[-1].rsplit(".", 1)[0], *parts[:-1]}
    return any(len(word) > 2 and word.encode("utf-8", "replace") in text for word in words)


def _is_formula(data: bytes) -> bool:
    """True for text that parses as Python and computes: an operator, a test, a call."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, (ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
                                 ast.NamedExpr))
               for node in ast.walk(tree))


def _is_python(data: bytes) -> bool:
    """True for text that parses as Python and defines something."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Lambda, ast.Import, ast.ImportFrom))
               for node in ast.walk(tree))


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

    Rows are counted at ``\\n`` only, as tokenize, ast and the canary count
    them. ``splitlines`` also breaks at a form feed, U+2028 and others, so after
    one of those the rows drifted and the mutants broke the line below the one
    the patch wrote (3.0.11; the diff parser had the same fix in 3.0.7).
    """
    old = baseline_text.split("\n")
    new = patch_text.split("\n")
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
#: the code computes. The name ends in ``__``, so a class does not mangle it. It is
#: deleted on the same line: left bound, it showed up in ``locals()`` and in a
#: class's ``vars()``, and an honest test of either failed on the copy (3.0.1).
_PROBE = "__adversary_probe__"
_PROBE_LINE = f"{_PROBE} = 0; del {_PROBE}"
_SIMPLE = (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return, ast.Raise,
           ast.Assert, ast.Pass, ast.Delete, ast.Import, ast.ImportFrom, ast.Global,
           ast.Nonlocal, ast.Break, ast.Continue)


class CanaryUnavailable(Exception):
    """No neutral copy can be built for a file the patch changed (AG-046)."""


def _is_probe(node: ast.AST) -> bool:
    return (isinstance(node, (ast.Assign, ast.Delete)) and len(node.targets) == 1
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


def _fstring_spans(source: str) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Outermost f-strings as ``(start, end)`` positions; Python 3.12+ tokenizes inside them."""
    start_type = getattr(tokenize, "FSTRING_START", None)
    if start_type is None:
        return []  # one STRING token each: no operator inside is a token of its own
    spans, stack = [], []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == start_type:
            stack.append(tok.start)
        elif tok.type == tokenize.FSTRING_END and stack:
            begin = stack.pop()
            if not stack:
                spans.append((begin, tok.end))
    return spans


def canary_source(source: str, edited: set) -> Optional[str]:
    """``source`` with a neutral change after every logical line that holds an edited row.

    Three kinds of change, all invisible to what the code computes:

    - a comment at the end of each such logical line, which changes the text of
      the code (a claim that hashes the file or reads it with
      ``inspect.getsource`` fails on it);
    - a space after each token on an edited row that a mutant would swap, which
      changes the text *inside* the line (a claim that looks for a piece of it,
      like ``"a + b" in source``, fails on it -- the comment leaves that intact);
    - an assignment to ``__adversary_probe__``, deleted on the same line, after
      each statement the patch wrote (or at the start of the block that follows
      a header), which changes the tree and the bytecode (a claim that hashes
      ``ast.dump`` or ``__code__.co_code`` fails on it). Text alone does not.

    A claim that fails on this copy reads the code, not what it does (AG-046).
    One that normalizes the text before comparing, or reads one expression of
    the tree, can still pass: the copy narrows what a reading claim can do, it
    does not prove a claim is not one.
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
    # A claim can read a *piece* of a changed line, too: every mutant swaps a
    # token inside it, so a substring that spans one fails on every mutant and
    # scores 1.0 while checking nothing -- the comment at the end of the line
    # left that substring intact (3.0.3). A space after each token a mutant
    # would swap breaks it, and changes nothing the code computes. Inside an
    # f-string a space could change what ``{x=}`` prints: those are left alone.
    spans = _fstring_spans(source)
    edits = [(row, col, _CANARY) for row, col in cuts]
    edits += [(site[1][0], site[1][1], " ") for site in _mutants_in(source, 10_000)
              if site[0][0] in edited and not any(a <= site[0] < b for a, b in spans)]
    for row, col, text in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        line = lines[row - 1]
        lines[row - 1] = line[:col] + text + line[col:]
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


def _number_mutation(text: str) -> Optional[str]:
    """``n`` -> ``n + 1`` for an integer literal, or ``None``.

    Until 3.0.11 no mutant touched a constant, so on a patch that only swapped
    an operator the one mutant was the baseline itself, and a test asserting
    ``!= 1`` scored the same as one asserting ``== 3``. Integers only: a float
    or complex literal moved by one is often a value no test pins.
    """
    try:
        value = int(text, 0)
    except ValueError:
        return None
    return str(value + 1)


def _mutants_in(source: str, limit: int, constants: bool = False) -> List[Mutation]:
    """Ordered, deterministic mutation sites in one module.

    ``constants`` adds :func:`_number_mutation`; off by default (3.0.11): a
    literal no test can observe, a timeout or a buffer size, survives and is
    counted against the tests, so turning it on changes verdicts.
    """
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
        elif token.type == tokenize.NUMBER:
            replacement = _number_mutation(token.string) if constants else None
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


#: Decorators whose removal changes no result a test can see -- a cache, or a
#: wrapper's metadata: breaking them would be an equivalent mutant, a survivor
#: that blames the tests for nothing.
_EQUIVALENT_DECORATORS = frozenset({"cache", "lru_cache", "wraps"})


def _decorator_name(node: ast.expr) -> str:
    """``functools.lru_cache(maxsize=8)`` -> ``lru_cache``."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _statement_sites(source: str, edited: set, operator_rows: set,
                     skip: Tuple[type, ...] = ()) -> Tuple[Dict[int, List[Mutation]], List[int]]:
    """A site for every changed statement that has no operator site.

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

    Outside a census the same sites break the lines no operator reaches: on
    3.0.3 ``return a`` -> ``return b`` merged beside a tested line, unmutated.
    ``skip`` leaves out the statements whose deletion no test should be asked
    to notice -- a call on its own line, such as a log call, and an ``assert``;
    their lines come back as uncovered. Columns are characters (``ast`` counts
    UTF-8 bytes), so a line with an accent is broken where it should be.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}, sorted(edited)
    lines = source.split("\n")

    def where(node: ast.AST) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        return ((node.lineno, _char_col(lines, node.lineno, node.col_offset)),
                (node.end_lineno, _char_col(lines, node.end_lineno, node.end_col_offset)))

    sites: Dict[int, List[Mutation]] = {}
    uncovered: List[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # What a header carries that its body does not: decorators, default
            # values and base classes. They run when the module is imported,
            # so line coverage counts them as run with no test looking; until
            # 3.0.7 a changed one had no mutant at all (a decorator line was in
            # no statement's span, so it was not even listed as unmutated).
            parts: List[Tuple[ast.expr, str]] = [(d, "(lambda f: f)") for d in node.decorator_list
                                                 if _decorator_name(d) not in _EQUIVALENT_DECORATORS]
            if isinstance(node, ast.ClassDef):
                parts += [(b, "object") for b in node.bases]
            else:
                parts += [(d, "None") for d in [*node.args.defaults, *node.args.kw_defaults]
                          if d is not None and not (isinstance(d, ast.Constant) and d.value is None)]
            for expr, replacement in parts:
                if expr.lineno in edited and expr.lineno not in operator_rows:
                    sites.setdefault(expr.lineno, []).append((*where(expr), replacement))
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
        if isinstance(node, skip):
            uncovered.extend(sorted(span & edited))
            continue
        mutation: Optional[Mutation] = None
        if isinstance(node, (ast.If, ast.While)):
            text = ast.get_source_segment(source, node.test)
            if text is not None:
                mutation = (*where(node.test), f"not ({text})")
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            mutation = (*where(node.iter), "()")
        elif isinstance(node, ast.Return) and node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            mutation = (*where(node.value), "None")
        elif not isinstance(node, _COMPOUND) and not isinstance(node, ast.Return):
            mutation = (*where(node), "pass")
        if mutation is None:
            # A header whose decorator, default or base got a site above is broken.
            uncovered.extend(row for row in sorted(span & edited) if row not in sites)
        else:
            sites.setdefault(mutation[0][0], []).append(mutation)
    return sites, sorted(set(uncovered))


def _char_col(lines: List[str], row: int, byte_col: int) -> int:
    """``ast`` columns count UTF-8 bytes; :func:`_apply` splices characters."""
    return len(lines[row - 1].encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


def _branch_sites(source: str, edited: set) -> Dict[int, List[Mutation]]:
    """Sites that take away a branch line coverage cannot see.

    Coverage is measured per line, and a line counts as run when any part of
    it ran. A conditional expression, an ``and``/``or`` operand after a
    short-circuit, and an ``if`` or ``case`` whose body sits on the header's
    line can all carry a branch no test takes while the line reads as
    covered -- and the operator mutants of the guard send the tested input
    *into* that branch, so the tests kill them and the kills count as
    strength. On 3.0.3, ``return 999 if x == 0 else x + 1`` next to two honest
    refactors merged at 5/5 with ``value(0)`` changed from 1 to 999.

    These sites remove the branch the tests may not take: a conditional
    expression is replaced by each of its arms, an ``and``/``or`` loses its
    last operand, and a one-line ``if`` or ``case`` is switched off. Each
    survives exactly when no claim takes that branch. An ``if`` whose body has
    a line of its own needs none: coverage already sees the body. Inner
    constructs come first on their line -- in ``x == 0 and 999 or x + 1`` it
    is the inner ``and`` that hides the 999 -- and a site whose mutant would
    not parse is dropped.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    lines = source.split("\n")
    found: List[Tuple[int, Mutation]] = []

    def where(node: ast.AST) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        return ((node.lineno, _char_col(lines, node.lineno, node.col_offset)),
                (node.end_lineno, _char_col(lines, node.end_lineno, node.end_col_offset)))

    def text(first: ast.AST, last: ast.AST) -> str:
        (row, col), _ = where(first)
        _, (end_row, end_col) = where(last)
        if row == end_row:
            return lines[row - 1][col:end_col]
        return "\n".join([lines[row - 1][col:], *lines[row:end_row - 1], lines[end_row - 1][:end_col]])

    def add(depth: int, start: Tuple[int, int], end: Tuple[int, int], replacement: str) -> None:
        try:
            ast.parse("\n".join(_apply(lines, start, end, replacement)))
        except (SyntaxError, ValueError):
            return
        found.append((depth, (start, end, replacement)))

    def visit(node: ast.AST, depth: int) -> None:
        if isinstance(node, (ast.IfExp, ast.BoolOp)) and set(
            range(node.lineno, node.end_lineno + 1)
        ) & edited:
            start, end = where(node)
            if isinstance(node, ast.IfExp):
                for kept in (node.orelse, node.body):
                    add(depth, start, end, f"({text(kept, kept)})")
            else:
                add(depth, start, end, f"({text(node.values[0], node.values[-2])})")
        elif isinstance(node, ast.If) and node.body and node.body[0].lineno == node.test.end_lineno:
            if {node.lineno, node.test.end_lineno} & edited:
                add(depth, *where(node.test), "False")
        elif isinstance(node, ast.match_case) and node.body:
            header_end = (node.guard or node.pattern).end_lineno
            if node.body[0].lineno == header_end and {node.pattern.lineno, header_end} & edited:
                if node.guard is not None:
                    add(depth, *where(node.guard), "False")
                else:
                    _, end = where(node.pattern)
                    add(depth, end, end, " if False")
        for child in ast.iter_child_nodes(node):
            visit(child, depth + 1)

    visit(tree, 0)
    sites: Dict[int, List[Mutation]] = {}
    # Innermost first on each line; ties keep source order (the sort is stable).
    for _depth, site in sorted(found, key=lambda item: (item[1][0][0], -item[0])):
        sites.setdefault(site[0][0], []).append(site)
    return sites


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


def _plan(by_line: Mapping[Tuple[str, int], List[Mutation]], required: set,
          limit: int) -> Tuple[List[Tuple[str, Mutation]], int]:
    """``(plan, needed)``: every changed line broken once, then a sample.

    ``needed`` is what breaking every line once takes: each site in
    ``required`` (the branch sites, ``(rel, site)``) and the first other site
    of each line. Until 3.0.3 the plan was :func:`_spread` alone, capped at 12:
    on a change with more lines than that, the lines after the twelfth (in
    file order) were never broken, and nothing said so -- an attacker chooses
    that order. When ``needed`` passes ``limit`` the plan is empty and nothing
    runs: the change cannot be measured under that ceiling. Otherwise the
    remaining sites fill a round robin up to ``MIN_SAMPLE`` mutants in all.
    """
    first: List[Tuple[str, Mutation]] = []
    rest: Dict[Tuple[str, int], List[Mutation]] = {}
    for (rel, line), sites in sorted(by_line.items()):
        first += [(rel, site) for site in sites if (rel, site) in required]
        others = [site for site in sites if (rel, site) not in required]
        if others:
            first.append((rel, others[0]))
            if others[1:]:
                rest[(rel, line)] = others[1:]
    needed = len(first)
    if needed > limit:
        return [], needed
    return first + _spread(rest, min(limit, max(MIN_SAMPLE, needed)) - needed), needed


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
    oracle_dir: Optional[Path] = None,
    oracle_targets: Optional[Sequence[str]] = None,
    mutate_constants: bool = False,
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

    ``oracle_targets`` are claims the verdict judged with the baseline's copy of
    their test file, because the patch rewrote it; they run against every
    mutant in ``oracle_dir`` -- the patch's code with the baseline's tests put
    back -- and the other ``targets`` in the patch's own tree. Until 3.0.3 every
    claim ran in the patch's tree, so the asserts a patch added to a test it
    rewrote killed the mutants while the verdict never ran them: 6/6 -> MERGE
    for a claim whose baseline copy called none of the changed functions. With
    an oracle tree the mutants run one at a time.

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
        "reference_after": None,
        "stillborn_lines": [],
        "source_sensitive_claims": False,
        "canary": {"files": [], "exit_code": None},
        "added_files": {},
        "mutants_needed": 0,
        "unmutated_lines": [],
        "blind_spots": [],
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
    #: Statement-level sites. They break the lines no operator reaches, and
    #: prove a census covers the change; they never count toward a sample --
    #: deleting a statement is an easy kill, and counted it diluted the one
    #: operator mutant that looked like the real bug (more-itertools d71c4ad:
    #: 4/5 -> MERGE on a real bug). One that survives is a veto instead.
    coarse: set = set()
    #: Branch sites (:func:`_branch_sites`): real mutants, labelled apart.
    branchy: set = set()
    #: Changed files that are not UTF-8 on either side. They were skipped in
    #: silence, so their change was never mutated and the score of the other
    #: files stood for the whole patch: a ``# -*- coding: latin-1 -*-`` module
    #: changing ``>=`` to ``>`` merged next to a tested file (3.0.3).
    undecodable: List[str] = []
    removal_only: List[str] = []
    #: Sites on changed lines whose only mutant was the baseline again.
    rebuilt: List[str] = []
    for rel in changed:
        try:
            source = (patch_dir / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            undecodable.append(rel)
            continue
        except OSError:
            continue  # deleted by the patch: nothing left to mutate
        try:
            before = (Path(baseline_dir) / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            undecodable.append(rel)
            continue
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
            # Only lines removed: nothing the patch wrote is left to break. Said
            # in the artefact and the reason, not a veto (3.0.9).
            if before != source:
                removal_only.append(rel)
                detail["removal_only_files"] = sorted(removal_only)
            continue
        edited_rows[rel] = edited
        # Scan generously, then keep only sites on lines this patch wrote,
        # grouped by line.
        operator_rows = set()
        source_lines = source.split("\n")
        for site in _mutants_in(source, 10_000, constants=mutate_constants):
            # A mutant that rebuilds the baseline (the patch turned ``-`` into
            # ``+``; the mutant turns it back) proves only what the claim
            # already did -- the test tells old from new -- and says nothing of
            # how strong the test is. Not counted (3.0.11).
            # Only when this file is the whole patch: with another file
            # changed too, the mutant is not the baseline -- a claim failing
            # on the baseline because of b.py says nothing of a.py, and a
            # flipped ``>`` there went unbroken (3.0.12).
            if len(changed) == 1 and "\n".join(_apply(source_lines, *site)) == before:
                if site[0][0] in edited:
                    rebuilt.append(f"{rel}:{site[0][0]}")
                continue
            if site[0][0] in edited:
                by_line.setdefault((rel, site[0][0]), []).append(site)
                operator_rows.add(site[0][0])
        # A branch line coverage cannot see goes first on its line, and every
        # branch site runs (see :func:`_plan`): nothing else can tell (3.0.3).
        for row, sites in _branch_sites(source, edited).items():
            by_line[(rel, row)] = sites + by_line.get((rel, row), [])
            branchy.update((rel, site) for site in sites)
            operator_rows.add(row)
        # Every changed line gets broken: a line no operator reaches gets a
        # statement-level site. Outside a census, a call on its own line and an
        # ``assert`` are left out (see :func:`_statement_sites`).
        extra, missing = _statement_sites(source, edited, operator_rows,
                                          skip=() if census_min > 0 else (ast.Expr, ast.Assert))
        for row, sites in extra.items():
            by_line.setdefault((rel, row), []).extend(sites)
            coarse.update((rel, site) for site in sites)
        uncovered_lines.extend(f"{rel}:{n}" for n in missing)
    total_sites = sum(len(v) for v in by_line.values())
    plan, needed = _plan(by_line, branchy, max_mutants)
    detail["added_files"] = pairing
    detail["mutants_needed"] = needed
    detail["unmutated_lines"] = sorted(uncovered_lines)
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
    if undecodable:
        shown = ", ".join(sorted(undecodable)[:5])
        more = f" (+{len(undecodable) - 5} more)" if len(undecodable) > 5 else ""
        return finish(
            SuiteStrength(0, 0, 0.0, is_measured=False),
            f"changed file(s) that are not UTF-8 cannot be mutated, so their change was never "
            f"broken and nothing about the patch can be measured: {shown}{more}",
        )
    if needed > max_mutants:
        detail["census"] = {"exact": False, "sites": total_sites, "minimum": census_min,
                            "why_not": (f"budget cut the sample: {needed} mutants break every changed "
                                        f"line once, --mutation-max is {max_mutants}")
                            if census_min > 0 else "off (--census-min-mutants 0)"}
        return finish(
            SuiteStrength(0, 0, 0.0, is_measured=False),
            f"breaking every changed line once takes {needed} mutants, more than "
            f"--mutation-max ({max_mutants}): raise it to at least {needed}; under the "
            "ceiling, the lines past it would go unjudged",
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
        twin: Optional[Path] = None
        if oracle_dir is not None and oracle_targets:
            twin = Path(tmp) / "oracle"
            shutil.copytree(oracle_dir, twin, ignore=_IGNORE)
        # Every run starts from these. The tree is the one place a sandboxed
        # run can write that the next run reads: a claim that counted its runs
        # in a file there passed the reference and the canary and failed every
        # mutant -- 0/12 became 12/12 and MERGE (3.0.12).
        pristine = Path(tmp) / "pristine"
        shutil.copytree(work, pristine)
        pristine_twin: Optional[Path] = None
        if twin is not None:
            pristine_twin = Path(tmp) / "pristine-oracle"
            shutil.copytree(twin, pristine_twin)

        def _fresh(directory: Path) -> None:
            """Put ``directory`` (and the oracle twin) back as the patch left it."""
            pairs = [(directory, pristine)]
            if twin is not None and pristine_twin is not None:
                pairs.append((twin, pristine_twin))
            for tree, source in pairs:
                shutil.rmtree(tree)
                shutil.copytree(source, tree)

        def _run(directory: Path = work):
            if twin is None:
                return _run_in(directory, targets)
            # Each claim against the copy of its test file that judged it: a
            # failure on either side is the mutant noticed.
            runs = [_run_in(twin, list(oracle_targets or ()))]
            if targets:
                runs.insert(0, _run_in(directory, targets))
            failed = [r for r in runs if r.exit_code == PYTEST_TESTS_FAILED]
            odd = [r for r in runs if r.exit_code != PYTEST_OK]
            return (failed or odd or runs)[0]

        def _run_in(directory: Path, targets: Optional[Sequence[str]]):
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
        _fresh(work)
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
        trees = [work] if twin is None else [work, twin]
        if canary_files:
            _fresh(work)
            for rel, neutral in canary_files.items():
                for tree in trees:
                    (tree / rel).write_text(neutral, encoding="utf-8")
            try:
                probe = _run()
                if probe.timed_out or probe.exit_code < 0:
                    # Once more, but only for a run a limit stopped: a busy
                    # machine can cut one short. A claim that failed decides at
                    # once -- until 3.0.7 it was run again too, and a claim that
                    # failed only the first time it saw the copy passed (F).
                    probe = _run()
            finally:
                for rel in canary_files:
                    for tree in trees:
                        (tree / rel).write_text(originals[rel], encoding="utf-8")
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
                "kind": ("statement" if (rel, (start, end, replacement)) in coarse
                         else "branch" if (rel, (start, end, replacement)) in branchy else "operator"),
            }
            detail["mutants"].append(entry)  # type: ignore[union-attr]
            if mutant_source == original:
                entry["result"] = "no-op"
                continue
            jobs.append((entry, Path(rel), mutant_source, original))

        def _attempt(directory: Path, job) -> Any:
            """One run of one mutant in ``directory``: the outcome, or the exception."""
            _entry, rel, mutant_source, original = job
            # The oracle twin breaks with the tree it pairs with (one at a time).
            targets_ = [directory / rel] + ([twin / rel] if twin is not None else [])
            try:
                if twin is None:
                    shutil.rmtree(directory)
                    shutil.copytree(pristine, directory)
                else:
                    _fresh(directory)
                for target in targets_:
                    target.write_text(mutant_source, encoding="utf-8")
                return _run(directory)
            except Exception as exc:  # noqa: BLE001 - a broken harness is not suite evidence
                return exc
            finally:
                for target in targets_:
                    if target.exists():
                        target.write_text(original, encoding="utf-8")

        # (outcome, contended): contended runs had a neighbour.
        outcomes: List[Tuple[Any, bool]] = []
        workers = 1 if twin is not None else max(1, min(parallel, len(jobs)))
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

        # The unmutated claims once more, after every mutant. Outside a sandbox
        # a run can leave state where the tree reset does not reach (/tmp, the
        # home directory); claims that now fail were not judging the code.
        _fresh(work)
        after = _run()
        detail["reference_after"] = {"exit_code": after.exit_code}
        if after.exit_code != PYTEST_OK and not (after.timed_out or after.exit_code < 0):
            return finish(
                SuiteStrength(0, 0, 0.0, is_measured=False),
                f"the claims passed on the unmutated patch before the mutants and fail on it "
                f"after them (exit {after.exit_code}): they depend on what earlier runs left "
                "behind, not on the code, so their kills prove nothing",
            )

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
    # A branch or statement-level mutant that survived is not sampling noise:
    # it took a branch away from a line the patch wrote, or broke a line no
    # operator reaches, and no claim noticed -- while line coverage counted the
    # line as run. No number of other kills speaks for it (3.0.3: 5/5 -> MERGE
    # with ``value(0)`` changed; ``return a`` -> ``return b`` merged beside a
    # tested line). The ratio stays what the operator mutants measured -- these
    # never enter it, so they cannot pad it -- and it is not strong.
    hidden = [m for m in entries if m.get("kind") in ("branch", "statement")  # type: ignore[union-attr]
              and m.get("result") == "survived"]
    if hidden:
        detail["blind_spots"] = [f"{m['file']}:{m['line']} -> {m['replaces']}" for m in hidden]
        shown = ", ".join(detail["blind_spots"][:5])  # type: ignore[index]
        more = f" (+{len(hidden) - 5} more)" if len(hidden) > 5 else ""
        branch = any(m.get("kind") == "branch" for m in hidden)
        return finish(
            replace(result, blind_spots=len(hidden)),
            ("a branch on a line the patch wrote is taken by no claim" if branch else
             "a line the patch wrote is checked by no claim")
            + f": breaking it changed nothing ({shown}{more}). Line coverage counts that "
            "line as run, so no other mutant speaks for it",
        )
    # A line whose every mutant was stillborn was never judged: a mutant that
    # breaks the import (``HOSTS = ["a"] + ["0.0.0.0/0"]``, ``+`` -> ``-``)
    # stops collection, exit 2, and left the ratio with only a note -- the
    # other lines' kills merged a change no claim had looked at (3.0.12).
    by_row: Dict[str, List[object]] = {}
    for m in entries:
        if m.get("result") != "no-op":  # type: ignore[union-attr]
            by_row.setdefault(f"{m['file']}:{m['line']}", []).append(m.get("result"))  # type: ignore[index,union-attr]
    unborn = sorted(row for row, results in by_row.items()
                    if results and all(r == "stillborn" for r in results))
    if unborn:
        detail["stillborn_lines"] = unborn
        detail["blind_spots"] = [f"{row} -> every mutant stillborn" for row in unborn]
        shown = ", ".join(unborn[:5]) + (f" (+{len(unborn) - 5} more)" if len(unborn) > 5 else "")
        return finish(
            replace(result, blind_spots=len(unborn)),
            f"a line the patch wrote was never judged: every mutant of it was stillborn -- "
            f"it stopped the tests from running at all ({shown}) -- so no claim was tried "
            "against it",
        )
    if result.is_measured:
        notes = []
        if foreign:
            # The score just produced covers the Python files it mutated and
            # nothing else. Leaving ``reason`` blank let a 1.0 read as the
            # strength of the whole patch while part of the change was never
            # executed; say what the number is actually a number for.
            notes.append(
                f"score covers the {len(changed)} Python file(s) mutated only; "
                f"{len(foreign)} non-Python source file(s) in this patch were "
                "never judged"
            )
        if removal_only:
            notes.append(
                "the patch only removed lines from " + ", ".join(sorted(removal_only))
                + ": nothing there was mutated, so the score does not speak for those removals"
            )
        stillborn = [m for m in entries if m.get("result") == "stillborn"]  # type: ignore[union-attr]
        if stillborn:
            # Left out of the sample, as they must be -- but until 3.0.9 only the
            # artefact's count said so, and the lines they broke went unjudged.
            shown = ", ".join(f"{m['file']}:{m['line']}" for m in stillborn[:5])
            more = f" (+{len(stillborn) - 5} more)" if len(stillborn) > 5 else ""
            notes.append(
                f"{len(stillborn)} mutant(s) were stillborn -- the tests could not run "
                f"against them -- and are not in the score: {shown}{more}"
            )
        return finish(result, "; ".join(notes))
    codes = [m.get("exit_code") for m in detail["mutants"] if m.get("result") == "stillborn"]  # type: ignore[union-attr]
    if not codes:
        # Nothing was stillborn: no mutant could be counted. Until 3.0.9 this
        # fell through to "every mutant was stillborn (exit codes [])".
        if rebuilt:
            # The changed lines had operators, but breaking them only turned
            # the patch back into the baseline (``+`` back to ``-``), which the
            # claim already tells apart. "No operator site" sent the reader
            # looking for a missing operator (3.0.11).
            detail["baseline_mutants_skipped"] = sorted(set(rebuilt))
            return finish(
                result,
                "no mutant could be counted: the only operator mutants on the changed lines "
                "(" + ", ".join(sorted(set(rebuilt))[:5]) + ") turn the patch back into the "
                "baseline, which proves only what the claim already did; statement-level "
                "mutants never count toward a sampled score",
            )
        return finish(
            result,
            "no mutant could be counted: the changed lines hold no operator site, and "
            "statement-level mutants (killed or not) never count toward a sampled score",
        )
    if all(isinstance(c, int) and c < 0 for c in codes):
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
