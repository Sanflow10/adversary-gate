# Releasing AdversaryGate

How a version reaches PyPI and the GitHub Release, and how to check what you
got. For installing, see the [README](../README.md#-installation).

## How a release is published

A release takes two steps, in this order, and only the first is done by hand:

1. **A person pushes a signed tag**: `git tag -s vX.Y.Z -m "X.Y.Z"` and
   `git push origin vX.Y.Z`. The push starts **Publish to PyPI**, which builds
   the tag's own tree — the core and the MCP package, `adversary-gate-mcp` —
   and uploads both through Trusted Publishing, so every file carries a PyPI
   provenance attestation. It refuses a tag whose `pyproject.toml` names
   another version, and it reads no API token. Before it builds anything it
   checks the tag's signature with `git verify-tag` against the key in the
   repository variable `RELEASE_ALLOWED_SIGNERS` (an `allowed_signers` line,
   kept outside the tree so a push cannot bring its own key): a lightweight
   tag, an annotated tag with no signature, a signature from another key, or
   no variable at all, and nothing is published.
2. **Actions → Release → Run workflow.** It refuses to run without that tag, or
   with one the same signature check refuses, and refuses while PyPI lacks either
   package. Then it runs the full test suite and **refuses to publish if it
   fails**, builds the wheel and sdist, installs the wheel into a clean
   interpreter, attaches them to the release, and moves the floating tag of the
   release's major line (`v3`) — only forward, so
   re-releasing an old version does not drag it back.

**Publish to PyPI → Run workflow** with a tag finishes a run that stopped
halfway: files already on PyPI are skipped, never replaced.

## History

Until 3.0.3 the Release workflow created the tag itself, with the workflow's
token, and uploaded the core alone with an API token. Events from that token
start no other workflow, so for 3.0.2 and 3.0.3 the PyPI publisher never ran:
`uvx adversary-gate-mcp` kept serving 3.0.0, the core files carry no
attestation, and those tags are not signed. Like every version before 3.0.4,
they are unsupported.

The audit this project grew from was run against `2.0.1`, which predates
every fix in the README: `--diff` and
`--coverage-json` did not exist yet (AG-002).

**AG-008 is closed (2026-10-04).** The Trusted Publisher is registered in the PyPI
project settings (`Sanflow10` / `adversary-gate` / `pypi-publish.yml`, no
environment), and `2.4.0` went up through it — OIDC, no token — with PyPI
provenance attestations attached to the wheel and the sdist. Until then the
upload step had failed on every tag with `invalid-publisher: valid token, but
no corresponding publisher`, and `2.2.0`/`2.3.0` never reached PyPI at all.

## Checking what you installed

- `pip index versions adversary-gate` shows what PyPI serves.
- Each file on PyPI carries a provenance attestation from Trusted Publishing.
- The tag `vX.Y.Z` is signed; GitHub shows it as **Verified**.
