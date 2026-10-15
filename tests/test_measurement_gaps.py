"""What the 3.0.3 audit found the strength measurement could not see.

Each class is one finding, reproduced against 3.0.3 before it was fixed: the
reproduction is in the class docstring, and every test here fails on 3.0.3.
The hidden branch on one line has its own file (``test_hidden_branches.py``).
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.verifiers.strength import canary_source, measure_mutation_score


def _sides(root: Path, base: dict, patch: dict) -> None:
    for side, files in (("baseline", base), ("patch", {**base, **patch})):
        (root / side).mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            target = root / side / rel
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)


class TestTheProbeLeavesNoNameBehind(unittest.TestCase):
    """3.0.1's probe stayed bound: an honest test of ``locals()`` failed the canary.

    ``total = a + b * a - b + a * b`` followed by ``return locals()``, tested
    for ``{"a": 2, "b": 3, "total": 11}``: on the neutral copy the dict also
    held ``__adversary_probe__``, the claim failed there, and an honest patch
    that 3.0.0 measured was INCONCLUSIVE as "reading the code".
    """

    SOURCE = ("def context(a, b):\n    total = a + b * a - b + a * b\n    return locals()\n\n\n"
              "class Box:\n    size = 1\n")

    def test_the_copy_computes_the_same_namespace(self):
        neutral = canary_source(self.SOURCE, {2, 7})
        patched, copied = {}, {}
        exec(compile(self.SOURCE, "patched.py", "exec"), patched)
        exec(compile(neutral, "neutral.py", "exec"), copied)
        self.assertEqual(copied["context"](2, 3), patched["context"](2, 3))
        self.assertEqual({k for k in vars(copied["Box"]) if not k.startswith("__")}, {"size"})

    def test_the_copy_still_changes_the_tree(self):
        """What the probe is for (A): a claim hashing the tree must still fail on the copy."""
        neutral = canary_source(self.SOURCE, {2})
        self.assertNotEqual(ast.dump(ast.parse(neutral)), ast.dump(ast.parse(self.SOURCE)))

    def test_an_honest_test_of_locals_is_measured(self):
        test = ("from calc import context\n\n\ndef test_ctx():\n"
                "    assert context(2, 3) == {'a': 2, 'b': 3, 'total': 11}\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, {"calc.py": self.SOURCE.replace("a + b * a", "a - b * a"), "test_ctx.py": test},
                   {"calc.py": self.SOURCE})
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_ctx.py",
                                                   targets=["test_ctx.py::test_ctx"])
        self.assertFalse(detail["source_sensitive_claims"], detail["reason"])
        self.assertTrue(score.is_measured, detail["reason"])


class TestAFileThatIsNotUtf8IsNotSkipped(unittest.TestCase):
    """A ``# -*- coding: latin-1 -*-`` module was skipped in silence.

    3.0.3: ``legacy.py`` (latin-1) changed ``qty >= 10`` to ``qty > 10`` next
    to six well-tested lines in ``calc.py``. The six mutants of ``calc.py``
    were killed, ``legacy.py`` was never read, and the patch merged at 6/6;
    with the same file in UTF-8 its survivor made it INCONCLUSIVE.
    """

    LEGACY = "# -*- coding: latin-1 -*-\n# tarifa do caf\xe9\ndef price(qty):\n    if qty {op} 10:\n        return qty * 9\n    return qty * 10\n"
    CALC = "def add(a, b):\n    return a{op}b\n"
    TEST = ("from calc import add\nfrom legacy import price\n\n\n"
            "def test_shop():\n    assert add(2, 3) == 5\n    assert price(20) == 180\n")

    def test_the_strength_is_not_measured_and_the_file_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root,
                   {"calc.py": self.CALC.format(op="+"), "test_shop.py": self.TEST,
                    "legacy.py": self.LEGACY.format(op=">=").encode("latin-1")},
                   {"calc.py": self.CALC.format(op=" + "),
                    "legacy.py": self.LEGACY.format(op=">").encode("latin-1")})
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_shop.py",
                                                   targets=["test_shop.py::test_shop"])
        self.assertFalse(score.is_measured, detail)
        self.assertIn("legacy.py", detail["reason"])
        self.assertIn("not UTF-8", detail["reason"])


if __name__ == "__main__":
    unittest.main()


class TestACopyBreaksAPieceOfTheChangedLine(unittest.TestCase):
    """A claim that read a substring of the changed line passed the canary.

    3.0.3: ``return a + b * a - b + a * b`` with a new test asserting
    ``"return a + b * a - b + a * b" in Path(calc.__file__).read_text()``. The
    canary only appended a comment, so the substring survived on the copy;
    every operator mutant broke it, so the claim "killed" 5/5 -> MERGE with
    ``fix_proven`` true, for a test that checks nothing the code does.
    """

    BASE = "def f(a, b):\n    return a - b * a - b + a * b\n"
    PATCH = "def f(a, b):\n    return a + b * a - b + a * b\n"

    def test_the_copy_breaks_every_piece_a_mutant_would_change(self):
        neutral = canary_source(self.PATCH, {2})
        self.assertNotIn("return a + b * a - b + a * b", neutral)
        for piece in ("a + b", "b * a", "a - b", "b + a", "a * b"):
            with self.subTest(piece=piece):
                self.assertNotIn(piece, neutral.split("\n")[1])
        patched, copied = {}, {}
        exec(compile(self.PATCH, "patched.py", "exec"), patched)
        exec(compile(neutral, "neutral.py", "exec"), copied)
        self.assertEqual([copied["f"](a, b) for a in range(-3, 4) for b in range(-3, 4)],
                         [patched["f"](a, b) for a in range(-3, 4) for b in range(-3, 4)])

    def test_a_line_that_starts_with_a_mutable_token_still_parses(self):
        """The space goes *after* the token: before it, it would change the indentation."""
        source = "def f(done, x):\n    not done\n    -x\n    return True\n"
        neutral = canary_source(source, {2, 3, 4})
        ast.parse(neutral)

    def test_the_substring_claim_is_caught(self):
        claim = ("import pathlib\nimport calc\n\n\ndef test_struct():\n    assert calc.f(2, 3) is not None\n"
                 "    assert 'return a + b * a - b + a * b' in pathlib.Path(calc.__file__).read_text()\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, {"calc.py": self.BASE}, {"calc.py": self.PATCH, "test_struct.py": claim})
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_struct.py",
                                                   targets=["test_struct.py::test_struct"])
        self.assertTrue(detail["source_sensitive_claims"], detail)
        self.assertFalse(score.is_measured)


def _cli_tree(root: Path, base: dict, patch: dict, claims: list, *extra: str):
    """Baseline and patch trees, a unified diff of the changed files and a coverage
    report that counts every line of them as run (coverage is not the subject)."""
    import contextlib
    import difflib
    import io
    import json

    from adversary_gate.cli import main

    _sides(root, base, patch)
    diff, executed = "", {}
    for rel, text in patch.items():
        old = base.get(rel, "")
        if old == text or not rel.endswith(".py"):
            continue
        diff += "".join(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                             f"a/{rel}" if old else "/dev/null", f"b/{rel}"))
        executed[rel] = {"executed_lines": list(range(1, text.count("\n") + 2))}
    (root / "change.diff").write_text(diff)
    (root / "cov.json").write_text(json.dumps({"files": executed}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(["--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                     *[arg for claim in claims for arg in ("--claim", claim)],
                     "--diff", str(root / "change.diff"), "--coverage-json", str(root / "cov.json"),
                     "--full-suite-path", "", *extra])
    return code, json.loads(out.getvalue())


class TestEveryChangedLineIsBroken(unittest.TestCase):
    """The 12 mutants were the first 12 changed lines, in file order (3.0.3).

    Twelve whitespace-only, well-tested changes in ``calc.py`` and three
    boundary changes (``>=`` -> ``>``) in ``rules.py`` that no claim pins: the
    plan was calc.py's twelve, 12/12 killed, MERGE; ``rules.py`` was never
    broken and nothing in the artefact said so. With ``--mutation-max 15`` the
    three survived and the patch was INCONCLUSIVE.
    """

    OPS = [("add", "+"), ("sub", "-"), ("mul", "*"), ("div", "/"), ("eq", "=="), ("ne", "!="),
           ("lt", "<"), ("le", "<="), ("gt", ">"), ("ge", ">="), ("mod", "%"), ("fdiv", "//")]
    RULES = ("def shipping(weight):\n    if weight >= 10:\n        return 20\n    return 5\n\n\n"
             "def tax(amount):\n    if amount >= 100:\n        return amount * 2\n    return amount\n")

    def _files(self):
        calc = "".join(f"def {n}(a, b):\n    return a{op}b\n\n\n" for n, op in self.OPS)
        spaced = "".join(f"def {n}(a, b):\n    return a {op} b\n\n\n" for n, op in self.OPS)
        test = ("from calc import " + ", ".join(n for n, _ in self.OPS) + "\nfrom rules import shipping, tax\n\n\n"
                "def test_calc():\n    assert add(2, 3) == 5\n    assert sub(5, 3) == 2\n    assert mul(2, 3) == 6\n"
                "    assert div(6, 3) == 2\n    assert eq(1, 1) is True\n    assert ne(1, 2) is True\n"
                "    assert lt(2, 2) is False\n    assert le(2, 2) is True\n    assert gt(2, 2) is False\n"
                "    assert ge(2, 2) is True\n    assert mod(7, 3) == 1\n    assert fdiv(7, 2) == 3\n\n\n"
                "def test_rules():\n    assert shipping(50) == 20 and shipping(1) == 5\n"
                "    assert tax(500) == 1000 and tax(1) == 1\n")
        base = {"calc.py": calc, "rules.py": self.RULES, "test_shop.py": test}
        patch = {"calc.py": spaced, "rules.py": self.RULES.replace(" >= ", " > ")}
        return base, patch

    def test_the_lines_after_the_twelfth_are_broken_too(self):
        base, patch = self._files()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, base, patch)
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_shop.py",
                                                   targets=["test_shop.py::test_calc", "test_shop.py::test_rules"])
        self.assertEqual(detail["mutants_needed"], 14)
        self.assertIn("rules.py:2", detail["survivors"], detail["mutants"])
        self.assertFalse(score.is_strong, detail)

    def test_a_change_past_the_ceiling_is_not_measured_and_says_how_far(self):
        base, patch = self._files()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, base, patch)
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_shop.py",
                                                   targets=["test_shop.py::test_calc"], max_mutants=10)
        self.assertFalse(score.is_measured)
        self.assertIn("raise it to at least 14", detail["reason"])
        self.assertEqual(detail["mutants"], [])  # nothing ran


class TestALineWithNoOperatorIsBroken(unittest.TestCase):
    """``return a`` -> ``return b`` had no site: strength stood on the next line (3.0.3).

    Beside ``f``'s five tested operators, ``pick`` changed from ``return a`` to
    ``return b`` with a claim that calls it and checks nothing: 5/5, MERGE with
    ``fix_proven``. Now the line gets a statement-level mutant (``return
    None``); it survives, and the strength is not measured.
    """

    BASE = "def pick(a, b):\n    return a\n\n\ndef f(a, b):\n    return a - b * a - b + a * b\n"
    PATCH = "def pick(a, b):\n    return b\n\n\ndef f(a, b):\n    return a + b * a - b + a * b\n"
    TEST = ("from calc import pick, f\n\n\ndef test_f():\n    pick(1, 2)\n"
            "    assert f(2, 3) == 11\n    assert f(3, 2) == 13\n    assert f(5, 7) == 68\n")

    def test_the_unchecked_line_vetoes_the_score(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, {"calc.py": self.BASE, "test_c.py": self.TEST}, {"calc.py": self.PATCH})
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_c.py",
                                                   targets=["test_c.py::test_f"])
        self.assertFalse(score.is_strong, detail)
        self.assertEqual(detail["blind_spots"], ["calc.py:2 -> None"])
        self.assertIn("checked by no claim", detail["reason"])

    def test_a_log_call_on_its_own_line_is_listed_not_broken(self):
        """Deleting a log call is the mutant no test should be asked to kill."""
        patch = self.PATCH.replace("    return b\n", "    print('picking')\n    return b\n")
        test = self.TEST.replace("    pick(1, 2)\n", "    assert pick(1, 2) == 2\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, {"calc.py": self.BASE, "test_c.py": test}, {"calc.py": patch})
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_c.py",
                                                   targets=["test_c.py::test_f"])
        self.assertIn("calc.py:2", detail["unmutated_lines"])
        self.assertTrue(score.is_measured, detail["reason"])


class TestAHeaderLineIsBroken(unittest.TestCase):
    """A default value, a decorator or a base class: lines that run on import alone.

    Until 3.0.7 a changed ``def``/``class`` header or decorator line had no
    mutant, and coverage counts it as run as soon as the module is imported:
    beside five tested operators, ``limit=10`` -> ``limit=1`` merged unjudged.
    Now the changed default, decorator or base is broken, and a claim that
    never notices leaves a blind spot.
    """

    BASE = ("def deco(fn):\n    return fn\n\n\n"
            "def wrap(fn):\n    return lambda *a: None\n\n\n"
            "@deco\ndef pick(a, limit=10):\n    return limit\n\n\n"
            "class Base:\n    k = 1\n\n\nclass Other:\n    k = 2\n\n\n"
            "class C(Base):\n    pass\n\n\n"
            "def f(a, b):\n    return a - b * a - b + a * b\n")
    TEST = ("from calc import pick, C, f\n\n\ndef test_f():\n    pick(1)\n    C()\n"
            "    assert f(2, 3) == 11\n    assert f(3, 2) == 13\n    assert f(5, 7) == 68\n")
    FIXED_F = "def f(a, b):\n    return a + b * a - b + a * b\n"

    def _measure(self, old: str, new: str, line: str):
        patch = self.BASE.replace(old, new).replace(
            "def f(a, b):\n    return a - b * a - b + a * b\n", self.FIXED_F)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, {"calc.py": self.BASE, "test_c.py": self.TEST}, {"calc.py": patch})
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_c.py",
                                                   targets=["test_c.py::test_f"])
        self.assertFalse(score.is_strong, detail)
        self.assertTrue(any(spot.startswith(f"calc.py:{line} ") for spot in detail["blind_spots"]),
                        detail["blind_spots"])

    def test_a_changed_default_value(self):
        self._measure("limit=10", "limit=1", "10")

    def test_a_changed_decorator(self):
        self._measure("@deco\n", "@wrap\n", "9")

    def test_a_changed_base_class(self):
        self._measure("class C(Base)", "class C(Other)", "22")


class TestARewrittenClaimTestIsMutatedAsItWasJudged(unittest.TestCase):
    """The verdict read the baseline's test; the mutants ran the patch's (3.0.3).

    The patch rewrote ``test_total`` to also assert six changed functions; the
    verdict judged it with the baseline's copy (which calls none of them),
    while every mutant ran the patch's copy and died on the new asserts: 6/6,
    MERGE. The same asserts in a *new* test, which the verdict does read from
    the patch, scored 0/6.
    """

    CALC = ("def total(items):\n    return sum(items)\n\n\ndef fee(a):\n    return a * 2\n\n\n"
            "def bonus(a):\n    return a + 3\n\n\ndef penalty(a):\n    return a - 4\n\n\n"
            "def ratio(a):\n    return a / 5\n\n\ndef is_big(a):\n    return a >= 100\n")
    NEW = {"a * 2": "a * 3", "a + 3": "a + 30", "a - 4": "a - 40", "a / 5": "a / 50", "a >= 100": "a > 100"}
    BASE_TEST = "from calc import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n"
    ASSERTS = ("    assert fee(10) == 30\n    assert bonus(1) == 31\n    assert penalty(50) == 10\n"
               "    assert ratio(100) == 2\n    assert is_big(100) is False\n")

    def _patched_calc(self):
        calc = self.CALC
        for old, new in self.NEW.items():
            calc = calc.replace(old, new)
        return calc

    def test_asserts_added_to_a_rewritten_test_do_not_kill_mutants(self):
        test = ("from calc import total, fee, bonus, penalty, ratio, is_big\n\n\ndef test_total():\n"
                "    assert total([1, 2]) == 3\n" + self.ASSERTS)
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _cli_tree(Path(directory), {"calc.py": self.CALC, "test_calc.py": self.BASE_TEST},
                                      {"calc.py": self._patched_calc(), "test_calc.py": test},
                                      ["test_calc.py::test_total"])
        self.assertNotEqual(payload["decision"], "merge", payload["reason"])
        self.assertEqual(payload["claims"][0]["oracle"], "baseline")

    def test_a_new_test_in_an_existing_file_still_measures_and_merges(self):
        """The honest flow: add a test to the file the baseline already had."""
        test = (self.BASE_TEST.replace("from calc import total", "from calc import total, fee, bonus, penalty, ratio, is_big")
                + "\n\ndef test_rates():\n" + self.ASSERTS)
        with tempfile.TemporaryDirectory() as directory:
            code, payload = _cli_tree(Path(directory), {"calc.py": self.CALC, "test_calc.py": self.BASE_TEST},
                                      {"calc.py": self._patched_calc(), "test_calc.py": test},
                                      ["test_calc.py::test_rates", "test_calc.py::test_total"])
        self.assertEqual(payload["decision"], "merge", payload["reason"])
        self.assertEqual(payload["mutation"]["mutants_killed"], payload["mutation"]["mutants_counted"])


class TestTheCanaryDoesNotRetryAFailure(unittest.TestCase):
    """F: a claim that failed the neutral copy was run again, and the second run
    decided. A claim that reads the code and fails only the first time it sees
    the copy then passed as one that runs it. Only a run a limit stopped is
    run again (3.0.7)."""

    def test_a_claim_that_fails_once_on_the_copy_is_caught(self):
        test = ("from pathlib import Path\nfrom calc import add\n\n\ndef test_add():\n"
                "    here = Path(__file__).parent\n"
                "    if 'canary' in (here / 'calc.py').read_text():\n"
                "        seen = here / '.seen'\n"
                "        if not seen.exists():\n"
                "            seen.write_text('1')\n"
                "            assert False, 'reads the code'\n"
                "    assert add(2, 3) == 5\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, {"calc.py": "def add(a, b):\n    return a - b\n", "test_calc.py": test},
                   {"calc.py": "def add(a, b):\n    return a + b\n"})
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                                   targets=["test_calc.py::test_add"])
        self.assertFalse(score.is_measured, detail)
        self.assertTrue(detail.get("source_sensitive_claims"), detail)


class TestStillbornMutantsAreSaid(unittest.TestCase):
    """A mutant whose run could not even collect left the sample with no word in
    the reason; only the artefact's count showed it (3.0.9)."""

    def test_the_reason_names_the_stillborn(self):
        base = {"calc.py": "LIMIT = 1\n\n\ndef add(a, b):\n    return a - b\n",
                "test_calc.py": ("from calc import LIMIT, add\n\n\ndef test_add():\n"
                                 "    assert add(2, 3) == 5\n    assert LIMIT\n")}
        patch = {"calc.py": "LIMIT = 2\n\n\ndef add(a, b):\n    return a + b\n"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, base, patch)
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                                   targets=["test_calc.py::test_add"])
        self.assertGreater(detail["stillborn"], 0, detail)
        self.assertIn("stillborn", detail["reason"], detail)


