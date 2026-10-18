# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

"""Coverage of added lines, from coverage.py JSON output."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from adversary_gate.verifiers.testpaths import is_test_rel, production_imported_tests


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
    #: Documents whose changed lines are left out of the ratio (E): they cannot run.
    excluded_doc_files: tuple[str, ...] = ()
    #: True when the patch changes files but no source line: there is nothing to
    #: cover, so the ratio is not a measurement. It was reported as 1.0 before,
    #: and a patch that only edits a document then passed a claim that reads it.
    unmeasured: bool = False

    @property
    def ratio(self) -> float | None:
        if self.unmeasured:
            return None
        return 1.0 if self.changed_lines == 0 else self.covered_lines / self.changed_lines


_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(body: str) -> str:
    """Git's C-style quoting undone: ``caf\\303\\251.py`` -> ``café.py``.

    Git quotes a path that holds a byte outside ASCII, a quote, a backslash or
    a control character. Until 3.0.11 the quoted spelling was the key, so a
    path check missed ``"b/.github/workflows/\\303\\251.yml"`` and a source
    named ``café.py`` lost its coverage.
    """
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567" and re.fullmatch(r"[0-7]{3}", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _diff_target(header: str) -> str | None:
    """``+++ b/calc.py\\t2026-01-01`` -> ``calc.py``; ``/dev/null`` -> ``None``.

    Accepts both git's ``b/`` prefix and the bare ``+++ calc.py`` that plain
    ``diff -u`` emits. Only the git form used to be recognised, so a standard
    unified diff parsed to *zero* files -- and measured as perfect coverage,
    fail-open, in a gate whose whole thesis is fail-closed.
    """
    path = header.split("\t", 1)[0].strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = _unquote(path[1:-1])
    if path == "/dev/null":
        return None
    if path.startswith("b/"):
        path = path[2:]
    elif path.startswith("a/"):
        path = path[2:]
    return path or None


_HUNK = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+(?P<start>\d+)(?:,(?P<new>\d+))? @@")


def _diff_lines(diff_text: str) -> list[str]:
    """The diff's lines, split on ``\\n`` only.

    ``str.splitlines`` also breaks on ``\\x0c``, ``\\x1c``-``\\x1e``, ``\\x85``
    and ``\\u2028``/``\\u2029``, which end a line neither in a diff nor in
    Python: one such character in a changed line moved every later line of
    its hunk one row down (3.0.7).
    """
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _walk_hunks(diff_text: str):
    """``(kind, line, row)`` for each line: ``header``, ``added``, ``removed``, ``context``, ``other``.

    Inside a hunk its header's counts decide what a line is, never how it
    starts: an added line whose text begins with ``++`` is spelled ``+++ y``,
    and a removed ``-- x`` then an added ``++ y`` look exactly like a file
    header. Until 3.0.7 both were read as headers, which dropped the line and
    moved, or misattributed, every changed line after it. ``row`` is the
    new-side line number of an added or context line.
    """
    old_left = new_left = 0
    row = 0
    for line in _diff_lines(diff_text):
        if old_left > 0 or new_left > 0:
            if line.startswith("\\"):
                yield "other", line, row  # "\ No newline at end of file"
                continue
            if line.startswith("+"):
                new_left -= 1
                yield "added", line, row
                row += 1
                continue
            if line.startswith("-"):
                old_left -= 1
                yield "removed", line, row
                continue
            if line and not line.startswith(" "):
                # A hunk that declares more lines than it holds: what follows
                # is the next file's header. Read as context, it swallowed that
                # file, whose lines then counted as this one's (3.0.11).
                raise UnparseableDiff(
                    f"a hunk ends before the {old_left} old and {new_left} new lines its "
                    f"header declares (at {line[:60]!r}); re-generate the diff")
            old_left -= 1
            new_left -= 1
            yield "context", line, row
            row += 1
            continue
        match = _HUNK.match(line)
        if match:
            old_left = int(match.group("old") if match.group("old") is not None else 1)
            new_left = int(match.group("new") if match.group("new") is not None else 1)
            row = int(match.group("start"))
            yield "hunk", line, row
            continue
        yield "header", line, row
    if old_left > 0 or new_left > 0:
        raise UnparseableDiff(
            f"the diff ends {old_left} old and {new_left} new lines short of what its "
            "last hunk header declares; re-generate the diff")


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
    previous_was_removed_header = False
    # "\ No newline at end of file" describes the line above it; it is not a
    # line of either file, and :func:`_walk_hunks` counts it as neither.
    for kind, line, row in _walk_hunks(diff_text):
        if kind == "header":
            if line.startswith("--- "):
                previous_was_removed_header = True
                previous_path = _diff_target(line[4:])
                continue
            if line.startswith("+++ ") and previous_was_removed_header:
                current = _diff_target(line[4:])
                if current is None:
                    # A deletion: attribute the hunk to the file that was removed.
                    current = previous_path
                if current is not None:
                    files.setdefault(current, set())
            previous_was_removed_header = False
            continue
        previous_was_removed_header = False
        if kind == "added" and current is not None:
            files[current].add(row)
    return files


_BINARY_DIFF = re.compile(r"^Binary files (?P<old>.+?) and (?P<new>.+) differ$")


def binary_python_diffs(diff_text: str) -> list[str]:
    """Python files whose diff is ``Binary files ... differ`` instead of hunks.

    ``git diff`` writes that for a file that .gitattributes marks ``-diff`` or
    ``binary``. The file's changed lines are then not in the diff at all, so
    coverage cannot see them and would judge the rest of the patch as if the
    file had not changed (candidate C, reproduced in 3.0.0 with a claim that
    passed while a change in ``calc.py`` was never checked).
    """
    found: list[str] = []
    for kind, line, _ in _walk_hunks(diff_text):
        if kind != "header":
            continue
        match = _BINARY_DIFF.match(line)
        if not match:
            continue
        side = match.group("new") if match.group("new") != "/dev/null" else match.group("old")
        path = _diff_target(side) or ""
        if path.endswith(".py"):
            found.append(path)
    return found


def hidden_text_diffs(diff_text: str, patch_root: Path) -> list[str]:
    """Files the diff calls binary whose patch-side content is text.

    ``-diff`` or ``binary`` in .gitattributes -- which the patch may write --
    turns any file's hunks into ``Binary files ... differ``. For a ``.py`` file
    :func:`binary_python_diffs` refuses it; a ``settings.json`` hid its changed
    lines from a ratio that counts config files (3.0.11). Git's own test is
    used: binary means a NUL in the first 8000 bytes.
    """
    found: list[str] = []
    for kind, line, _ in _walk_hunks(diff_text):
        if kind != "header":
            continue
        match = _BINARY_DIFF.match(line)
        if not match or match.group("new") == "/dev/null":
            continue
        path = _diff_target(match.group("new")) or ""
        target = Path(patch_root) / path
        try:
            if path and target.is_file() and not target.is_symlink() \
                    and b"\0" not in target.read_bytes()[:8000]:
                found.append(path)
        except OSError:
            continue
    return found


def validate_diff(diff_text: str) -> dict[str, set[int]]:
    """Parse the diff, or refuse to.

    Hunks with no attributable file header means the parser and the input
    disagree about the format. Returning an empty mapping there would flow
    into ``DiffCoverage(0, 0)`` and come out as ``ratio == 1.0`` -- perfect
    coverage computed from nothing. Refusing turns a silent false pass into
    exit 3, which CI reads as "the input was bad", not as "the patch is clean".

    The same goes for a Python file the diff shows only as "Binary files
    differ": its changes are missing, so refusing is the only honest answer.
    Regenerate the diff with ``git diff --text``.
    """
    files = changed_lines_from_unified_diff(diff_text)
    if not files and "@@" in diff_text:
        raise UnparseableDiff(
            "diff contains hunk headers but no '--- / +++' file pair this parser "
            "could attribute; re-generate it with 'git diff' or 'diff -u'"
        )
    hidden = binary_python_diffs(diff_text)
    if hidden:
        raise UnparseableDiff(
            "git wrote 'Binary files ... differ' for " + ", ".join(hidden) + ": their changed "
            "lines are not in the diff, so their coverage cannot be judged. This happens when "
            ".gitattributes marks the file -diff or binary; re-generate the diff with "
            "'git diff --text'"
        )
    return files


def _non_code_added_lines(diff_text: str) -> dict:
    """Added lines of ``.py`` files that are blank or comment-only, by path (AG-040).

    Judged from the line's own text in the diff, so only what can never be
    executed is removed: a docstring or a continuation line still counts,
    which can only make the ratio lower, never higher.
    """
    out: dict = {}
    path: str | None = None
    for kind, line, row in _walk_hunks(diff_text):
        if kind == "header":
            if line.startswith("+++ "):
                path = _diff_target(line[4:])
            continue
        if kind != "added" or path is None or not path.endswith(".py"):
            continue
        text = line[1:].strip()
        if not text or text.startswith("#"):
            out.setdefault(path, set()).add(row)
    return out


#: Documents cannot run, so their changed lines cannot be covered (E). The list
#: is deliberately narrow: a configuration, a requirements file or a workflow can
#: change what the code does, so it stays in the ratio.
DOCUMENT_SUFFIXES = (".md", ".markdown", ".rst", ".adoc", ".org", ".tex")
DOCUMENT_NAMES = frozenset({
    "readme", "license", "licence", "notice", "copying", "changelog", "changes",
    "authors", "contributors", "code_of_conduct", "security",
})


def is_documentation(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(DOCUMENT_SUFFIXES):
        return True
    stem, dot, suffix = name.rpartition(".")
    if not dot:  # no suffix: LICENSE, NOTICE, CHANGELOG
        return name in DOCUMENT_NAMES
    return suffix == "txt" and stem in DOCUMENT_NAMES


#: Bigger sources are not parsed: their lines are judged one by one, as before.
_SOURCE_LIMIT = 4 * 1024 * 1024


def _statement_starts(source_root: Path | None, filename: str) -> dict[int, int] | None:
    """Line -> first line of the innermost statement holding it, in the patch's file.

    coverage.py reports a statement on its first line only, so the second line of
    a statement is never in ``executed_lines`` (E). An ``except`` clause and a
    ``case`` pattern start their own entry: they run apart from what holds them.
    ``None`` -- each line judged on its own -- when there is no source, the path
    leaves ``source_root`` or crosses a symlink, or the file does not parse.
    """
    if source_root is None or not filename.endswith(".py"):
        return None
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = Path(source_root)
    try:
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                return None
        if not path.is_file() or path.stat().st_size > _SOURCE_LIMIT:
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    starts: dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.stmt, ast.excepthandler)):
            first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))])
            last = node.end_lineno or node.lineno
        elif isinstance(node, ast.match_case):
            first = node.pattern.lineno
            last = max(node.pattern.end_lineno or first,
                       (node.guard.end_lineno or first) if node.guard else first)
        else:
            continue
        for row in range(first, last + 1):
            # What is nested starts on or after the line its parent starts on,
            # so the latest start is the innermost entry.
            if starts.get(row, 0) < first:
                starts[row] = first
    return starts


def _judge(diff_text: str, coverage_json: Path | Mapping, source_root: Path | None):
    """The changed source lines of each file, and which of them ran."""
    changed = validate_diff(diff_text)
    payload = (
        json.loads(Path(coverage_json).read_text())
        if isinstance(coverage_json, (str, Path))
        else coverage_json
    )
    files = payload.get("files", {})
    judged: dict[str, tuple[set[int], set[int]]] = {}
    excluded_lines = 0
    excluded_files: list[str] = []
    excluded_docs: list[str] = []
    non_code = _non_code_added_lines(diff_text)
    # A module that wears a test's name but the code under test imports is source:
    # it stays in the denominator so an untested change to it is counted.
    disguised = production_imported_tests(source_root)
    for filename, lines in changed.items():
        if is_test_rel(filename, disguised):
            excluded_lines += len(lines)
            excluded_files.append(filename)
            continue
        if is_documentation(filename):
            excluded_docs.append(filename)
            continue
        lines = set(lines) - non_code.get(filename, set())
        executed = set((files.get(filename) or {}).get("executed_lines", []))
        # Every changed line counts. One coverage.py does not report on its own --
        # the second line of a statement -- is covered when its statement ran (E).
        # Dropping it instead let a statement that never ran change for free: a
        # patch that broke ``mul`` there went from INCONCLUSIVE to MERGE. A
        # docstring line counts and is never covered, as before.
        starts = _statement_starts(source_root, filename) or {}
        judged[filename] = (lines, {row for row in lines
                                    if row in executed or starts.get(row, row) in executed})
    return changed, judged, excluded_lines, excluded_files, excluded_docs


def uncovered_lines(diff_text: str, coverage_json: Path | Mapping, *,
                    source_root: Path | None = None) -> dict[str, list[int]]:
    """The changed source lines the ratio counts against the patch, by file."""
    _, judged, *_ = _judge(diff_text, coverage_json, source_root)
    return {name: sorted(lines - covered) for name, (lines, covered) in sorted(judged.items())
            if lines - covered}


def covered_diff_ratio(diff_text: str, coverage_json: Path | Mapping, *,
                       source_root: Path | None = None) -> DiffCoverage:
    """``source_root`` is the patch's tree, read to judge a statement's second line (E)."""
    changed, judged, excluded_lines, excluded_files, excluded_docs = _judge(
        diff_text, coverage_json, source_root
    )
    total = sum(len(lines) for lines, _ in judged.values())
    covered = sum(len(ran) for _, ran in judged.values())
    # A patch that changes documents or configuration and no source line has
    # nothing to cover. Its ratio is not a measurement: a claim can read those
    # files, and in 3.0.0 a patch that only edited the README merged on a claim
    # that read it. The gate's own list of non-source files decides what counts.
    # Tests are left out: a patch of tests alone keeps its vacuous 1.0 (AG-022),
    # since a changed test is judged by the baseline's copy and a new conftest.py
    # is a protected path.
    from adversary_gate.verifiers.strength import _is_non_source  # noqa: PLC0415 -- the one list

    disguised = production_imported_tests(source_root)
    unmeasured = total == 0 and any(
        _is_non_source(name) and not is_test_rel(name, disguised) for name in changed
    )
    return DiffCoverage(
        total,
        covered,
        excluded_lines,
        tuple(sorted(excluded_files)),
        tuple(sorted(excluded_docs)),
        unmeasured,
    )
