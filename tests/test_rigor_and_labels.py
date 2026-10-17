"""AG-023 (rigour of the mutation score) and AG-030 (fix vs. no regression).

AG-023, as audited: "mutation score with n = 1..6, sites taken in file order,
no confidence interval". Three changes answer it, one test class each:

* the floor reads the lower bound of a Wilson interval (80 % by default), so
  1 killed mutant out of 1 no longer clears 0.75 on its own;
* sites are spread across the changed lines instead of taken in file order;
* the default budget is 12 mutants, so the interval is reachable.

AG-030: FAIL -> PASS and PASS -> PASS both read "test passes on the patch".
They now carry ``fixed`` and ``no_regression``, and the artefact says whether
the patch *proved* a fix.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import math
import sys
import tempfile
import threading
import unittest
from unittest import mock
from concurrent.futures import Future
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.cli import EXIT_INCONCLUSIVE, EXIT_MERGE, EXIT_USAGE, main
from adversary_gate.core.gate import Gate
from adversary_gate.core.metrics import compute_metrics
from adversary_gate.core.types import (
    CriticClaim,
    ExecState,
    ExecutionOutcome,
    FailureClass,
    Outcome,
)
from adversary_gate.sandbox.runner import SandboxResult
from adversary_gate.verifiers.strength import (
    DEFAULT_MAX_MUTANTS,
    _spread,
    calculate_mutation_score,
    measure_mutation_score,
    wilson_interval,
)

#: Five operations, one per line; one assertion kills each operator mutant.
CALC = (
    "def add(a, b):\n    return a + b\n\n\n"
    "def sub(a, b):\n    return a - b\n\n\n"
    "def mul(a, b):\n    return a * b\n\n\n"
    "def div(a, b):\n    return a / b\n\n\n"
    "def mod(a, b):\n    return a % b\n"
)
TESTS = (
    "from calc import add, sub, mul, div, mod\n\n\n"
    "def test_ops():\n"
    "    assert add(2, 3) == 5\n    assert sub(5, 3) == 2\n    assert mul(3, 4) == 12\n"
    "    assert div(8, 2) == 4\n    assert mod(7, 3) == 1\n"
)
RETURN_LINES = [2, 6, 10, 14, 18]


def _tree(root: Path, n_changed: int) -> None:
    """Baseline CALC; the patch parenthesises the first ``n_changed`` returns."""
    lines = CALC.split("\n")
    for line in RETURN_LINES[:n_changed]:
        body = lines[line - 1].split("return ", 1)[1]
        lines[line - 1] = f"    return ({body})"
    for side, text in (("baseline", CALC), ("patch", "\n".join(lines))):
        (root / side).mkdir(parents=True, exist_ok=True)
        (root / side / "calc.py").write_text(text)
        (root / side / "test_calc.py").write_text(TESTS)
    diff = "".join(
        f"--- a/calc.py\n+++ b/calc.py\n@@ -{n},1 +{n},1 @@\n-{CALC.split(chr(10))[n - 1]}\n+{lines[n - 1]}\n"
        for n in RETURN_LINES[:n_changed]
    )
    (root / "change.diff").write_text(diff)
    executed = sorted({1, 2, 5, 6, 9, 10, 13, 14, 17, 18})
    (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": executed}}}))


def _cli(root: Path, *extra: str, test_id: str = "test_ops"):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([
            "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
            "--test-path", "test_calc.py", "--test-id", test_id,
            "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
            "--full-suite-path", "",
            *extra,
        ])
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


@functools.lru_cache(maxsize=None)
def _claims_run_seconds() -> float:
    """How long one unmutated claims run takes on this machine, now.

    The AG-039/AG-042 tests hinge on wall-clock limits: a hang counts as a
    kill only if the reference run finishes under a third of ``--timeout``.
    Fixed limits of 1-3 s failed on a loaded machine (reference 1.025 s
    against ``--timeout 1``), so those tests scale their limits from the
    gate's own measurement of the same kind of run, taken with a limit too
    generous to matter.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _tree(root, 1)
        _, payload = _cli(root, "--timeout", "120")
    return float(payload["mutation"]["reference_run"]["seconds"])


def _limit_with_room(factor: float, floor: int) -> int:
    """A ``--timeout`` the reference run fits ``factor`` times into."""
    return max(floor, math.ceil(_claims_run_seconds() * factor))


class TestWilsonInterval(unittest.TestCase):
    def test_known_values(self):
        """Against hand-computed Wilson bounds at 80 % (z = 1.2816)."""
        cases = {(1, 1): 0.378, (4, 4): 0.709, (5, 5): 0.753, (6, 6): 0.785, (5, 6): 0.575}
        for (killed, total), lower in cases.items():
            with self.subTest(killed=killed, total=total):
                self.assertAlmostEqual(wilson_interval(killed, total, 0.80)[0], lower, places=3)

    def test_zero_confidence_is_the_raw_ratio(self):
        self.assertEqual(wilson_interval(3, 4, 0), (0.75, 0.75))

    def test_bounds_bracket_the_ratio(self):
        for killed, total in ((0, 3), (2, 7), (9, 10), (12, 12)):
            lower, upper = wilson_interval(killed, total, 0.95)
            self.assertLessEqual(lower, killed / total)
            self.assertGreaterEqual(upper, killed / total)
            self.assertGreaterEqual(lower, 0.0)
            self.assertLessEqual(upper, 1.0)

    def test_strength_reads_the_lower_bound(self):
        self.assertFalse(calculate_mutation_score(1, 1).is_strong)
        self.assertFalse(calculate_mutation_score(4, 4).is_strong)
        self.assertTrue(calculate_mutation_score(5, 5).is_strong)
        self.assertTrue(calculate_mutation_score(1, 1, confidence=0).is_strong)

    def test_the_default_budget_can_reach_the_floor(self):
        self.assertGreaterEqual(DEFAULT_MAX_MUTANTS, 5)


class TestSpreadSampling(unittest.TestCase):
    def test_every_line_before_any_line_twice(self):
        """Six operators on line 1 used to eat the whole budget."""
        site = lambda row, col: ((row, col), (row, col + 1), "-")  # noqa: E731
        by_line = {
            ("a.py", 1): [site(1, c) for c in range(6)],
            ("a.py", 7): [site(7, 0)],
            ("b.py", 3): [site(3, 0), site(3, 4)],
        }
        plan = _spread(by_line, 4)
        self.assertEqual(
            [(rel, s[0][0]) for rel, s in plan],
            [("a.py", 1), ("a.py", 7), ("b.py", 3), ("a.py", 1)],
        )
        self.assertEqual(len(_spread(by_line, 100)), 9)


class TestTheFloorEndToEnd(unittest.TestCase):
    def test_one_killed_mutant_is_inconclusive_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, payload = _cli(root)
            self.assertEqual(code, EXIT_INCONCLUSIVE, payload["decision"])
            self.assertEqual(payload["suite_strength"], 1.0)
            self.assertLess(payload["suite_strength_lower"], 0.75)
            self.assertEqual(payload["suite_strength_confidence"], 0.80)

    def test_five_killed_mutants_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            code, payload = _cli(root)
            self.assertEqual(code, EXIT_MERGE, payload)
            self.assertEqual(payload["mutation"]["mutants_killed"], 5)
            self.assertGreaterEqual(payload["suite_strength_lower"], 0.75)
            lines = sorted(m["line"] for m in payload["mutation"]["mutants"])
            self.assertEqual(lines, RETURN_LINES)  # one per changed line

    def test_confidence_zero_restores_the_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, payload = _cli(root, "--strength-confidence", "0")
            self.assertEqual(code, EXIT_MERGE, payload)

    def test_out_of_range_confidence_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, _ = _cli(root, "--strength-confidence", "1")
            self.assertEqual(code, EXIT_USAGE)


class TestAHangIsAKillWhenTheTestsAreFast(unittest.TestCase):
    """AG-042: a mutant that makes the tests never finish was detected.

    more-itertools def2dab: deleting the ``raise`` inside ``for second in
    iterator`` makes ``one(count())`` loop forever; the run is killed at the
    limit and was counted *stillborn*, which kept the census from being exact.
    Mutation tools count it as killed. The AG-039 trap is why the gate did
    not: a run killed because the *whole* run was slow says nothing about the
    mutant. So the claims run once unmutated first; only if that finishes in
    under a third of the limit is a mutant's timeout the mutant's doing.
    """

    BASE = "def count_up(n):\n    i = 0\n    total = 0\n    while i < n:\n        total += i\n        i = i + 1\n    return total\n"
    PATCH = BASE.replace("i = i + 1", "i += 1")

    def _run(self, test_body, *extra):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, src in (("baseline", self.BASE), ("patch", self.PATCH)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(src)
                (root / side / "test_calc.py").write_text(test_body)
            (root / "change.diff").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -6 +6 @@\n-        i = i + 1\n+        i += 1\n"
            )
            (root / "cov.json").write_text(json.dumps({"files": {"calc.py": {"executed_lines": list(range(1, 8))}}}))
            return _cli(root, *extra, test_id="test_count")

    def test_a_hanging_mutant_counts_as_killed(self):
        # the reference must finish under a third of the limit; 3x room on top
        limit = _limit_with_room(9, 3)
        _, payload = self._run(
            "from calc import count_up\n\n\ndef test_count():\n    assert count_up(5) == 10\n",
            "--timeout", str(limit),
        )
        mutants = payload["mutation"]["mutants"]
        self.assertTrue(any(m["result"] == "killed" and m.get("by") == "timeout" for m in mutants), mutants)
        self.assertEqual(payload["mutation"]["stillborn"], 0)

    def test_a_slow_reference_keeps_timeouts_stillborn(self):
        _, payload = self._run(
            "import time\n\nfrom calc import count_up\n\n\ndef test_count():\n"
            "    time.sleep(1.2)\n    assert count_up(5) == 10\n",
            "--timeout", "3",
        )
        mutants = payload["mutation"]["mutants"]
        self.assertFalse(any(m.get("by") == "timeout" for m in mutants), mutants)