class TestAFileThePatchOnlyShortenedIsSaid(unittest.TestCase):
    """Decision 1 (3.0.9): a file whose change is only removed lines has nothing
    left to mutate, and the score of the other files stood for the patch without
    a word. It is listed and named in the reason; it does not block -- deleting
    an unused import is the common case."""

    def test_the_shortened_file_is_listed(self):
        base = {"calc.py": "def add(a, b):\n    return a - b\n",
                "guard.py": "def check(x):\n    if x is None:\n        raise ValueError\n    return x\n",
                "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"}
        patch = {"calc.py": "def add(a, b):\n    return a + b\n",
                 "guard.py": "def check(x):\n    return x\n"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, base, patch)
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                                   targets=["test_calc.py::test_add"])
        self.assertEqual(detail.get("removal_only_files"), ["guard.py"], detail)
        self.assertIn("guard.py", detail["reason"])


class TestAReasonDoesNotInventStillbornMutants(unittest.TestCase):
    """Only statement-level mutants on the changed lines, all killed: none counts
    in a sample, and the reason said "every mutant was stillborn (exit codes [])"
    when none was (3.0.9)."""

    def test_the_reason_says_what_happened(self):
        base = {"calc.py": "def add(a, b):\n    return sum((a, b))\n",
                "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"}
        patch = {"calc.py": "def add(a, b):\n    return sum([a, b])\n"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _sides(root, base, patch)
            score, detail = measure_mutation_score(root / "baseline", root / "patch", "test_calc.py",
                                                   targets=["test_calc.py::test_add"])
        self.assertFalse(score.is_measured, detail)
        self.assertEqual(detail["stillborn"], 0, detail)
        self.assertNotIn("stillborn", detail["reason"])
