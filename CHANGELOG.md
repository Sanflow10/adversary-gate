# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
Versionado: [SemVer 2.0.0](https://semver.org/lang/pt-BR/).

Duas regras que este arquivo obedece, e que valem mais que o formato:

1. **Uma entrada por mudança que um usuário poderia notar**, escrita quando a
   mudança acontece — não quando ela fica "pronta".
2. **A seção diz a verdade sobre o artefato.** Uma versão sem tag não é uma
   release, e este projeto já sofreu com README dizendo uma coisa e pacote
   dizendo outra (ver [`docs/AUDITORIA_CONFRONTO_v2.0.2.md`](docs/AUDITORIA_CONFRONTO_v2.0.2.md)).

Versions before 3.0.4 are unsupported; their sections below are history.

Only the latest release is distributed. The tags, GitHub Releases and PyPI
files of earlier versions are no longer published, so the routes the sections
below name for them (`@v2`, `@v2.14.0`, `adversary-gate<3`, `==2.14.0`, a
release wheel URL) no longer resolve.

O AG-008 está **fechado** desde 2026-10-04: o Trusted Publisher foi registrado
no pypi.org (`Sanflow10` / `adversary-gate` / `pypi-publish.yml`, sem
environment) e a `2.4.0` subiu por OIDC, com atestados de proveniência. A
`2.2.0` e a `2.3.0` nunca chegaram ao PyPI: lá a sequência é `2.1.0` → `2.4.0`.

From 3.0.4 a release is a signed tag pushed by hand, which publishes both
packages through Trusted Publishing (see the README's release steps). Until
then the routes varied: `2.0.0`, `2.0.1` and `2.1.0` went up by API token, and
the Release workflow uploaded by token whenever `PYPI_API_TOKEN` existed.

---

## [Unreleased]

A seção fica sem número de propósito: uma versão no `pyproject.toml` sem tag
correspondente é uma promessa de artefato que não existe, e o bump só acontece
quando esta seção vira uma com versão e data.

### Fixed

- **A universal macOS library is code.** A fat Mach-O (`cafebabe` or
  `cafebabf`, the usual shape of a `.dylib` built for Intel and Apple Silicon)
  was not among the native magics, so one in a test folder, loaded by `ctypes`,
  was skipped unjudged; single-architecture Mach-O, ELF and PE already counted.
  Reported by an external audit of 3.0.15.

### Clearer answers (no verdict changes)

Found by running 3.0.15 as a first-time user would, on a correct one-line fix.

- **A change too small to measure now says so.** Every mutant killed, too few
  of them to clear the strength floor (1/1 bounds it at 0.38; 0.75 takes 5):
  the decision stays `INCONCLUSIVE`, but the `reason` now says *"the change is
  too small to measure at this floor, which takes at least N killed mutants"*,
  and `next_steps` explains it instead of `off (--census-min-mutants 0)`. The
  step names no flag: how the floor is judged is the operator's decision, and
  an agent reads that list. A survivor still reads as weak tests. When the
  operator turned the census on, its own reason (for example *"1 mutant(s),
  below the minimum of 3 for a census"*) is kept in that step.
- **README: "First run, in one minute"**, a copy-paste script from a git
  repository, showing the answer a one-line fix gets and why, and a "Small
  changes" section with the operator's options.
- **Ledger: external findings that do not hold.** A new section 23 in
  `ERRORS_AND_INCONSISTENCIES.md` records F-003 ("a race between mutation
  lanes"), checked against the code: one lane by default, and each opt-in
  lane on its own copy. The real risk of that family stays AG-043.
- **Ledger: external findings that hold in part.** A new section 24 records
  F-004 (the rewritten-test scan repeats for every claim, uncached). Measured
  at about 0.3 s per call on this repository: a slowdown on very large trees
  that ends at most in a timeout `INCONCLUSIVE`, never a wrong `MERGE`. The
  fix is planned for the release after this one.

---

## [3.0.15] — 2026-10-22

Holes found by an audit of 3.0.14 and confirmed with crafted repositories,
plus one found by running the gate on this release's own branch. Each change
has a test that fails on 3.0.14.

> **Stricter verdicts.** With the full suite off, a patch that deletes or
> changes a baseline test file now has that file's baseline tests run on its
> code; a file hidden from git by an index flag, a test helper kept in `.tox`
> or `.nox`, and a module named like an installed pytest plugin are now
> judged where they could reach `MERGE`.

### Security

- **A deleted baseline test is run even with the full suite off.** The
  claims run the baseline's version of their own test only, so with
  `--full-suite-path ''` every other test in a baseline test file the patch
  deleted or changed was judged by nothing. A patch could break code and
  delete the one test that said so, and merge. Those files now run, any
  Python file whatever its name, the baseline's version on the patch's code:
  failing there while passing on the baseline (claims deselected) is `BLOCK`;
  any other failure, or more tests skipped or xfailed than on the baseline
  (a test that skips itself once the patch drops what it looks for), is
  `INCONCLUSIVE`. The run is recorded as `execution.rewritten_tests_run`.
- **A file hidden from git is in the diff.** `verify_repo` read the change
  with `git diff`, which obeys `git update-index --assume-unchanged` and
  `--skip-worktree`, while the patch tree copied the working tree as it was.
  A data file a module reads, changed and marked so, ran in every test yet was
  in no diff, and the patch merged. The diff now reads a private copy of the
  index without either mark; the repository's own index is left as it was.
- **A test helper in `.tox` or `.nox` comes back from the baseline.** The
  baseline oracle puts every baseline test file back over the patch, but it
  skipped those two directories by name, so a helper a repository kept in
  `tests/.nox/` stayed the patch's, and a patch that loosened it merged with
  the bug in place. The environments tox and nox build there are still
  skipped, by their `pyvenv.cfg`.
- **A module named like an installed pytest plugin is the harness.** pytest
  imports every `pytest11` plugin by name, and `python -m pytest` puts the
  tree first on `sys.path`, so a `pytest_ordering.py` the patch added at the
  root ran in place of the installed plugin: three lines cleared the one
  failure that showed the bug, and the patch merged. The plugins installed for
  the interpreter that runs the tests (`--python`) now join the standard
  library and the runner's modules in the shadowing check.

### Fixed

- **A run nested in a tighter limit starts.** Each limit the runner sets was
  asked for as is, and an unprivileged process cannot raise its hard limit:
  `verify_repo`'s coverage pass asks for 4096 open files, and run inside a
  test the gate was judging (256) it died in `preexec_fn` before pytest
  started. Each limit is now capped at the inherited hard one, which is the
  stricter value. Found by running the gate on its own 3.0.15 branch.

---

## [3.0.14] — 2026-10-19

Holes found by a read-only audit of 3.0.12 and confirmed with crafted
repositories. Each change has a test that fails on 3.0.12. There is no 3.0.13.

> **Stricter verdicts.** A claim whose run passed nothing (every test skipped
> or xfailed), a changed native library or compiled module in any directory, a
> changed document next to a module that starts a child process, and a changed
> data file a test names are now `INCONCLUSIVE` where they could be `MERGE`.

### Security

- **A claim that skipped itself is not a pass.** pytest exits 0 when every
  selected test is skipped or xfailed. A baseline test marked
  `skipif(not hasattr(pay, "LEGACY"))` skipped once the patch dropped the
  flag and counted as passing on both sides, so the guarantee it held
  vanished. A pytest run with no count line that says `passed` is now
  "nothing ran".
- **Native code in a test or build directory is code.** A `.so` under
  `tests/_bin/` behind `sys.path.insert` held the logic and, test-pathed, was
  never judged. Compiled modules and native libraries (by name or by their
  ELF, PE or Mach-O header) now count as unjudged source wherever they sit.
- **A document a child process runs is code.** `subprocess.run([sys.executable,
  "rules.conf"])` ran a file of plain assignments, which is not counted as
  Python. A module that starts a process now makes changed documents count,
  as one that calls `exec` already did.
- **A data file a test names is judged.** The code and `golden/expected.json`
  rewritten together kept the old test passing and merged. A changed data
  file (JSON, YAML, CSV, text and the like; not a document such as a README)
  that the diff names and whose name, stem or folder a test mentions now
  counts as unjudged source.

## [3.0.12] — 2026-10-18

Holes found by a read-only audit of 3.0.11 and confirmed with crafted
repositories. Each change has a test that fails on 3.0.11.

> **Stricter verdicts.** A patch that changes a document or data file and also
> runs source text (`exec`, `compile`, `runpy`, a file loader), or adds a zip
> that holds a module, or writes a line whose every mutant stops the tests
> from collecting, is now `INCONCLUSIVE` where it could be `MERGE`.

### Security

- **The MCP server no longer runs the checkout's modules inside the gate.** It
  started the gate with `python -m`, which puts the working directory first on
  `sys.path`; an MCP client starts the server in the checkout the agent
  writes, so a `json.py` there ran in the judge. The Action already ran with
  `python -I`.
- **A claim cannot count its runs.** The mutants ran in one tree and only the
  mutated file was put back, so a test that kept a counter in a file there
  passed the unmutated run and the canary and failed every mutant: a test
  asserting `is not None` scored 12/12 and merged. Every run now starts from a
  fresh copy, and the claims run once more, unmutated, after the mutants; if
  they fail then, the score is not measured.
- **A zip that holds a module is code.** `sys.path.insert(0, "deps.zip")`
  imports from it whatever the file is called (`logo.png` works too), and as a
  non-source suffix or a test fixture it was never judged: the logic sat
  inside it behind one covered import line. Decided by content now, and
  reported as unjudged source.
- **A document Python runs is code.** `exec(open("NOTICE.txt").read())` made a
  text file the logic, and documents are not counted. A changed document or
  data file now counts as unjudged source when it parses as Python and
  defines something, or when a changed module runs source text.

### Fixed

- **A passing run is never rewritten to a harness error.** A run that exited 0
  but printed `pytest: error:` became "stillborn", so a surviving mutant left
  the score instead of lowering it.
- **The "turns the patch back into the baseline" skip is for one-file patches
  only.** In a patch of two files, a `>` flipped in the second one was skipped
  as the baseline, though the claim failed on the baseline because of the
  first; that line went unbroken.
- **A line whose every mutant is stillborn is a blind spot.** A mutant that
  breaks the import (`HOSTS = ["a"] + ["0.0.0.0/0"]`, `+` to `-`) stops
  collection; such a line was left out of the ratio with a note, and the
  other lines' kills merged a change no claim had been tried against.

## [3.0.11] — 2026-10-18

Holes and false verdicts found by reading the code and by crafted repositories
run through the MCP server. Each change has a test that fails on 3.0.10.

> **Breaking for MCP callers.** `verify_repo` now refuses an agent-chosen
> `base_ref` that is not on a remote-tracking branch (a local `HEAD` or an
> unpushed commit) and answers `inconclusive` without running anything. Pass a
> pushed ref such as `origin/main`, or have the operator set
> `ADVERSARY_GATE_BASE_REF`. A repository with no remote cannot be judged from
> an agent-chosen base. The patch side is also no longer the working tree but a
> copy of what a commit would hold: files git ignores are not in it.

### Security

- **The MCP server judges what a commit would hold.** The patch side was the
  working tree itself, so a file kept out by `.gitignore` or
  `.git/info/exclude` ran in every test while appearing in no diff, no
  coverage and no denylist check, and a `MERGE` described code that would not
  be committed. It is now a copy of the tracked files plus the untracked ones
  git does not ignore; the coverage pass runs there too.
- **A `pyvenv.cfg` hides only a virtualenv's own parts.** The harness check
  skipped any directory holding one, so an empty `tests/pyvenv.cfg` (left out
  of the diff) made a `tests/conftest.py` beside it invisible, and that
  conftest could make every mutant pass. Only `bin`, `lib`, `Scripts`,
  `include` and the like are skipped now, and a claim inside them is refused.
- **`.gitattributes` no longer hides a file's lines.** The MCP server and the
  Action diff with an empty tree as the attribute source (git 2.40+), so
  `-diff` or `binary` on a `settings.json` cannot turn its hunks into "Binary
  files differ". The gate also refuses a caller's diff that calls a file
  binary when it is text on the patch side, or was text on the baseline (a
  text file rewritten with one NUL byte).
- **A new file in another encoding stays in the MCP diff.** An untracked file
  that was not UTF-8 counted as binary and left the diff, so its lines were
  never counted (a latin-1 `settings.json` measured 1.0 coverage). Only a NUL
  byte makes a file binary now.
- **Git-quoted paths are unquoted.** A path outside ASCII came through as
  `"b/caf\303\251.py"`, which no path check recognised. The diff is written
  with `core.quotepath=false` and the parser undoes git's quoting.
- **A hunk shorter than its header is refused.** It swallowed the next file's
  header and lines, which then vanished from coverage and the path checks.
- **AG-047: more ways the product reaches a test-pathed module.** Attribute
  chains from an imported name, a test package imported whole, and
  `importlib.import_module` / `__import__` with a literal name.
- **The Action runs its own gate** with `python -I`, not a copy the PR could
  put on `PYTHONPATH`.
- **MCP `mergeable` needs the gate's own `merge`.** Exit 0 with no parsable
  payload, or next to another decision, was reported as mergeable.
- **`--aggression paranoid` is refused.** It was never implemented and ran as
  `normal`. The unused quarantine store is removed.

### Changed

- **The full suite gets a second run before it blocks.** The patch side runs
  alongside the mutants; when it failed there and the baseline passed alone,
  the result was `BLOCK`. It now runs again alone: still failing is `BLOCK`,
  passing is `INCONCLUSIVE` (a time limit under load, or a flaky test).
- **A suite red on both sides says so** in the reason and in `next_steps`,
  instead of quoting a claim that held.
- **A mutant that rebuilds the baseline is not counted,** and the reason says
  when that left a changed line with no mutant (`baseline_mutants_skipped`).
- **`--mutate-constants`** adds integer mutants (`n` → `n+1`); off by default.
- **The MCP coverage pass uses `timeout_seconds`**, not a fixed 600 s.
- **Changed rows are counted at newlines only,** as Python counts them.
- **A low coverage names the configuration and data files** it counted.
- **`--aggression` and `--rounds-used` help** says the rounds are the
  caller's count, never measured; with the defaults `nuclear` decides as
  `normal`.

---

## [3.0.10] — 2026-10-17

Two fixes, both found by reading the code. Each has a test that fails on 3.0.9.

### Security

- **AG-049: a recursive deny glob now matches at the root.** The fallback
  that lets `**/X` match a root-level path stripped `**/` with `str.lstrip`,
  which removes a set of characters, not a prefix. `**/*.env` became the
  literal `.env`, so a root-level `app.env` produced no violation while
  `sub/app.env` did, and nothing said the rule had not applied. It now uses
  `removeprefix`. The other recursive globs only matched at the root because
  a root-level twin was also in the list; a test now checks every one.
- **The bwrap sandbox no longer exposes the whole user base.** To keep pytest
  importable with `HOME=/tmp`, the runner bound `site.USER_BASE` (usually
  `~/.local`) read-only. That also exposed `share/` and `state/`, where other
  tools keep credentials, and since the repository bind is writable a test
  could copy them into the tree. Only the user site-packages and `bin/` are
  bound now. The README sentence on what the guest sees was wrong and is
  corrected.

---

## [3.0.9] — 2026-10-15

One hole in the harness check, found by reading the code, and three gaps
where the gate said too little. Each change has a test that fails on 3.0.8.

### Security

- **AG-048: a module that can shadow a real one is the harness.** `python -m
  pytest` searches the tree's root first, pytest adds the directory of every
  test module and conftest that is not a package, and a `pythonpath` setting
  or a test's own `sys.path.insert` can add more. An `inspect.py`, `json.py`
  or `pytest/` package the patch added in any such directory ran in place of
  the real module, on the patch side only (measured with a harmless marker
  module on pytest 9.1.1), and no harness check named it. The tree comparison
  now checks every directory that is not a package and treats a module or
  package there with a standard-library name, or the name of a module pytest
  loads, as a harness change when the patch adds or changes it. A directory
  without `__init__.py` is not counted (a namespace never wins over a real
  module), virtualenvs are skipped, and a file the baseline already had is
  compared, not refused. The AG-047 resolver also knows a module on pytest's
  path by its bare name, so a `tests/requests.py` the product imports as
  `requests` is product code, measured and mutated. Not reproduced as an
  attack; the decision now names the file. Tests:
  `TestAModuleTheRunnerWouldImportIsTheHarness`,
  `test_a_standard_module_name_on_the_import_path_is_not_a_merge`,
  `test_a_module_on_pytests_path_is_matched_by_its_bare_name`.
- **Claims cut by `--max-claims` with the full suite off are not a `MERGE`.**
  The tests discovery cut were judged by nothing, and the kept claims could
  merge. That is now `INCONCLUSIVE`, with a `raise_limit` step.

### Changed

- **A file the patch only shortened is named.** Nothing in it is left to
  mutate, and the other files' score stood for the patch without a word. It is
  listed as `mutation.removal_only_files` and named in the reason; it does not
  block, since removing an unused import is the common case.
- **Stillborn mutants are named in the reason,** not only counted in the
  artefact.
- **A reason no longer invents stillborn mutants.** When the changed lines
  held only statement-level mutants, none counted, and the reason said "every
  mutant was stillborn (exit codes [])" with none stillborn. It now says that
  no mutant could be counted, and why.

---

## [3.0.8] — 2026-10-15

The low-severity items of the same internal audit as 3.0.7. Each code change
has a test that fails on 3.0.7 and passes here. None was reported from outside.

### Security

- **AG-047 reads more of what the product imports.** The resolver that tells a
  product module wearing a test's name from a real test now follows a chain
  (product -> `tests/a.py` -> `tests/b.py` makes both product), matches a
  top-level module under `src/` by its import name (`src/test_util.py` as
  `test_util`), and reads a module in another encoding (`# coding: latin-1`),
  which it used to skip. Tests: `test_testpaths_content.py`.
- **MCP: an untracked symlink is its target, and a FIFO is not opened.** The
  diff of untracked files followed symlinks, so a link put a file from outside
  the repository into the diff, and read anything, so a FIFO hung the server.
  A symlink is now one line with its target, as git stores it; anything that
  is not a regular file is left out. Tests: `TestAnUntrackedFileIsReadAsGitWould`.
- **MCP: `@file` in `pytest_args` is refused like any option.** pytest 8.2+
  reads further arguments from it.
- **CI's token reads the repository and nothing else** (`permissions:
  contents: read`); every workflow must declare its permissions.
- **Third-party actions are pinned by commit,** the PyPI publisher among them,
  and the tools that build, test and scan releases are exact versions.
- **The floating `v3` tag follows signed tags only.** An unsigned `v3.9.9`
  counted as the newest 3.x and kept `v3` from moving again.

### Fixed

- **A failing `--report` no longer changes the exit code.** The decision was
  printed, then the metrics report raised, and the exit code became a usage
  error under a JSON that said `BLOCK` or `MERGE`. The report's failure is now
  a warning on stderr.

---

## [3.0.7] — 2026-10-15

Fixes from an internal read-only audit of 3.0.6. Each one has a test that
fails on 3.0.6 and passes here. None was reported from outside.

### Security

- **The MCP tool's coverage pass runs inside the operator's boundary.**
  `verify_repo` ran the patch's tests for coverage as a bare subprocess with
  the server's whole environment, outside `--sandbox bwrap` and every resource
  limit, before the gate started. It now goes through the gate's runner: the
  policy's sandbox and network-isolation requirement, the minimal environment,
  and the coverage data directory as the one extra writable path. The report
  is no longer extracted from inside the repository, where a `json.py` the
  patch added would have run. Tests: `test_integrations.py`.
- **The Action's `base-sha` coverage pass runs inside the gate's boundary.**
  `coverage-command` ran as a plain shell step with the job's whole
  environment, outside `sandbox: bwrap`, able to write the baseline the oracle
  reads and the gate's own install. It now runs through the gate's runner
  (`adversary_gate.integrations.coverage_pass`): the `sandbox` input, the
  minimal environment plus `pass-env`/`env`, and one fresh writable directory
  for the report. This bounds the run; the report itself is still produced by
  the patch's code running inside coverage.py, as it must be for coverage to
  be measured. Tests: `test_coverage_pass.py`.
- **Compiled modules are code.** `.so`, `.pyd`, `.pyc` and other files a
  runtime loads were on the not-code list, and Python imports `rules.so`
  before `rules.py`: a change shipped as a compiled file beside the source was
  never judged. They now count as source the gate cannot judge
  (`INCONCLUSIVE`), and the tests never read bytecode from the tree's
  `__pycache__`.
- **Changed decorators, default values and base classes are broken.** These
  lines run on import, so coverage counted them as run with no test looking,
  and they had no mutant. A decorator is now replaced by `lambda f: f`, a
  default by `None`, a base class by `object`. Lines that still get no mutant
  are listed in `mutation.unmutated_lines`; the README says plainly that a
  listed line does not stop a `MERGE`.
- **The diff is read by its hunk counts.** An added line whose text starts
  with `++` (spelled `+++ y` in the diff) was read as a file header and
  dropped, and a removed `-- x` followed by an added `++ y` looked like a new
  file; the diff was also split with `str.splitlines`, which breaks on
  characters such as a form feed that end a line neither in a diff nor in
  Python. Each moved later changed lines onto other rows, where a line that
  ran could cover one that did not. Tests: `test_diff_parsing.py`.
- **The canary does not retry a failure.** A claim that failed on the neutral
  copy was run again and the second run decided, so a claim that read the
  code and failed only the first time passed as one that runs it. Only a run
  a limit stopped is retried now. Tests: `TestTheCanaryDoesNotRetryAFailure`.
- **A patch may not write `pyvenv.cfg`.** The harness check skips a
  virtualenv inside the checkout, recognised by its `pyvenv.cfg`, so a patch
  that wrote one hid every `conftest.py`, `.pth` or pytest config beside it. A
  `pyvenv.cfg` in the diff is now a protected path; a virtualenv CI creates is
  not in the diff and stays skipped. Tests:
  `TestAPatchCannotWriteAVirtualenvMarker`.
- **A release tag's signature is checked, not assumed.** "A person pushes a
  signed tag" was a convention: `pypi-publish.yml` checked the tag's name and
  version and nothing else, so a lightweight or unsigned `vX.Y.Z` tag went to
  PyPI with full attestations, and `release.yml` refused a lightweight tag but
  passed an annotated one with no signature (`git tag -a`). Both now run
  `git verify-tag` against the key in the repository variable
  `RELEASE_ALLOWED_SIGNERS`, kept outside the tree so a push cannot bring its
  own key; with no variable, nothing is published. SECURITY.md carries the
  release key. Tests: `TestOneRouteToPyPI` (static) and
  `TestTheTagSignatureStepRuns`, which runs the publisher's step as written
  against tags signed by the release key, by another key, unsigned and
  lightweight.

### Added

- **MCP: a pinned baseline can be made mandatory.** Unpinned, `verify_repo`
  judged against the `base_ref` the agent passed and only labelled the answer
  `"baseline_chosen_by": "agent"`, and whoever picks the baseline picks the
  oracle. With `ADVERSARY_GATE_REQUIRE_PINNED_BASE=1` and no
  `ADVERSARY_GATE_BASE_REF`, it answers `inconclusive` and runs nothing;
  `gate_policy` reports `baseline_pin_required` and stops listing `base_ref`
  among the agent's controls. Off by default: a server that relies on
  `base_ref` keeps working. Tests: three in `TestVerifyRepoTool`.

### Changed

- **The Docker image runs as an unprivileged user.** It ran as root, so the
  tests it ran -- the patch's code -- ran as root inside the container. It now
  runs as uid 10001, and CI fails if the image ever runs as root again. git's
  ownership check is switched off inside the image, because a mounted
  repository belongs to another uid. The README's *Execution boundary* gains
  a recipe for running a hostile patch: no network, no capabilities, a
  read-only root, a throwaway copy of the repository.

### Tests

- **The 3.0.6 fail-open, end to end.** 3.0.6 was verified at the mechanism
  level only. Now reproduced through the CLI: `calc.py` imports `rate` from
  `test_rules.py`, and the patch changes `rate()` from 2 to 3 next to an
  honest, covered edit. Measured on 3.0.5: the claim was judged with the
  baseline's `test_rules.py` put back and read `no_regression`; the
  collateral suite still caught it (BLOCK), and with it off
  (`--full-suite-path ""`) the answer was **MERGE, exit 0**. On 3.0.6 the
  claim itself fails on the patch (`regression`, oracle `patch`): BLOCK with
  or without the suite. Tests: `TestTheDisguisedModuleEndToEnd` in
  `test_testpaths_content.py`.

---

## [3.0.6] — 2026-10-14

A fail-open an audit raised against the test-file classification: a product
module that wears a test's name was judged as a test and never measured. It is
not reproduced here as an end-to-end attack; the fix is verified by tests at the
mechanism level.

### Security

- **A test file's name is confirmed against who imports it.** The gate decided a
  file was a test by its path alone -- a `test_*`/`*_test.py` name or a `tests`
  directory. A product module wearing one of those names, imported by the code
  under test, was kept out of coverage and mutation and reverted to the baseline
  by the oracle, so a change to it was never judged -- a route, the audit noted,
  to `MERGE` behind a bent assertion. Now a test-pathed Python module that a
  non-test module imports is treated as source: measured, mutated and judged,
  not reverted. A helper imported only by other tests stays a test;
  `conftest.py` and `--test-support` are never reclassified. The detection reads
  imports from the AST, so a module reached only through a path the resolver
  does not match, or through a dynamic import, is not caught; SECURITY.md says
  so. Tests: `test_testpaths_content.py`.

---

## [3.0.5] — 2026-10-14

Two false `INCONCLUSIVE`s and one Android default. Nothing here lets a patch
reach `MERGE` on less evidence than before. Each change has a test that fails
on 3.0.4.

### Fixed

- **`\ No newline at end of file` is not a line.** The diff parser counted
  git's marker as a context line, so every line the patch added after it was
  numbered one row too far down. When the old file ended without a newline, a
  patch that rewrote its last line and added more measured 1/2 although every
  new line ran — `INCONCLUSIVE` — and `next_steps` named a line past the end
  of the file. The marker is skipped. Tests: `TestNoNewlineMarkerIsNotALine`
  in `test_gate.py`.
- **The MCP tool's time limit leaves room for the gate it runs.**
  `verify_repo` defaulted to 900 s, the gate's own limit for one full-suite
  run alone, before the claims and up to 60 mutants; a run past it came back
  `inconclusive` with only the exception's text. The default is 3600 s. A run
  past the limit says so, with a `raise_limit` next step naming
  `timeout_seconds`, and the Hermes example's client timeout (3700 s) no
  longer cuts the gate short. Tests: `TestVerifyRepoTimeLimit` in
  `test_integrations.py`.

### Changed

- **Android: no address-space limit by default (#4).** `RLIMIT_AS` aborts
  every test process on Android, so with the defaults every run there was
  unrunnable until the operator passed `--memory none --full-suite-memory
  none`. On Android (`sys.platform == "android"`, or `sys.getandroidapilevel`
  present) both now default to none; a limit that is asked for still applies,
  and the artefact records `execution.android`. Elsewhere the defaults stay
  512M and 4G. Tests: `TestAndroidHasNoAddressSpaceLimitByDefault` in
  `test_execution_and_claims.py`.

---

## [3.0.4] — 2026-10-13

From here on this file is written in English.

Closes six ways to get a `MERGE` the evidence did not earn and two false
`INCONCLUSIVE`s, found by an audit of 3.0.3, and the release route that left
`uvx adversary-gate-mcp` on 3.0.0. Each code fix has a test that fails on
3.0.3.

### Changed

- Documents and comments no longer carry local paths (`/home/...`); MCP
  client lists read "any MCP client", and test fixtures use neutral model
  labels.

### Fixed

- **A branch that shares its line with its guard is measured.** On 3.0.3, with
  default flags, a patch that turned `return x + 1` into
  `return 999 if x == 0 else x + 1`, next to two honest and tested refactors,
  reached `MERGE` — 5/5 mutants killed, lower bound 0.753, diff coverage 1.0 —
  while `value(0)` went from 1 to 999. Coverage is per line and the line ran,
  through the `else`; the guard's only operator mutant (`==` → `!=`) sends the
  tested input into the hidden arm, so the test killed it and the kill counted
  as strength. The same held for `x == 0 and 999 or x + 1`, for the ternary
  split over two lines, and for `if x == 0: return 999` with the body on the
  header's line. A conditional expression is now also replaced by each of its
  arms, an `and`/`or` loses its last operand, and an `if` or `case` whose body
  sits on the header's line is switched off (`kind: "branch"`, first on its
  line). A survivor means a branch on a changed line that no claim takes: it is
  listed in `mutation.blind_spots`, `suite_strength_unverified` is `true` and
  the decision is INCONCLUSIVE, whatever the other mutants scored (the ratio
  stays what the operator mutants measured); `next_steps` names the line.
  Killed, these mutants stay out of the ratio, so they cannot pad it. Two shapes stay open and are written down in the README:
  a special case kept in data (`{0: 999}.get(x, x + 1)`), and a body on its own
  line with an operator on it, inside the coverage floor's tolerance and
  outnumbered by honest kills. Tests: `test_hidden_branches.py`;
  each fails on 3.0.3.
- **Every changed line is broken at least once.** The 12 mutants were the
  first 12 changed lines in file order, and nothing said which lines were left
  out: twelve whitespace-only, tested changes followed by three untested
  boundary changes (`>=` → `>`) merged at 12/12. A line no operator reaches
  was never broken either: `return a` → `return b` merged beside a tested
  line, with `fix_proven`. Now each changed line gets its first operator site
  or a statement-level mutant (a call on its own line and an `assert` are
  listed in `mutation.unmutated_lines` instead), the sample is filled to 12,
  and `--mutation-max` (default 60, was 12) is a ceiling: a change that needs
  more to break every line once is not measured, and the reason says how far
  to raise it (`mutation.mutants_needed`). A statement-level survivor is a
  blind spot too, like a branch one.
- **A rewritten claim test meets the mutants as it was judged.** The verdict
  read a test the patch rewrote from the baseline's copy, but the mutants ran
  the patch's: asserts the patch added to `test_total` killed 6/6 while the
  baseline's copy called none of the changed functions -> `MERGE`. Claims
  judged with the baseline's copy now run in the oracle tree (the patch's code,
  the baseline's tests), the rest in the patch's tree; a mutant is killed when
  either side fails.
- **A changed file that is not UTF-8 is not skipped.** A
  `# -*- coding: latin-1 -*-` module changing `>=` to `>` was never read by the
  mutation engine, and the other files' 6/6 stood for the patch -> `MERGE`. The
  strength is now not measured, and the reason names the file.
- **The canary breaks a piece of the changed line, too.** It only appended a
  comment, so a new test asserting `"return a + b * a - b + a * b" in source`
  passed it and "killed" every mutant -> `MERGE` with `fix_proven`. The copy
  now also puts a space after each token a mutant would swap. A claim that
  normalizes the text before comparing can still pass; README says so.
- **The canary's probe no longer shows in `locals()`.** 3.0.1's
  `__adversary_probe__ = 0` stayed bound, so an honest test of
  `return locals()` failed on the copy and the patch was INCONCLUSIVE as
  "reading the code". The probe is deleted on the same line.
- **The coverage report reads the project's config again.** 3.0.3 reported
  through the Action's rcfile (or a report-only one in
  `prepare_evidence.sh`), which replaces the project's config: with
  `[tool.coverage.run] data_file = "cov/data.db"` the run wrote one file and
  the report looked for another, "No data to report", INCONCLUSIVE. The
  default commands use `coverage json --ignore-errors`; the discover path,
  whose run also uses the Action's rcfile, is unchanged.
- **One route to PyPI, from a signed tag.** 3.0.2 and 3.0.3 were released by
  the Release workflow, which created the tag itself with the workflow token
  and uploaded the core alone with an API token. Events from that token start
  no other workflow, so Publish to PyPI — the only workflow that builds the
  MCP package — never ran: `uvx adversary-gate-mcp` and the MCP registry kept
  serving 3.0.0, pinned to the core 3.0.0 and missing every fix of 3.0.1. The
  core files of 3.0.1–3.0.3 carry no provenance attestation, and the tags
  `v3.0.2` and `v3.0.3` are not signed. Now a person pushes a signed tag, the
  push publishes both packages through Trusted Publishing, and the Release
  workflow refuses to run without that annotated tag or while PyPI lacks
  either package; neither workflow reads an API token, and Publish to PyPI
  runs the suite before it builds. SECURITY.md states the
  attestation gap. Tests: `TestOneRouteToPyPI` in
  `test_release_consistency.py`; five of its six fail on 3.0.3.

---

## [3.0.3] — 2026-10-12

Um frame compilado sem arquivo atrás não mata mais o relatório de coverage.
Os testes de canário deste repositório executam
`compile(..., "m.py", "exec")` — o nome é rótulo, não arquivo — e o coverage
registrava esses frames como `m.py` na raiz do workspace; o `coverage json`
se recusava a escrever o relatório inteiro por causa deles, e o job
"Composite action discovers claims" do CI morria sem gravar evidência alguma
desde o 3.0.1 (runs 89–91 e o push da correção de versão). A Action e o
`prepare_evidence.sh` passam a gerar o relatório com `ignore_errors = True`:
o arquivo sem origem vira aviso, e o relatório sai. O contrato fica em
`test_execution_and_claims.py`; a prova de execução é o próprio job dogfood,
que roda a ação sobre esta suíte envenenada.

Leva adiante o conteúdo da `3.0.2` (uma versão em todo lugar). O CLI no PyPI
não era afetado — ele só lê o JSON que o usuário gera —, mas a Action em
`@v3` e o caminho do MCP passavam pelos comandos default corrigidos aqui.

---

## [3.0.2] — 2026-10-12

Consistência de versão. A `3.0.1` foi publicada com o bump só no `pyproject.toml`:
`__init__.py`, `README.md`, `demo/demo.py`, `site/index.html`,
`packaging/mcp/pyproject.toml` e `server.json` seguiam dizendo `3.0.0`. A
`tests/test_release_consistency.py` — escrita para pegar exatamente esse defeito —
acusava 8 testes em todas as versões do CI (runs 89, 90 e 91), e o wheel no PyPI se
reporta como `3.0.0` pelo `__version__`: o `METADATA` diz `3.0.1`, e os dois valores
convivem no mesmo install. O PyPI não substitui arquivo de versão publicada, daí
esta versão existir.

O comportamento do gate não estava entre os 8: 396 testes de lógica passavam em
todas as versões. O que faltava era o artefato dizer a mesma versão em todos os
lugares — o defeito que a própria suíte de consistência foi escrita para impedir
(ver o cabeçalho de `test_release_consistency.py` e o AG-026). O release da `3.0.1`
no GitHub recebeu a nota de correção no corpo.

### Alterado

- Versão para `3.0.2` nos sete lugares que a suíte de consistência mede, mais os
  pins de instalação do README e do site.
- A seção da `3.0.1` abaixo ganhou data e a nota do drift: o corpo já estava escrito
  sob `[Unreleased]`, com "Nenhuma foi publicada", enquanto a tag e o pacote já
  existiam havia horas.

---

## [3.0.1] — 2026-10-12

Fecha 7 formas de enganar o gate que a `3.0.0` não pegava. A tag saiu às 18:45 UTC.

### Corrigido

Estas correções valem a partir da 3.0.0 e não reeditam a tag `v3.0.0`. Cada conserto
de código tem teste que falha na 3.0.0 (conferido rodando os testes novos contra ela);
os textos de ajuda, a nota do Android e o CI não têm teste. O bump de versão saiu
incompleto — o drift está na `3.0.2` acima.

- **Arquivo novo que não pode ser pareado deixa a força não medida** (B). Acima de 400
  arquivos `.py` ou 5000 linhas na baseline, um módulo que o patch cria não era mutado,
  e a força saía medida sobre o resto. Um patch misto chegou a MERGE com o código novo
  sem julgamento.
- **A referência precisa passar, com ou sem limite de tempo** (D, UX-4). Se as claims
  falham sem mutação, a força não é medida: um mutante que falha do mesmo jeito não é
  morte. Sem `--timeout`, a referência era pulada e o canário marcava claims que já
  falhavam como sensíveis ao texto.
- **Claim que lê o arquivo, a árvore inteira ou o bytecode da função alterada não passa
  no canário** (A). O canário agora também acrescenta, no bloco de cada instrução que o
  patch escreveu, uma atribuição sem efeito, que muda a árvore e o bytecode. Uma claim
  que compara o hash da AST passava com o canário de comentário e dava MERGE com força
  1,0. O conserto é parcial: uma claim que lê um pedaço menor da árvore, como uma
  expressão só, ainda passa. A atribuição nunca vai antes de uma docstring nem dos
  `from __future__`; uma linha como `if x: return y` a recebe antes do `if`, e uma
  linha `case`, no bloco dela. Se a cópia neutra não puder ser montada para um
  arquivo alterado, a força não é medida (antes o arquivo era pulado em silêncio).
- **O MCP não para mais com um arquivo binário alterado** (achado ao conferir o C). Na
  3.0.0 o MCP passava `--text` a todo arquivo e lia a saída do git como texto estrito:
  um PR que alterava uma imagem parava com `UnicodeDecodeError`, e um `.py` em outra
  codificação também. Agora o `--text` vai só aos `.py`, e a saída é lida em bytes.
- **Arquivo Python que o git mostra como binário é recusado** (C). Com `-diff` ou
  `binary` no `.gitattributes`, o `git diff` escreve `Binary files ... differ`, e as
  linhas alteradas somem da diff de cobertura: um PR que mudava `calc.py` oculto e
  tinha cobertura em outro arquivo dava MERGE. Agora `validate_diff` recusa (exit 3), e o
  `prepare_evidence.sh` usa `--no-textconv` e passa `--text` só aos arquivos `.py`: num
  binário, como uma imagem, `--text` escreve os bytes como linhas que nenhum teste roda.
- **A segunda linha de uma instrução conta como coberta quando a instrução rodou** (E).
  O coverage.py marca uma instrução só pela primeira linha, então a segunda contava
  sempre como não coberta. O gate agora lê o arquivo do patch para saber a que instrução
  a linha pertence. Se a instrução não rodou, a linha continua contando contra o patch.
  Docstring continua contando e nunca é coberta.
- **Documento não é código, e patch sem linha de código não mede cobertura** (E, e um buraco achado ao conferir). README, LICENSE, CHANGELOG e afins saem do cálculo. Um patch que muda documentação ou configuração e nenhuma linha de código não tem o que cobrir: a razão de cobertura fica *não medida* e a decisão não pode ser MERGE por cobertura. Um PR só com testes continua como na 3.0.0, com 1,0 por vacuidade: teste alterado é julgado pela cópia da baseline, e `conftest.py` novo é caminho protegido. Na 3.0.0, apagar uma linha do README dava cobertura 1,0, e uma claim que lia o README passava: MERGE.
- **Rótulo FIXED confirmado sem o lado do patch** (#4 do relatório de validação, resíduo do AG-043). Os dois lados de uma claim rodam ao mesmo tempo; quando a baseline falha assim, o gate agora reexecuta a baseline sem o lado do patch antes de aceitar o rótulo. A suíte completa ainda pode rodar ao lado; `--serial-sides` tira isso também. A decisão não dependia do rótulo; `fix_proven` dependia.
- **Próximos passos nomeiam o limite que matou a claim** (#1, issue #4). SIGABRT ou SIGSEGV indica `--memory` (no Android, `--memory none`); SIGKILL ou SIGXCPU indica `--timeout` e `--cpu-seconds`.
- **Mensagem de sinal correta** (F). Um processo morto por SIGABRT ou SIGSEGV é, em geral,
  o limite de memória, e a mensagem agora diz isso, em vez de mandar aumentar o timeout.
- **`--test-path .` tem mensagem própria** (G): pede um arquivo de teste, não "escapes
  repository".
- **Textos de ajuda** de `--test-path`, `--memory` e `--full-suite-memory` dizem qual
  limite cada um governa e que, no Android, `--memory none` é o caminho.
- **Android/Termux documentado** (PF-1): `RLIMIT_AS` aborta processos lá; use
  `--memory none` e `--full-suite-memory none`.
- **CI** passa a testar Python 3.13 e 3.14.

---

## [3.0.0] — 2026-10-11

A primeira versão sob AGPL-3.0-only. Traz as correções do AG-045 e do AG-046 —
as mesmas que a `2.14.2` leva para a linha MIT — e a nova licença. A tag
flutuante `v3` nasce aqui; a `v2` continua na linha 2.x.

### Corrigido

- **AG-046 — um teste que lê o código não mede nada.** Os claims passam a rodar
  também numa cópia do patch com um comentário ao fim de cada linha que ele
  escreveu; se falham ali, leem o texto do código (hash, `inspect.getsource`) e
  não o comportamento: a força fica não medida e `fix_proven` vira `false`.
  Antes, esse teste chegava a MERGE com força 1,0. A linha de decisão do
  registro de evidências (`--evidence-log`) segue a mesma regra: as contagens da
  resposta e do registro saem de um lugar só.
- **Arquivos novos recebem mutantes.** Antes, um módulo que o patch cria nunca
  era mutado. Agora é mutado nas linhas que o patch escreveu, quando dá para
  compará-lo com o baseline; renomeado ou copiado, é comparado com o arquivo do
  baseline de que veio. `mutation.added_files` diz o pareamento e se cada
  arquivo foi mutado.
- **AG-045 — o baseline do MCP é o que o id do commit contém.** O baseline passa
  a ser lido objeto a objeto (`ls-tree` + `cat-file`), com a substituição de
  objetos desligada, e cada blob é conferido contra o id que a árvore cita; o
  `ref` é resolvido uma vez por execução e o diff roda contra esse id, sempre
  como texto. `verify_repo` devolve `baseline_sha`, e `gate_policy` diz se o pin
  do operador é um id de commit (que não se move) ou um nome de ref. Limite
  mantido: um agente que executa código com o mesmo usuário ainda alcança a
  pasta temporária do gate (a fronteira já documentada).

### Alterado

- **Licença: AGPL-3.0-only, com termos adicionais.** Qualquer cópia
  modificada, inclusive oferecida como serviço, tem que publicar o código. O
  `NOTICE` traz os termos que a seção 7 da AGPL permite: manter os avisos de
  autoria, marcar a versão modificada, não usar os nomes Sanflow/Sanflow10 para
  promover derivados e nenhum direito à marca AdversaryGate. A linha 2.x, até a
  `2.14.2`, foi publicada sob MIT e continua sob MIT para quem a obteve. Licença comercial, sem as obrigações da AGPL, sob consulta.
- **Versão major por causa da licença.** Nenhum contrato da ferramenta mudou;
  o número muda para que a troca de licença não chegue em silêncio a quem segue
  `@v2` ou instala `adversary-gate` sem limite de versão. Para ficar no MIT:
  `@v2` ou `adversary-gate<3`, que recebem as mesmas correções na `2.14.2`.
  (Essas rotas não existem mais: ver o topo deste arquivo.)

---

## [2.14.1] — 2026-10-09

Corrige dois fail-open achados na revisão da `2.14.0` (ledger, seção 18). A tag
flutuante `v2` passa para a `2.14.1` sozinha quando o Release roda; quem fixou
uma versão anterior (`@v2.14.0`, `==2.14.0` ou mais antiga) deve atualizar. O
padrão da `2.14.0` podia dar `MERGE` para um teste que não confere nada.

### Corrigido

- **AG-043 — uma execução que outra execução fez falhar contava como mutante
  morto (só na `2.14.0`).** Os mutantes rodavam em paralelo e a suíte completa
  junto com eles, e só a morte por limite de tempo era refeita sozinha. Uma
  execução que falha porque outra mexeu na mesma coisa fora do repositório (um
  caminho fixo em `/tmp`, uma porta, um banco de teste) sai com exit 1, igual a
  uma detecção. Medido: um teste que não confere nenhum valor foi de 0/8 mortos
  (`INCONCLUSIVE`) para 8/8 e `MERGE`; só a suíte ao lado, com os mutantes um
  por vez, já levava 0/8 a 4/8. Agora a mutação espera a suíte terminar e roda
  um mutante por vez. Custo no caso real que mais ganhava com o paralelo
  (more-itertools d992be0, 12 mutantes): a parte do gate foi de 126,5 s para
  153,6 s, com a mesma decisão e os mesmos 12/12 mortos. As claims continuam
  rodando com os lados juntos e com a suíte ao lado: ali a interferência pode inflar o rótulo
  `fixed` (`fix_proven`), nunca a decisão; `--serial-sides` evita.
- **AG-044 — o baseline perdia o que o `.gitattributes` do projeto tira do
  pacote.** O baseline era montado com `git archive`, que aplica os atributos
  de exportação: `export-ignore` tira arquivos e `export-subst` os reescreve.
  Num projeto com `tests/ export-ignore` (prática comum para deixar os testes
  fora do sdist), o baseline saía sem os testes, um teste reescrito pelo patch
  era julgado como novo e o oráculo do baseline (AG-021) não se aplicava. Na
  Action e no servidor MCP, o baseline agora é a árvore do commit escrita por
  um índice temporário (`read-tree` + `checkout-index`): todo arquivo como foi
  commitado, como o checkout do workspace o escreveu, sem tocar no índice nem
  na árvore de trabalho.

### Adicionado

- **`--mutation-workers N`** (padrão 1): mutantes ao mesmo tempo, cada um na
  sua cópia da árvore. Só para testes que não dividem nada fora do
  repositório; recusado junto com `--census-min-mutants`, porque uma contagem
  completa exige todos mortos e lê a razão exata. `--serial-sides` força 1.

### Alterado

- **A documentação do servidor MCP diz o limite do baseline fixado:** fixar a
  ref nomeia o baseline; não é defesa contra um agente que pode escrever no
  `.git` do repositório (refs, objetos, configuração) — esse agente tem que
  rodar onde não pode, como qualquer código que roda com os seus privilégios.
- **`TestWholeFileRunsHaveSuiteLimits` deixou de depender da máquina:** usava
  `--timeout 1` fixo e falhava onde o pytest leva mais que isso só para subir
  (1,8 s com alguns plugins). O limite sai de uma execução medida na hora.

---

## [2.14.0] — 2026-10-08

**Errata (2.14.1).** Duas afirmações desta seção não valem para os mutantes.
"Cada um desses derruba um lado, o que termina em BLOCK ou INCONCLUSIVE, nunca
em MERGE" vale para os lados de uma checagem; uma execução de mutante que
outra execução fez falhar contava como **morta**, a direção do `MERGE`
(AG-043). E "a decisão ... saíram idênticos" foi medido numa matriz cujos
testes não dividem nada fora do repositório, onde isso não aparece. Use a
`2.14.1`.

O gate ficou mais rápido sem mudar o que decide. Os lados baseline e patch, a
suíte completa e os mutantes passam a rodar ao mesmo tempo. Na matriz de
cinco casos reais do more-itertools, a parte do gate em relação à `2.13.0`
caiu de 319→127 s, 177→101 s, 285→109 s e 285→108 s (a reversão 6b1907d, que
não tem nada para sobrepor, ficou em ~90 s). A decisão, `fix_proven`, as
claims e o morto/sobrevivente de cada mutante saíram idênticos. `--serial-sides`
volta tudo ao modo um-depois-do-outro.

### Alterado

- **Os dois lados de cada verificação rodam ao mesmo tempo.** A claim no
  baseline e no patch, o arquivo de testes do baseline nos dois lados (AG-021)
  e, quando o patch reescreveu testes, a suíte completa no patch e no
  transplante. As execuções são as mesmas, na mesma quantidade e em diretórios
  diferentes. Em cinco casos reais do more-itertools (reversão, correção e
  correção com asserções esvaziadas), a decisão, `fix_proven` e a
  classificação de cada claim saíram idênticas, e a parte do gate caiu
  de 285→156 s, 319→199 s, 177→119 s e 286→171 s (a reversão 6b1907d, sem par
  a sobrepor, ficou em ~90 s). O que a sobreposição pode mudar é o que a
  disputa por máquina causa: um limite de tempo de relógio estourado, memória
  para dois processos, estado que os lados dividem fora do repositório (um
  caminho fixo em `/tmp`, uma porta, um `HOME` passado com `--pass-env`).
  Cada um desses derruba um lado, o que termina em BLOCK ou INCONCLUSIVE,
  nunca em MERGE; um lado que estourou o tempo não é repetido, porque uma
  segunda chance também seria uma segunda chance para um teste que só trava
  às vezes. `--serial-sides` volta ao modo um-depois-do-outro, e o artefato
  registra o modo em `execution.serial_sides`. Quando a suíte completa passa
  no baseline e estoura o tempo só no patch, `next_steps` sugere subir o
  limite ou `--serial-sides` em vez de "corrigir o código".
- **A suíte completa roda junto com as claims e os mutantes**, não depois
  deles. Ela não depende de nada que eles encontram, então começa antes da
  primeira claim, numa cópia do patch feita nesse momento (com `.git`, para
  uma suíte que consulta o git não falhar só na cópia e virar "regressão
  colateral"), e o baseline continua rodando só se o patch falhar. A disputa
  por máquina abria um caminho para fail-open: um mutante que estoura o tempo
  conta como morto (AG-042), e um estouro causado pela suíte ao lado inflaria
  o score. Por isso um mutante que estoura o tempo enquanto a suíte ainda roda
  é repetido depois que ela termina, sozinho, e a repetição decide
  (`rerun_alone` no artefato traz o código da primeira); a repetição só pode
  trocar morto por sobrevivente ou natimorto. A execução de referência é
  repetida do mesmo jeito quando saiu lenta demais sob carga. Na matriz dos
  cinco casos do more-itertools, a decisão, `fix_proven`, as claims e o
  morto/sobrevivente de cada mutante saíram idênticos, e a parte do gate caiu
  de 192→145 s, 120→100 s, 158→106 s e 153→103 s (a reversão 6b1907d ficou
  em ~93 s). `--serial-sides` também desliga isto (suíte depois, no próprio
  diretório do patch), e o artefato registra o modo em
  `execution.full_suite_alongside`.
- **Mutantes rodam em paralelo**, metade das CPUs e no máximo 4 ao mesmo
  tempo, cada worker na sua própria cópia da árvore; o artefato traz a
  quantidade em `execution.mutation_workers` e as entradas saem na ordem do
  plano, seja qual for o worker que terminar primeiro. Toda execução teve
  vizinhas, então a mesma regra de cima vale: um mutante morto pelo relógio é
  repetido sozinho, depois que todos terminam, e a repetição decide. Um morto
  pelo limite de **CPU** não é repetido, porque tempo de CPU é do próprio
  processo e não cresce com a disputa, a não ser que a referência sem
  mutação tenha usado um terço desse limite (aí ele também é repetido). Na
  matriz do more-itertools, o morto/sobrevivente de cada mutante, as claims e
  a decisão saíram idênticos ao modo um-por-vez, sem nenhuma repetição; o
  ganho aparece onde há mutantes em número (d992be0, 12 mutantes: 145→127 s),
  e com 3 mutantes é nulo. `--serial-sides` volta a um por vez.
- **O transplante da suíte completa é copiado antes de a suíte do patch
  rodar** (antes, depois). Arquivos que a suíte do patch gerava dentro do
  repositório não entram mais na cópia que o oráculo do baseline julga.
- **A criação de cada processo de teste é serializada** (só o `fork`; a espera
  pelo processo continua em paralelo): `preexec_fn`, que aplica os limites, é
  documentado como inseguro com outras threads rodando.

---

## [2.13.0] — 2026-10-07

A Action expõe o código de saída do gate, para que um workflow possa separar
`BLOCK`, `INCONCLUSIVE` e erro sem `continue-on-error` às cegas.

### Adicionado

- **Saída `exit-code` na Action** (0 merge, 1 block, 2 inconclusive, 3/4 erro
  de uso ou do harness; vazia se a Action parou antes do gate). Um workflow que
  quer deixar `INCONCLUSIVE` passar onde a medição é impossível (C++, SQL)
  precisava de `continue-on-error`, que deixa passar `BLOCK` junto; e
  `decision` não serve para separar, porque uma execução sem veredito (exit
  3/4) também lê `inconclusive`. Receita no README. Apontado por um leitor do
  artigo no dev.to.

---

## [2.12.0] — 2026-10-07

O gate operado como agente até o `MERGE`. Mutantes rodam só as claims (AG-039);
`next_steps` diz o que transformaria cada resultado em `MERGE`; mutante que trava
os testes conta como morto (AG-042); comentário deixou de contar como linha sem
teste (AG-040); contagem completa experimental, desligada por padrão. Validação
de 06/out, 40 execuções reais do `more-itertools` (padrão e
`--census-min-mutants 1`): nenhum bug reintroduzido chegou a `MERGE`, e 7 de 8
correções reais foram provadas (`fix_proven`). Detalhes no ledger, seções 16 e 17.

### Desempenho

- A checagem de que os testes antigos de um arquivo ainda passam roda **uma vez
  por arquivo**, não uma vez por teste novo nele: ela depende do baseline, do
  patch e do arquivo, não de qual teste perguntou. `def2dab` real (três testes
  novos num arquivo): 782 → 477 s, mesma decisão.

### Corrigido

- **AG-039** — mutantes rodam os node ids das claims, não seus arquivos de
  teste inteiros: a força não pode mais ser inflada por um teste fora das
  claims que falha de qualquer jeito, e arquivos grandes não deixam todo
  mutante "natimorto". O `reason` separa mutante morto por limite.
- **AG-040** — linhas adicionadas em branco ou só de comentário (em `.py`) não
  contam mais como linhas alteradas sem teste na cobertura de diff.
- **AG-042** — mutante que faz os testes nunca terminarem conta como morto,
  desde que as claims sem mutação terminem em menos de 1/3 do limite
  (`reference_run` no artefato); senão continua natimorto.

### Adicionado

- **`next_steps`** no artefato e no `verify_repo` (MCP): o que transformaria
  um resultado em `MERGE`, em ações que um agente executa — cobrir linhas,
  matar um mutante nomeado, adicionar um teste novo, corrigir o código, pedir
  revisão humana de um teste do baseline alterado, subir um limite. Nunca
  sugere editar teste do baseline, harness, piso ou política.
- **`--census-min-mutants N` (experimental, padrão 0 = desligado):** quando a
  mutação cobriu todos os pontos da mudança e todos morreram, julga a razão
  exata em vez do limite de Wilson. Detalhes e a validação em 24 execuções
  reais no ledger, seção 16.

---

## [2.11.0] — 2026-10-05

O gate usado como agente: o servidor MCP ligado a um agente de código e chamado sobre
oito correções reais do `more-itertools`, nos dois sentidos. Antes: 4 de 8 bugs
reintroduzidos bloqueados, 0 de 8 correções provadas. Agora: 7 de 8 e 6 de 8,
nenhum `MERGE` errado em 16 execuções. Detalhes no ledger, seções 14 e 15.

```bash
uvx adversary-gate-mcp
```

### Corrigido

- **AG-033** — a suíte colateral rodava sob os limites de um teste
  (`--timeout 30`, `--cpu-seconds 10`) e morria dos dois lados em qualquer
  suíte de tamanho real; patches que só removem código voltavam `INCONCLUSIVE`
  em vez de `BLOCK`; e sob 512M de memória o pytest de uma suíte real morre
  com `MemoryError`. Novos `--full-suite-timeout` (900),
  `--full-suite-cpu-seconds` (nenhum) e `--full-suite-memory` (4G).
- **AG-034** — o `reason` explica a decisão quando ela veio da suíte colateral
  (regressão fora das claims, ou suíte morta pelo limite).
- **AG-035** — o arquivo `.git` de uma worktree ou submódulo não conta mais como
  código estrangeiro alterado.

### Adicionado

- **Teste adicionado pode provar a correção (FAIL_TO_PASS).** Um teste que o
  patch acrescenta roda contra o código do baseline; falhando lá (exit 1, em
  todas as execuções) e passando no patch, vira `fixed` e `fix_proven: true`.
  Medido: 6 de 8 correções reais do `more-itertools` (antes 0).

### Corrigido (cont.)

- **AG-036** — a rodada do arquivo de teste inteiro (teste novo num arquivo
  existente) usa os limites da suíte colateral.
- **AG-037** — teste definido numa mixin vira os nós das classes que o
  executam.
- **AG-038** — `BLOCK` pelo oráculo do baseline diz que mudança de
  comportamento intencional precisa de revisão humana do teste alterado.
- O `reason` de um `INCONCLUSIVE` com todas as claims verificadas nomeia o
  piso que faltou.

### Desempenho

- **`verify_repo` (MCP) não gera mais `coverage json --show-contexts`.** Ele
  escrevia toda linha de todo arquivo medido com todo teste que a executou —
  no `more-itertools`, 193 s e 312 MB — para um relatório do qual descoberta
  e cobertura de diff só leem as linhas alteradas. Agora lê o banco do
  coverage direto, só para os arquivos e linhas do diff (0,3 s). Medido em
  duas reversões reais: 411 → 278 s e 412 → 332 s, mesmas decisões.

---

## [2.10.0] — 2026-10-05

As ferramentas MCP dizem a quem chama quando chamar e o que acontece; o extra
`[mcp]` passa a exigir um SDK que de fato sobe o servidor.

```bash
uvx adversary-gate-mcp
```

### Adicionado

- **Título e dicas de comportamento nas ferramentas MCP** (`annotations`):
  `gate_policy` é somente leitura, idempotente e não sai da máquina;
  `verify_repo` não é somente leitura e é *open-world* — os testes do
  repositório rodam, e um `--triage` do operador manda o diff para fora.
- **Descrições das ferramentas reescritas** para quem chama: quando chamar,
  o que acontece, efeitos colaterais (os testes do repositório executam; a
  rodada de cobertura usa o repositório como diretório de trabalho), timeout
  vira inconclusive, e o que volta. `tests/test_integrations.py`
  (`TestToolMetadata`) trava as dicas.

### Corrigido

- **Piso do extra `[mcp]`: `mcp>=1.9` → `mcp>=1.14`.** Medido em 1.9.4, 1.12 e 1.13:
  o `tool()` do SDK falha ao registrar `verify_repo`
  (`issubclass() arg 1 must be a class`) e o servidor não sobe; 1.14, 1.30 e
  2.3 registram as duas ferramentas. Quem tinha um SDK antigo fixado recebia um
  servidor quebrado.
- O registro das ferramentas passa ao SDK só os argumentos que o `tool()`
  dele aceita, e nunca tenta de novo após um erro: um registro que falha
  estoura, em vez de deixar a ferramenta de fora em silêncio.

---

## [2.9.0] — 2026-10-05

O servidor MCP com um comando só, do jeito que os registros de MCP instalam
servidores Python.

```bash
uvx adversary-gate-mcp
```

### Adicionado

- **Pacote `adversary-gate-mcp`** (`packaging/mcp/`) — não traz código: fixa
  `adversary-gate[mcp]` na mesma versão e declara o executável
  `adversary-gate-mcp`. Registros de MCP iniciam servidores Python com
  `uvx <pacote>`, e `uvx adversary-gate` abre a CLI sem o SDK MCP (que é
  extra). O núcleo continua dependendo só do pytest.
- **`server.json`** — a entrada do servidor para o registro oficial de MCP
  (`io.github.Sanflow10/adversary-gate`); a linha `mcp-name` no README do
  pacote novo é o que prova a posse no PyPI.
- `pypi-publish.yml` constrói e envia os dois pacotes da mesma tag.
  `tests/test_release_consistency.py` trava a versão do pacote novo, o pino da
  dependência e as versões do `server.json` na versão do núcleo.

## [2.8.0] — 2026-10-04

Integrações com agentes: o gate como servidor MCP (Hermes, qualquer cliente MCP) e
triagem de risco com o Jev (TypeSafe AI). Nenhuma das duas pode afrouxar uma
decisão.

```bash
pip install "adversary-gate[mcp]==2.8.0"
```

### Adicionado

- **`adversary-gate-mcp`** (extra `[mcp]`) — servidor MCP stdio com
  `verify_repo` e `gate_policy`. Julga a árvore de trabalho de um repositório
  git contra um baseline: `git archive` do baseline, diff com arquivos não
  rastreados, coverage.py com contextos por teste fora do repo, descoberta de
  claims. **O agente só nomeia evidência**; política
  (`ADVERSARY_GATE_POLICY`), baseline (`ADVERSARY_GATE_BASE_REF`) e
  interpretador (`ADVERSARY_GATE_PYTHON`) são do operador. Opções em
  `pytest_args` e flags de evidência na política são recusadas.
- **Skill do Hermes** e bloco de `config.yaml`, empacotados:
  `adversary-gate-mcp --print-hermes-skill` / `--print-hermes-config`.
- **`--triage jev`** (`--triage-endpoint`, `--triage-threshold`): pergunta ao
  Jev o risco do diff. `high` confiante sobe os pisos desta execução
  (confiança 0.95, cobertura 0.90); qualquer outra resposta, erro ou timeout
  não muda nada. Registrado em `execution.triage`. O diff sai da máquina
  (64 KiB, só https) — por isso é opt-in. Cliente testado contra servidor local
  no formato publicado pela TypeSafe, não contra a API real.

---

## [2.7.0] — 2026-10-04

Fecha o AG-023 e o AG-030.

```bash
pip install adversary-gate==2.7.0
```

### Alterado — **muda decisões**

- **AG-023 — o piso de força lê um intervalo de confiança.** O piso
  (`--suite-strength-floor`, 0.75) é aplicado ao **limite inferior de um
  intervalo de Wilson a 80 %** em volta de `mortos / contados`, não à razão.
  1 de 1 morto era `1.0` e passava; agora é `INCONCLUSIVE`. **Patches com
  menos de 5 operadores mutáveis nas linhas alteradas não chegam a `MERGE`**
  no padrão. `--strength-confidence 0` (Action: `strength-confidence: '0'`)
  restaura o comportamento anterior. Artefato: `suite_strength_lower`,
  `suite_strength_confidence`, `mutation.interval`, `mutation.confidence`.
- Sítios de mutação espalhados (um por linha alterada antes de repetir), não
  mais em ordem de arquivo; `--mutation-max` padrão 6 → 12.

### Adicionado

- **AG-030 — `fixed` × `no_regression`.** `FAIL→PASS` é `fixed`, `PASS→PASS` é
  `no_regression` (`discarded` fica para teste novo). Saída: `claims_fixed`,
  `fix_proven`. Relatório de métricas separa os dois.

---

## [2.6.0] — 2026-10-04

Fecha o que o oráculo do baseline tinha deixado aberto.

```bash
pip install adversary-gate==2.6.0
```

### Adicionado

- **`--test-support GLOB`** (Action: `test-support`) — declara helpers cujo
  caminho não diz que são de teste (`testing_utils.py`, `helpers/*`). Contam
  como teste em todo lugar: fora da cobertura e da mutação, e restaurados do
  baseline pelo oráculo. Registrado em `execution.test_support`.
- **A suíte colateral também roda os testes do baseline** contra o código do
  patch quando o patch mudou testes que o baseline tinha
  (`full_suite_oracle`). Teste fora de qualquer claim, entortado para
  concordar com o bug → `BLOCK`.

### Alterado

- Claims de `--test-command` com `test-id` passam pelo oráculo (antes:
  `UNVERIFIED`).
- `is_test_path` reconhece `__tests__/`, `*.test.*`, `*.spec.*`, `*_test.go`,
  `*Test.java`/`*Tests.java`, `*Test.kt`, `*_spec.rb`, `*_test.rb`. Nenhum é
  módulo Python, então cobertura e mutação de Python não mudam.

---

## [2.5.0] — 2026-10-04

O oráculo do baseline — o item aberto do AG-021.

```bash
pip install adversary-gate==2.5.0
```

### Alterado

- **Um teste reescrito é julgado pela cópia do baseline.** Se o patch mudou o
  arquivo de teste da claim (ou qualquer arquivo de teste que o baseline já
  tinha — helpers incluídos), o gate copia a árvore do patch, devolve os
  arquivos de teste do baseline e roda a claim ali. O ataque do AG-021 (bug +
  teste reescrito) passa de `INCONCLUSIVE` (exit 2) a **`BLOCK` (exit 1)**; a
  refatoração honesta do arquivo de teste passa de `INCONCLUSIVE` a `VERIFIED`.
  Um helper entortado para concordar com o bug também é pego.
- **Teste novo num arquivo existente** — o formato mais comum de um agente, e
  até aqui sempre `INCONCLUSIVE` — é julgado como teste adicionado, depois que
  todos os testes que o baseline tinha naquele arquivo passam no código do
  patch. Se o patch entortou um deles: `REFUTED`.
- O artefato ganha `"oracle"` por veredito: `patch`, `baseline` ou
  `baseline-file`.
- `--discover-claims` não exclui mais arquivos de teste reescritos; o detalhe
  passa de `excluded_rewritten_test_files` a
  `rewritten_test_files_judged_by_baseline`.

---

## [2.4.0] — 2026-10-04

Fecha o AG-032 (fail-open) e o AG-027; AG-023 parcial; AG-029 mitigado.

```bash
pip install https://github.com/Sanflow10/adversary-gate/releases/download/v2.4.0/adversary_gate-2.4.0-py3-none-any.whl
```

**Quebra para quem importa como biblioteca:** `core.*`, `verifiers.*`,
`sandbox.*` e `cli` passam a ser `adversary_gate.core.*` etc. A CLI e a Action
não mudam.

### Segurança

- **AG-032 — o patch não configura mais o runner que o julga.** Um patch com
  bug + `.pytest.ini` (`addopts = -p plugin`) + um plugin que só mente para os
  bytes exatos do bug chegava a `MERGE`, exit 0, `suite_strength: 1.0`. Agora
  os arquivos de harness são comparados **árvore contra árvore** com o
  baseline, não pela lista de caminhos declarados: qualquer diferença deixa a
  claim `UNVERIFIED`. O denylist passou a cobrir `.pytest.ini`, `pytest.toml`,
  `.pytest.toml`, `sitecustomize.py`, `usercustomize.py` e `*.pth`.

### Alterado

- **AG-027 — um só pacote.** O código foi para `src/adversary_gate/`; o wheel
  instala `adversary_gate` e mais nada no topo (antes: `cli`, `core`,
  `sandbox`, `verifiers`, que colidiam com pacotes do projeto testado). Quem
  importava `core.gate` passa a importar `adversary_gate.core.gate`. O console
  script `adversary-gate` não muda; sem instalar,
  `PYTHONPATH=src python3 -m adversary_gate`. Novo `--version`.
- **AG-023 (operadores)** — a mutação passa a quebrar `*` `/` `//` `%` `**`,
  `+=` `-=` `*=` `/=` e `True`/`False`. Um patch `a + b` → `a * b` deixava de
  ser medido ("no mutable operator"); agora tem mutante. `*` e `/` em posição
  de sintaxe (`*args`, `import *`, marcadores de assinatura) não são tocados.
  Tamanho de amostra e intervalo de confiança continuam abertos.

### Documentação

- README: a tese está em inglês; o `self_deception_index` saiu do pitch,
  porque é 0 por construção nos logs do próprio gate (AG-029); nova seção
  *What a test cannot see, by construction* (código no mesmo processo do
  pytest, código que detecta que está sob teste).

---

## [2.3.0] — 2026-10-04

Fecha AG-024, AG-025 e AG-031: **como** os testes rodam, **em qual** Python, e
**quais** testes. Até aqui um run verificava um único teste, escolhido à mão,
no interpretador do gate, sob limites gravados no código — e a Action trocava o
Python do job inteiro para conseguir um.

```bash
pip install https://github.com/Sanflow10/adversary-gate/releases/download/v2.3.0/adversary_gate-2.3.0-py3-none-any.whl
```

### Adicionado

- **AG-031 — várias claims por run, e descoberta automática.** `--test-id`
  repete, `--claim PATH::ID` repete, e `--claim-json` aceita qualquer número de
  claims (antes: exatamente uma). A decisão é sobre todas: um `REFUTED`
  bloqueia, um `UNVERIFIED` deixa `INCONCLUSIVE`. `--discover-claims` verifica
  os testes que **executaram uma linha de código-fonte alterada**, lidos dos
  contextos por teste do coverage.py (`dynamic_context = test_function` +
  `coverage json --show-contexts`) — não adivinhados. Só linhas de código
  contam (um teste sempre executa as próprias linhas); um arquivo de teste que
  o patch **reescreveu** nunca é escolhido, porque o veredito dele seria o
  patch se corrigindo (AG-021), e aparece nomeado em `discovery`. Relatório sem
  contextos é erro de uso (exit 3), não "nenhum teste". `--max-claims`
  (padrão 10) limita o custo e o artefato diz quantas foram cortadas.
- **AG-024 — limites e ambiente configuráveis.** `--timeout`, `--cpu-seconds`
  e `--memory` (`512M`, `4G`…, ou `none` — a JVM e o Node não sobem sob limite
  de espaço de endereçamento); `--pass-env NOME` e `--env NOME=VALOR`. Valem
  para todas as execuções — claims, mutantes e suíte completa — e o artefato
  registra tudo em `execution`, com os **nomes** das variáveis e nunca os
  valores.
- **AG-025 — `--python`.** O interpretador que roda o pytest: o do projeto,
  onde as dependências estão. Validado antes de qualquer execução (um caminho
  errado é exit 3) e registrado no artefato. Com `--sandbox bwrap`, o venv e a
  instalação dele são montados somente leitura.
- **Action:** entradas `python`, `claims`, `discover-claims` (`auto` por
  padrão: liga quando nenhum teste é nomeado), `max-claims`, `timeout`,
  `cpu-seconds`, `memory`, `pass-env`, `env`; saídas `claims-total` e
  `python`. `test-path` deixou de ser obrigatório.

### Corrigido

- **AG-024 — morte por limite deixou de ser evidência contra o patch.**
  Executado: sob 512 MiB, alocar 4 GiB num teste gera `MemoryError`, o pytest
  sai com 1, e o mapa de saída lia isso como teste falhando — `BLOCK` por algo
  que o patch não fez. Exit 1 com `MemoryError`, `Fatal process out of memory`,
  `Could not reserve enough space` e afins agora é morte do harness (exit 3 →
  `UNVERIFIED` → `INCONCLUSIVE`). O erro vai no sentido seguro: uma falha real
  que imprima um desses textos vira `INCONCLUSIVE`, nunca `MERGE`.
- **AG-025 — a Action não troca mais o Python do job.** Ela fazia
  `setup-python 3.12` no `PATH`: os testes rodavam num interpretador sem as
  dependências do projeto, e todo passo depois dela herdava esse Python. Agora
  o gate vive num venv privado (`update-environment: false`), os testes rodam
  no primeiro `python`/`python3` do `PATH` que importa pytest — o do
  `setup-python` do próprio usuário — ou no indicado em `python:`, e o
  coverage.py roda nesse mesmo interpretador (instalado ao lado dele, em
  diretório próprio, quando falta — nunca dentro do ambiente do projeto).
- **Os mutantes ignoravam `--sandbox bwrap`.** A medição de força rodava cada
  mutante sem o sandbox pedido, sempre no Python do gate e sem o ambiente do
  chamador. Agora recebe as mesmas opções das claims.
- **Ícone da Action.** `shield-check` não está no conjunto do Feather que o
  branding de Actions aceita; virou `shield`.

### Alterado

- Cada execução recebe um `HOME` privado, criado para ela e apagado depois
  (a menos que `--env HOME=…` diga outro). Ferramentas que insistem em gravar
  no home (npm, cargo, caches do pip) não escrevem no repositório nem no home
  real. Os pacotes do *user site* continuam visíveis via `PYTHONUSERBASE`.
- A saída JSON ganhou `claims_total`, `claims` (um registro por claim),
  `execution` e, quando há descoberta, `discovery`. Os campos de topo
  (`outcome`, `classification`, `reason`, `evidence`) descrevem o veredito que
  **explica** a decisão — o primeiro `REFUTED`, senão o primeiro
  `UNVERIFIED` — e com uma claim só são os mesmos de antes. O mesmo vale para a
  saída `outcome` da Action.
- A mesma claim nomeada duas vezes roda uma vez só.
- **Publicar no PyPI uma release que já existe.** O `pypi-publish.yml` ganhou
  *Run workflow* com a tag como entrada. A `2.2.0` saiu como GitHub Release, mas
  a tag foi criada pelo token do próprio workflow Release, e eventos desse
  token não disparam outros workflows. Sem este caminho, a única saída era
  refazer a Release, o que o Release recusa. O workflow constrói a partir da
  árvore da tag, recusa uma tag cujo `pyproject.toml` diga outra versão, e
  autentica com `PYPI_API_TOKEN` quando o segredo existe ou por Trusted
  Publishing quando não existe.
- O aviso do Release quando falta o token deixou de mandar "rodar este workflow
  de novo", o que falharia. Agora aponta para o *Publish to PyPI*.

### Comportamento que muda

- **Um teste morto pelo limite de memória era `BLOCK` e agora é
  `INCONCLUSIVE`.** Se o limite padrão não cabe na sua suíte, aumente-o com
  `--memory` (Action: `memory`) em vez de ler o bloqueio como regressão.
- **Passos depois da Action veem o Python que *você* configurou**, não mais o
  3.12 dela. Um workflow que, sem perceber, dependia desse 3.12 (ou do
  `coverage` que ela instalava) precisa do próprio `setup-python`.
- **Sem nenhum teste nomeado, a Action agora descobre os testes** em vez de
  falhar com exit 3. Se a descoberta não encontra nenhum teste que execute uma
  linha alterada, a decisão é `INCONCLUSIVE` — nunca `MERGE` por falta de
  claims.
- `--claim-json` com mais de uma claim deixou de ser erro de uso.

---

## [2.2.0] — 2026-10-03

Tudo o que saiu de `v2.1.0`: os achados da
[auditoria de produto de 2026-10-03](docs/AUDITORIA_PRODUTO_v2.1.1.md) e as
correções que estavam na seção `[2.1.1]`. Aquela seção foi fundida aqui porque
descrevia uma GitHub Release que nunca foi publicada — não há tag `v2.1.1` — e
este arquivo não pode descrever versões que nunca existiram.

**Minor, e não patch:** o artefato ganhou campos (`test_lines_excluded`,
`test_files_excluded`) e três situações mudam de decisão — ver *Comportamento
que muda*, abaixo, antes de atualizar.

```bash
pip install https://github.com/Sanflow10/adversary-gate/releases/download/v2.2.0/adversary_gate-2.2.0-py3-none-any.whl
```

### Corrigido

- **AG-021 (correção mínima) — um patch que reescreve o teste da claim já não
  chega a `MERGE`.** O teste era lido da árvore do *patch*: trocar `a - b` por
  `a + b` e `== 2` por `== 8` dava baseline verde, patch verde, mutante morto —
  `exit 0`, `suite_strength: 1.0`. Agora, se o arquivo de teste da claim já
  existia no baseline e seus bytes diferem no patch, um `VERIFIED` vira
  `UNVERIFIED` (`INCONCLUSIVE`) com o motivo escrito. `REFUTED` não é rebaixado:
  um teste reescrito que ainda falha continua sendo evidência contra o patch.
  Um teste que o patch *adiciona* não é reescrita e segue como antes.
  **Continua aberto** o oráculo do baseline (rodar a versão *original* do teste
  contra o código do patch) — ver §4.1 da auditoria.
- **AG-021 — os caminhos do `--diff` passam a alimentar a policy de caminhos
  protegidos.** `PathPolicy` já recusava `conftest.py`, `pytest.ini`,
  `pyproject.toml` etc., mas só enxergava `--changed-path`, que a Action nunca
  passa. O `--diff` é a declaração autoritativa do que mudou; é *unido* a
  `--changed-path`, não o substitui.
- **AG-022 — linhas de teste não entram mais na cobertura do diff.** Um teste
  executa por construção; contá-lo deixava 10 linhas de código novo sem executar
  passarem no piso de 0,80 com 40 linhas de teste (`0.8`). Agora o ratio é sobre
  arquivos-fonte, e o artefato grava `test_lines_excluded` e
  `test_files_excluded` ao lado de `changed_lines`. Um diff só com testes não tem
  linha de fonte a cobrir: ratio `1.0` por vacuidade, com `note` dizendo isso.
- **AG-028 — `SECURITY.md` já não manda o repórter a um e-mail inexistente.** A
  rota de fallback era "o endereço em `pyproject.toml`", que não tem endereço.
  Agora a rota primária é um link direto para o advisory e o fallback é uma issue
  pública *sem nenhum detalhe técnico*, que só pede um canal privado. Não há
  e-mail na política de propósito, e um teste impede que ela volte a apontar para
  um que não existe.
- **AG-018 — patch misto modificado+deletado já não chega a `MERGE`.** Um
  arquivo deletado não tem linhas para mutar, não gerava mutante e não baixava
  a força de teste — então passava em silêncio por baixo da nota do arquivo que
  sobrou. Reproduzido por terceiro e confirmado aqui (`exit 0` / `merge` com
  `deleted_files: ["gone.py"]` gravado no próprio artefato), agora
  `deleted_files` não-vazio levanta `suite_strength_unverified` e a decisão é
  `INCONCLUSIVE`. O `suite_strength` segue sendo reportado: o número continua
  verdadeiro sobre o que mediu, só deixa de autorizar o patch inteiro. É a
  terceira instância da mesma classe de AG-001 (deleção fora de `changed_files`)
  e AG-012/AG-013 (fonte fora do motor de mutação).
- **AG-019 — o artefato de diff não depende mais da cor do git de quem chama.**
  `prepare_evidence.sh` gravava `git diff` sem `--no-color`, e o teste o lê com
  `grep`: com `color.ui = always` no ambiente o diff continuava correto, mas o
  teste deixava de enxergar a linha adicionada e falhava (`exit 1`). A CI verde
  não desmentia, porque o runner não impõe a cor — o teste passava por acidente
  de ambiente. Corrigido, com regressão no próprio script que força a cor via
  `GIT_CONFIG_COUNT`.

### Adicionado

- **Site do produto** em `site/index.html`, uma página estática em inglês
  publicada no GitHub Pages pelo workflow `Site` (`.github/workflows/pages.yml`)
  a cada push em `main` que mude `site/`. O Pages precisa ser ligado uma vez em
  *Settings → Pages → Source: GitHub Actions*. O teste de consistência de versão
  passa a cobrir também a versão que o site manda instalar.

### Alterado

- **Release:** o workflow passa a mover a tag flutuante `vN` (só para frente: não
  arrasta `v2` para trás ao refazer uma versão antiga) e, havendo o segredo
  `PYPI_API_TOKEN`, também envia ao PyPI por token. Sem o segredo ele **avisa**
  que o PyPI não foi atualizado em vez de deixá-lo atrás em silêncio (AG-026).
  O `pypi-publish.yml` agora só dispara em `vX.Y.Z`, para a tag `v2` não iniciar
  uma publicação.

### Comportamento que muda — leia antes de atualizar

Três situações que antes davam `MERGE` (ou um ratio diferente) agora dão
`INCONCLUSIVE`, e é intencional:

1. o patch altera o arquivo de teste da claim que já existia no baseline;
2. o `--diff` nomeia um arquivo da policy (`conftest.py`, `pytest.ini`,
   `tox.ini`, `setup.cfg`, `pyproject.toml`, `.github/workflows/*`,
   `**/fixtures/**`, `**/Dockerfile*`, `**/docker-compose*.yml`, `**/*.env` —
   a lista é `DEFAULT_DENYLIST`, sem mudança nesta versão);
3. `diff_coverage.changed_lines` conta só linhas de fonte — o número é menor, e
   o ratio, que antes era inflado por testes, pode cair abaixo do piso.

`INCONCLUSIVE` não é erro: é o gate dizendo que a revisão humana decide.

---

## [2.1.0] — 2026-09-29

Tudo o que saiu de `v2.0.1`. As três seções que estavam empilhadas em `main`
(`[Unreleased]`, `[2.1.0]` e `[2.0.2]`) foram fundidas aqui, porque nenhuma
delas houve de virar artefato — e este arquivo não pode descrever versões que
nunca existiram.

```bash
pip install adversary-gate==2.1.0
```

### Adicionado

- **`base-sha`** — um ref entra, três artefatos saem: o baseline é materializado
  daquele commit, o checkout vira o patch, e o diff são `base-sha...HEAD`.
  Sem ele, cada usuário montava os três artefatos à mão e errava algum.
- **Demo de aceitação** (`demo/demo.py`) exercitando as três decisões.
- **Input `full-suite-path` no `action.yml`** — escopo da checagem colateral de
  regressão. Por padrão ela roda a árvore inteira, que num repositório que
  hospeda outro projeto como fixture é a suíte *do próprio repositório*, não a
  do que foi corrigido. Sem este input era impossível apontá-la: o CLI já tinha
  `--full-suite-path`, a Action não o repassava.
- **`--test-command CMD`** — executa um comando arbitrário no lugar do pytest,
  nos dois lados (baseline e patch). Convenção: `0` passou, `1` falhou,
  `2`/`3`/`4` o próprio harness quebrou → `UNVERIFIED`, timeout → `UNVERIFIED`.
  Antes disso, um repositório não-pytest saía com `exit 5 / no tests collected`
  e **nunca** chegava a `MERGE`; agora é possível `BLOCK` real.
- **`--full-suite-command CMD`** — comando da checagem colateral de regressão.
  Padrão: herda de `--test-command`, então declarar uma suíte não-pytest não
  custa silenciosamente a checagem colateral.
- **`--sandbox {none,bwrap}`** — envolve toda execução em bubblewrap: sem rede,
  PID e `/tmp` próprios, sistema somente-leitura, e só o repositório gravável.
  **Opcional e explicitamente limitado**: não há seccomp nem drop de privilégio,
  e não substitui container/VM. Se `bwrap` não existir a porta **recusa a
  partida** (exit 3) em vez de rodar sem isolamento.
- **Inputs novos no `action.yml`**: `test-command`, `full-suite-command`,
  `sandbox`. `test-id` deixou de ser obrigatório (ele nunca precisou existir
  para uma suíte que é um comando).
- **CI com análise estática**: jobs `static-analysis` (bandit em severidade
  MEDIUM+, semgrep `p/security-audit`) — era o único item da checklist de
  maturidade que faltava de verdade.
- **CI com piso de cobertura**: job `coverage` com `--fail-under=87`, para que
  o número da suíte própria não caia em silêncio.
- **`tests/test_runner.py`** — 52 testes para o módulo que mais executa código
  alheio e era o menos coberto do projeto.
- **`SECURITY.md`** — política de divulgação responsável, escopo, limitações
  conhecidas e tempos de resposta.
- **`CHANGELOG.md`** — este arquivo.
- **`scripts/benchmark.py`** — mede a latência de decisão (uma invocação
  completa) e imprime junto o CPU, o Python, o modo de sandbox **e o custo de
  carregamento dos plugins do pytest**. Fica fora do CI de propósito: tempo em
  runner compartilhado é barulhento demais para ser um gate, e um threshold
  frouxo não testa nada enquanto um apertado fica vermelho com a carga de
  outra pessoa.
  - **Medido:** 20,7 s de mediana numa máquina com **9 plugins do pytest**
    auto-carregados. A decisão são **8 execuções** de pytest (baseline, patch,
    4 rodadas de estabilidade, ≥1 mutante, suíte colateral) × 2,29 s de
    startup cada = 18,3 s de ambiente; ≈2,4 s é o gate. O teste em si roda em
    0,01 s.
  - **Limitação operacional registrada:** o runner entrega ao filho um
    ambiente mínimo por design, então `PYTEST_DISABLE_PLUGIN_AUTOLOAD` **não
    chega a ele** — numa máquina com plugins pesados o gate herda o custo oito
    vezes sem como optar out. É a consequência honesta de "isolamento é o filho
    ver só o que eu passo", não um defeito, mas é por que o ambiente precisa
    ser impresso ao lado do número.
- **README**: seção *"What this decision is, and what it is not"* — o
  `MERGE` é permissão para *olhar*, não instrução para mesclar; a decisão
  pertence a quem assina o PR. Antes o disclaimer de revisão humana não
  existia.

### Corrigido

Cada um abaixo foi **reproduzido por exit code real** antes da correção.

- **AG-001..AG-011** — a auditoria sênior de `v2.0.1` foi confrontada com o
  código *executado*, não lido. Cada achado foi reproduzido por exit code e
  corrigido, com teste de regressão explícito.
  A cobertura da suíte própria passou a ser **evidência** medida, não afirmação.
  Ver [`docs/AUDITORIA_CONFRONTO_v2.0.2.md`](docs/AUDITORIA_CONFRONTO_v2.0.2.md)
  para a reprodução de cada um.
- **AG-012** — um patch alterando **apenas** código não-Python não podia chegar
  a `MERGE`. Caso puro e caso misto (`calc.py` + `calculator.cpp`) fechados.
- **A Action escrevia outputs vazios em toda saída não-zero.** O step roda sob
  `bash -e`, e `JSON_OUT=$(…cli.py)` é um comando que falha quando o gate
  termina com `1` (BLOCK) ou `2` (INCONCLUSIVE) — então `-e` abortava o step
  **antes** de escrever `decision` e `outcome`. O resultado: um workflow com
  `if: steps.gate.outputs.decision == 'block'` **nunca disparava**, e a única
  saída que algum dia funcionou foi `exit 0`. `cmd || var=$?` é falha tratada,
  então o `-e` não aborta. Presente na `v2.0.1`, reproduzido com
  `bash --noprofile --norc -eo pipefail`.

Os AG-013 a seguir eram *fail-open*: produziam `MERGE` que a evidência não
sustentava.

- **AG-013 — allowlist de sufixos de origem.** `FOREIGN_SOURCE_SUFFIXES` era
  uma lista de linguagens que alguém se lembrou de digitar; qualquer sufixo
  fora dela era invisível. Um patch cujo único efeito era `schema.sql` saía
  com `decision: merge` e `mutation.reason: "no source file changed between
  baseline and patch"` — a frase falsa que o AG-012 existia para eliminar.
  Virou **denylist** (`NON_SOURCE_SUFFIXES`), e `changed_foreign_source_files`
  agora **une** o scan de diretório com os caminhos que o `--diff` do chamador
  nomeia: nenhuma das duas fontes sozinha fecha o buraco. `.proto`, `.pyi`,
  `.vue`, `.sol`, `.tf`, `.r` e `.sql` passaram a contar.
- **AG-014 — o guard do AG-012 vivia dentro do ramo de mutação.**
  `--mutation-max 0` pulava a checagem inteira: patch só-C++ saía com
  `exit 0 MERGE` e um artefato **sem** `changed_files` / `foreign_changed_files`
  / `deleted_files`, com `unverified: false`. Zero testes cobriam
  `mutation_max=0`. Agora `classify_changes()` calcula o que mudou **antes**
  do teste de orçamento e alimenta os dois ramos, mantendo a *fixed shape* do
  artefato em todo caminho de retorno.
- **AG-015 — o parser de diff só reconhecia o prefixo `+++ b/`.** Um `diff -u`
  padrão parseava para **zero** arquivos → `DiffCoverage(0, 0).ratio == 1.0` →
  piso de cobertura limpo com **zero** linhas medidas. Aceita `+++ ` com e sem
  `b/`, e um diff com hunks sem cabeçalho atribuível agora levanta
  `UnparseableDiff` → `exit 3`, em vez de medir perfeição a partir do nada.
- **AG-016 — `--test-id` obrigatório para suíte que não tem node id.** Impossível
  declarar `--test-command` sem inventar um identificador que não existe
  naquela stack. A claim recebe o rótulo `(test-command)` no lugar.
- **AG-017 — deleção de arquivo inteiro era recusada como "unparseable".**
  `+++ /dev/null` é a grafia de um arquivo removido, e o lado `--- ` era
  descartado, deixando o hunk sem arquivo a que pertencer — o gate acusando o
  *seu input* de malformado quando ele estava perfeitamente válido. O caminho
  antigo virou o fallback de atribuição. Um diff misto (um deletado + um
  modificado) agora atribui os dois.

### Documentado

- `--mutation-max 0` desliga a **medição** (como `--coverage-floor 0` desliga o
  piso), **não** desliga a guarda de origem estrangeira — que é propriedade do
  patch, não do seu orçamento.
- `README.md` passou a descrever a **denylist** em vez da allowlist antiga.
- Seção *"Execution boundary"* reescrita em torno do `--sandbox bwrap`, com a
  tabela do que ele contém e a lista explícita do que ainda não contém.
- **Correção de um item da auditoria anterior**: ela listava *"Ausência de CI
  verificado"* como o bloqueador mais grave. Não era — `.github/workflows/ci.yml`
  existia desde AG-006 com `push` + `pull_request` e matriz 3.10/3.11/3.12. O
  que faltava era análise estática, e agora existe.

### Desempenho da suíte

| | antes | depois |
| --- | --- | --- |
| testes | 122 | 170 + 1 skip |
| cobertura total de `src` | 86,9 % | 89,8 % — medida no CI por `coverage run --source=src`, piso `--fail-under=87` |
| `src/sandbox/runner.py` | 63,4 % | 96,7 % |

A queda de 90,0 % para 89,8 % no CI é o probe de `bubblewrap`: onde ele não
consegue criar um namespace de rede, os 7 testes e2e **pulam** em vez de falhar
e as linhas que cobriam saem da conta. O probe executa `bwrap` cru, sem nenhum
argumento nosso, para que "o ambiente não sabe rodar bwrap" e "nosso prefixo
está quebrado" não virem a mesma coisa: a primeira nomeia o erro do kernel e
vira skip, a segunda continua sendo um teste que falha.

---

## [2.0.1] — tag `v2.0.1`

### Corrigido

- Mutation score passou a ser **verificado**, não apenas coletado; `suite_strength`
  usa regra estrita.
- Atribuição da suíte completa passou a ser estrita — `full_suite_ran` separa
  "passou" de "nunca rodou", que em `2.0.1` eram indistinguíveis no artefato.
- `exit 3` de uso, outputs da Action, detecção de harness e inputs do piso de
  força passaram a se comportar como documentam.
- Publish deixou de publicar artefatos `dist/` velhos; URLs de projeto
  corrigidas.

---

## [2.0.0] — tag `v2.0.0`

### Adicionado

- Primeiro release: gate de verificação baseado em evidência com rigor de
  filtro duplo — `diff_coverage`, `suite_strength`, estabilidade e a cadeia de
  saída `MERGE` / `BLOCK` / `INCONCLUSIVE`.

---

## Como cortar uma release

Porque a disciplina semver só vale se o procedimento for explícito:

1. Mover as entradas de `[Unreleased]` para uma seção com a versão e a data.
2. `pyproject.toml` → a mesma versão. Se mudou `Added`, bump de **minor**; se
   mudou apenas `Fixed`, bump de **patch**; se quebrou um contrato documentado,
   bump de **major**.
3. `README.md:1` e o banner do `demo/demo.py` → a mesma versão. Um destes
   divergindo é o defeito que a auditoria já apontou.
4. Abrir PR, CI verde (suíte, análise estática, cobertura, build, demo).
5. Só então tag `vX.Y.Z`.
6. Verificar se o `pypi-publish.yml` está saudável (AG-008) **antes** de taggar
   à mão — uma tag com publish quebrado deixa uma run vermelha a cada release.
   Foi por isso que `2.0.2` nunca foi taggeada. `2.1.0` ignorou a regra e o
   registro do publisher não foi feito: as três runs de publish falharam e ela
   subiu por token — é assim que fica registrada, e não como uma release
   publicada pelo workflow.

   **A rota do workflow Release não tem este custo.** O
   `release.yml` é `workflow_dispatch`: a tag é criada pelo `GITHUB_TOKEN` do
   próprio workflow, e eventos que esse token produz **não** disparam outros
   workflows, então `pypi-publish.yml` não roda e nenhuma run vermelha nasce.
   Por isso os passos 5 e 6 valem para tag empurrada à mão; no fluxo do Release
   a tag é *consequência* do Run, não pré-requisito dele.
   Com o segredo `PYPI_API_TOKEN` configurado, o mesmo Run também envia ao
   PyPI; sem ele, a run termina com um aviso dizendo que o PyPI não foi
   atualizado. Para mandar ao PyPI uma release que já existe: *Actions →
   Publish to PyPI → Run workflow*, com a tag.

**Não** bumpar a versão com mudanças soltas em `main`: uma versão no
`pyproject.toml` sem tag correspondente é uma promessa de artefato que não
existe. É por isso que esta seção `[Unreleased]` não tem número.