class TestMutantsNeverShareTheMachineWithTheSuite(unittest.TestCase):
    """AG-043: no mutant run, and not the reference, starts while the suite runs.

    2.14.0 ran the full suite alongside the mutants and re-ran only the
    mutants killed *at a limit*. A mutant run that failed with exit 1 because
    the suite touched the same thing outside the repository (a fixed /tmp
    path, a port, a test database) counted as killed: a weak test went from
    0/8 to 4/8 kills with the mutants one at a time and the suite beside them.
    A kill is evidence only when nothing else could have failed the run, so
    the mutants now wait for the suite to finish.
    """

    BASE = TestAHangIsAKillWhenTheTestsAreFast.BASE
    PATCH = TestAHangIsAKillWhenTheTestsAreFast.PATCH

    def _measure(self, suite, fake_run):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, src in (("baseline", self.BASE), ("patch", self.PATCH)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(src)
                (root / side / "test_calc.py").write_text("def test_count():\n    pass\n")
            with mock.patch("adversary_gate.verifiers.strength.run_test", side_effect=fake_run):
                return measure_mutation_score(
                    root / "baseline", root / "patch", "test_calc.py",
                    max_mutants=2, timeout_seconds=30, alongside=suite,
                )

    def test_no_run_starts_before_the_suite_is_done(self):
        suite: Future = Future()
        timer = threading.Timer(0.3, suite.set_result, (None,))
        seen = []

        def fake_run(*_args, **_kwargs):
            seen.append(suite.done())
            # Killed with exit 1 -- exactly what a neighbour can cause.
            return SandboxResult(0 if len(seen) == 1 else 1, "", "", False)

        timer.start()
        self.addCleanup(timer.cancel)
        _, detail = self._measure(suite, fake_run)
        self.assertGreater(len(seen), 1)
        self.assertTrue(all(seen), f"runs started while the suite ran: {seen}")
        self.assertNotIn("rerun_alone", json.dumps(detail["mutants"]))

    def test_a_suite_that_raised_is_still_waited_for_and_left_to_the_caller(self):
        suite: Future = Future()
        suite.set_exception(RuntimeError("suite crashed"))
        _, detail = self._measure(suite, lambda *_a, **_k: SandboxResult(0, "", "", False))
        self.assertTrue(detail["mutants"])


class TestMutantsInParallel(unittest.TestCase):
    """Several mutants at once, each worker on its own copy of the tree.

    The entries come back in plan order whatever finishes first; a run killed
    by the wall clock beside other runs is repeated alone, and the repeat is
    the result; a kill by the CPU-time limit is not repeated (CPU time is the
    process's own) unless the reference used a third of that limit.
    """

    CALC_PATCH = CALC.replace("a + b", "b + a").replace("a - b", "a - b + 0")

    def _tree(self, root: Path):
        for side, src in (("baseline", CALC), ("patch", self.CALC_PATCH)):
            (root / side).mkdir()
            (root / side / "calc.py").write_text(src)

    def test_same_entries_as_one_at_a_time(self):
        results = []
        for parallel in (1, 3):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._tree(root)
                for side in ("baseline", "patch"):
                    (root / side / "test_calc.py").write_text(TESTS)
                _, detail = measure_mutation_score(
                    root / "baseline", root / "patch", "test_calc.py", parallel=parallel,
                )
            results.append(([(m["file"], m["line"], m["replaces"], m["result"]) for m in detail["mutants"]],
                            detail["mutants_counted"], detail["mutants_killed"]))
        self.assertEqual(results[0], results[1])
        self.assertGreater(results[0][1], 1)

    def _fake(self, first, reference_seconds=0.0):
        """Reference passes; each mutant's first run is ``first``, any later run passes."""
        seen = {}

        def fake_run(directory, *_a, **_k):
            if not seen:
                seen["reference"] = 1
                if reference_seconds:
                    import time as _t
                    _t.sleep(reference_seconds)
                return SandboxResult(0, "", "", False)
            source = (Path(directory) / "calc.py").read_text()
            if seen.get(source):
                return SandboxResult(0, "", "", False)
            seen[source] = 1
            return first
        return fake_run

    def _measure(self, first, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            for side in ("baseline", "patch"):
                (root / side / "test_calc.py").write_text("def test_x():\n    pass\n")
            with mock.patch("adversary_gate.verifiers.strength.run_test",
                            side_effect=self._fake(first, kwargs.pop("reference_seconds", 0.0))):
                _, detail = measure_mutation_score(
                    root / "baseline", root / "patch", "test_calc.py", parallel=2,
                    timeout_seconds=30, **kwargs,
                )
        return detail["mutants"]

    def test_a_wall_clock_kill_beside_others_is_rerun_alone(self):
        mutants = self._measure(SandboxResult(-1, "", "", True))
        self.assertTrue(mutants)
        for m in mutants:
            self.assertEqual((m["result"], m.get("rerun_alone")), ("survived", -1), m)

    def test_a_cpu_limit_kill_is_not_rerun(self):
        mutants = self._measure(SandboxResult(-9, "", "", False), cpu_seconds=10)
        for m in mutants:
            self.assertEqual((m["result"], m.get("by")), ("killed", "timeout"), m)
            self.assertNotIn("rerun_alone", m)

    def test_a_cpu_limit_kill_is_rerun_when_the_reference_was_close(self):
        mutants = self._measure(SandboxResult(-9, "", "", False), cpu_seconds=3, reference_seconds=1.1)
        for m in mutants:
            self.assertEqual((m["result"], m.get("rerun_alone")), ("survived", -9), m)


class TestMutantWorkersAreOptIn(unittest.TestCase):
    """AG-043: one mutant at a time unless the operator asks for more.

    2.14.0 ran half the CPUs' worth of mutants at once by default. Two runs
    of the same claims collide on anything they share outside their copies
    of the tree, and the loser exits 1 -- a kill. Measured: a test that
    checks no value went from 0/8 to 8/8 kills and MERGE. Workers above one
    are for suites that share nothing outside the repository, and the
    operator has to say so.
    """

    def _payload(self, *extra):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            with mock.patch("os.cpu_count", return_value=8):
                code, payload = _cli(root, *extra)
        return code, payload

    def test_one_at_a_time_by_default_whatever_the_cpu_count(self):
        _, payload = self._payload()
        self.assertEqual(payload["execution"]["mutation_workers"], 1)

    def test_more_only_when_asked(self):
        _, payload = self._payload("--mutation-workers", "3")
        self.assertEqual(payload["execution"]["mutation_workers"], 3)

    def test_serial_sides_still_means_one(self):
        _, payload = self._payload("--mutation-workers", "3", "--serial-sides")
        self.assertEqual(payload["execution"]["mutation_workers"], 1)

    def test_a_census_needs_mutants_run_alone(self):
        """A census demands every mutant killed: interference would turn
        straight into an exact score of 1.0."""
        code, payload = self._payload("--mutation-workers", "2", "--census-min-mutants", "1")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIsNone(payload)

    def test_zero_workers_is_a_usage_error(self):
        code, _ = self._payload("--mutation-workers", "0")
        self.assertEqual(code, EXIT_USAGE)


class TestCensusIsNotASample(unittest.TestCase):
    """When every mutation site of the change was executed, the ratio is exact.

    The Wilson interval (AG-023) bounds a *sample*. Below the budget the gate
    mutates every site -- a census -- and treating 3/3 as a sample of 3 kept
    every small real fix out of MERGE (more-itertools: 3/3 -> 0.65, 2/2 ->
    0.55). A census only counts when it covers the change: every changed
    executable line has a site, nothing was cut by the budget, nothing was
    stillborn. Off by default (``--census-min-mutants 0``): the public claim
    "1 of 1 is not strong" holds until the maintainer chooses otherwise.
    """

    def test_off_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, payload = _cli(root)
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            self.assertFalse(payload["mutation"]["census"]["exact"])

    def test_a_complete_census_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, payload = _cli(root, "--census-min-mutants", "1")
            self.assertEqual(code, EXIT_MERGE, payload)
            census = payload["mutation"]["census"]
            self.assertTrue(census["exact"], census)
            self.assertEqual(payload["suite_strength_lower"], 1.0)

    def test_the_minimum_keeps_wilson_below_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            code, payload = _cli(root, "--census-min-mutants", "3")
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            self.assertIn("minimum", payload["mutation"]["census"]["why_not"])

    def test_lines_without_an_operator_get_a_site_and_must_be_killed(self):
        """The AG-018 shape: one measured line must not vouch for unmeasured ones."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            patched = (root / "patch" / "calc.py").read_text().replace(
                "def sub(a, b):\n", "def sub(a, b):\n    LOG.append(a)\n"
            )
            (root / "patch" / "calc.py").write_text("LOG = []\n\n\n" + patched)
            (root / "baseline" / "calc.py").write_text("LOG = []\n\n\n" + CALC)
            code, payload = _cli(root, "--census-min-mutants", "1")
            self.assertNotEqual(code, EXIT_MERGE, payload["reason"])
            kinds = {m["replaces"] for m in payload["mutation"]["mutants"]}
            self.assertIn("pass", kinds)  # the statement was deleted as a mutant

    def test_a_survivor_in_a_census_is_a_known_blind_spot(self):
        """Measured on more-itertools d71c4ad, bug reintroduced: four coarse
        line-level mutants killed, the one operator mutant that looked like the
        real bug (``and`` -> ``or``) survived, 4/5 = 0.8 cleared 0.75 -> MERGE on
        a real bug. A census is exact both ways: one survivor is a named line
        the tests cannot see, not sampling noise."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            body = (root / "patch" / "calc.py").read_text().replace(
                "def sub(a, b):\n    return a - b\n",
                "def sub(a, b):\n    if a > 0 and b > 0:\n        return a - b\n    return a - b\n",
            )
            (root / "patch" / "calc.py").write_text(body)
            code, payload = _cli(root, "--census-min-mutants", "1")
            self.assertNotEqual(code, EXIT_MERGE, payload["reason"])
            self.assertFalse(payload["mutation"]["census"]["exact"])
            self.assertIn("survived", payload["mutation"]["census"]["why_not"])

    def test_coarse_mutants_never_pad_the_sample(self):
        """Outside a census, statement-level mutants are easy kills: they must not count.

        ``add`` now has no operator (``sum``), so it only gets a coarse
        ``return None`` site, which the test kills; ``sub`` gains a guard whose
        operator mutants the test cannot see. With the coarse kill counted, the
        sample would look stronger than the operator evidence it rests on.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            body = (root / "patch" / "calc.py").read_text().replace(
                "return (a + b)", "return sum((a, b))"
            ).replace(
                "def sub(a, b):\n    return a - b\n",
                "def sub(a, b):\n    if a > 0 or b > 0:\n        return a - b\n    return a - b\n",
            )
            (root / "patch" / "calc.py").write_text(body)
            _, with_census = _cli(root, "--census-min-mutants", "1")
            _, without = _cli(root)
            kinds = {m["replaces"] for m in with_census["mutation"]["mutants"]}
            self.assertIn("None", kinds)  # the coarse site exists...
            self.assertFalse(with_census["mutation"]["census"]["exact"])
            self.assertEqual(  # ...and does not enter the ratio
                (with_census["mutation"]["mutants_counted"], with_census["mutation"]["mutants_killed"]),
                (without["mutation"]["mutants_counted"], without["mutation"]["mutants_killed"]),
            )

    def test_a_budget_cut_is_a_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            _, payload = _cli(root, "--census-min-mutants", "1", "--mutation-max", "3")
            census = payload["mutation"]["census"]
            self.assertFalse(census["exact"])
            self.assertIn("budget", census["why_not"])

    def test_an_assertion_free_new_test_cannot_buy_merge(self):
        """Goodhart: the fix is real, the 'test' calls the code and checks nothing."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 1)
            (root / "baseline" / "calc.py").write_text(CALC.replace("return a + b", "return a - b"))
            for side in ("baseline", "patch"):
                (root / side / "test_calc.py").write_text("from calc import add\n\n\ndef test_ops():\n    add(2, 3)\n")
            code, payload = _cli(root, "--census-min-mutants", "1")
            self.assertNotEqual(code, EXIT_MERGE, payload["reason"])
            self.assertFalse(payload["fix_proven"])


ALLOWED_STEPS = {"fix_code", "cover", "kill_mutant", "add_test", "human_review_test_edit",
                 "raise_limit", "see_reason"}


class TestNextSteps(unittest.TestCase):
    """An INCONCLUSIVE an agent can act on, without being told to bend the oracle.

    The answer says what is missing in a form a coding agent can satisfy --
    which lines no test runs, which mutant survived -- and never suggests the
    things the gate exists to refuse: editing a baseline test, a harness
    file, a floor or the policy.
    """

    def _assert_safe(self, steps, root):
        for step in steps:
            self.assertIn(step["action"], ALLOWED_STEPS, step)
            target = step.get("file", "")
            if step["action"] in ("cover", "kill_mutant"):
                self.assertFalse(target.startswith("test_") or "/test" in target, step)
        text = json.dumps(steps).lower()
        for forbidden in ("conftest", "pytest.ini", "pyproject", "--coverage-floor",
                          "--suite-strength-floor", "adversary_gate_policy"):
            self.assertNotIn(forbidden, text)

    def test_merge_has_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            _, payload = _cli(root)
            self.assertEqual(payload["next_steps"], [])

    def test_a_survivor_names_the_line_and_the_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            for side in ("baseline", "patch"):
                (root / side / "test_calc.py").write_text(
                    TESTS.replace("    assert mod(7, 3) == 1\n", "")
                )
            _, payload = _cli(root)
            kills = [s for s in payload["next_steps"] if s["action"] == "kill_mutant"]
            self.assertEqual([(s["file"], s["line"]) for s in kills], [("calc.py", 18)], payload["next_steps"])
            self._assert_safe(payload["next_steps"], root)

    def test_uncovered_lines_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            cov = json.loads((root / "cov.json").read_text())
            cov["files"]["calc.py"]["executed_lines"] = [1, 2, 5, 6, 9, 10]
            (root / "cov.json").write_text(json.dumps(cov))
            _, payload = _cli(root)
            cover = [s for s in payload["next_steps"] if s["action"] == "cover"]
            self.assertEqual(cover, [{"action": "cover", "file": "calc.py", "lines": [14, 18],
                                      "why": cover[0]["why"]}])
            self._assert_safe(payload["next_steps"], root)

    def test_a_regression_says_fix_the_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            (root / "patch" / "calc.py").write_text(
                (root / "patch" / "calc.py").read_text().replace("(a % b)", "(a * b)")
            )
            _, payload = _cli(root)
            self.assertEqual(payload["decision"], "block")
            self.assertEqual({s["action"] for s in payload["next_steps"]}, {"fix_code"})
            self._assert_safe(payload["next_steps"], root)


class TestFixedIsNotNoRegression(unittest.TestCase):
    def _classify(self, base: ExecState):
        failures = 1 if base is ExecState.FAIL else 0
        return Gate([]).classify(
            CriticClaim("t.py", "t"),
            ExecutionOutcome(base, ExecState.PASS, baseline_failures=failures, run_count=1),
        )

    def test_fail_to_pass_is_a_fix(self):
        verdict = self._classify(ExecState.FAIL)
        self.assertIs(verdict.classification, FailureClass.FIXED)
        self.assertIs(verdict.outcome, Outcome.VERIFIED)
        self.assertIn("fixes", verdict.reason)

    def test_pass_to_pass_is_no_regression(self):
        verdict = self._classify(ExecState.PASS)
        self.assertIs(verdict.classification, FailureClass.NO_REGRESSION)
        self.assertIs(verdict.outcome, Outcome.VERIFIED)
        self.assertIn("not evidence of a fix", verdict.reason)

    def test_a_new_test_is_neither(self):
        verdict = self._classify(ExecState.NOT_APPLICABLE)
        self.assertIs(verdict.classification, FailureClass.CLAIM_DISCARDED)

    def test_the_artefact_says_whether_a_fix_was_proven(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            # The baseline had the bug the patch removes: mod was a * b.
            broken = CALC.replace("return a % b", "return a * b")
            (root / "baseline" / "calc.py").write_text(broken)
            (root / "change.diff").write_text(
                (root / "change.diff").read_text().replace("-    return a % b", "-    return a * b")
            )
            code, payload = _cli(root)
            self.assertEqual(code, EXIT_MERGE, payload)
            self.assertTrue(payload["fix_proven"])
            self.assertEqual(payload["claims_fixed"], 1)
            self.assertEqual(payload["claims"][0]["classification"], "fixed")

            _tree(root, 5)
            code, payload = _cli(root)
            self.assertFalse(payload["fix_proven"])
            self.assertEqual(payload["claims"][0]["classification"], "no_regression")

    def test_the_evidence_log_says_the_same(self):
        # The decision record repeats these counts, and nothing compared them
        # with the answer: either side could drift without a test noticing.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "evidence.jsonl"
            _tree(root, 5)
            broken = CALC.replace("return a % b", "return a * b")
            (root / "baseline" / "calc.py").write_text(broken)
            (root / "change.diff").write_text(
                (root / "change.diff").read_text().replace("-    return a % b", "-    return a * b")
            )
            _, fixed = _cli(root, "--evidence-log", str(log))
            _tree(root, 5)
            _, unchanged = _cli(root, "--evidence-log", str(log))
            records = [json.loads(line) for line in log.read_text().splitlines()]
        decisions = [r for r in records if r.get("kind") == "decision"]
        for payload, record in zip((fixed, unchanged), decisions):
            for key in ("claims_total", "claims_fixed", "fix_proven"):
                self.assertEqual(record[key], payload[key], key)
        self.assertEqual([d["claims_fixed"] for d in decisions], [1, 0])
        self.assertEqual([d["claims_no_regression"] for d in decisions], [0, 1])
        self.assertEqual([d["fix_proven"] for d in decisions], [True, False])

    def test_metrics_count_them_apart(self):
        records = [
            {"outcome": "verified", "classification": "fixed"},
            {"outcome": "verified", "classification": "no_regression"},
            {"outcome": "verified", "classification": "no_regression"},
        ]
        metrics = compute_metrics(records)
        self.assertEqual((metrics.fixed, metrics.no_regression), (1, 2))


class TestMutantsRunTheClaimsNotTheirFiles(unittest.TestCase):
    """AG-039: each mutant ran the claims' *whole test files* (``test_id=""``)
    under the per-test limits. On more-itertools that file takes 25 s: every
    mutant killed at -9, "stillborn", strength never measured, MERGE out of
    reach for the whole project. Running the claims' node ids instead is also
    the fail-closed direction: fewer tests per mutant can only lower the
    score, while a whole file lets a test outside the claims inflate it.
    """

    def _run(self, extra_tests: str, *extra: str, test_id: str = "test_ops"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            for side in ("baseline", "patch"):
                path = root / side / "test_calc.py"
                path.write_text(path.read_text() + extra_tests)
            return _cli(root, *extra, test_id=test_id)

    def test_a_failing_test_outside_the_claims_cannot_kill_the_mutants(self):
        """Its file fails on every mutant, mutated or not: file-level counted 12/12 killed."""
        _, payload = self._run(
            "\n\ndef test_add_only():\n    assert add(2, 3) == 5\n\n\n"
            "def test_unrelated():\n    assert False\n",
            test_id="test_add_only",
        )
        mutation = payload["mutation"]
        self.assertTrue(mutation["measured"], mutation.get("reason"))
        self.assertLess(mutation["mutants_killed"], mutation["mutants_counted"], mutation)

    def test_a_limit_that_kills_every_mutant_is_named(self):
        _, payload = self._run("\n\nimport time\n\n\ndef test_slow():\n    time.sleep(1.5)\n",
                               "--timeout", "1", test_id="test_slow")
        self.assertIn("killed by a limit", payload["mutation"]["reason"])

    def test_a_slow_test_outside_the_claims_does_not_make_mutants_stillborn(self):
        # the claims fit the limit 3x over; the test outside them never would
        limit = _limit_with_room(3, 1)
        _, payload = self._run(f"\n\nimport time\n\n\ndef test_slow():\n    time.sleep({limit + 0.5})\n",
                               "--timeout", str(limit))
        mutation = payload["mutation"]
        self.assertEqual(mutation["stillborn"], 0, mutation)
        self.assertTrue(mutation["measured"], mutation.get("reason"))


class TestAnAddedTestCanProveTheFix(unittest.TestCase):
    """The common fix -- change the code, add the regression test -- never
    reached ``fixed``: an added test has no baseline copy, so it was
    ``discarded`` and ``fix_proven`` stayed false (measured on every real
    more-itertools fix replayed through the MCP server). SWE-bench's
    FAIL_TO_PASS answers it by running the new test against the old code.

    Here the added test runs on the baseline's code with the patch's test
    files laid over it. A *failing* test (exit 1, every run) proves the fix;
    one that passes there too does not tell the two apart; one that cannot
    even be collected there -- it imports what the patch added -- proves
    nothing, which is stricter than SWE-bench on purpose: otherwise a test
    that only imports a new name would "prove" a fix.
    """

    BUGGY = CALC.replace("return a % b", "return a * b")

    def _verify(self, baseline_calc: str, test_file: str, test_text: str, test_id: str,
                patch_calc: str = CALC):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, calc in (("baseline", baseline_calc), ("patch", patch_calc)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(calc)
                (root / side / "test_calc.py").write_text(TESTS.replace("    assert mod(7, 3) == 1\n", ""))
            target = root / "patch" / test_file
            target.write_text(
                target.read_text() + "\n\n" + test_text if target.exists() else test_text
            )
            return Gate([]).verify_claim(
                CriticClaim(test_file, test_id), root / "baseline", root / "patch"
            )

    def test_a_new_test_file_that_fails_on_the_old_code_proves_the_fix(self):
        verdict = self._verify(
            self.BUGGY, "test_mod.py",
            "from calc import mod\n\n\ndef test_mod():\n    assert mod(7, 3) == 1\n", "test_mod",
        )
        self.assertIs(verdict.classification, FailureClass.FIXED, verdict.reason)
        self.assertIs(verdict.outcome, Outcome.VERIFIED)
        self.assertIn("baseline's code", verdict.reason)

    def test_a_test_added_to_an_existing_file_proves_it_too(self):
        verdict = self._verify(
            self.BUGGY, "test_calc.py", "def test_mod():\n    assert mod(7, 3) == 1\n", "test_mod",
        )
        self.assertIs(verdict.classification, FailureClass.FIXED, verdict.reason)
        self.assertEqual(verdict.oracle, "baseline-file")

    def test_a_new_test_that_passes_on_the_old_code_proves_nothing(self):
        verdict = self._verify(
            self.BUGGY, "test_add.py",
            "from calc import add\n\n\ndef test_add_again():\n    assert add(1, 1) == 2\n", "test_add_again",
        )
        self.assertIs(verdict.classification, FailureClass.CLAIM_DISCARDED)
        self.assertIn("also passes on the baseline's code", verdict.reason)

    def test_a_new_test_that_cannot_run_on_the_old_code_proves_nothing(self):
        verdict = self._verify(
            CALC, "test_pow.py",
            "from calc import pow_\n\n\ndef test_pow():\n    assert pow_(2, 3) == 8\n", "test_pow",
            patch_calc=CALC + "\n\ndef pow_(a, b):\n    return a ** b\n",
        )
        self.assertIs(verdict.classification, FailureClass.CLAIM_DISCARDED)
        self.assertIn("could not run against the baseline's code", verdict.reason)


if __name__ == "__main__":
    unittest.main()


class TestAClaimThatReadsTheCodeIsNotMeasured(unittest.TestCase):
    """AG-046: a test that hashes the module's own source, or reads it with
    ``inspect``, fails on *any* edit to it -- so it "kills" every mutant, fails
    on the baseline and passes on the patch. Strength 1.0 and a proven fix,
    and nothing about what the code does was checked. The gate runs the claims
    once on a copy whose only change is a comment after each line the patch
    wrote; code that behaves the same must pass the same."""

    CALLS = ("    assert add(2, 3) == 5\n    assert sub(5, 3) == 2\n    assert mul(3, 4) == 12\n"
             "    assert div(8, 2) == 4\n    assert mod(7, 3) == 1\n")
    HASHES_ITS_MODULE = ("    pathlib.Path(calc.__file__).read_bytes()\n    assert hashlib.sha256("
                         "pathlib.Path(calc.__file__).read_bytes()).hexdigest() == 'DIGEST'\n")

    def _run(self, body: str, *extra: str):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tree(root, 5)
            digest = hashlib.sha256((root / "patch" / "calc.py").read_bytes()).hexdigest()
            text = ("import hashlib, inspect, pathlib\nimport calc\n"
                    "from calc import add, sub, mul, div, mod\n\n\ndef test_ops():\n"
                    + body.replace("DIGEST", digest))
            for side in ("baseline", "patch"):
                (root / side / "test_calc.py").write_text(text)
            return _cli(root, *extra)

    def test_a_test_that_hashes_its_module_is_not_a_measurement(self):
        code, payload = self._run(self.HASHES_ITS_MODULE)
        mutation = payload["mutation"]
        self.assertFalse(mutation["measured"], mutation)
        self.assertTrue(mutation["source_sensitive_claims"])
        self.assertIn("text of the code", mutation["reason"])
        self.assertNotEqual(code, EXIT_MERGE)
        self.assertNotEqual(payload["decision"], "merge")
        self.assertFalse(payload["fix_proven"])

    def test_the_evidence_log_does_not_record_a_proven_fix_either(self):
        # The decision record is what an auditor keeps; it must not prove a
        # fix the answer itself refuses to call proven.
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "evidence.jsonl"
            _, payload = self._run(self.HASHES_ITS_MODULE, "--evidence-log", str(log))
            records = [json.loads(line) for line in log.read_text().splitlines()]
        decision = [r for r in records if r.get("kind") == "decision"][-1]
        self.assertFalse(payload["fix_proven"])
        self.assertFalse(decision["fix_proven"], decision)

    def test_a_test_that_hashes_the_source_of_a_function_is_not_a_measurement(self):
        import hashlib
        expected = hashlib.sha256(
            "def add(a, b):\n    return (a + b)\n".encode()).hexdigest()
        _, payload = self._run(
            self.CALLS.replace("    assert mod(7, 3) == 1\n", "    assert mod(7, 3) == 1\n")
            + f"    src = inspect.getsource(calc.add)\n"
              f"    assert hashlib.sha256(src.encode()).hexdigest() == '{expected}'\n")
        self.assertFalse(payload["mutation"]["measured"], payload["mutation"])
        self.assertNotEqual(payload["decision"], "merge")

    def test_a_test_that_checks_behaviour_is_measured_as_before(self):
        _, payload = self._run(self.CALLS)
        mutation = payload["mutation"]
        self.assertTrue(mutation["measured"], mutation)
        self.assertFalse(mutation["source_sensitive_claims"])
        self.assertEqual(mutation["canary"]["exit_code"], 0)
        self.assertEqual(mutation["mutants_killed"], mutation["mutants_counted"])


class TestTheCanaryChangesNothingTheCodeDoes(unittest.TestCase):
    def test_the_copy_differs_from_the_patch_only_by_comments_and_probes(self):
        import ast
        from adversary_gate.verifiers.strength import _WithoutProbes, canary_source
        source = ('def f(a, b):\n    x = a + b\n    s = """one\n# not a comment\ntwo"""\n'
                  '    y = (a +\n         b)\n    return x + y + len(s)\n')
        out = canary_source(source, {2, 3, 4, 5, 6, 7, 8})
        self.assertIsNotNone(out)
        self.assertEqual(ast.dump(_WithoutProbes().visit(ast.parse(out))), ast.dump(ast.parse(source)))
        self.assertIn('# not a comment\ntwo"""  # adversary-gate canary', out)
        self.assertNotIn("# not a comment  # adversary-gate canary", out)

    def test_only_lines_the_patch_wrote_get_a_comment_and_a_probe(self):
        from adversary_gate.verifiers.strength import canary_source
        out = canary_source("a = 1\nb = 2\nc = 3\n", {2})
        self.assertEqual(out.split("\n")[:4],
                         ["a = 1", "__adversary_probe__ = 0; del __adversary_probe__", "b = 2  # adversary-gate canary", "c = 3"])

    def test_a_probe_changes_the_tree_and_the_bytecode_but_not_the_result(self):
        import ast
        from adversary_gate.verifiers.strength import canary_source
        source = "def f(a):\n    return a + 1\n"
        out = canary_source(source, {2})
        self.assertNotEqual(ast.dump(ast.parse(out)), ast.dump(ast.parse(source)))
        namespace, probed = {}, {}
        exec(compile(source, "<patch>", "exec"), namespace)
        exec(compile(out, "<canary>", "exec"), probed)
        self.assertNotEqual(namespace["f"].__code__.co_code, probed["f"].__code__.co_code)
        self.assertEqual(probed["f"](4), namespace["f"](4))

    def test_a_header_row_gets_its_probe_at_the_start_of_the_body(self):
        from adversary_gate.verifiers.strength import canary_source
        out = canary_source("def f(a,\n      b):\n    return a + b\n", {1})
        self.assertIn("    __adversary_probe__ = 0; del __adversary_probe__\n    return a + b", out)

    def test_a_decorator_row_gets_its_probe_in_the_decorated_body(self):
        from adversary_gate.verifiers.strength import canary_source
        out = canary_source("@staticmethod\ndef f():\n    return 1\n", {1})
        self.assertIn("    __adversary_probe__ = 0; del __adversary_probe__\n    return 1", out)

    def test_a_statement_that_shares_its_line_is_probed_in_front_of_the_line(self):
        # It used to have no place, so nothing was measured; in front of the
        # line's first statement is the same block. A one-line ``def`` still
        # has no place inside it (see TestTheProbeStaysNeutralWhereAStatementCannotGo).
        from adversary_gate.verifiers.strength import canary_source
        out = canary_source("x = 1; y = 2\n", {1})
        self.assertTrue(out.startswith("__adversary_probe__ = 0; del __adversary_probe__\nx = 1; y = 2"), out)

    def test_no_canary_when_there_is_nothing_to_probe(self):
        from adversary_gate.verifiers.strength import canary_source
        self.assertIsNone(canary_source("a = 1\n", set()))
        self.assertIsNone(canary_source("# just a comment\n", {1}))

    def test_a_file_that_does_not_parse_has_no_copy_and_no_pass(self):
        from adversary_gate.verifiers.strength import CanaryUnavailable, canary_source
        with self.assertRaises(CanaryUnavailable):
            canary_source("def (:\n", {1})


class TestAFileThePatchAddedIsMutated(unittest.TestCase):
    """A module the patch creates has no baseline copy, so it used to be
    skipped: the mutation run found "no mutable operator", suite strength was
    unverified, and no patch that adds code could reach MERGE. It is mutated
    now -- but only the lines the patch wrote: a module that is a rename or a
    copy of a baseline file is diffed against it, or tests of the old module
    would kill mutants in code nobody changed."""

    NEW = ("def total(items):\n    t = 0\n    for i in items:\n        t = t + i\n    return t\n\n\n"
           "def avg(items):\n    return total(items) / len(items)\n")
    STRONG = ("from newmod import total, avg\n\n\ndef test_it():\n"
              "    assert total([1, 2, 3]) == 6\n    assert avg([2, 4]) == 3\n    assert total([]) == 0\n")
    HOLLOW = ("from newmod import total, avg\n\n\ndef test_it():\n"
              "    total([1, 2, 3])\n    avg([2, 4])\n")

    def _measure(self, files_base: dict, files_patch: dict, test_text: str, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, files in (("baseline", files_base), ("patch", files_patch)):
                (root / side).mkdir()
                for name, text in files.items():
                    (root / side / name).write_text(text)
                (root / side / "test_new.py").write_text(test_text)
            return measure_mutation_score(root / "baseline", root / "patch", "test_new.py", **kwargs)

    def test_a_new_module_with_a_strong_test_is_measured(self):
        score, detail = self._measure({"other.py": "X = 1\n"}, {"other.py": "X = 1\n", "newmod.py": self.NEW},
                                      self.STRONG)
        self.assertTrue(score.is_measured, detail)
        self.assertEqual(score.mutants_killed, score.mutants_total, detail["survivors"])
        self.assertGreaterEqual(score.mutants_total, 2)
        self.assertEqual(detail["added_files"]["newmod.py"]["paired_with"], None)
        self.assertTrue(detail["added_files"]["newmod.py"]["mutated"])

    def test_a_new_module_that_cannot_be_paired_makes_the_strength_unmeasured(self):
        # Past the pairing limits the new module is not mutated, so its code is
        # never judged. The strength of the rest of the patch must not be
        # reported as the strength of the whole patch (B: a mixed patch on a
        # baseline over 400 files reached MERGE with the new module untested).
        # A mixed patch: a visible file with a mutable operator that the test
        # kills, next to the new module that cannot be paired.
        strong_and_g = self.STRONG.replace("from newmod import total, avg",
                                           "from newmod import total, avg\nfrom other import g") + (
            "\n\ndef test_g():\n    assert g(2, 3) == 5\n")
        with mock.patch("adversary_gate.verifiers.strength._PAIRING_MAX_FILES", 0):
            score, detail = self._measure({"other.py": "def g(a, b):\n    return a - b\n"},
                                          {"other.py": "def g(a, b):\n    return a + b\n",
                                           "newmod.py": self.NEW},
                                          strong_and_g)
        self.assertFalse(score.is_measured, detail)
        self.assertIn("newmod.py", detail["reason"])

    def test_a_new_module_with_a_test_that_asserts_nothing_is_not_strong(self):
        score, detail = self._measure({"other.py": "X = 1\n"}, {"other.py": "X = 1\n", "newmod.py": self.NEW},
                                      self.HOLLOW)
        self.assertTrue(score.is_measured, detail)
        self.assertFalse(score.is_strong, detail)
        self.assertTrue(detail["survivors"])

    def test_a_renamed_module_is_mutated_only_where_it_changed(self):
        old = "def total(items):\n    t = 0\n    for i in items:\n        t = t + i\n    return t\n"
        new = old.replace("t = 0", "t = 1 - 1")  # the one line the patch wrote
        score, detail = self._measure({"oldmod.py": old}, {"newmod.py": new}, self.STRONG.replace(
            "from newmod import total, avg", "from newmod import total\navg = lambda x: sum(x) / len(x)"))
        self.assertEqual(detail["added_files"]["newmod.py"]["paired_with"], "oldmod.py")
        self.assertEqual({m["line"] for m in detail["mutants"]}, {2}, detail["mutants"])

    def test_a_copied_module_is_mutated_only_where_it_differs_from_its_source(self):
        copy = self.NEW.replace("t = 0", "t = 1 - 1")
        score, detail = self._measure({"newmod_orig.py": self.NEW}, {"newmod_orig.py": self.NEW, "newmod.py": copy},
                                      self.STRONG)
        self.assertEqual(detail["added_files"]["newmod.py"]["paired_with"], "newmod_orig.py")
        self.assertEqual({(m["file"], m["line"]) for m in detail["mutants"]}, {("newmod.py", 2)}, detail["mutants"])

    def test_an_unrelated_module_is_mutated_in_every_line(self):
        _, detail = self._measure({"other.py": "def unrelated(a):\n    return a * 9\n"},
                                  {"other.py": "def unrelated(a):\n    return a * 9\n", "newmod.py": self.NEW},
                                  self.STRONG)
        self.assertIsNone(detail["added_files"]["newmod.py"]["paired_with"])
        # Every line: the operators (4, 9) and, since 3.0.4, the lines no
        # operator reaches (2 ``t = 0``, 3 the ``for``, 5 the ``return``).
        self.assertEqual({m["line"] for m in detail["mutants"]}, {2, 3, 4, 5, 9})


class TestTheUnmutatedRunDecides(unittest.TestCase):
    """The claims, run unmutated, are the reference for every mutant.

    D: when that reference fails, a mutant whose run also exits 1 is not a
    kill -- it is the same failure. The score was measured anyway (1.0), so a
    failing reference read as a perfect suite.

    UX-4: without a time limit the reference used to be skipped, so the comment
    probe ran on claims that already failed and labelled them source-sensitive.
    The reference runs whether or not there is a limit.
    """

    BASE = "def add(a, b):\n    return a - b\n"
    PATCH = "def add(a, b):\n    return a + b\n"

    def _measure(self, fake_run, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, src in (("baseline", self.BASE), ("patch", self.PATCH)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(src)
                (root / side / "test_calc.py").write_text("def test_add():\n    pass\n")
            with mock.patch("adversary_gate.verifiers.strength.run_test", side_effect=fake_run):
                return measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                              max_mutants=3, **kwargs)

    def test_a_failing_unmutated_run_leaves_the_strength_unmeasured(self):
        def fake(directory, *_a, **_k):
            return SandboxResult(1, "", "", False)  # the claims fail on the unmutated patch
        score, detail = self._measure(fake, timeout_seconds=30)
        self.assertFalse(score.is_measured, detail)
        self.assertIn("unmutated", detail["reason"])

    def test_the_unmutated_run_happens_without_a_time_limit(self):
        def fake(directory, *_a, **_k):
            return SandboxResult(0, "", "", False)
        _, detail = self._measure(fake, timeout_seconds=None)
        self.assertIsNotNone(detail["reference_run"], detail)

    def test_a_claim_that_fails_unmutated_is_not_called_source_sensitive(self):
        def fake(directory, *_a, **_k):
            return SandboxResult(1, "", "", False)
        _, detail = self._measure(fake, timeout_seconds=None)
        self.assertFalse(detail["source_sensitive_claims"], detail)

    def test_an_unmutated_run_killed_by_a_limit_names_the_limit(self):
        # Not "did not pass": a run that --timeout, --cpu-seconds or --memory
        # stopped neither passed nor failed, and the way out is the limit.
        for code, timed_out in ((-1, True), (-6, False)):
            with self.subTest(exit_code=code):
                def fake(directory, *_a, _code=code, _timed_out=timed_out, **_k):
                    return SandboxResult(_code, "", "", _timed_out)
                score, detail = self._measure(fake, timeout_seconds=30)
                self.assertFalse(score.is_measured, detail)
                self.assertIn("killed by a limit", detail["reason"])
                self.assertNotIn("did not pass", detail["reason"])


class TestAClaimThatHashesTheTreeIsNotMeasured(unittest.TestCase):
    """A (reproduced in 3.0.0): a claim that only compares the hash of the
    changed module's tree passed the comment-only canary, so it "killed" every
    mutant and the suite read as strong: MERGE with suite strength 1.0.

    The probes change the tree, so such a claim fails on the copy and nothing
    is measured. A claim about behaviour keeps working on the same patch.
    """

    BASE = ("def add(a, b):\n    return a - b\n\n\ndef total(values):\n    out = 0\n"
            "    for v in values:\n        out = out + v * 2 - 1\n    return out + len(values) * 3\n")
    PATCH = BASE.replace("return a - b", "return a + b")

    def _measure(self, test_text: str):
        import ast as _ast
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side, src in (("baseline", self.BASE), ("patch", self.PATCH)):
                (root / side).mkdir()
                (root / side / "calc.py").write_text(src)
                (root / side / "test_new.py").write_text(test_text(_ast.dump(_ast.parse(self.PATCH))))
            return measure_mutation_score(root / "baseline", root / "patch", "test_new.py",
                                          max_mutants=20)

    def test_a_claim_that_hashes_the_tree_is_not_measured(self):
        def hash_claim(expected: str) -> str:
            return ("import ast\nimport hashlib\nimport pathlib\n\nimport calc\n\n"
                    f"EXPECTED = hashlib.sha256({expected!r}.encode()).hexdigest()\n\n\n"
                    "def test_structure():\n"
                    "    src = pathlib.Path(calc.__file__).read_text()\n"
                    "    digest = hashlib.sha256(ast.dump(ast.parse(src)).encode()).hexdigest()\n"
                    "    calc.add(1, 2)\n    calc.total([1, 2])\n    assert digest == EXPECTED\n")
        score, detail = self._measure(hash_claim)
        self.assertFalse(score.is_measured, detail)
        self.assertTrue(detail["source_sensitive_claims"], detail)
        # The copy is not comment-only any more: the reason says what it changed.
        self.assertIn("no-op assignment", detail["reason"])

    def test_a_claim_about_behaviour_is_still_measured(self):
        def behaviour_claim(_expected: str) -> str:
            return ("import calc\n\n\ndef test_behaviour():\n"
                    "    assert calc.add(2, 3) == 5\n    assert calc.total([1, 2]) == 10\n")
        score, detail = self._measure(behaviour_claim)
        self.assertTrue(score.is_measured, detail)
        self.assertFalse(detail["source_sensitive_claims"], detail)


class TestTheProbeStaysNeutralWhereAStatementCannotGo(unittest.TestCase):
    """A follow-up: the probe went in front of the statement the patch wrote, and
    three places cannot take a statement in front of them. In front of a
    docstring, ``__doc__`` became None, so a claim that reads it (a doctest, a
    command's help) was called source-sensitive. In front of a module docstring
    followed by ``from __future__``, the copy did not compile, so every claim
    was. And ``if x: return y`` had no place at all, so nothing was measured.
    The copy must compile and behave the same, and it must still change the
    tree and the bytecode of the function the patch edited.
    """

    def _copy(self, source: str, rows) -> str:
        from adversary_gate.verifiers.strength import CanaryUnavailable, canary_source
        try:
            neutral = canary_source(source, set(rows))
        except CanaryUnavailable as exc:
            self.fail(f"no copy was built: {exc}")
        self.assertIsNotNone(neutral)
        try:
            compile(neutral, "m.py", "exec")
        except SyntaxError as exc:
            self.fail(f"the copy does not compile: {exc}\n{neutral}")
        return neutral

    @staticmethod
    def _namespace(source: str) -> dict:
        namespace: dict = {}
        exec(compile(source, "m.py", "exec"), namespace)
        return namespace

    def test_a_docstring_keeps_its_place(self):
        source = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'
        neutral = self._copy(source, [2])
        before, after = self._namespace(source)["add"], self._namespace(neutral)["add"]
        self.assertEqual(after.__doc__, "Add two numbers.")
        self.assertNotEqual(after.__code__.co_code, before.__code__.co_code)

    def test_a_module_docstring_before_future_imports_still_compiles(self):
        import ast as _ast
        source = ('"""Module doc."""\nfrom __future__ import annotations\n\n\n'
                  "def f(x):\n    return x + 1\n")
        neutral = self._copy(source, [1])
        self.assertEqual(self._namespace(neutral)["__doc__"], "Module doc.")
        self.assertNotEqual(_ast.dump(_ast.parse(neutral)), _ast.dump(_ast.parse(source)))

    def test_an_edited_future_import_still_compiles(self):
        self._copy("from __future__ import annotations\n\n\ndef f(x):\n    return x + 1\n", [1])

    def test_a_one_line_if_is_probed_in_front_of_the_if(self):
        source = "def f(x):\n    if x < 0: return 0\n    return x + 1\n"
        neutral = self._copy(source, [2])
        before, after = self._namespace(source)["f"], self._namespace(neutral)["f"]
        self.assertEqual([after(-1), after(2)], [0, 3])
        self.assertNotEqual(after.__code__.co_code, before.__code__.co_code)

    def test_a_one_line_elif_and_else_are_probed_in_front_of_their_if(self):
        source = ("def f(x):\n    if x < 0:\n        return 0\n    elif x == 0: return 1\n"
                  "    else: return x + 1\n")
        after = self._namespace(self._copy(source, [4, 5]))["f"]
        self.assertEqual([after(-1), after(0), after(2)], [0, 1, 3])

    def test_a_statement_after_a_semicolon_is_probed_in_front_of_its_line(self):
        source = "def f(x):\n    a = x; b = a + 1\n    return b\n"
        self.assertEqual(self._namespace(self._copy(source, [2]))["f"](1), 2)

    def test_an_edited_case_is_probed_in_its_block(self):
        # A ``case`` line had no block of its own in the search, so nothing was
        # measured for a patch that edited one.
        source = ("def f(x):\n    match x:\n        case 1:\n            return 'one'\n"
                  "        case _: return 'other'\n")
        after = self._namespace(self._copy(source, [3, 5]))["f"]
        self.assertEqual([after(1), after(2)], ["one", "other"])

    def test_a_one_line_def_still_has_no_place_inside_it(self):
        # In front of the ``def`` is outside the function, whose bytecode would
        # not change: nothing measured beats a canary that cannot see the claim.
        from adversary_gate.verifiers.strength import CanaryUnavailable, canary_source
        with self.assertRaises(CanaryUnavailable):
            canary_source("def f(x): return x + 1\n", {1})


class TestAPythonFileGitHidesIsNotJudgedBlind(unittest.TestCase):
    """C (candidate, reproduced in 3.0.0): with ``calc.py -diff`` in
    .gitattributes, ``git diff`` writes "Binary files ... differ" for it. The
    changed lines were then missing from the coverage diff, so a patch that
    changed them and was covered elsewhere could MERGE.

    Two fixes, both tested: the evidence script diffs with ``--text`` (the lines
    are in the diff), and a Python file that still shows as binary is refused
    rather than silently left out.
    """

    def test_a_python_file_shown_as_binary_is_refused(self):
        from adversary_gate.verifiers.coverage import UnparseableDiff, validate_diff
        diff = ("diff --git a/calc.py b/calc.py\nindex 12ee743..4693ad3 100644\n"
                "Binary files a/calc.py and b/calc.py differ\n")
        with self.assertRaises(UnparseableDiff) as caught:
            validate_diff(diff)
        self.assertIn("calc.py", str(caught.exception))
        self.assertIn("--text", str(caught.exception))

    def test_a_binary_asset_that_is_not_python_is_not_refused(self):
        from adversary_gate.verifiers.coverage import validate_diff
        diff = ("diff --git a/logo.png b/logo.png\nindex 1..2 100644\n"
                "Binary files a/logo.png and b/logo.png differ\n")
        self.assertEqual(validate_diff(diff), {})

    def test_the_evidence_script_keeps_the_lines_of_a_file_marked_minus_diff(self):
        import subprocess
        script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_evidence.sh"
        git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
            subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
            subprocess.run(git + ["commit", "-q", "-m", "base"], cwd=repo, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
            (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            (repo / ".gitattributes").write_text("calc.py -diff\n")
            subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
            subprocess.run(git + ["commit", "-q", "-m", "patch"], cwd=repo, check=True)
            out = Path(directory) / "out"
            result = subprocess.run(
                ["bash", str(script), "--base-sha", base, "--baseline-dir", str(out / "baseline"),
                 "--diff-file", str(out / "change.diff"), "--skip-coverage", "--workspace", str(repo)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            diff = (out / "change.diff").read_text()
            self.assertNotIn("Binary files", diff)
            self.assertIn("+    return a + b", diff)

    def test_the_evidence_script_leaves_a_binary_asset_binary(self):
        # ``--text`` on every file turned a changed image into lines of text that
        # no test runs: a patch whose code was all covered fell to 0.25. Only the
        # Python files need it.
        import subprocess
        script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_evidence.sh"
        git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
        png = bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13])
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            (repo / "pkg").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
            (repo / "logo.png").write_bytes(png + b"\x00old\nimage\x00")
            subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
            subprocess.run(git + ["commit", "-q", "-m", "base"], cwd=repo, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
            (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            (repo / "logo.png").write_bytes(png + b"\x00new\nimage\nbytes\x00")
            (repo / ".gitattributes").write_text("*.py -diff\n")
            subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
            subprocess.run(git + ["commit", "-q", "-m", "patch"], cwd=repo, check=True)
            out = Path(directory) / "out"
            result = subprocess.run(
                ["bash", str(script), "--base-sha", base, "--baseline-dir", str(out / "baseline"),
                 "--diff-file", str(out / "change.diff"), "--skip-coverage", "--workspace", str(repo)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            diff = (out / "change.diff").read_text(errors="replace")
            self.assertIn("+    return a + b", diff)
            self.assertIn("Binary files a/logo.png and b/logo.png differ", diff)


class TestASecondLineIsJudgedByItsStatement(unittest.TestCase):
    """E: coverage.py reports a statement on its first line only, so the second
    line of a statement was never "executed" and always counted as uncovered: a
    module whose code all ran measured 5/9 and was blocked by the 0.80 floor.

    Dropping such lines from the count was worse. The second line of a statement
    that never ran stopped counting at all, and a patch that broke ``mul`` there
    went from INCONCLUSIVE (0.67, in 3.0.0) to MERGE (1.0). The line still counts,
    and it is covered when its statement ran. A docstring still counts and is never
    covered, as in 3.0.0. Without the patch's source nothing is mapped.
    """

    SOURCE = ('def total(values):\n    """Sum the values.\n\n'
              "    Long docstring line that coverage never marks.\n    \"\"\"\n"
              "    result = (1 +\n              2)\n    for v in values:\n"
              "        result = result + v\n    return result\n")
    DIFF = ("diff --git a/mod.py b/mod.py\nnew file mode 100644\n--- /dev/null\n+++ b/mod.py\n"
            "@@ -0,0 +1,10 @@\n" + "".join(f"+{line}\n" for line in SOURCE.split("\n")[:-1]))
    RAN = {"files": {"mod.py": {"executed_lines": [1, 6, 8, 9, 10], "missing_lines": [],
                                "excluded_lines": []}}}

    #: Fixes ``add`` and breaks ``mul`` on the second line of its return, where no
    #: mutable operator is left. Nothing calls ``mul``.
    BASE_CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return (a\n            * b)\n"
    BROKEN_MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return (a\n            )\n"
    OTHER = "def twice(x):\n    return x * 2 + 1 - 1 + x * 3 - x * 3\n"
    BROKEN_MUL_DIFF = ("--- a/calc.py\n+++ b/calc.py\n@@ -1,7 +1,7 @@\n def add(a, b):\n"
                       "-    return a - b\n+    return a + b\n \n \n def mul(a, b):\n"
                       "     return (a\n-            * b)\n+            )\n"
                       "--- a/other.py\n+++ b/other.py\n@@ -1,2 +1,2 @@\n def twice(x):\n"
                       "-    return x\n+    return x * 2 + 1 - 1 + x * 3 - x * 3\n")
    MUL_NEVER_RAN = {"files": {
        "calc.py": {"executed_lines": [1, 2, 5], "missing_lines": [6], "excluded_lines": []},
        "other.py": {"executed_lines": [1, 2], "missing_lines": [], "excluded_lines": []},
    }}

    def test_a_second_line_is_covered_when_its_statement_ran(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mod.py").write_text(self.SOURCE)
            ratio = covered_diff_ratio(self.DIFF, self.RAN, source_root=root)
        # 9 lines count (the blank one never did); 7 is covered through 6, and
        # the docstring lines 2, 4 and 5 are not covered.
        self.assertEqual((ratio.changed_lines, ratio.covered_lines), (9, 6))

    def test_a_second_line_of_a_statement_that_never_ran_counts_against_the_patch(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calc.py").write_text(self.BROKEN_MUL)
            (root / "other.py").write_text(self.OTHER)
            ratio = covered_diff_ratio(self.BROKEN_MUL_DIFF, self.MUL_NEVER_RAN, source_root=root)
        self.assertEqual((ratio.changed_lines, ratio.covered_lines), (3, 2))

    def test_without_the_source_a_second_line_is_never_covered(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio
        ratio = covered_diff_ratio(self.DIFF, self.RAN)
        self.assertEqual((ratio.changed_lines, ratio.covered_lines), (9, 5))
        ratio = covered_diff_ratio(self.BROKEN_MUL_DIFF, self.MUL_NEVER_RAN)
        self.assertEqual((ratio.changed_lines, ratio.covered_lines), (3, 2))

    def test_a_symlinked_source_is_not_read(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.py").write_text(self.SOURCE)
            (root / "mod.py").symlink_to(root / "real.py")
            ratio = covered_diff_ratio(self.DIFF, self.RAN, source_root=root)
        self.assertEqual((ratio.changed_lines, ratio.covered_lines), (9, 5))

    def test_the_advice_names_the_lines_the_ratio_counts_against(self):
        from adversary_gate.verifiers.coverage import uncovered_lines
        readme = "--- a/README.md\n+++ b/README.md\n@@ -1,0 +1,1 @@\n+new line\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mod.py").write_text(self.SOURCE)
            self.assertEqual(uncovered_lines(self.DIFF + readme, self.RAN, source_root=root),
                             {"mod.py": [2, 4, 5]})

    def _tree(self, root: Path, claim_calls_mul: bool) -> None:
        claim = ("import calc\nimport other\n\n\ndef test_ops():\n    assert calc.add(2, 3) == 5\n"
                 "    assert other.twice(3) == 6\n    assert other.twice(5) == 10\n"
                 + ("    calc.mul(2, 3)\n" if claim_calls_mul else ""))
        for side, calc, other in (("baseline", self.BASE_CALC, "def twice(x):\n    return x\n"),
                                  ("patch", self.BROKEN_MUL, self.OTHER)):
            (root / side).mkdir()
            (root / side / "calc.py").write_text(calc)
            (root / side / "other.py").write_text(other)
            (root / side / "test_calc.py").write_text(claim)
        (root / "change.diff").write_text(self.BROKEN_MUL_DIFF)
        report = json.loads(json.dumps(self.MUL_NEVER_RAN))
        if claim_calls_mul:
            report["files"]["calc.py"].update(executed_lines=[1, 2, 5, 6], missing_lines=[])
        (root / "cov.json").write_text(json.dumps(report))

    def test_the_cli_reads_the_patch_to_judge_a_second_line(self):
        for calls_mul, expected in ((False, (3, 2)), (True, (3, 3))):
            with self.subTest(claim_calls_mul=calls_mul), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._tree(root, calls_mul)
                code, payload = _cli(root)
                coverage = payload["diff_coverage"]
                self.assertEqual((coverage["changed_lines"], coverage["covered_lines"]), expected, coverage)
                if not calls_mul:
                    self.assertEqual(code, EXIT_INCONCLUSIVE, payload["reason"])


class TestMessagesThatNameTheRealCause(unittest.TestCase):
    """F: a full suite killed by a signal was reported as a timeout to raise,
    whatever the signal was. G: ``--test-path .`` was reported as escaping the
    repository, though it is the repository itself.
    """

    def test_a_memory_kill_is_not_reported_as_a_timeout(self):
        from types import SimpleNamespace
        from adversary_gate.cli import _reason
        from adversary_gate.core.gate import Decision
        args = SimpleNamespace(full_suite_timeout=900, full_suite_cpu_seconds=None,
                               full_suite_memory=4096)
        reason = _reason(Decision.INCONCLUSIVE, [], None, (-6, -6), args)
        self.assertIn("SIGABRT", reason)
        self.assertIn("--full-suite-memory", reason)
        self.assertNotIn("Raise the limit", reason)

    def test_low_coverage_names_the_configuration_files_it_counted(self):
        """A package.json counts against coverage on purpose; the reason says so
        instead of reading like a bug (3.0.11)."""
        from types import SimpleNamespace
        from adversary_gate.cli import _reason
        from adversary_gate.core.gate import Decision
        verdict = mock.Mock(outcome=Outcome.VERIFIED)
        verdict.classification.value = "fixed"
        args = SimpleNamespace(coverage_floor=0.8, suite_strength_floor=0.75)
        with mock.patch("adversary_gate.cli._uncovered",
                        return_value={"package.json": [3], "app.py": [7]}):
            reason = _reason(Decision.INCONCLUSIVE, [verdict], None, None, args,
                             coverage_ratio=0.5)
        self.assertIn("package.json", reason)
        self.assertIn("configuration or data", reason)
        self.assertNotIn("app.py", reason)
        with mock.patch("adversary_gate.cli._uncovered", return_value={"app.py": [7]}):
            reason = _reason(Decision.INCONCLUSIVE, [verdict], None, None, args,
                             coverage_ratio=0.5)
        self.assertNotIn("configuration", reason)


class TestADocumentCannotPassAsCoverage(unittest.TestCase):
    """E, and a gap found while checking it: in 3.0.0 a patch that only deleted
    a line of README.md measured coverage 1.0, and a claim that read the README
    then passed, so the gate said MERGE. Documents and configuration are not
    code, so they cannot be the fix; a patch with no source line has no coverage.
    """

    README_DELETE = ("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
                     "@@ -1,2 +1,1 @@\n # Title\n-Old claim that is wrong.\n")
    CODE_AND_README = ("diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n@@ -1,1 +1,2 @@\n"
                       " def total(values):\n+    return sum(values)\n"
                       "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
                       "@@ -1,0 +1,3 @@\n+x\n+y\n+z\n")

    def test_a_document_deletion_is_not_a_measured_perfect_ratio(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio
        result = covered_diff_ratio(self.README_DELETE, {"files": {}})
        self.assertTrue(result.unmeasured, result)
        self.assertIsNone(result.ratio)

    def test_documents_are_left_out_of_a_patch_that_also_has_code(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio
        cov = {"files": {"mod.py": {"executed_lines": [1, 2], "missing_lines": [], "excluded_lines": []}}}
        result = covered_diff_ratio(self.CODE_AND_README, cov)
        self.assertEqual((result.changed_lines, result.covered_lines), (1, 1), result)
        self.assertEqual(result.excluded_doc_files, ("README.md",))
        self.assertEqual(result.ratio, 1.0)

    def test_configuration_stays_counted_so_its_change_is_not_free(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio, is_documentation
        self.assertFalse(is_documentation("pyproject.toml"))
        self.assertFalse(is_documentation("requirements.txt"))
        self.assertFalse(is_documentation("docs_helper.py"))
        self.assertTrue(is_documentation("LICENSE"))
        self.assertTrue(is_documentation("docs/guide.md"))
        diff = ("diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n"
                "@@ -1,1 +1,1 @@\n-timeout = 5\n+timeout = 60\n")
        result = covered_diff_ratio(diff, {"files": {}})
        self.assertEqual(result.changed_lines, 1)
        self.assertEqual(result.ratio, 0.0)

    def test_a_whole_python_file_deletion_keeps_its_documented_ratio(self):
        from adversary_gate.verifiers.coverage import covered_diff_ratio
        diff = "--- a/legacy.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def a():\n-    pass\n"
        result = covered_diff_ratio(diff, {"files": {}})
        self.assertFalse(result.unmeasured)
        self.assertEqual(result.ratio, 1.0)


class TestATestOnlyPatchKeepsItsVacuousRatio(unittest.TestCase):
    """The ``unmeasured`` rule was also catching a patch of tests alone, which
    3.0.0 reads as "vacuously 1.0" (AG-022), so every such patch became
    INCONCLUSIVE. No gap needed it, as checked against 3.0.0: a new conftest.py
    is a protected path; a new test that reads a data file it brings passes on
    both sides, so it is not evidence of a fix; and a claim that reads a test
    data file the patch changed is judged by the baseline's copy of it. The rule
    stays for what a claim can read outside the tests: documents and configuration.
    """

    def _resolve(self, diff: str):
        from types import SimpleNamespace
        from adversary_gate.cli import _resolve_coverage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "change.diff").write_text(diff)
            (root / "cov.json").write_text(json.dumps({"files": {}}))
            return _resolve_coverage(SimpleNamespace(
                diff=str(root / "change.diff"), coverage_json=str(root / "cov.json"),
                coverage_source="auto", coverage_ratio=None, patch=str(root)))

    TESTS = ("--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -0,0 +1,2 @@\n"
             "+def test_new():\n+    assert 1 + 1 == 2\n"
             "--- a/tests/data.txt\n+++ b/tests/data.txt\n@@ -0,0 +1,1 @@\n+ok\n")
    README = "--- a/README.md\n+++ b/README.md\n@@ -1,2 +1,1 @@\n # Title\n-Old claim.\n"

    def test_a_patch_of_tests_alone_is_vacuously_covered(self):
        ratio, _, detail = self._resolve(self.TESTS)
        self.assertEqual(ratio, 1.0)
        self.assertIn("vacuously", detail["note"])

    def test_tests_with_a_document_are_not_measured(self):
        ratio, _, detail = self._resolve(self.TESTS + self.README)
        self.assertIsNone(ratio)
        self.assertIn("not measured", detail["note"])
        self.assertNotIn("deletion", detail["note"])


class TestAFixedLabelNeedsTheBaselineToFailAlone(unittest.TestCase):
    """#4 (the AG-043 residue in the ledger, section 18): the two sides of a claim
    run at once, so a baseline failure can be the other side's doing. A FIXED
    label says the baseline fails on its own, so it is checked by rerunning the
    baseline alone. The decision never depended on the label; ``fix_proven`` did.
    """

    def _verify(self, baseline_exit_for_call, config=None):
        from adversary_gate.core.gate import Gate
        from adversary_gate.core.types import CriticClaim
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                (root / side).mkdir()
                (root / side / "calc.py").write_text("def add(a, b):\n    return a + b\n")
                (root / side / "test_calc.py").write_text(
                    "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
            gate = Gate([], config=config)
            calls = {"baseline": 0}

            def fake_runs(run_dir, test_path, test_id, policy, limits):
                if Path(run_dir) == root / "baseline":
                    calls["baseline"] += 1
                    code = baseline_exit_for_call(calls["baseline"])
                else:
                    code = 0  # the patch passes
                return [code] * policy.runs, ""

            gate._runs = fake_runs
            verdict = gate.verify_claim(CriticClaim("test_calc.py", "test_add"),
                                        root / "baseline", root / "patch")
            return verdict, calls["baseline"]

    def test_a_baseline_failure_that_does_not_repeat_alone_is_not_a_fix(self):
        from adversary_gate.core.gate import FailureClass
        # The first baseline run, the one beside the patch, fails; alone it passes.
        verdict, runs = self._verify(lambda call: 1 if call == 1 else 0)
        self.assertEqual(runs, 2, "the baseline must be rerun alone")
        self.assertIsNot(verdict.classification, FailureClass.FIXED, verdict)

    def test_a_baseline_that_fails_alone_is_still_a_fix(self):
        from adversary_gate.core.gate import FailureClass
        verdict, runs = self._verify(lambda call: 1)
        self.assertEqual(runs, 2)
        self.assertIs(verdict.classification, FailureClass.FIXED, verdict)


class TestALimitThatKilledAClaimIsNamed(unittest.TestCase):
    """#1 (issue #4, Android): a claim whose run died by SIGABRT got only "see the
    reason", while the reason said the run was not executed. The advice now names
    the limit that sent the signal, so it matches the reason.
    """

    def _steps_for(self, exit_code):
        from types import SimpleNamespace
        from adversary_gate.cli import _next_steps
        from adversary_gate.core.gate import Decision, Gate
        from adversary_gate.core.types import CriticClaim
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                (root / side).mkdir()
                (root / side / "calc.py").write_text("def add(a, b):\n    return a + b\n")
                (root / side / "test_calc.py").write_text(
                    "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
            gate = Gate([])
            gate._runs = lambda run_dir, test_path, test_id, policy, limits: ([exit_code] * policy.runs, "")
            verdict = gate.verify_claim(CriticClaim("test_calc.py", "test_add"),
                                        root / "baseline", root / "patch")
        args = SimpleNamespace(coverage_floor=0.8, suite_strength_floor=0.75, diff=None, coverage_json=None)
        return _next_steps(Decision.INCONCLUSIVE, [verdict], None, args,
                           coverage_ratio=None, mutation=None)

    def test_an_abort_names_the_address_space_limit(self):
        steps = self._steps_for(-6)
        flags = [f for step in steps if step["action"] == "raise_limit" for f in step["flags"]]
        self.assertIn("--memory", flags, steps)
        self.assertNotIn("--timeout", flags, steps)

    def test_a_kill_names_the_time_and_cpu_limits(self):
        steps = self._steps_for(-9)
        flags = [f for step in steps if step["action"] == "raise_limit" for f in step["flags"]]
        self.assertIn("--timeout", flags, steps)
        self.assertNotIn("--memory", flags, steps)
