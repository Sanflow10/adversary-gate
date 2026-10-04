"""Which tests exercise the lines a patch changed -- read from coverage, not guessed.

AG-031: a run used to verify exactly one claim, named by hand. In a workflow
file that meant one fixed test for every pull request, which says nothing about
a patch that never touches it. Coverage already knows better: run with
``dynamic_context = test_function`` and ``coverage json --show-contexts``, it
records, for every executed line, which test executed it. The tests that
executed a changed source line are the tests whose verdict is about this patch.

Three rules keep the list honest:

* **Only source lines count.** A test always executes its own lines; a changed
  test file proves nothing about the code under test (the same reason AG-022
  keeps tests out of the coverage ratio).
* **A test file the patch rewrote is not an oracle.** Its verdict would be the
  patch grading itself (AG-021). Those files are left out and named in the
  detail, so the exclusion is visible rather than silent.
* **No contexts is a usage error, not "no tests".** A report recorded without
  contexts cannot answer the question; reading it as "nothing covers this"
  would turn a missing measurement into a decision.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from adversary_gate.verifiers.coverage import validate_diff
from adversary_gate.verifiers.testpaths import is_test_path

#: Directories that never hold the tests under judgement.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".venv", "venv", "node_modules", "build",
    "dist", ".eggs",
})

#: pytest-cov writes ``path::test|run``; the phase suffix is not part of the id.
_PHASE = re.compile(r"\|(setup|run|teardown)$")
#: ``test_x[1-2]`` -> ``test_x``: parameters are re-run whole by node id.
_PARAMS = re.compile(r"\[.*\]$")


class NoTestContexts(ValueError):
    """The coverage report carries no per-test contexts, so it cannot say who ran what."""


def _test_files(root: Path) -> List[str]:
    found: List[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1]):
            continue
        posix = rel.as_posix()
        if path.is_file() and is_test_path(posix):
            found.append(posix)
    return found


def _resolve_dotted(context: str, test_files: Iterable[str]) -> Tuple[Optional[str], str]:
    """``test_calc.TestSub.test_sub`` -> ``(tests/test_calc.py, TestSub::test_sub)``.

    coverage.py names a test by its dotted module path, which depends on how
    pytest imported it (``test_calc`` from a rootdir insert, ``tests.test_calc``
    from a package). The longest module prefix that matches exactly one test
    file wins. ``None`` with a reason when no prefix, or more than one file,
    matches.
    """
    parts = context.split(".")
    files = list(test_files)
    for split in range(len(parts) - 1, 0, -1):
        suffix = "/".join(parts[:split]) + ".py"
        matches = [f for f in files if f == suffix or f.endswith("/" + suffix)]
        if len(matches) == 1:
            return matches[0], "::".join(parts[split:])
        if len(matches) > 1:
            return None, f"ambiguous: {len(matches)} files end in {suffix}"
    return None, "no test file matches"


def _node_id(context: str, test_files: Iterable[str]) -> Tuple[Optional[Tuple[str, str]], str]:
    """A coverage context as ``(test_path, test_id)``, or ``None`` and why not."""
    context = _PHASE.sub("", context.strip())
    if "::" in context:
        path, _, test_id = context.partition("::")
    else:
        resolved, rest = _resolve_dotted(context, test_files)
        if resolved is None:
            return None, rest  # ``rest`` is the reason here
        path, test_id = resolved, rest
    test_id = "::".join(_PARAMS.sub("", piece) for piece in test_id.split("::"))
    if not path or not test_id:
        return None, "not a test node"
    return (path, test_id), ""


def _has_test_context(entry: object) -> bool:
    """Whether a file entry names at least one test.

    ``--show-contexts`` on data recorded *without* dynamic contexts still
    writes a ``contexts`` key, with every line attributed to the empty static
    context. Its presence alone would read as "nothing covers the change".
    """
    if not isinstance(entry, Mapping):
        return False
    per_line = entry.get("contexts")
    if not isinstance(per_line, Mapping):
        return False
    return any(name for names in per_line.values() for name in (names or []))


def _rewritten(baseline_dir: Path, patch_dir: Path, rel: str) -> bool:
    before, after = baseline_dir / rel, patch_dir / rel
    if not before.is_file():
        return False  # added by the patch: no earlier answer to change
    try:
        return before.read_bytes() != after.read_bytes()
    except OSError:
        return True


def discover_claims(
    diff_text: str,
    coverage: Mapping,
    baseline_dir: Path,
    patch_dir: Path,
    *,
    max_claims: int = 20,
) -> Tuple[List[Tuple[str, str]], Dict[str, object]]:
    """Node ids of the tests that executed a changed source line.

    Returns ``(claims, detail)``: ``claims`` sorted and capped at
    ``max_claims``; ``detail`` records what was found, left out and why, for
    the evidence artefact.
    """
    if max_claims < 1:
        raise ValueError("max_claims must be at least 1")

    files: Mapping = coverage.get("files", {}) if isinstance(coverage, Mapping) else {}
    if not any(_has_test_context(entry) for entry in files.values()):
        raise NoTestContexts(
            "the coverage report has no per-test contexts, so it cannot say which "
            "tests ran the changed lines; record them with "
            "'[run] dynamic_context = test_function' and write the report with "
            "'coverage json --show-contexts' (the Action does both when it writes "
            "the report itself from base-sha)"
        )

    changed = validate_diff(diff_text)
    source_lines = {path: lines for path, lines in changed.items() if not is_test_path(path)}

    contexts: Set[str] = set()
    for path, lines in source_lines.items():
        per_line = files.get(path, {}).get("contexts", {}) or {}
        for line in lines:
            contexts.update(name for name in per_line.get(str(line), []) if name)

    test_files = _test_files(Path(patch_dir))
    nodes: Set[Tuple[str, str]] = set()
    unresolved: Dict[str, str] = {}
    for context in sorted(contexts):
        node, why = _node_id(context, test_files)
        if node is None:
            unresolved[context] = why
        else:
            nodes.add(node)

    rewritten = sorted({path for path, _ in nodes if _rewritten(Path(baseline_dir), Path(patch_dir), path)})
    kept = sorted(node for node in nodes if node[0] not in rewritten)

    detail: Dict[str, object] = {
        "source": "coverage contexts",
        "changed_source_files": sorted(source_lines),
        "contexts_seen": len(contexts),
        "tests_found": len(nodes),
        "excluded_rewritten_test_files": rewritten,
        "unresolved_contexts": unresolved,
        "max_claims": max_claims,
        "truncated": max(0, len(kept) - max_claims),
    }
    return kept[:max_claims], detail
