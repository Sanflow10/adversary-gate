# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

"""What counts as a test file, decided in one place.

Two verifiers need the answer and used to disagree about who owned it:
``strength`` needs it to keep tests out of the mutation targets, and
``coverage`` needs it to keep tests out of the diff-coverage denominator. A
test file is not code under test in either question, so both read this. The
baseline oracle reads it too: every baseline file that is a test file is put
back before the patch's code is judged.

The built-in rules are only the conventions that are never product code. A
helper with an ordinary name -- ``testing_utils.py`` at the root, or a
``testing`` package -- cannot be told apart from code under test by its path:
``numpy.testing`` is public API. Guessing either way is a hole (a helper read
as source is taken from the patch; source read as a helper is taken from the
baseline and never judged), so those are *declared*, with ``--test-support``.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Tuple

#: Declared by the caller (``--test-support``). Module state because the CLI
#: is one decision per process; ``declared_test_support`` scopes it for tests.
_EXTRA: Tuple[str, ...] = ()

#: Suffixes that only ever name tests, in the languages ``--test-command``
#: runs. None of them is a Python module, so none changes what coverage or
#: mutation measure for Python.
_FOREIGN_TEST_SUFFIXES = (
    "_test.go",
    "Test.java",
    "Tests.java",
    "Test.kt",
    "Tests.kt",
    "_spec.rb",
    "_test.rb",
)


def _normalize(rel: str) -> str:
    posix = str(rel).replace("\\", "/")
    while posix.startswith("./"):
        posix = posix[2:]
    return posix


def set_test_support(globs: Iterable[str]) -> None:
    """Declare extra test-support paths.

    ``fnmatch`` globs against the repo-relative path, where ``*`` also crosses
    ``/``: ``testing_utils.py``, ``*/testing/*``, ``helpers/*``.
    """
    global _EXTRA
    _EXTRA = tuple(_normalize(g) for g in globs if str(g).strip())


def declared_globs() -> Tuple[str, ...]:
    return _EXTRA


class declared_test_support:  # noqa: N801 - used as a context manager
    def __init__(self, globs: Iterable[str]) -> None:
        self.globs = tuple(globs)

    def __enter__(self):
        self.previous = _EXTRA
        set_test_support(self.globs)
        return self

    def __exit__(self, *exc) -> None:
        global _EXTRA
        _EXTRA = self.previous


def _declared(posix: str) -> bool:
    return any(fnmatch.fnmatch(posix, glob) for glob in _EXTRA)


def is_test_path(rel: str) -> bool:
    """True for test modules and fixtures, by path alone.

    The name-only answer. Where the tree is on hand, :func:`is_test_file_in`
    refines it for Python modules: one that wears a test's name but is imported
    by the code under test is product code, and judging it as a test would hide
    a change to it.
    """
    posix = _normalize(rel)
    path = PurePosixPath(posix)
    name = path.name
    if _declared(posix):
        return True
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "tests" in path.parts[:-1]
        or "__tests__" in path.parts[:-1]
        or ".test." in name
        or ".spec." in name
        or name.endswith(_FOREIGN_TEST_SUFFIXES)
    )


def _overridable_test(rel: str) -> bool:
    """A test-pathed Python module whose classification the imports may overturn.

    Never ``conftest.py`` (pytest's own) and never a ``--test-support``
    declaration (the operator's word): only a module that is a test by the weak
    path conventions alone, which product code could wear on purpose.
    """
    posix = _normalize(rel)
    if PurePosixPath(posix).name == "conftest.py" or _declared(posix) or not posix.endswith(".py"):
        return False
    return is_test_path(posix)


def _module_path(rel: str) -> str:
    """The module a file is imported as, as a ``/`` path with no ``.py``."""
    if rel.endswith("/__init__.py"):
        return rel[: -len("/__init__.py")]
    return rel[:-3]


def production_imported_tests(root: Optional[Path]) -> "frozenset[str]":
    """Test-pathed Python modules a non-test module imports -- product in disguise.

    A module under a ``tests`` directory, or named ``test_*``/``*_test.py``, that
    the code under test imports runs in production: a change to it changes
    behaviour, so it must be measured, not reverted to the baseline. A test
    *helper*, imported only by other tests, is not here and stays a test;
    ``conftest.py`` and declared support are never reclassified. Read from the
    AST, so a file that will not parse is left as its name says.
    """
    if root is None:
        return frozenset()
    base = Path(root)
    if not base.is_dir():
        return frozenset()
    rels = [
        p.relative_to(base).as_posix()
        for p in base.rglob("*.py")
        if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
    ]
    candidates = {rel: _module_path(rel) for rel in rels if _overridable_test(rel)}
    non_tests = [rel for rel in rels if not is_test_path(rel)]
    if not candidates or not non_tests:
        return frozenset()

    imported: set = set()

    def record(dotted: str) -> None:
        dotted = dotted.strip("/")
        if not dotted:
            return
        for rel, mod in candidates.items():
            if rel in imported:
                continue
            if mod == dotted or (
                "/" in dotted and (mod.endswith("/" + dotted) or dotted.endswith("/" + mod))
            ):
                imported.add(rel)

    for rel in non_tests:
        try:
            tree = ast.parse((base / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            continue
        pkg = rel.split("/")[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    record(alias.name.replace(".", "/"))
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").replace(".", "/")
                if node.level:
                    up = node.level - 1
                    base_parts = pkg[: len(pkg) - up] if up <= len(pkg) else []
                    prefix = "/".join([*base_parts, mod]) if mod else "/".join(base_parts)
                else:
                    prefix = mod
                record(prefix)
                for alias in node.names:
                    record("/".join([prefix, alias.name]) if prefix else alias.name)
        if len(imported) == len(candidates):
            break
    return frozenset(imported)


def is_test_rel(rel: str, production_imported: "frozenset[str]") -> bool:
    """Test file? ``production_imported`` from :func:`production_imported_tests`."""
    return is_test_path(rel) and _normalize(rel) not in production_imported


def is_test_file_in(rel: str, root: Optional[Path]) -> bool:
    """:func:`is_test_path`, overturned when the tree shows a disguised module.

    A test-pathed Python module that the code under test imports is source;
    every other file keeps its name-based answer. ``root is None`` is the
    name-based answer, so a caller without the tree loses nothing. Walking many
    files? Compute :func:`production_imported_tests` once and use
    :func:`is_test_rel`, so the tree is scanned once, not per file.
    """
    if not is_test_path(rel):
        return False
    if not _overridable_test(_normalize(rel)):
        return True
    return is_test_rel(rel, production_imported_tests(root))
