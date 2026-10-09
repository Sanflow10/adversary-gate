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
