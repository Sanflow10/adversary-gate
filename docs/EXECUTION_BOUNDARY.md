# Execution boundary

Read this before trusting the gate with untrusted code. Back to the [README](../README.md).

## 🛡️ Execution boundary (read this before trusting it with untrusted code)

`src/adversary_gate/sandbox/runner.py` applies POSIX resource limits on every run: `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, `RLIMIT_NPROC`, plus a timeout that kills the process group. By itself that is a **resource-limited runner, not a security sandbox** — it bounds how *much* a test can do, not *what*.

**Android / Termux.** On Android, `RLIMIT_AS` (`--memory`, `--full-suite-memory`) aborts some processes at start, because of how the runtime reserves address space; in the runs reported in issue #4, no finite address-space limit worked there. Since 3.0.5 neither limit is set on Android unless you pass one: the gate recognises it (`sys.platform == "android"`, or `sys.getandroidapilevel` present) and the artefact records `execution.android`. The other limits still apply.

### `--sandbox bwrap` — opt-in, and what it honestly buys

```bash
adversary-gate ... --sandbox bwrap
```

Wraps every claim run and the collateral suite in [bubblewrap](https://github.com/containers/bubblewrap):

| Property | Effect |
| --- | --- |
| `--unshare-net` | no network at all — exfiltration has nowhere to go |
| `--unshare-pid` | the guest cannot see or signal host processes |
| `--unshare-ipc`, `--unshare-uts` | no shared memory or message queues with the host, its own hostname |
| `--unshare-user-try`, `--cap-drop ALL` | a user namespace where the kernel allows one, and no capability inside it |
| `--tmpfs /tmp` + `HOME=/tmp` | nothing persists between runs, nothing lands in the repo |
| system tree read-only | `/usr`, `/bin`, `/lib`, `/etc` cannot be written |
| only the repo is writable | a test cannot touch anything outside the patch |

If `bwrap` is missing the gate **refuses to start** (exit 3) rather than quietly running unsandboxed — asking for isolation and not getting it is the failure mode the flag exists to prevent.

**What it is still not.** There is no seccomp profile, the repository itself is writable, and the kernel is the host's: a kernel bug is reachable from inside. Outside the repository, the guest sees only the read-only system tree, the interpreter's prefix, and the user site-packages and scripts directory (not the rest of `~/.local`). It contains casual and opportunistic damage; it is not a defence against code whose purpose is to escape. For that, and for anything where a compromise would matter, use the outer sandbox below — this flag is for the case where you *have no infrastructure and the patch is probably fine*, not the case where the patch is known hostile.

`--require-network-isolation` now has something real to point at: under `--sandbox bwrap` the network genuinely is gone, so `ADVERSARY_NETWORK_ISOLATED=1` becomes a statement you can make honestly instead of a wish. Outside bwrap it remains a **declaration, not a mechanism** — an assertion by *you* about *your* environment, which the tool cannot verify about itself.

### What no mode of this tool provides

Network namespaces beyond bwrap, seccomp, chroot, containers, an unprivileged user, or any restriction on what the test code may do *by design*. The test code runs arbitrary code from the repository, with whatever privileges you gave the process.

**Run hostile patches inside an outer sandbox you control** — rootless container, VM, or an ephemeral isolated runner. AdversaryGate verifies test outcomes; it does not contain hostile code.

The GitHub Action already runs on an ephemeral runner: the job's VM is thrown away when it ends, which is that outer sandbox, as long as the job holds no secret the patch could read. Off GitHub, the repository's image is a starting point. It runs as an unprivileged user (uid 10001), never as root. Your project's dependencies are not in it, so extend it:

```dockerfile
FROM adversary-gate-mcp            # docker build -t adversary-gate-mcp .
USER root
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
USER 10001
```

Then run the gate with no network, no capabilities, a read-only root and a **throwaway copy** of the repository (a fresh clone, never your working tree: the tests write into it):

```bash
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
  --read-only --tmpfs /tmp:rw,exec --tmpfs /home/gate:rw \
  --pids-limit 512 --memory 4g \
  -v "$PWD/throwaway":/work --entrypoint adversary-gate my-project-gate \
  --baseline /work/baseline --patch /work/patch \
  --diff /work/changes.diff --coverage-json /work/coverage.json --discover-claims
```

`--network none` is the boundary `--require-network-isolation` asks you to vouch for; inside it the declaration is true. A container shares the host's kernel: for code known to be hostile, a VM is the stronger boundary.

### What a test cannot see, by construction

Two limits that no runner setting closes, written down so nobody has to discover them:

- **The code under test runs in the same process as the test runner.** A source module the tests import can reach into pytest itself — rewrite a report, patch `assert`'s helpers, swap a fixture — and nothing in the evidence would show it. AG-032 takes the *configuration* away from the patch; it cannot take the interpreter away from the code being tested.
- **Code can detect that it is under test.** `if "pytest" in sys.modules:` (or a check on the environment, the call stack, the clock) lets a module behave in the test run and misbehave in production. Mutation testing does not help when the branch that matters is the one the tests never take.

Both are deliberate deception, not accidents, and both are reasons `MERGE` means *"permission for a human to look"*, never *"ship it"*.

Two more that the branch mutants narrow but do not close, measured on the case they were built for:

- **A special case kept in data, not in syntax.** `return {0: 999}.get(x, x + 1)` has no conditional for a branch mutant to remove, and the gate never changes a literal. If no claim passes `0`, nothing the gate does reaches the `999`: next to honest, tested changes that bring the sample to five kills, it merges.
- **A hidden branch whose body has its own line, with an operator on it.** Coverage sees that body — it counts against the ratio — and the default floor (`--coverage-floor 0.80`) tolerates up to one changed line in five that no claim executed. A body with no operator (`return 999`) is broken outright and blocks; one with an operator (`return x + 998`) is a single surviving operator mutant in the ratio, which enough honest kills elsewhere can outweigh (eleven against one, at the defaults). `--coverage-floor 1.0` refuses any changed line that never ran.
