"""Map pytest exit codes to :class:`ExecState`.

These values were **measured** against pytest 9.0.3 on this machine, not
taken from memory::

    0 -> tests passed
    1 -> at least one test failed      <- the ONLY code that is evidence
    2 -> collection error (import blew up)
    3 -> internal error (plugin crash; observed: RLIMIT_NPROC thread denial)
    4 -> usage error (missing file or nonexistent ``::node id``)
    5 -> no tests collected
   -1 -> sandbox timeout, set by ``runner.py``

The old gate used ``exit_code != 0`` as "test failed". That collapsed six
distinct situations into one boolean and produced a matrix where

* a collection error in the patch alone  -> reported as REGRESSION
* the same error on both sides           -> reported as INVALID -> approved

i.e. the harness bug was indistinguishable from a real bug in one direction
and from a clean patch in the other. Only ``0``, ``1`` and ``-1`` mean the
requested test actually ran.
"""

from __future__ import annotations

from core.types import ExecState

PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1
SANDBOX_TIMEOUT = -1

#: Exit codes that mean "the execution did not happen".
UNRUNNABLE_CODES: frozenset[int] = frozenset({2, 3, 4, 5})


def classify_exit(exit_code: int) -> ExecState:
    """Translate one pytest exit code into an execution state."""
    if exit_code == PYTEST_OK:
        return ExecState.PASS
    if exit_code == PYTEST_TESTS_FAILED:
        return ExecState.FAIL
    if exit_code == SANDBOX_TIMEOUT:
        return ExecState.TIMED_OUT
    return ExecState.UNRUNNABLE


def is_unrunnable(exit_code: int) -> bool:
    return exit_code in UNRUNNABLE_CODES


def describe(exit_code: int) -> str:
    """Human-readable label, used in verdict reasons so logs stay legible."""
    return {
        0: "tests passed",
        1: "tests failed",
        -1: "timed out",
        2: "collection error (import failed)",
        3: "internal error (harness crashed)",
        4: "usage error (missing file or node id)",
        5: "no tests collected",
    }.get(exit_code, f"unexpected exit code {exit_code}")
