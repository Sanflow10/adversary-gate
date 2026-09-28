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
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, List

DEFAULT_DENYLIST = [
    "conftest.py",
    "**/conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
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
                if any(part.is_symlink() for part in (root, *unresolved.parents)):
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
