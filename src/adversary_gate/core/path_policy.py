# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

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
import sys
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
    # A directory with one is a virtualenv, and the harness check skips it as
    # tooling state; a patch that writes one hides every harness file beside
    # it (G, 3.0.7). A virtualenv CI creates is not in the diff, so it is not
    # caught here.
    "pyvenv.cfg",
    "**/pyvenv.cfg",
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
    found.extend(_shadowing_drift(Path(baseline_dir), Path(patch_dir)))
    return found


#: Modules the runner itself imports, beyond the standard library.
_RUNNER_MODULES = frozenset({
    "pytest", "_pytest", "pluggy", "iniconfig", "packaging", "py", "coverage",
    "exceptiongroup", "tomli", "pygments", "colorama",
})
_SHADOW_SUFFIXES = (".py", ".pyc", ".so", ".pyd")


def _is_collected(name: str) -> bool:
    """A file pytest imports from where it lies: a test module or a conftest."""
    return name == "conftest.py" or (
        name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _import_roots(root: Path) -> set:
    """Directories pytest's default import mode puts on ``sys.path``, relative.

    The root (``python -m pytest`` runs there), and for every test module and
    conftest the first directory above it that is not a package -- its
    ``basedir``. Measured on pytest 9.1.1: a module beside any of these was
    imported in place of a standard-library one of that name.
    """
    dirs = {""}
    for current, subdirs, files in os.walk(root):
        here = Path(current)
        subdirs[:] = [d for d in subdirs
                      if d not in _SKIP_DIRS and not (here / d / "pyvenv.cfg").is_file()]
        if not any(_is_collected(name) for name in files):
            continue
        base = here
        while base != root and (base / "__init__.py").is_file():
            base = base.parent
        dirs.add("" if base == root else base.relative_to(root).as_posix())
    return dirs


def _plain_directories(root: Path) -> set:
    """Every directory under ``root`` that is not a package, relative; the root is ``""``.

    Any of them can be put first on ``sys.path``: the root by ``python -m``,
    the basedirs by pytest (:func:`_import_roots`), ``src/`` by a ``pythonpath``
    setting or a test's ``sys.path.insert`` -- this repository's own tests do
    that. A package's directory is reached through its package name, so a
    module inside it shadows nothing. Virtualenvs and tool state are skipped.
    """
    dirs = {""}
    for current, subdirs, _files in os.walk(root):
        here = Path(current)
        subdirs[:] = [d for d in subdirs
                      if d not in _SKIP_DIRS and not (here / d / "pyvenv.cfg").is_file()]
        if here != root and not (here / "__init__.py").is_file():
            dirs.add(here.relative_to(root).as_posix())
    return dirs


def _shadowing(root: Path, dirs: set) -> dict:
    """Entries in ``dirs`` an import of a standard or runner module would find first.

    A module file, or a *package* directory: one without ``__init__.py`` is a
    namespace portion, and a namespace never wins over a real module.
    """
    names = set(getattr(sys, "stdlib_module_names", ())) | _RUNNER_MODULES
    found = {}
    for rel_dir in dirs:
        try:
            entries = list((root / rel_dir).iterdir())
        except OSError:
            continue
        for entry in entries:
            stem = entry.name.split(".", 1)[0]
            if stem not in names:
                continue
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_dir():
                if entry.name == stem and (entry / "__init__.py").is_file():
                    found[rel] = entry
            elif entry.name.endswith(_SHADOW_SUFFIXES):
                found[rel] = entry
    return found


def _shadowing_drift(baseline_dir: Path, patch_dir: Path) -> List[PathViolation]:
    """AG-048: a module on pytest's path that would be imported in place of a real one.

    ``python -m pytest`` puts the working directory -- the tree under test --
    first on ``sys.path``, pytest adds the directory of every test module and
    conftest that is not a package (:func:`_import_roots`), and a config or a
    test can add more; so every directory that is not a package is checked
    (:func:`_plain_directories`). An ``inspect.py`` or a ``pytest/`` package the
    patch adds there runs instead of the real module, on the patch side only: it can
    report a test as passed as surely as a ``pytest.ini`` plugin could. A
    directory the baseline already had keeps its contents free (``test/`` is a
    standard-library name and a common test directory); a file is compared.
    """
    dirs = _plain_directories(baseline_dir) | _plain_directories(patch_dir)
    before = _shadowing(baseline_dir, dirs)
    after = _shadowing(patch_dir, dirs)
    found: List[PathViolation] = []
    for name in sorted(after):
        why = "would be imported in place of the module of that name"
        if name not in before:
            found.append(PathViolation(name, f"added by the patch on pytest's import path: {why}"))
        elif after[name].is_file():
            try:
                same = before[name].read_bytes() == after[name].read_bytes()
            except OSError:
                same = False
            if not same:
                found.append(PathViolation(name, f"changed by the patch on pytest's import path: {why}"))
    return found
