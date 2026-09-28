"""Coverage of added lines, from coverage.py JSON output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class DiffCoverage:
    changed_lines: int
    covered_lines: int

    @property
    def ratio(self) -> float:
        return 1.0 if self.changed_lines == 0 else self.covered_lines / self.changed_lines


def changed_lines_from_unified_diff(diff_text: str) -> dict[str, set[int]]:
    files: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            files.setdefault(current, set())
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                new_line = int(match.group(1))
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            files[current].add(new_line)
            new_line += 1
        elif current is not None and not line.startswith("-"):
            new_line += 1
    return files


def covered_diff_ratio(diff_text: str, coverage_json: Path | Mapping) -> DiffCoverage:
    changed = changed_lines_from_unified_diff(diff_text)
    payload = (
        json.loads(Path(coverage_json).read_text())
        if isinstance(coverage_json, (str, Path))
        else coverage_json
    )
    files = payload.get("files", {})
    covered = 0
    total = 0
    for filename, lines in changed.items():
        total += len(lines)
        executed = set(files.get(filename, {}).get("executed_lines", []))
        covered += len(lines & executed)
    return DiffCoverage(total, covered)
