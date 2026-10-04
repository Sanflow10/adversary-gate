"""Coverage of added lines, from coverage.py JSON output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from adversary_gate.verifiers.testpaths import is_test_path


class UnparseableDiff(ValueError):
    """The diff carries hunks but no file header this parser can attribute.

    Raised instead of returning "zero changed lines", because a ratio derived
    from an empty parse is *not* an empty measurement -- it is a measurement
    that never happened, and ``DiffCoverage(0, 0).ratio`` would report it as
    a perfect ``1.0``.
    """


@dataclass(frozen=True)
class DiffCoverage:
    """Covered added lines, over the added lines of **source** files only.

    Test files are left out of both sides of the ratio (AG-022). A test
    executes by construction, so counting its lines let a patch buy coverage
    with volume: ten unexecuted source lines plus forty executed test lines
    measured ``0.8`` and cleared the floor. What was left out is reported next
    to the ratio rather than dropped, so the number can be re-derived from the
    artefact.
    """

    changed_lines: int
    covered_lines: int
    excluded_test_lines: int = 0
    excluded_test_files: tuple[str, ...] = ()

    @property
    def ratio(self) -> float:
        return 1.0 if self.changed_lines == 0 else self.covered_lines / self.changed_lines


def _diff_target(header: str) -> str | None:
    """``+++ b/calc.py\\t2026-01-01`` -> ``calc.py``; ``/dev/null`` -> ``None``.

    Accepts both git's ``b/`` prefix and the bare ``+++ calc.py`` that plain
    ``diff -u`` emits. Only the git form used to be recognised, so a standard
    unified diff parsed to *zero* files -- and measured as perfect coverage,
    fail-open, in a gate whose whole thesis is fail-closed.
    """
    path = header.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith("b/"):
        path = path[2:]
    elif path.startswith("a/"):
        path = path[2:]
    return path or None


def changed_lines_from_unified_diff(diff_text: str) -> dict[str, set[int]]:
    """Added line numbers per file.

    A ``+++`` line is a file header only when the line before it was ``---``:
    that pairing is what the unified format guarantees, and it keeps an added
    line whose *content* starts with ``++`` from being mistaken for one.

    The ``---`` side is kept as a fallback because ``+++ /dev/null`` is how a
    deleted file is spelled: there is no new path to attribute the hunk to, and
    the only name that exists is the old one. Dropping it made every whole-file
    deletion parse to zero files and then be refused as unparseable (AG-017).
    """
    files: dict[str, set[int]] = {}
    current: str | None = None
    previous_path: str | None = None
    new_line = 0
    previous_was_removed_header = False
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            previous_was_removed_header = True
            previous_path = _diff_target(line[4:])
        elif line.startswith("+++ ") and previous_was_removed_header:
            previous_was_removed_header = False
            current = _diff_target(line[4:])
            if current is None:
                # A deletion: attribute the hunk to the file that was removed.
                current = previous_path
            if current is not None:
                files.setdefault(current, set())
        else:
            previous_was_removed_header = False
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                new_line = int(match.group(1))
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            files[current].add(new_line)
            new_line += 1
        elif current is not None and not line.startswith("-"):
            new_line += 1
    return files


def validate_diff(diff_text: str) -> dict[str, set[int]]:
    """Parse the diff, or refuse to.

    Hunks with no attributable file header means the parser and the input
    disagree about the format. Returning an empty mapping there would flow
    into ``DiffCoverage(0, 0)`` and come out as ``ratio == 1.0`` -- perfect
    coverage computed from nothing. Refusing turns a silent false pass into
    exit 3, which CI reads as "the input was bad", not as "the patch is clean".
    """
    files = changed_lines_from_unified_diff(diff_text)
    if not files and "@@" in diff_text:
        raise UnparseableDiff(
            "diff contains hunk headers but no '--- / +++' file pair this parser "
            "could attribute; re-generate it with 'git diff' or 'diff -u'"
        )
    return files


def covered_diff_ratio(diff_text: str, coverage_json: Path | Mapping) -> DiffCoverage:
    changed = validate_diff(diff_text)
    payload = (
        json.loads(Path(coverage_json).read_text())
        if isinstance(coverage_json, (str, Path))
        else coverage_json
    )
    files = payload.get("files", {})
    covered = 0
    total = 0
    excluded_lines = 0
    excluded_files: list[str] = []
    for filename, lines in changed.items():
        if is_test_path(filename):
            excluded_lines += len(lines)
            excluded_files.append(filename)
            continue
        total += len(lines)
        executed = set(files.get(filename, {}).get("executed_lines", []))
        covered += len(lines & executed)
    return DiffCoverage(total, covered, excluded_lines, tuple(sorted(excluded_files)))
