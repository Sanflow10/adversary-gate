"""What counts as a test file, decided in one place.

Two verifiers need the answer and used to disagree about who owned it:
``strength`` needs it to keep tests out of the mutation targets, and
``coverage`` needs it to keep tests out of the diff-coverage denominator. A
test file is not code under test in either question, so both read this.
"""

from __future__ import annotations

from pathlib import Path


def is_test_path(rel: str) -> bool:
    """True for test modules and fixtures -- never mutation targets, never covered code."""
    path = Path(rel)
    return (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or path.name == "conftest.py"
        or "tests" in path.parts[:-1]
    )
