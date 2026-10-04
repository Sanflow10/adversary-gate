"""Path policy: what a patch is not allowed to touch.

Two fixes over the original:

1. **Symlink resolution.** The original checked nothing about symlinks; a
   link inside the repo pointing outside it would be validated as a normal
   relative path. Resolving first and checking afterwards does not work --
   ``resolve()`` dereferences, so the link is already gone by the time you
   look. The check runs on the *unresolved* path components.

2. **Violation severity is declared, not implied.** The original returned a
   list of strings and let the caller decide; ``gate.py`` fed it into a
   verdict whose ``accepted=False`` then approved the patch. Violations are
   now a named class so the caller cannot silently downgrade them.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, List

DEFAULT_DENYLIST = [
    "conftest.py",
    "**/conftest.py",
    "pytest.ini",
    "**/pytest.ini",
    ".pytest.ini",
    "**/.pytest.ini",
    "pytest.toml",
    "**/pytest.toml",
    ".pytest.toml",
    "**/.pytest.toml",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "sitecustomize.py",
    "**/sitecustomize.py",
    "usercustomize.py",
    "**/usercustomize.py",
    "*.pth",
    "**/*.pth",
    ".github/workflows/*",
    "**/fixtures/**",
    "**/*.env",
    "**/Dockerfile*",
    "**/docker-compose*.yml",
]


def normalize_path(path: str) -> str:
    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return str(PurePosixPath(value))


@dataclass
class PathViolation:
    path: str
    rule: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule}"


@dataclass
class PathPolicy:
    critic_test_paths: List[str] = field(default_factory=list)
    denylist_globs: List[str] = field(default_factory=lambda: list(DEFAULT_DENYLIST))

    #: A violation always means the patch cannot merge. Kept explicit so a
    #: future caller reading ``violations`` sees the consequence attached.
    blocks_merge: bool = True

    def violations(
        self, changed_paths: Iterable[str], repo_dir: Path | None = None
    ) -> List[PathViolation]:
        found: List[PathViolation] = []
        critic = {normalize_path(p) for p in self.critic_test_paths}
        root = repo_dir.resolve() if repo_dir is not None else None

        for raw_path in changed_paths:
            path = normalize_path(raw_path)
            if path.startswith("/") or ".." in PurePosixPath(path).parts:
                found.append(
                    PathViolation(raw_path, "path must be relative and cannot contain '..'")
                )
                continue

            if root is not None:
                unresolved = root / path
                # ``unresolved`` itself must be in the set: a *file* that is a
                # symlink is not covered by ``unresolved.parents``. Measured on
                # v2.0.1 -- changing to a symlink returned no violation at all,
                # with or without ``repo_dir``.
                if any(part.is_symlink() for part in (root, unresolved, *unresolved.parents)):
                    found.append(PathViolation(raw_path, "path traverses a symlink"))
                    continue

            if path in critic:
                found.append(
                    PathViolation(raw_path, "Builder may not modify a Critic test file")
                )
                continue

            for pattern in self.denylist_globs:
                normalized = normalize_path(pattern)
                if fnmatch.fnmatch(path, normalized) or fnmatch.fnmatch(
                    path, normalized.lstrip("**/")
                ):
                    found.append(
                        PathViolation(raw_path, f"matches test-infra denylist ({pattern})")
                    )
                    break
        return found


# ----------------------------------------------------------------------
# harness integrity: the tree, not the diff (AG-032)
# ----------------------------------------------------------------------
#: Files that configure how the test runner itself behaves, wherever they sit
#: in the tree. pytest reads its ini from the nearest ancestor of the test, so
#: a ``pytest.ini`` in a subdirectory is as much the harness as one at the root.
HARNESS_FILENAMES = frozenset(
    {
        # every name pytest 9 accepts as a config file
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
        # plugins pytest loads without being asked
        "conftest.py",
        # modules the interpreter imports at startup when they are importable
        "sitecustomize.py",
        "usercustomize.py",
    }
)

#: ``.pth`` files run arbitrary code at interpreter start when they land on a
#: site directory.
HARNESS_SUFFIXES = (".pth",)

#: Directories that are tooling state, not the project. A virtualenv inside
#: the checkout is recognised by its ``pyvenv.cfg`` instead of by name.
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", ".tox", ".nox", ".pytest_cache", ".mypy_cache"}
)


def _harness_files(root: Path) -> dict:
    found = {}
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(
            d for d in dirs if d not in _SKIP_DIRS and not (here / d / "pyvenv.cfg").is_file()
        )
        for name in files:
            if name in HARNESS_FILENAMES or name.endswith(HARNESS_SUFFIXES):
                path = here / name
                found[path.relative_to(root).as_posix()] = path
    return found


def harness_drift(baseline_dir: Path, patch_dir: Path) -> List[PathViolation]:
    """Test-harness files the patch added, removed or changed.

    The denylist above only sees the paths somebody *reports* (``--diff``,
    ``--changed-path``), and only the names somebody remembered to type.
    Measured on 2.3.0: a patch that broke ``add`` and added a ``.pytest.ini``
    with ``addopts = -p agplug`` -- a plugin that reports "passed" only while
    the source has exactly the broken bytes, so every mutant still died -- got
    ``MERGE``, exit 0, ``suite_strength: 1.0``. ``.pytest.ini`` was not on the
    list, and nothing compared the trees.

    This compares the trees themselves. Whoever writes the patch does not get
    to configure the runner that judges it: the configuration the claim runs
    under must be byte-for-byte the baseline's.
    """
    before = _harness_files(Path(baseline_dir))
    after = _harness_files(Path(patch_dir))
    found: List[PathViolation] = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            found.append(PathViolation(rel, "test-harness file added by the patch"))
        elif rel not in after:
            found.append(PathViolation(rel, "test-harness file removed by the patch"))
        else:
            try:
                same = before[rel].read_bytes() == after[rel].read_bytes()
            except OSError:
                same = False  # unreadable is not "unchanged"
            if not same:
                found.append(PathViolation(rel, "test-harness file changed by the patch"))
    return found
