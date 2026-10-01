#!/usr/bin/env bash
# End-to-end test for scripts/prepare_evidence.sh.
#
# Builds a throwaway git repository, asks the script for its three artefacts,
# and then feeds them to the gate -- because producing a diff and a coverage
# report is only worth anything if the decision that follows is right.
#
# Exit 0 means: artefacts correct, gate says MERGE, and both failure modes
# fail closed with a reason. Run it with no arguments.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PREPARE="$HERE/prepare_evidence.sh"
GATE="$REPO_ROOT/src/cli.py"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "  ok  $*"; }

[ -f "$PREPARE" ] || fail "missing $PREPARE"
[ -f "$GATE" ] || fail "missing $GATE"

# --------------------------------------------------------------- fixture repo
mkdir -p "$WORK/repo"
cd "$WORK/repo"
git init -q .
git config user.email "test@example.com"
git config user.name "prepare-evidence test"

printf 'def add(a, b):\n    return a + b\n' > calc.py
printf 'from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n' > test_calc.py
git add -A
git commit -qm "baseline"
BASE_SHA="$(git rev-parse HEAD)"

# The patch: behaviour-preserving, so the claim still passes.
printf 'def add(a, b):\n    return (a + b)\n' > calc.py
git add -A
git commit -qm "patch"

# ------------------------------------------------- happy path: three artefacts
OUT="$WORK/evidence"
if ! bash "$PREPARE" \
    --workspace "$WORK/repo" \
    --base-sha "$BASE_SHA" \
    --baseline-dir "$OUT/baseline" \
    --diff-file "$OUT/change.diff" \
    --coverage-file "$OUT/coverage.json" 2>/dev/null; then
  fail "prepare_evidence.sh exited non-zero on a valid ref"
fi
note "prepare_evidence.sh produced its artefacts"

[ -f "$OUT/baseline/calc.py" ] || fail "baseline was not materialised"
grep -q "return a + b" "$OUT/baseline/calc.py" \
  || fail "baseline holds the patched text; it must hold the committed one"
note "baseline is the committed state, not the worktree"

grep -q "^+    return (a + b)$" "$OUT/change.diff" \
  || fail "diff does not contain the added line"
grep -q "^-    return a + b$" "$OUT/change.diff" \
  || fail "diff does not contain the removed line"
note "diff is a real unified diff of the change"

# AG-019: the identical run with colour forced on.
#
# `prepare_evidence.sh` writes a file, it does not print -- so whether that
# file carries ANSI sequences is decided by the *caller's* git config, not by
# the patch. With `color.ui=always` anywhere in the environment the artefact
# is still a correct diff, and the grep above silently stopped matching it:
# the check was blind while the artefact was fine, and CI never noticed
# because its default config does not impose the colour.
#
# GIT_CONFIG_COUNT applies the setting to this process only: no file is
# written, so the caller's own config is neither read for this nor altered
# afterwards.
COLOURED="$WORK/evidence_coloured"
if ! GIT_CONFIG_COUNT=1 \
     GIT_CONFIG_KEY_0=color.ui \
     GIT_CONFIG_VALUE_0=always \
     bash "$PREPARE" \
       --workspace "$WORK/repo" \
       --base-sha "$BASE_SHA" \
       --baseline-dir "$COLOURED/baseline" \
       --diff-file "$COLOURED/change.diff" \
       --coverage-file "$COLOURED/coverage.json" 2>/dev/null; then
  fail "prepare_evidence.sh exited non-zero with color.ui=always"
fi
grep -q "^+    return (a + b)$" "$COLOURED/change.diff" \
  || fail "diff lost the added line when the caller forces colour"
if LC_ALL=C grep -q $'\033' "$COLOURED/change.diff"; then
  fail "the diff artefact contains escape sequences; it is parsed, not read"
fi
note "diff is independent of the caller's color.ui"

python3 - "$OUT/coverage.json" <<'PY' || exit 1
import json, sys
data = json.load(open(sys.argv[1]))
files = data.get("files", {})
if "calc.py" not in files:
    sys.exit("coverage report has no entry for calc.py")
lines = files["calc.py"].get("executed_lines")
if not isinstance(lines, list) or not lines:
    sys.exit("executed_lines is missing or empty -- the gate cannot measure from this")
PY
note "coverage.json has the shape the gate measures"

# ------------------------------------------------- the gate must accept them
PYTHONPATH="$REPO_ROOT/src" python3 "$GATE" \
  --baseline "$OUT/baseline" \
  --patch "$WORK/repo" \
  --test-path test_calc.py \
  --test-id test_add \
  --diff "$OUT/change.diff" \
  --coverage-json "$OUT/coverage.json" > "$WORK/decision.json" 2>"$WORK/gate.err"
GATE_EXIT=$?
if [ "$GATE_EXIT" -ne 0 ]; then
  cat "$WORK/gate.err" >&2
  fail "gate exited $GATE_EXIT on artefacts prepare_evidence.sh produced (expected 0)"
fi
python3 - "$WORK/decision.json" <<'PY' || exit 1
import json, sys
d = json.load(open(sys.argv[1]))
assert d["decision"] == "merge", f"decision is {d['decision']}, expected merge"
assert d["diff_coverage_source"] == "computed", d["diff_coverage_source"]
assert d["mutation"]["foreign_changed_files"] == [], d["mutation"]
PY
note "gate merges, coverage measured (not asserted), no foreign source"

# ---------------------------------------------------- failure: unknown ref
bash "$PREPARE" --workspace "$WORK/repo" --base-sha deadbeef \
  --baseline-dir "$WORK/b2" --diff-file "$WORK/d2.diff" \
  --coverage-file "$WORK/c2.json" >/dev/null 2>"$WORK/shallow.err"
SHALLOW_EXIT=$?
if [ "$SHALLOW_EXIT" -ne 4 ]; then
  fail "unknown ref exited $SHALLOW_EXIT, expected 4"
fi
grep -q "fetch-depth: 0" "$WORK/shallow.err" \
  || fail "the shallow-clone hint is missing; a user would be left guessing"
note "unknown ref -> exit 4, and the message names fetch-depth: 0"

# ---------------------------------------------------- failure: usage error
bash "$PREPARE" --workspace "$WORK/repo" --base-sha "$BASE_SHA" \
  --baseline-dir "$WORK/b3" --diff-file "$WORK/d3.diff" >/dev/null 2>&1
USAGE_EXIT=$?
if [ "$USAGE_EXIT" -ne 3 ]; then
  fail "missing --coverage-file exited $USAGE_EXIT, expected 3"
fi
note "missing argument -> exit 3"

# ------------------------------------------- failure: coverage not produced
bash "$PREPARE" --workspace "$WORK/repo" --base-sha "$BASE_SHA" \
  --baseline-dir "$WORK/b4" --diff-file "$WORK/d4.diff" \
  --coverage-file "$WORK/c4.json" --coverage-command "true" \
  >/dev/null 2>"$WORK/nocov.err"
NOCOV_EXIT=$?
if [ "$NOCOV_EXIT" -ne 4 ]; then
  fail "coverage-command producing nothing exited $NOCOV_EXIT, expected 4"
fi
grep -q "did not produce a non-empty file" "$WORK/nocov.err" \
  || fail "the coverage failure message is missing"
grep -q '`' "$WORK/nocov.err" && fail "message contains a bare backtick (command substitution)"
note "no coverage artefact -> exit 4, with a readable reason"

echo
echo "prepare_evidence.sh: all checks passed"
