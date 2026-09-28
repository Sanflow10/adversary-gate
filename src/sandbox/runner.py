"""Sandboxed pytest execution.

Two changes from the previous runner, both driven by measured failures:

1. **The target is ``path::test_id``, not just ``path``.** The old runner
   never mentioned ``test_id`` (``grep test_id runner.py`` -> nothing), so
   the claim granularity was the *file* while the decision granularity was
   the *test*. That let a claim about a passing test be accepted because a
   sibling test in the same file failed.

2. **``RLIMIT_NPROC`` default raised.** At 64 (and 256) this environment
   dies with ``INTERNALERROR> RuntimeError: can't start new thread`` because
   ``pytest_rerunfailures`` spawns a thread during ``pytest_configure``.
   Baseline and patch then fail identically for reasons unrelated to the
   code under test. 4096 runs clean. The limit is still a limit -- it just
   no longer trips on plugin startup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore

#: Measured floor: 64 and 256 both die on plugin thread startup, 4096 is fine.
DEFAULT_PROCESSES = 4096

#: Truncate captured output before it reaches the evidence log. Enough to
#: show the failing assertion, small enough to keep the artefact readable.
OUTPUT_TAIL_BYTES = 4000


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def output_tail(self) -> str:
        """Combined output, truncated, for the evidence artefact."""
        blob = (self.stderr or "") + ("\n" + (self.stdout or "") if self.stdout else "")
        blob = blob.strip()
        if len(blob) > OUTPUT_TAIL_BYTES:
            return "...[truncated]...\n" + blob[-OUTPUT_TAIL_BYTES:]
        return blob


def build_target(test_path: str, test_id: str) -> str:
    """``tests/test_x.py`` + ``test_add`` -> ``tests/test_x.py::test_add``."""
    if not test_id:
        return test_path
    return f"{test_path}::{test_id}"


def _limit_resources(
    cpu_seconds: int,
    mem_bytes: int,
    file_bytes: int,
    open_files: int,
    processes: int,
):
    def _apply() -> None:
        if resource is None:
            return
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))

    return _apply


def run_test(
    repo_dir: Path,
    test_path: str,
    test_id: str = "",
    timeout_seconds: int = 30,
    cpu_seconds: int = 10,
    mem_bytes: int = 512 * 1024 * 1024,
    env: Optional[Mapping[str, str]] = None,
    file_bytes: int = 16 * 1024 * 1024,
    open_files: int = 256,
    processes: int = DEFAULT_PROCESSES,
    require_network_isolation: bool = False,
) -> SandboxResult:
    """Run one pytest target under resource limits and return raw evidence.

    Output is *kept*, not discarded: callers persist it. The previous version
    threw ``stdout``/``stderr`` away and logged only exit-code counts, which
    made the "evidence log" contain no evidence.
    """
    for name, value in (
        ("timeout_seconds", timeout_seconds),
        ("cpu_seconds", cpu_seconds),
        ("mem_bytes", mem_bytes),
        ("file_bytes", file_bytes),
        ("open_files", open_files),
        ("processes", processes),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    if require_network_isolation and os.environ.get("ADVERSARY_NETWORK_ISOLATED") != "1":
        raise RuntimeError(
            "network isolation is required; run inside an outer sandbox and "
            "set ADVERSARY_NETWORK_ISOLATED=1"
        )

    root = Path(repo_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_dir is not a directory: {repo_dir}")

    safe_env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    if env:
        safe_env.update(env)

    target = build_target(test_path, test_id)
    preexec = (
        _limit_resources(cpu_seconds, mem_bytes, file_bytes, open_files, processes)
        if resource
        else None
    )

    proc: Optional[subprocess.Popen[str]] = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=safe_env,
            preexec_fn=preexec,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return SandboxResult(proc.returncode, stdout, stderr, False)
    except subprocess.TimeoutExpired as exc:
        assert proc is not None
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        out = exc.stdout if exc.stdout is not None else stdout
        err = exc.stderr if exc.stderr is not None else stderr
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        # -1 is deliberately distinct from pytest's own codes: a timeout is
        # the harness giving up, not pytest reporting a failure.
        return SandboxResult(-1, out or "", err or "", True)
