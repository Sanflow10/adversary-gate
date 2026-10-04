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
import shutil
import signal
import site
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore

#: Measured floor: 64 and 256 both die on plugin thread startup, 4096 is fine.
DEFAULT_PROCESSES = 4096

#: Truncate captured output before it reaches the evidence log. Enough to
#: show the failing assertion, small enough to keep the artefact readable.
OUTPUT_TAIL_BYTES = 4000

#: The exit code a run gets when it died of the limits rather than of the code
#: under test -- pytest's own "internal error", which ``exitmap`` already reads
#: as UNRUNNABLE.
HARNESS_DIED = 3

#: Text a process prints when the memory limit, not the code under test, ended
#: it (AG-024). Measured: under RLIMIT_AS the JVM exits 1 with "Could not
#: reserve enough space", Node aborts with "Fatal process out of memory", and a
#: Python allocation raises MemoryError and fails the test -- exit 1 in all
#: three, which the exit map would otherwise read as a test failure, i.e. as
#: evidence against the patch.
RESOURCE_DEATH_MARKERS = (
    "MemoryError",
    "Could not reserve enough space",
    "Fatal process out of memory",
    "java.lang.OutOfMemoryError",
    "Cannot allocate memory",
    "std::bad_alloc",
)


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


#: Directories a test process needs to read to start at all. Bound read-only,
#: so "the code under test can rewrite /usr" stops being a thing that needs
#: arguing about.
_SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc")


def bwrap_available() -> bool:
    """Whether an optional real sandbox can be requested at all."""
    return shutil.which("bwrap") is not None


def bwrap_prefix(repo_dir: Path, python: Optional[str] = None) -> List[str]:
    """``bwrap`` argv that runs the next command inside a restricted namespace.

    This is *optional defence in depth* over the resource limits, not a
    replacement for them: it denies network (``--unshare-net``), hides the
    host's processes (``--unshare-pid``), makes the system tree read-only and
    gives the guest a private ``/tmp``. What it still does not do is make
    hostile code safe -- there is no seccomp profile, no user drop and no
    read-only view of the repository's own parent, so treat it as "the casual
    damage is contained", which is a different promise from "this is a VM".

    Raises ``ValueError`` when ``bwrap`` is missing, so a caller who asked for
    isolation learns that rather than silently getting none.
    """
    if not bwrap_available():
        raise ValueError(
            "--sandbox bwrap was requested but 'bwrap' is not on PATH; "
            "install bubblewrap or drop the flag (the resource limits still apply)"
        )

    root = Path(repo_dir).resolve()
    argv: List[str] = [
        "bwrap",
        "--unshare-net",
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        # The guest gets its own /tmp so a test cannot leave files behind for
        # the next run to read, and HOME points into it so tools that insist
        # on writing *somewhere* do not write into the repository. Both are
        # bwrap mount/env specifications, not tempfile calls -- hence nosec.
        "--tmpfs", "/tmp",  # nosec B108
        "--setenv", "HOME", "/tmp",  # nosec B108
        "--proc", "/proc",
        "--dev", "/dev",
        # Read-only system, read-write only the repository under test.
        "--bind", str(root), str(root),
        "--chdir", str(root),
    ]
    for path in _SYSTEM_RO:
        candidate = Path(path)
        if not candidate.exists():
            continue
        if candidate.resolve() == root or candidate.resolve().is_relative_to(root):
            continue
        argv += ["--ro-bind", path, path]

    # pytest and any third-party plugin usually live in the *user* site, which
    # Python derives from $HOME -- and HOME is /tmp inside the guest, so the
    # guest would look in /tmp/.local and die with "No module named pytest".
    # Bind the real user base read-only and point PYTHONUSERBASE at it, which
    # is resolved at interpreter start and so does not follow HOME.
    user_base = Path(site.USER_BASE) if site.USER_BASE else None
    if user_base and user_base.exists() and not user_base.is_relative_to(root):
        argv += [
            "--ro-bind", str(user_base), str(user_base),
            "--setenv", "PYTHONUSERBASE", str(user_base),
        ]

    # A venv interpreter is outside /usr as well; bind exactly its prefix
    # rather than the whole home directory it may sit in. ``python`` is the
    # project's interpreter when one was named (AG-025): its venv (``bin/..``,
    # read through the symlink) and the installation it points at (``bin/..``
    # of the resolved file) are bound the same way.
    prefixes = [sys.base_prefix, sys.prefix]
    if python:
        prefixes += [
            os.path.dirname(os.path.dirname(os.path.abspath(python))),
            os.path.dirname(os.path.dirname(os.path.realpath(python))),
        ]
    for prefix in prefixes:
        resolved = Path(prefix).resolve() if prefix else None
        if not resolved or not resolved.exists() or resolved == Path("/usr"):
            continue
        if resolved.is_relative_to(root) or resolved in (Path("/usr"), Path("/bin")):
            continue
        if any(part == str(resolved) for part in argv):
            continue
        argv += ["--ro-bind", str(resolved), str(resolved)]

    argv += ["--"]
    return argv


