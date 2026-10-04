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
- a way to forge, truncate, reorder or drop entries from `evidence.jsonl`
  without the artefact showing it;
- a `diff` or coverage input that parses into a *different* file set than the
  patch actually contains (parser confusion is fail-open by construction);
- code execution, file write or network access **outside** the boundary
  described in [README → Execution boundary](README.md#execution-boundary),
  reached by a test the gate ran;
- a way to bypass `--require-network-isolation` or `--sandbox bwrap` such that
  the run proceeds while the property does not hold.

## Scope

**In scope:** `src/`, `action.yml`, `.github/workflows/`, `tests/`, packaging
(`pyproject.toml`), and anything published from this repository.

**Out of scope (by design, and documented as such):**

- code the patch itself contains doing what it likes *inside* the runner's
  privileges. That is the [Execution boundary](README.md#execution-boundary),
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

- **The test code runs with your privileges.** `--sandbox bwrap` adds no
  network, a private PID namespace, a private `/tmp`, a read-only system tree
  and a repository that is the only writable path. It has no seccomp profile
  and does not drop privileges. It is containment for opportunistic code, not
  for code written to escape. For hostile input, run the whole gate in a
  container or VM.
- **`self_deception_index` measures decisions, not correctness** — and on logs
  this gate wrote it is `0` by construction (AG-029). A gate that is
  confidently wrong in both directions looks identical to one that is not.
- **Code under test shares the runner's process.** The patch can no longer
  configure pytest (AG-032), but a source module the tests import can still
  tamper with pytest in-process, or behave differently when it detects a test
  run. That is out of reach of any test-based gate; see *What a test cannot
  see* in the README.
- **Only pytest and `--test-command` are executed.** There is no mutation
  adapter yet, so non-Python stacks cannot reach `MERGE` at all — that is a
  missing feature, not a bypass.
- **Releases are not signed with a project key.** Since `2.4.0` PyPI uploads go
  through Trusted Publishing (AG-008 closed) and carry PyPI provenance
  attestations tying each file to `pypi-publish.yml` in this repository; the
  GitHub Release artefacts carry no signature. Verify them against the tag.

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

There is deliberately no email address in this policy. An address that is not
monitored is worse than none — it looks like a route and drops the report — and
`pyproject.toml` does not carry one either. When a monitored address exists it
will be added here and to the package metadata together.

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
