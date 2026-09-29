#!/usr/bin/env bash
# Prepare the three artefacts AdversaryGate measures, from a single git ref.
#
# The gate itself only ever consumes files and directories: it does not know
# about git, PRs or GitHub. Everything about turning "here is a base commit"
# into `baseline/`, `change.diff` and `coverage.json` lives here so it can be
# run -- and tested -- outside a runner.
#
# Exit codes mirror the product's habit of failing closed with a reason:
#   0  artefacts written
#   3  usage error (missing/unknown argument)
#   4  environment cannot answer (not a git repo, ref not fetched, no coverage)
#
# Usage:
#   scripts/prepare_evidence.sh --base-sha <ref> \
#       --baseline-dir <dir> --diff-file <file> \
#       [--coverage-file <file>] [--coverage-command <cmd>] \
#       [--workspace <dir>] [--skip-coverage]
set -euo pipefail

readonly EXIT_USAGE=3
readonly EXIT_ENV=4

die() { # $1=exit code, rest=message
  local code="$1"; shift
  echo "adversary-gate: $*" >&2
  exit "$code"
}

BASE_SHA=""
BASELINE_DIR=""
DIFF_FILE=""
COVERAGE_FILE=""
COVERAGE_CMD=""
WORKSPACE="$PWD"
SKIP_COVERAGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --base-sha)        BASE_SHA="${2:?--base-sha needs a value}"; shift 2 ;;
    --baseline-dir)    BASELINE_DIR="${2:?--baseline-dir needs a value}"; shift 2 ;;
    --diff-file)       DIFF_FILE="${2:?--diff-file needs a value}"; shift 2 ;;
    --coverage-file)   COVERAGE_FILE="${2:?--coverage-file needs a value}"; shift 2 ;;
    --coverage-command) COVERAGE_CMD="${2:?--coverage-command needs a value}"; shift 2 ;;
    --workspace)       WORKSPACE="${2:?--workspace needs a value}"; shift 2 ;;
    --skip-coverage)   SKIP_COVERAGE=1; shift ;;
    -h|--help)         sed -n '2,20p' "$0"; exit 0 ;;
    *)                 die "$EXIT_USAGE" "unknown argument: $1" ;;
  esac
done

[ -n "$BASE_SHA" ]     || die "$EXIT_USAGE" "--base-sha is required"
[ -n "$BASELINE_DIR" ] || die "$EXIT_USAGE" "--baseline-dir is required"
[ -n "$DIFF_FILE" ]    || die "$EXIT_USAGE" "--diff-file is required"
if [ "$SKIP_COVERAGE" -eq 0 ]; then
  [ -n "$COVERAGE_FILE" ] || die "$EXIT_USAGE" \
    "--coverage-file is required unless --skip-coverage is given"
fi

[ -d "$WORKSPACE" ] || die "$EXIT_ENV" "--workspace is not a directory: $WORKSPACE"
if ! git -C "$WORKSPACE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  die "$EXIT_ENV" "$WORKSPACE is not a git repository; --base-sha needs one"
fi

# ---------------------------------------------------------------- ref exists
# actions/checkout defaults to fetch-depth: 1, so the base commit simply is
# not there. That is the most likely failure by a wide margin, so it gets the
# most specific message instead of git's "unknown revision".
if ! git -C "$WORKSPACE" rev-parse --verify --quiet "$BASE_SHA^{commit}" >/dev/null 2>&1; then
  die "$EXIT_ENV" \
"base-sha '$BASE_SHA' is not in the local history.
The usual cause is a shallow clone: actions/checkout defaults to fetch-depth: 1,
which fetches only the tip commit. Add this to the workflow:

    - uses: actions/checkout@v4
      with:
        fetch-depth: 0

Refusing to continue: guessing at a baseline would put a number in the
evidence artefact that no one actually measured."
fi

HEAD_SHA="$(git -C "$WORKSPACE" rev-parse HEAD)"
if [ "$HEAD_SHA" = "$(git -C "$WORKSPACE" rev-parse "$BASE_SHA^{commit}")" ]; then
  echo "adversary-gate: warning: base-sha is HEAD; the diff will be empty" >&2
fi

# ------------------------------------------------------------------ baseline
# `git archive` gives the committed tree and nothing else: no build output, no
# untracked files, no worktree state that a previous step may have left behind.
rm -rf "$BASELINE_DIR"
mkdir -p "$BASELINE_DIR"
if ! git -C "$WORKSPACE" archive "$BASE_SHA" | tar -x -C "$BASELINE_DIR"; then
  rm -rf "$BASELINE_DIR"
  die "$EXIT_ENV" "could not materialise '$BASE_SHA' into $BASELINE_DIR"
fi
[ -e "$BASELINE_DIR" ] || die "$EXIT_ENV" "baseline ended up empty: $BASELINE_DIR"

# ---------------------------------------------------------------------- diff
# Three-dot, so it is the merge-base against HEAD rather than a diff against a
# base branch that may have moved -- the same range a pull request shows.
git -C "$WORKSPACE" diff --no-ext-diff "$BASE_SHA...HEAD" > "$DIFF_FILE"
if [ ! -s "$DIFF_FILE" ]; then
  echo "adversary-gate: warning: the diff is empty; the gate will see no changed lines" >&2
fi

# ------------------------------------------------------------------ coverage
# Coverage describes the PATCH side: which lines of the changed files ran in
# the patched code. So this runs in the workspace, not in the baseline.
if [ "$SKIP_COVERAGE" -eq 0 ]; then
  if [ -z "$COVERAGE_CMD" ]; then
    # coverage.py writes its data file even when the suite fails; the gate runs
    # the tests itself and reads their real exit codes, so a failure here must
    # not suppress the measurement it just produced.
    COVERAGE_CMD='coverage run -m pytest || true; coverage json -o "$COVERAGE_JSON"'
  fi
  if ! ( cd "$WORKSPACE" && COVERAGE_JSON="$COVERAGE_FILE" bash -c "$COVERAGE_CMD" ); then
    die "$EXIT_ENV" "coverage-command failed in $WORKSPACE"
  fi
  if [ ! -s "$COVERAGE_FILE" ]; then
    die "$EXIT_ENV" \
"coverage-command did not produce a non-empty file at:
  $COVERAGE_FILE

The gate needs a coverage.py JSON report -- the shape 'coverage json' writes:
a mapping of {path: {executed_lines: [...]}}. Without it there is no diff
coverage to measure and the decision would be INCONCLUSIVE for a reason that
has nothing to do with the patch. Pass --coverage-file/--coverage-command, or
--skip-coverage and hand the artefacts to the gate yourself."
  fi
fi

echo "adversary-gate: baseline -> $BASELINE_DIR" >&2
echo "adversary-gate: diff     -> $DIFF_FILE ($(wc -l < "$DIFF_FILE") lines)" >&2
if [ "$SKIP_COVERAGE" -eq 0 ]; then
  echo "adversary-gate: coverage -> $COVERAGE_FILE" >&2
fi
