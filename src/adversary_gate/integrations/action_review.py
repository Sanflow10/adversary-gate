# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

# The Action's ``small-change`` input: the step's exit code, from the gate's
# answer. Code, not shell in action.yml, so the tests reach it (3.0.18). The gate's
# JSON comes on stdin, its exit code and the input as arguments. A change too
# small to measure, where every other measurement held, passes the step only
# with ``review``, with a warning; every other answer keeps its exit code.

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

EXIT_INCONCLUSIVE = 2
EXIT_USAGE = 3


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="action_review")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.mode not in ("fail", "review"):
        print(f"::error::small-change: {args.mode!r} is not 'fail' or 'review'")
        return EXIT_USAGE
    try:
        answer = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        answer = {}
    review = (answer.get("human_review") or {}) if isinstance(answer, dict) else {}
    required = review.get("kind") == "small_change"
    with open(args.output, "a", encoding="utf-8") as out:
        out.write(f"review-required={'true' if required else 'false'}\n")
    if required and args.exit_code == EXIT_INCONCLUSIVE and args.mode == "review":
        print("::warning title=AdversaryGate::human review required: every measurement held, "
              "but the change is too small to measure the tests; a person must review it "
              "before merging")
        return 0
    return args.exit_code


if __name__ == "__main__":
    sys.exit(main())
