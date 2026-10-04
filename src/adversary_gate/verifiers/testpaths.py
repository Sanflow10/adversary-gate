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

import fnmatch
from pathlib import PurePosixPath
from typing import Iterable, Tuple

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


def is_test_path(rel: str) -> bool:
    """True for test modules and fixtures -- never mutation targets, never covered code."""
    posix = _normalize(rel)
    path = PurePosixPath(posix)
    name = path.name
    if any(fnmatch.fnmatch(posix, glob) for glob in _EXTRA):
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
