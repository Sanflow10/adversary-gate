"""Where the canary puts its probe: after a docstring row, in front of code."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adversary_gate.verifiers.strength import _CANARY, _probe_line, canary_source


class TestTheProbeGoesWhereItChangesNothing(unittest.TestCase):
    # In a file of its own: tests/test_rigor_and_labels.py exists in the
    # baseline, so the gate judges it by the baseline's copy, and that copy has
    # none of these placements.

    def test_a_docstring_or_future_import_row_is_probed_after_it(self):
        # In front of it, a docstring stops being one (``__doc__`` is None) and a
        # ``__future__`` import is a syntax error, so the probe goes after the row.
        for source, first in (('"""Doc."""\nx = 1\n', '"""Doc."""'),
                              ("from __future__ import annotations\nx = 1\n",
                               "from __future__ import annotations")):
            with self.subTest(first=first):
                out = canary_source(source, {1})
                self.assertEqual(out.split("\n")[:3], [first + _CANARY, _probe_line(), "x = 1"])
                namespace = {}
                exec(compile(out, "<canary>", "exec"), namespace)
                self.assertEqual(namespace.get("__doc__"), "Doc." if first.startswith('"') else None)

    def test_a_statement_row_is_probed_in_front_of_it(self):
        out = canary_source("a = 1\nb = 2\nc = 3\n", {2})
        self.assertEqual(out.split("\n")[:4], ["a = 1", _probe_line(), "b = 2" + _CANARY, "c = 3"])

    def test_a_header_row_gets_its_probe_at_the_start_of_the_body(self):
        out = canary_source("def f(a,\n      b):\n    return a + b\n", {1})
        self.assertIn(f"    {_probe_line()}\n    return a + b", out)


if __name__ == "__main__":
    unittest.main()
