"""The three artefacts the gate measures, from a git working tree and a ref.

``scripts/prepare_evidence.sh`` does this for CI, where the patch is a commit
(``base...HEAD``). An agent's patch usually is not: it is whatever the working
tree holds right now, committed or not. So here

* **baseline** is the committed tree of ``<base_ref>``, every file as committed.
  The ref is resolved once to a commit id; the tree is read object by object
  (``ls-tree`` and ``cat-file``) with object substitution switched off, and every
  blob is checked against the id the tree names. No archive, no checkout, so no
  attribute, filter or export rule of the repository can change what is written
  (AG-044, AG-045);
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

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from adversary_gate.verifiers.coverage import validate_diff


class EvidenceError(RuntimeError):
    """The repository cannot answer: not a git tree, unknown ref, no coverage."""


#: Runs under the project's interpreter, which has coverage.py but not
#: necessarily this package: self-contained on purpose. Writes the subset of
#: ``coverage json --show-contexts`` that the gate reads, for the files and
#: lines the diff names, plus a marker that contexts were recorded at all --
#: without it, a change no test executes would look like a report recorded
#: without per-test contexts.
_EXTRACT = r"""
import json, os, sys
import coverage
data_file, wanted_file, repo, out = sys.argv[1:5]
data = coverage.CoverageData(basename=data_file)
data.read()
wanted = json.load(open(wanted_file, encoding="utf-8"))
by_rel = {os.path.relpath(f, repo).replace(os.sep, "/"): f for f in data.measured_files()}
files = {}
for rel, lines in wanted.items():
    path = by_rel.get(rel)
    if path is None:
        continue
    per_line = data.contexts_by_lineno(path)
    files[rel] = {
        "executed_lines": sorted(data.lines(path) or []),
        "contexts": {str(n): sorted(per_line.get(n, [])) for n in lines if per_line.get(n)},
    }
recorded = any(c for c in data.measured_contexts())
json.dump({"meta": {"format": 3, "show_contexts": True,
                    "adversary_gate_contexts_recorded": recorded,
                    "adversary_gate_extract": "changed lines only"},
           "files": files}, open(out, "w", encoding="utf-8"))
"""


@dataclass
class Evidence:
    baseline: Path
    patch: Path
    diff: Path
    coverage: Optional[Path]
    untracked: List[str]
    #: The commit the baseline was read from, as an object id: what ``base_ref``
    #: resolved to when this run started.
    baseline_sha: str = ""


#: Settings every git call of the gate carries. Object substitution
#: (``refs/replace``, grafts) would let the repository answer for a commit with
#: another one's content; the gate reads the objects the ids name.
_GIT_FIXED = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat", "GIT_NO_REPLACE_OBJECTS": "1"}
_GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull)


def _git_env(extra: Optional[dict] = None) -> dict:
    return {**os.environ, **(extra or {}), **_GIT_FIXED}


def _git(repo: Path, *args: str, binary: bool = False, env: Optional[dict] = None):
    proc = subprocess.run(
        ["git", *_GIT_CONFIG, "-C", str(repo), *args],
        capture_output=True,
        text=not binary,
        env=_git_env(env),
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


_MODES = {"100644": 0o644, "100755": 0o755}


def _blob_hasher(object_format: str):
    return hashlib.sha256 if object_format == "sha256" else hashlib.sha1


def _read_blobs(repo: Path, oids: Sequence[str], hasher) -> dict:
    """``{oid: bytes}`` through one ``git cat-file --batch``, each blob checked
    against its own id: the content hashed as git hashes it must give the id
    the tree names."""
    if not oids:
        return {}
    proc = subprocess.Popen(
        ["git", *_GIT_CONFIG, "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_git_env(),
    )

    def feed() -> None:
        try:
            proc.stdin.write("".join(f"{oid}\n" for oid in oids).encode())
        finally:
            proc.stdin.close()

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    blobs: dict = {}
    failed = True
    try:
        for oid in oids:
            header = proc.stdout.readline().split()
            if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
                raise EvidenceError(f"git could not read blob {oid}")
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # the newline that follows each object
            if len(data) != size:
                raise EvidenceError(f"blob {oid} was cut short")
            if hasher(b"blob %d\0" % size + data).hexdigest() != oid:
                raise EvidenceError(f"blob {oid} does not match its own id")
            blobs[oid] = data
        failed = False
    finally:
        if failed:
            proc.kill()
        writer.join()
        proc.stdout.close()
        proc.wait()
    return blobs


def _write_tree(repo: Path, commit: str, dest: Path, object_format: str) -> None:
    """Write the tree of ``commit`` under ``dest``, byte for byte as committed."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, binary=True)
    entries = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, raw = record.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        if kind != "blob":
            continue  # submodule commits are not part of the repository's own files
        rel = raw.decode("utf-8", errors="surrogateescape")
        parts = rel.split("/")
        if rel.startswith("/") or ".." in parts or ".git" in (p.lower() for p in parts):
            raise EvidenceError(f"refusing to write {rel!r} from the baseline tree")
        entries.append((mode, oid, rel))
    blobs = _read_blobs(repo, sorted({oid for _, oid, _ in entries}), _blob_hasher(object_format))
    for mode, oid, rel in entries:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            os.symlink(blobs[oid].decode("utf-8", errors="surrogateescape"), target)
        elif mode in _MODES:
            target.write_bytes(blobs[oid])
            target.chmod(_MODES[mode])
        else:
            raise EvidenceError(f"unexpected mode {mode} for {rel!r} in the baseline tree")


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
    # Resolved once; everything below uses the id, so a ref that moves while the
    # run is under way changes nothing.
    commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise EvidenceError(f"{base_ref!r} does not name a commit")
    object_format = _git(repo, "rev-parse", "--show-object-format").strip()

    baseline = workdir / "baseline"
    baseline.mkdir(parents=True)
    _write_tree(repo, commit, baseline, object_format)

    diff_text = _git(repo, "-c", "color.ui=never", "diff", "--no-ext-diff", "--no-textconv", "--text", "--no-color", commit, "--")
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
        # Not ``coverage json --show-contexts``: it writes every line of every
        # measured file with every test that ran it -- on more-itertools 193 s
        # and 312 MB, for a report of which discovery and diff coverage read
        # the changed lines only. Read those from the database instead.
        wanted = workdir / "changed-lines.json"
        wanted.write_text(json.dumps({
            path: sorted(lines) for path, lines in validate_diff(diff_text).items()
            if path.endswith(".py")
        }), encoding="utf-8")
        report = subprocess.run(
            [python, "-c", _EXTRACT, str(workdir / ".coverage"), str(wanted),
             str(repo), str(coverage_path)],
            cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if report.returncode != 0 or not coverage_path.is_file():
            raise EvidenceError(
                "coverage produced no report: " + (report.stderr or run.stderr)[-500:].strip()
            )
    return Evidence(baseline, repo, diff, coverage_path, sorted(untracked), commit)
