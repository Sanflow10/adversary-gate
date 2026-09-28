"""The evidence artefact.

Deliberately not a debug log. Previous version wrote 14 fields and, measured
on the source, **zero** occurrences of ``exit_code``, ``stdout`` or
``stderr`` -- the executed evidence was collected in ``verify_claim`` and
then thrown away. For a product whose whole claim is "executed evidence",
the evidence itself was the one thing not persisted.

Now: every record carries the exit codes per side, a tail of the actual test
output, which model produced the patch, and the commit. That is what turns
"we switched models and it felt better" into a comparison, and what makes
``INCONCLUSIVE`` auditable instead of invisible.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.types import GateVerdict, outcome_of


class EvidenceLog:
    def __init__(
        self,
        path: Path,
        max_bytes: int = 20 * 1024 * 1024,
        backup_count: int = 3,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if max_bytes < 0 or backup_count < 0:
            raise ValueError("max_bytes and backup_count must be non-negative")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        # Context travels with every record: which model produced the patch,
        # which commit, which agent run. This is the join key for comparing
        # model switches on the *same* harness.
        self.context = dict(context or {})
        self._lock = threading.Lock()

    def _rotate_if_needed(self, incoming: int) -> None:
        if self.backup_count == 0 or self.max_bytes <= 0 or not self.path.exists():
            return
        if self.path.stat().st_size + incoming <= self.max_bytes:
            return
        for i in range(self.backup_count - 1, 0, -1):
            src = self.path.with_suffix(self.path.suffix + f".{i}")
            dst = self.path.with_suffix(self.path.suffix + f".{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        first = self.path.with_suffix(self.path.suffix + ".1")
        if first.exists():
            first.unlink()
        self.path.rename(first)

    def append(self, verdict: GateVerdict) -> None:
        run = verdict.outcome_run
        record: Dict[str, Any] = {
            "record_id": uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            # --- the claim ---
            "test_path": verdict.claim.test_path,
            "test_id": verdict.claim.test_id,
            "bug_kind": verdict.claim.bug_kind.value,
            "cited_criterion_id": verdict.claim.cited_criterion_id,
            "rationale": verdict.claim.rationale,
            # --- the verdict ---
            "classification": verdict.classification.value,
            "outcome": verdict.outcome.value,
            "reason": verdict.reason,
            "duration_seconds": round(verdict.duration_seconds, 6),
            # --- the executed evidence ---
            "baseline": run.baseline.value if run else None,
            "patch": run.patch.value if run else None,
            "runs": run.run_count if run else 0,
            "baseline_exit_codes": list(run.baseline_exit_codes) if run else [],
            "patch_exit_codes": list(run.patch_exit_codes) if run else [],
            "baseline_failures": run.baseline_failures if run else 0,
            "patch_failures": run.patch_failures if run else 0,
            "evidence": (
                {
                    "baseline_output": run.baseline_output,
                    "patch_output": run.patch_output,
                }
                if run
                else None
            ),
            # --- provenance: model, commit, agent ---
            **{f"ctx_{k}": v for k, v in self.context.items()},
        }
        line = (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(line))
            with self.path.open("ab") as fh:
                fh.write(line)

    def append_decision(
        self,
        decision: str,
        rounds_used: int,
        diff_coverage_ratio: float,
        summary: Dict[str, Any],
    ) -> None:
        """One record per patch-level decision.

        The verdict log says what each claim did; this says what the gate
        actually decided. Keeping them separate is what lets
        ``unverified_merges`` be counted -- a claim can be UNVERIFIED without
        you knowing whether it was merged anyway.
        """
        record = {
            "record_id": uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "decision",
            "decision": decision,
            "rounds_used": rounds_used,
            "diff_coverage_ratio": round(diff_coverage_ratio, 6),
            **summary,
            **{f"ctx_{k}": v for k, v in self.context.items()},
        }
        line = (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(line))
            with self.path.open("ab") as fh:
                fh.write(line)

    def read_all(self) -> List[Dict]:
        with self._lock:
            if not self.path.exists():
                return []
            text = self.path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]
