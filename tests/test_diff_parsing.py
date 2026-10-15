# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

"""The diff parser reads a hunk by its counts, not by what its lines look like.

Until 3.0.7 a line was a file header whenever it started with ``+++ `` after a
``--- `` line, even inside a hunk, and the diff was split with
``str.splitlines``, which also breaks on characters that are not line breaks
in a diff or in Python. Each of those moved or dropped changed lines, and a
line moved onto a row that ran counts as covered.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adversary_gate.verifiers.coverage import (  # noqa: E402
    _non_code_added_lines,
    changed_lines_from_unified_diff,
)

HEAD = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"


class TestTheHunkCountsDecide(unittest.TestCase):
    def test_an_added_line_that_starts_with_two_pluses_is_a_line(self):
        """``++ y`` is a valid statement; added, it is spelled ``+++ y``."""
        diff = HEAD + "@@ -1,1 +1,3 @@\n x = 1\n+++ y\n+z = 2\n"
        self.assertEqual(changed_lines_from_unified_diff(diff), {"f.py": {2, 3}})

    def test_a_removed_and_an_added_line_are_not_a_file_header(self):
        """``-- x`` removed then ``++ y`` added read as ``--- x`` / ``+++ y``."""
        diff = HEAD + "@@ -1,2 +1,3 @@\n--- x\n+++ y\n z = 1\n+w = 2\n"
        self.assertEqual(changed_lines_from_unified_diff(diff), {"f.py": {1, 3}})

    def test_a_form_feed_is_not_a_line_break(self):
        """Python does not end a line at ``\\x0c``; ``splitlines`` did."""
        diff = HEAD + "@@ -1,1 +1,3 @@\n x = 1\n+\x0c\n+z = 2\n"
        self.assertEqual(changed_lines_from_unified_diff(diff), {"f.py": {2, 3}})

    def test_the_line_after_a_separator_character_keeps_its_row(self):
        diff = HEAD + "@@ -1,1 +1,3 @@\n x = 1\n+s = 'a\x1cb c'\n+z = 2\n"
        self.assertEqual(changed_lines_from_unified_diff(diff), {"f.py": {2, 3}})

    def test_a_second_file_after_a_hunk_is_still_read(self):
        diff = (HEAD + "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
                + "diff --git a/g.py b/g.py\n--- a/g.py\n+++ b/g.py\n@@ -3,0 +4,1 @@\n+k = 1\n")
        self.assertEqual(changed_lines_from_unified_diff(diff), {"f.py": {2}, "g.py": {4}})

    def test_blank_and_comment_lines_are_counted_by_the_same_rule(self):
        diff = HEAD + "@@ -1,1 +1,4 @@\n x = 1\n+++ y\n+\n+# note\n"
        self.assertEqual(_non_code_added_lines(diff), {"f.py": {3, 4}})


if __name__ == "__main__":
    unittest.main()
