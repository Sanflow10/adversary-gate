# Security Policy

## Read this first: a bug here is a security bug

AdversaryGate exists to stop a patch from being merged on bad evidence. Its
failure mode is not "a crash" — it is a **`MERGE` that should have been a
`BLOCK`**. Any defect that can produce that is in scope, and is treated as
higher severity than the same defect in an ordinary application, because the
whole value of the tool is the guarantee.

Concretely, the report-worthy class includes:

- any path where a patch reaches `MERGE` while its evidence does not support it
  (this is exactly how AG-012, AG-013, AG-014 and AG-015 were found);
- a way to make `UNVERIFIED` or `INCONCLUSIVE` render as `verified`;
- a `diff` or coverage input that parses into a *different* file set than the
  patch actually contains (parser confusion is fail-open by construction);
- code execution, file write or network access **outside** the boundary
  described in [Execution boundary](docs/EXECUTION_BOUNDARY.md),
  reached by a test the gate ran;
- a way to bypass `--require-network-isolation` or `--sandbox bwrap` such that
  the run proceeds while the property does not hold.

## Scope

**In scope:** `src/`, `action.yml`, `.github/workflows/`, `tests/`, packaging
(`pyproject.toml`), and anything published from this repository.

**Out of scope (by design, and documented as such):**

- code the patch itself contains doing what it likes *inside* the runner's
  privileges. That is the [Execution boundary](docs/EXECUTION_BOUNDARY.md),
  not a vulnerability — see "Known limitations" below.
- `--require-network-isolation` being set without an actual isolated
  environment. It is a declaration by you about your environment; the tool
  cannot verify it about itself.
- an `INCONCLUSIVE` result you consider too strict. Ambiguity is the product.
- vulnerabilities in dependencies — report them upstream, and tell us here only
  if we ship a version that is affected.

## Known limitations

These are open, deliberate, and are **not** reports unless you can show one of
them produces a `MERGE` it should not:

- **The test code runs with your privileges.** The sandbox (on by default
  where bubblewrap can run, `--sandbox bwrap` to require it) adds no
  network, a private PID namespace, a private `/tmp`, a read-only system tree
  and a repository that is the only writable path (the coverage pass of the
  MCP tool and of the Action's `base-sha` mode also writes one fresh directory
  outside it, for the coverage data). It drops every capability
  (`--cap-drop ALL`, in a user namespace when the kernel allows one) but keeps
  your user id, and has no seccomp profile. It is containment for opportunistic code, not
  for code written to escape. For hostile input, run the whole gate in a
  container or VM.
- **`self_deception_index` measures decisions, not correctness** — and on logs
  this gate wrote it is `0` by construction (AG-029). A gate that is
  confidently wrong in both directions looks identical to one that is not.
- **Code under test shares the runner's process.** The patch can no longer
  configure pytest (AG-032), but a source module the tests import can still
  tamper with pytest in-process, or behave differently when it detects a test
  run. That is out of reach of any test-based gate; see
  [What a test cannot see](docs/EXECUTION_BOUNDARY.md#what-a-test-cannot-see-by-construction).
- **Without the sandbox, the gate's runs share your machine.** The claims and
  the measurement run in copies of the trees (3.0.18), but a test can still
  write outside them -- a file in `/tmp`, the bytecode cache the runs share --
  and a later run can read it. With bubblewrap each run has its own `/tmp` and
  the cache is read-only. When `--sandbox auto` falls back to none, the answer
  says so (`sandbox_note`); for untrusted patches require `--sandbox bwrap`.
- **A claim written to recognise the measurement can still pass it.** The
  neutral copy that catches a claim reading the code has markers drawn at
  random, a random size and the file's own times (3.0.18), but a claim that
  normalises the code's structure before comparing it can tell a mutant from
  the patch without running either. Mutation testing assumes the claims test
  behaviour; the copy narrows how far a claim that does not can go.
- **Only pytest and `--test-command` are executed.** There is no mutation
  adapter yet, so non-Python stacks cannot reach `MERGE` at all — that is a
  missing feature, not a bypass.
- **A product module disguised under a test path is caught by its imports.**
  From `3.0.6`, a Python module that wears a test's name (`test_*`/`*_test.py`,
  or a `tests` directory) but that the code under test imports is judged as
  source, not reverted to the baseline as a test (before `3.0.6` it was not, a
  fail-open an audit raised). The detection reads imports from the AST, and a
  name written out as a string in `importlib.import_module` or `__import__` is
  read too: a module reached only through a name built at run time, or an
  import path the resolver does not match, is not caught. Declare a real test
  helper with `--test-support`; do not place product code under a test path.