def _argv(
    repo_dir: Path,
    test_path: str,
    test_id: str,
    command: Optional[str],
    python: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
) -> List[str]:
    """What actually gets executed: a caller's command, or pytest on a target.

    ``--test-command`` exists because "run my suite" is not always "run
    pytest", and a gate that can only execute pytest is a gate that reports
    *no tests collected* (exit 5 -> UNVERIFIED -> INCONCLUSIVE) for any
    repository that is not Python/pytest. The convention is documented at the
    flag: 0 passes, 1 fails, 2/3/4 are harness errors, and anything else is
    neither, exactly as the pytest table already says.

    ``python`` is the interpreter that runs pytest. It defaults to the one
    running the gate, which is only right when the gate is installed next to
    the project's dependencies; ``--python`` names the project's own (AG-025).
    ``targets`` runs several node ids in one invocation.
    """
    if command:
        return ["/bin/sh", "-c", command]
    chosen = list(targets) if targets else [build_target(test_path, test_id)]
    return [python or sys.executable, "-m", "pytest", *chosen, "-q",
            "-p", "no:cacheprovider"]


def _limit_resources(
    cpu_seconds: Optional[int],
    mem_bytes: Optional[int],
    file_bytes: int,
    open_files: int,
    processes: int,
):
    """``None`` for CPU or memory means "no limit", and is applied as no call.

    It is a decision the caller makes out loud (``--cpu-seconds none``,
    ``--memory none``) and records in the artefact; the JVM and Node cannot
    start under an address-space limit at all.
    """
    def _apply() -> None:
        if resource is None:
            return
        if cpu_seconds is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        if mem_bytes is not None:
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
    timeout_seconds: Optional[int] = 30,
    cpu_seconds: Optional[int] = 10,
    mem_bytes: Optional[int] = 512 * 1024 * 1024,
    env: Optional[Mapping[str, str]] = None,
    file_bytes: int = 16 * 1024 * 1024,
    open_files: int = 256,
    processes: int = DEFAULT_PROCESSES,
    require_network_isolation: bool = False,
    command: Optional[str] = None,
    sandbox: Optional[str] = None,
    python: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
) -> SandboxResult:
    """Run one test target under resource limits and return raw evidence.

    Output is *kept*, not discarded: callers persist it. The previous version
    threw ``stdout``/``stderr`` away and logged only exit-code counts, which
    made the "evidence log" contain no evidence.

    ``command`` replaces pytest with the caller's own command (see
    :func:`_argv` for the pass/fail/harness convention). ``sandbox`` wraps
    the whole thing in ``bwrap`` when asked; it is opt-in because it needs a
    binary this package cannot install for you.

    ``timeout_seconds``, ``cpu_seconds`` and ``mem_bytes`` accept ``None`` for
    "no limit"; zero and negatives are still refused, because a limit of zero
    is a typo, not a policy.

    ``HOME`` points at a private directory created for this run and removed
    after it, unless ``env`` names one. Tools that insist on a home (npm,
    cargo, pip caches) get somewhere to write that is neither the repository
    nor the operator's real home. The user site the gate itself sees stays
    importable through ``PYTHONUSERBASE``, which does not follow ``HOME``.
    """
    for name, value in (
        ("timeout_seconds", timeout_seconds),
        ("cpu_seconds", cpu_seconds),
        ("mem_bytes", mem_bytes),
        ("file_bytes", file_bytes),
        ("open_files", open_files),
        ("processes", processes),
    ):
        if value is not None and value <= 0:
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
    if site.USER_BASE:
        safe_env["PYTHONUSERBASE"] = site.USER_BASE
    if env:
        safe_env.update(env)

    argv = _argv(root, test_path, test_id, command, python=python, targets=targets)
    if sandbox == "bwrap":
        # Resolved *before* Popen so a missing binary is a usage error the
        # caller sees, not a run that quietly happened unsandboxed.
        argv = bwrap_prefix(root, python=python) + argv
    elif sandbox not in (None, "", "none"):
        raise ValueError(f"unknown sandbox: {sandbox!r} (expected 'none' or 'bwrap')")

    preexec = (
        _limit_resources(cpu_seconds, mem_bytes, file_bytes, open_files, processes)
        if resource
        else None
    )

    # Created last, after everything above that can raise, so the ``finally``
    # below is the only exit and always removes it.
    private_home: Optional[str] = None
    if "HOME" not in safe_env:
        private_home = tempfile.mkdtemp(prefix="adversary-home-")
        safe_env["HOME"] = private_home

    proc: Optional[subprocess.Popen[str]] = None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=safe_env,
            preexec_fn=preexec,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        # The pytest heuristics below look for pytest's own error strings;
        # applying them to an arbitrary caller command would rewrite that
        # command's real exit code on the strength of unrelated text.
        code = (
            proc.returncode
            if command
            else _detect_harness_error(proc.returncode, stdout, stderr)
        )
        return SandboxResult(_detect_resource_death(code, stdout, stderr), stdout, stderr, False)
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
    finally:
        if private_home is not None:
            shutil.rmtree(private_home, ignore_errors=True)


def _detect_resource_death(exit_code: int, stdout: str, stderr: str) -> int:
    """A run the limits killed is not a test that failed (AG-024).

    Only exit 1 is rewritten, because it is the only code the exit map counts
    as evidence; anything else is already unrunnable. The rewrite goes in the
    safe direction: a real failure that happens to print one of these markers
    becomes INCONCLUSIVE, never MERGE.
    """
    if exit_code != 1:
        return exit_code
    combined = f"{stdout}\n{stderr}"
    if any(marker in combined for marker in RESOURCE_DEATH_MARKERS):
        return HARNESS_DIED
    return exit_code


def _detect_harness_error(exit_code: int, stdout: str, stderr: str) -> int:
    """Ensure missing pytest or harness crashes become UNRUNNABLE (3/4)."""
    combined = f"{stdout}\n{stderr}".lower()
    if "no module named pytest" in combined or "no module named 'pytest'" in combined:
        return 3
    if "pytest: error:" in combined:
        return 4
    return exit_code

