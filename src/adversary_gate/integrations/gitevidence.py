"""The three artefacts the gate measures, from a git working tree and a ref.

``scripts/prepare_evidence.sh`` does this for CI, where the patch is a commit
(``base...HEAD``). An agent's patch usually is not: it is whatever the working
tree holds right now, committed or not. So here

* **baseline** is ``git archive <base_ref>``: the committed tree and nothing else;
* **patch** is the working tree itself, untracked files included;
* **diff** is ``git diff <base_ref>`` (tracked changes, staged or not) plus a
  new-file hunk for every untracked, non-ignored file -- ``git diff`` alone
  would leave a brand-new module out of the coverage denominator;
* **coverage** is coverage.py run on the working tree with per-test contexts,
  so claims can be discovered. Its data file goes to a scratch directory, never
  into the repository.

Nothing in the working tree is modified, staged or committed.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence


class EvidenceError(RuntimeError):
    """The repository cannot answer: not a git tree, unknown ref, no coverage."""


@dataclass
class Evidence:
    baseline: Path
    patch: Path
    diff: Path
    coverage: Optional[Path]
    untracked: List[str]


def _git(repo: Path, *args: str, binary: bool = False):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=not binary,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat"},
    )
    if proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise EvidenceError(f"git {' '.join(args)} failed: {err.strip()}")
    return proc.stdout


def _new_file_hunk(rel: str, path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""  # binary or unreadable: nothing line-based to cover
    lines = text.splitlines()
    if not lines:
        return ""
    body = "".join(f"+{line}\n" for line in lines)
    return f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def prepare(
    repo: Path,
    base_ref: str,
    workdir: Path,
    *,
    python: str,
    coverage: bool = True,
    pytest_args: Sequence[str] = (),
    timeout: int = 600,
) -> Evidence:
    repo = Path(repo).resolve()
    workdir = Path(workdir)
    if _git(repo, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise EvidenceError(f"{repo} is not a git working tree")
    top = Path(_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != repo:
        raise EvidenceError(f"pass the repository root ({top}), not a subdirectory")
    _git(repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")

    baseline = workdir / "baseline"
    baseline.mkdir(parents=True)
    archive = _git(repo, "archive", "--format=tar", base_ref, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(baseline, filter="data")
        else:  # pragma: no cover - Python < 3.10.12 / 3.11.4
            for member in tar.getmembers():
                target = (baseline / member.name).resolve()
                if not target.is_relative_to(baseline.resolve()) or member.issym() or member.islnk():
                    raise EvidenceError(f"refusing archive member outside the baseline: {member.name}")
            tar.extractall(baseline)  # nosec B202 - every member checked above

    diff_text = _git(repo, "-c", "color.ui=never", "diff", "--no-ext-diff", "--no-color", base_ref, "--")
    untracked = [
        line for line in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        if line and "__pycache__" not in line.split("/")
    ]
    diff_text += "".join(_new_file_hunk(rel, repo / rel) for rel in sorted(untracked))
    diff = workdir / "change.diff"
    diff.write_text(diff_text, encoding="utf-8")

    coverage_path: Optional[Path] = None
    if coverage:
        rc = workdir / "coveragerc"
        rc.write_text("[run]\ndynamic_context = test_function\n")
        coverage_path = workdir / "coverage.json"
        env = {
            **os.environ,
            "COVERAGE_FILE": str(workdir / ".coverage"),
            "COVERAGE_RCFILE": str(rc),
            # No __pycache__ left behind in somebody else's working tree.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        run = subprocess.run(
            [python, "-m", "coverage", "run", "--rcfile", str(rc), "-m", "pytest", "-q",
             "-p", "no:cacheprovider", *pytest_args],
            cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if "No module named coverage" in run.stderr:
            raise EvidenceError(f"coverage.py is not installed for {python}")
        report = subprocess.run(
            [python, "-m", "coverage", "json", "--rcfile", str(rc), "--show-contexts",
             "-o", str(coverage_path)],
            cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if report.returncode != 0 or not coverage_path.is_file():
            raise EvidenceError(
                "coverage produced no report: " + (report.stderr or run.stderr)[-500:].strip()
            )
    return Evidence(baseline, repo, diff, coverage_path, sorted(untracked))