- **The evidence log is not tamper-evident.** `evidence.jsonl` is plain JSON
  lines, rotated at 20 MiB with the oldest file dropped; whoever can write it
  can edit it, and nothing in it shows that. Each decision record carries
  `policy_sha256` (3.0.18), so a record's rules can be compared; to keep a
  record as evidence, hash or sign it outside the run.
- **Releases are not signed with a project key.** PyPI uploads go through
  Trusted Publishing (AG-008 closed), and every file published today (3.0.4
  onward; earlier versions are no longer distributed) carries a PyPI
  provenance attestation tying it to `pypi-publish.yml` in this repository.
  The GitHub Release artefacts carry no signature; verify them against the
  signed tag.
  Release tags are SSH-signed, and both release workflows refuse a tag that is
  not signed by the key in the repository variable `RELEASE_ALLOWED_SIGNERS`
  (before this, a tag's name was all `pypi-publish.yml` checked). The release
  key is:

  ```
  ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM9vCvqwQR4opVWnagKysacyO0pWZFt5Qk4b+HcV30jq
  ```

  To check a tag yourself: put `release <that line>` in a file and run
  `git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=<file> verify-tag vX.Y.Z`.

## Reporting a vulnerability

Open a **GitHub Security Advisory** on this repository:
<https://github.com/Sanflow10/adversary-gate/security/advisories/new>
(or *Security* → *Report a vulnerability*). That route is private — the report
reaches the maintainers and nobody else, which is what you want for something
that could be exploited before a fix exists.

**If that page is unavailable to you**, open a public issue titled
`[SECURITY] please contact me` with **no technical detail** — no commands, no
inputs, no description of the flaw, not even which component. Say only that you
have a report and how a maintainer can reach you privately. A maintainer will
move the conversation to a private channel before anything technical is said,
and the issue is closed once that has happened.

**Or email** <sanflow086@gmail.com> with `[SECURITY] adversary-gate` in the
subject. The address is monitored, and it is the same one in the package
metadata (`pyproject.toml`). Email is not encrypted end to end: for a
fail-open, prefer the advisory above, or send only a first message asking for a
private channel.

Please include:

1. what you did, exactly — commands, inputs, and the artefact you got;
2. what you expected, and why the difference is a fail-open;
3. the exit code and the `decision`/`outcome` fields, if a run is involved;
4. the commit SHA you ran;
5. whether you have disclosed it anywhere else.

A minimal reproducer is worth more than a description. If it is a fail-open,
the single most useful artifact is a pair: *this input → `MERGE`*, and *what
the evidence inside that artefact says*.

## Response commitments

Be realistic about who maintains this: the target is honest, not impressive.

| Stage | Target |
| --- | --- |
| Acknowledge | 5 working days |
| Severity triage | 10 working days |
| Fix or workaround for a confirmed fail-open | 30 days, faster if trivially exploitable |
| Public disclosure | after a fix ships, or 90 days after report, whichever is first |

If a target will be missed, you will be told so, with a reason and a new date.
Silence is the one response that is off the table.

There is **no bug bounty**. This is an open-source project with no revenue
program; offering money we cannot pay would be a lie. Credit is offered
instead: your handle in `ERRORS_AND_INCONSISTENCIES.md` and the release notes,
unless you ask not to be named.

## Coordinated disclosure

- Report privately first. Public issues about an unpatched fail-open are
  handled by triage, not by debate.
- If you intend to publish after 90 days regardless of response, say so in the
  report. That is acceptable; it is also a fair thing to tell us up front.
- We will credit reports that were actionable, that we could reproduce, and
  that were disclosed to us first.

## Testing the policy itself

A policy nobody has exercised is a document, not a process. This one has been
walked through as a tabletop exercise — a synthetic fail-open (AG-013's
allowlist gap) was replayed against the steps above to confirm the routing,
the information requested and the response targets are each specific enough to
act on. It has **not** yet been exercised by an external reporter, because
there have not been any. Treat the first real report as the actual test, and
expect the policy to be revised afterwards.
